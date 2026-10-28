"""Job orchestration: validated preparation, persistence and reuse."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from lecture_localizer import audio, media, mux, verification, xai_client
from lecture_localizer.audio import NormalizedSpeech, SegmentPlacement
from lecture_localizer.contracts import (
    ContractError,
    TranslationDocument,
    TranslationSegment,
    load_job_manifest,
    load_schema,
    load_strict_json_file,
    load_translation_document,
    validate_instance,
    verify_binding,
)
from lecture_localizer.media import MediaToolError
from lecture_localizer.mux import (
    ConvertedAudioTrack,
    CopiedAudioStream,
    MuxRequest,
    TrackProfile,
)
from lecture_localizer.settings import Settings, load_settings
from lecture_localizer.storage import (
    DestinationExistsError,
    IntegrityRecord,
    JobLock,
    LockUnavailableError,
    StorageError,
    atomic_write_json,
    copy_new_file,
    create_new_file,
    ensure_distinct_files,
    ensure_outside,
    fsync_directory,
    move_new_file,
    sha256_bytes,
    sha256_file,
    utc_now_iso,
    verify_integrity,
)
from lecture_localizer.subtitles import (
    SegmentWindow,
    SourceCue,
    SubtitlesError,
    derive_windows,
    parse_srt,
    russian_srt,
)
from lecture_localizer.verification import (
    CandidateExpectation,
)
from lecture_localizer.xai_client import (
    DefinitiveSpeechFailure,
    SpeechClient,
    SpeechRequestError,
    UncertainSpeechFailure,
    request_signature,
    validate_posted_text,
)

MANIFEST_NAME = 'manifest.json'
SNAPSHOT_DIR = 'snapshots'
LOCK_NAME = 'lock'
OPERATIONS_NAME = 'operations.json'
EVIDENCE_DIR = 'evidence'
PUBLICATION_RECEIPT = 'evidence/publication.json'
STAGE_NAMES = ('synthesis', 'fitting', 'subtitle', 'container')
SUPPORTED_CONTAINERS = frozenset({'mp4', 'mov', 'matroska'})
SUPPORTED_AUDIO_CODECS = frozenset({'aac', 'pcm_s16le', 'mp3', 'opus'})
HDR_TRANSFERS = frozenset({'smpte2084', 'arib-std-b67'})
UNRESOLVED_OPERATION_STATES = frozenset({'reserved', 'started', 'uncertain'})
OPERATIONS_SCHEMA = load_schema('operations-v1.schema.json')
TIMELINE_SAMPLE_RATE = audio.SAMPLE_RATE
TIMELINE_SAMPLES_PER_MS = audio.SAMPLES_PER_MS
TIMELINE_BYTES_PER_SAMPLE = audio.BYTES_PER_SAMPLE
COPY_CHUNK_BYTES = 1 << 20
ORIGINAL_AUDIO_SETTINGS: dict[str, Any] = {
    'codec': 'aac_lc',
    'sample_rate_hz': 48000,
    'bitrate_kbps': 192,
}
SYNTHESIS_SETTINGS: dict[str, Any] = {
    'provider': 'xai',
    'voice': 'eve',
    'language': 'ru',
    'speed': 1.0,
    'response_format': 'wav',
    'sample_rate_hz': 48000,
}
NARRATION_AUDIO_SETTINGS: dict[str, Any] = {
    'codec': 'aac_lc',
    'sample_rate_hz': 48000,
    'bitrate_kbps': 192,
    'channels': 1,
}


def _signature(payload: dict[str, Any]) -> str:
    """Hash a canonical JSON payload into a stage signature."""
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    )
    return sha256_bytes(canonical.encode('utf-8'))


@dataclass(frozen=True)
class VideoStreamFacts:
    """Approved profile fields of the video stream."""

    index: int
    codec: str
    pix_fmt: str
    width: int
    height: int
    color_transfer: str | None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON serializable view."""
        return {
            'index': self.index,
            'codec': self.codec,
            'pix_fmt': self.pix_fmt,
            'width': self.width,
            'height': self.height,
            'color_transfer': self.color_transfer,
        }


@dataclass(frozen=True)
class AudioStreamFacts:
    """Approved profile fields of one audio stream."""

    index: int
    codec: str
    profile: str | None
    channels: int
    sample_rate_hz: int
    language: str | None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON serializable view."""
        return {
            'index': self.index,
            'codec': self.codec,
            'profile': self.profile,
            'channels': self.channels,
            'sample_rate_hz': self.sample_rate_hz,
            'language': self.language,
        }


@dataclass(frozen=True)
class MediaStreamFacts:
    """Parsed ffprobe facts of one input file."""

    container_format: str
    duration_seconds: float | None
    video: VideoStreamFacts
    audio_streams: tuple[AudioStreamFacts, ...]

    @property
    def container_tokens(self) -> frozenset[str]:
        """Return the lowercase container format tokens."""
        return frozenset(self.container_format.lower().split(','))


def _probe_int(payload: dict[str, Any], key: str, where: str) -> int:
    """Extract one integer field from an ffprobe stream entry."""
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise MediaToolError(f'unusable ffprobe {where} {key}: {value!r}')
    return value


def _probe_str(payload: dict[str, Any], key: str, where: str) -> str:
    """Extract one string field from an ffprobe stream entry."""
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise MediaToolError(f'unusable ffprobe {where} {key}: {value!r}')
    return value


def _probe_optional_str(
    payload: dict[str, Any], key: str, where: str
) -> str | None:
    """Extract one optional string field from an ffprobe entry."""
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise MediaToolError(f'unusable ffprobe {where} {key}: {value!r}')
    return value


def _video_facts(payload: dict[str, Any]) -> VideoStreamFacts:
    """Build video stream facts from one ffprobe entry."""
    where = f'video stream {payload.get("index")!r}'
    transfer = _probe_optional_str(payload, 'color_transfer', where)
    return VideoStreamFacts(
        index=_probe_int(payload, 'index', where),
        codec=_probe_str(payload, 'codec_name', where),
        pix_fmt=_probe_str(payload, 'pix_fmt', where),
        width=_probe_int(payload, 'width', where),
        height=_probe_int(payload, 'height', where),
        color_transfer=transfer,
    )


def _probe_rate(payload: dict[str, Any], where: str) -> int:
    """Extract the sample rate, which ffprobe reports as a string."""
    value = payload.get('sample_rate')
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise MediaToolError(
            f'unusable ffprobe {where} sample_rate: {value!r}'
        )
    try:
        rate = int(value)
    except (TypeError, ValueError) as error:
        raise MediaToolError(
            f'unusable ffprobe {where} sample_rate: {value!r}'
        ) from error
    if rate < 1:
        raise MediaToolError(f'unusable ffprobe {where} sample_rate')
    return rate


def _audio_facts(payload: dict[str, Any]) -> AudioStreamFacts:
    """Build audio stream facts from one ffprobe entry."""
    where = f'audio stream {payload.get("index")!r}'
    tags = payload.get('tags')
    language = None
    if isinstance(tags, dict):
        language = _probe_optional_str(tags, 'language', where)
    channels = _probe_int(payload, 'channels', where)
    if channels < 1:
        raise MediaToolError(f'unusable ffprobe {where} channels')
    rate = _probe_rate(payload, where)
    return AudioStreamFacts(
        index=_probe_int(payload, 'index', where),
        codec=_probe_str(payload, 'codec_name', where),
        profile=_probe_optional_str(payload, 'profile', where),
        channels=channels,
        sample_rate_hz=rate,
        language=language,
    )


def _media_facts(streams: Any, media_format: Any) -> MediaStreamFacts:
    """Build complete media facts from parsed ffprobe JSON."""
    if not isinstance(streams, list) or not isinstance(media_format, dict):
        raise MediaToolError('unusable ffprobe output structure')
    videos: list[VideoStreamFacts] = []
    audios: list[AudioStreamFacts] = []
    for entry in streams:
        if not isinstance(entry, dict):
            raise MediaToolError('unusable ffprobe stream entry')
        kind = entry.get('codec_type')
        if kind == 'video':
            videos.append(_video_facts(entry))
        elif kind == 'audio':
            audios.append(_audio_facts(entry))
    if len(videos) != 1:
        raise MediaToolError(
            f'expected exactly one video stream, found {len(videos)}'
        )
    container = _probe_str(media_format, 'format_name', 'format')
    duration: float | None = None
    raw_duration = media_format.get('duration')
    if isinstance(raw_duration, str):
        try:
            duration = float(raw_duration)
        except ValueError as error:
            raise MediaToolError(
                f'unusable ffprobe format duration: {raw_duration!r}'
            ) from error
    elif raw_duration is not None and not isinstance(
        raw_duration, (int, float)
    ):
        raise MediaToolError(
            f'unusable ffprobe format duration: {raw_duration!r}'
        )
    return MediaStreamFacts(
        container_format=container,
        duration_seconds=duration,
        video=videos[0],
        audio_streams=tuple(audios),
    )


def inspect_media(video_path: Path, settings: Settings) -> MediaStreamFacts:
    """Probe one video file with ffprobe and parse its facts."""
    ffprobe_path = media.discover_tool('ffprobe', settings.ffprobe_path)
    result = media.checked_run_strict(
        [
            str(ffprobe_path),
            '-v',
            'error',
            '-show_streams',
            '-show_format',
            '-of',
            'json',
            str(video_path),
        ],
        settings.subprocess_timeout_seconds,
    )
    try:
        payload = json.loads(result.stdout)
        streams = payload['streams']
        media_format = payload['format']
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise MediaToolError(
            f'unusable ffprobe output for {video_path}: {error}'
        ) from error
    return _media_facts(streams, media_format)


def _validate_video(facts: MediaStreamFacts) -> None:
    """Validate the video stream against the approved profile."""
    if facts.container_tokens.isdisjoint(SUPPORTED_CONTAINERS):
        raise ContractError(
            'unsupported container format:'
            f' {facts.container_format}; expected MP4, MOV or MKV'
        )
    video = facts.video
    if video.codec != 'h264':
        raise ContractError(
            f'unsupported video codec {video.codec!r}; expected H.264'
        )
    if video.pix_fmt != 'yuv420p':
        raise ContractError(
            f'unsupported pixel format {video.pix_fmt!r}; expected 8-bit'
            ' yuv420p SDR'
        )
    if video.color_transfer in HDR_TRANSFERS:
        raise ContractError(
            'unsupported HDR color transfer'
            f' {video.color_transfer!r}; expected SDR'
        )


def _select_audio(
    facts: MediaStreamFacts, source_audio: int | None
) -> tuple[AudioStreamFacts, str, str]:
    """Select the original audio track; return stream, mode, disposition."""
    if not facts.audio_streams:
        raise ContractError(
            'the input has no audio track; speech localization requires'
            ' the original audio'
        )
    if len(facts.audio_streams) > 1 and source_audio is None:
        indexes = ', '.join(str(s.index) for s in facts.audio_streams)
        raise ContractError(
            f'the input has {len(facts.audio_streams)} audio tracks'
            f' (indexes {indexes}); pass --source-audio INDEX to select'
            ' the English track explicitly'
        )
    if source_audio is None:
        selected = facts.audio_streams[0]
        mode = 'single'
    else:
        matches = [s for s in facts.audio_streams if s.index == source_audio]
        if not matches:
            raise ContractError(
                f'--source-audio {source_audio} does not select an audio'
                ' stream in this input'
            )
        selected = matches[0]
        mode = 'explicit'
    if selected.codec not in SUPPORTED_AUDIO_CODECS:
        raise ContractError(
            f'unsupported audio codec {selected.codec!r} on stream'
            f' {selected.index}; supported: aac LC, pcm_s16le, mp3, opus'
        )
    if selected.codec == 'aac' and selected.profile != 'LC':
        raise ContractError(
            f'unsupported AAC profile {selected.profile!r} on stream'
            f' {selected.index}; expected AAC-LC'
        )
    if selected.channels not in (1, 2):
        raise ContractError(
            f'unsupported channel count {selected.channels} on stream'
            f' {selected.index}; expected mono or stereo'
        )
    disposition = 'copy' if selected.codec == 'aac' else 'convert'
    return selected, mode, disposition


def _segment_text_hash(text: str) -> str:
    """Hash one text value for signature and snapshot identity."""
    return sha256_bytes(text.encode('utf-8'))


def _synthesis_signature(segment: TranslationSegment) -> str:
    """Signature of the synthesis work for one segment."""
    return _signature(
        {
            'kind': 'synthesis-v1',
            'narration_text_sha256': _segment_text_hash(
                segment.resolved_narration
            ),
            'settings': SYNTHESIS_SETTINGS,
        }
    )


def _fitting_signature(
    segment: TranslationSegment,
    window: SegmentWindow,
    max_speedup: float,
) -> str:
    """Signature of the fitting work for one segment.

    The window is part of the fitting identity: the same speech placed
    on a different source interval is different fitting work.
    """
    return _signature(
        {
            'kind': 'fitting-v1',
            'synthesis': _synthesis_signature(segment),
            'max_speedup': max_speedup,
            'window': {'start_ms': window.start_ms, 'end_ms': window.end_ms},
        }
    )


def _policy_dict(settings: Settings) -> dict[str, Any]:
    """Return the persisted job policy from validated settings."""
    return {
        'fitting_max_speedup': settings.fitting_max_speedup,
        'startup_narration_default': settings.startup_narration_default,
        'startup_original_default': settings.startup_original_default,
        'startup_subtitles_default': settings.startup_subtitles_default,
        'startup_subtitles_forced': settings.startup_subtitles_forced,
    }


def _cue_snapshot_payload(cues: tuple[SourceCue, ...]) -> dict[str, Any]:
    """Build the source cue snapshot payload."""
    return {
        'schema_version': 1,
        'cues': [
            {
                'position': cue.position,
                'start_ms': cue.start_ms,
                'end_ms': cue.end_ms,
                'text': cue.text,
            }
            for cue in cues
        ],
    }


def _snapshot_bytes(
    kind: str, document: TranslationDocument, cues: tuple[SourceCue, ...]
) -> bytes:
    """Serialize one immutable snapshot payload."""
    if kind == 'translation':
        return document.raw_bytes
    if kind == 'narration':
        payload = {
            'schema_version': 1,
            'segments': [
                {'id': s.id, 'text': s.resolved_narration}
                for s in document.segments
            ],
        }
    else:
        payload = _cue_snapshot_payload(cues)
    return (json.dumps(payload, ensure_ascii=False, indent=2) + '\n').encode(
        'utf-8'
    )


def _next_snapshot_revision(job_dir: Path, desired: int) -> int:
    """Pick the next free immutable snapshot revision number."""
    snapshot_dir = job_dir / SNAPSHOT_DIR
    highest = desired - 1
    if snapshot_dir.is_dir():
        for entry in snapshot_dir.iterdir():
            name = entry.name
            marker = name.rfind('-r')
            if marker < 0 or not name.endswith('.json'):
                continue
            digits = name[marker + 2 : -len('.json')]
            if digits.isdigit():
                highest = max(highest, int(digits))
    return highest + 1


def _write_snapshots(
    job_dir: Path,
    desired_revision: int,
    document: TranslationDocument,
    cues: tuple[SourceCue, ...],
) -> dict[str, dict[str, Any]]:
    """Write the three immutable text snapshots for one content revision."""
    snapshot_dir = job_dir / SNAPSHOT_DIR
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    revision = _next_snapshot_revision(job_dir, desired_revision)
    records: dict[str, dict[str, Any]] = {}
    for kind in ('translation', 'narration', 'source_cues'):
        payload = _snapshot_bytes(kind, document, cues)
        relative = f'{SNAPSHOT_DIR}/{kind}-r{revision:03d}.json'
        create_new_file(job_dir / relative, payload)
        records[kind] = {
            'revision': revision,
            'path': relative,
            'sha256': sha256_bytes(payload),
        }
    return records


def _snapshot_integrity_records(
    manifest: dict[str, Any],
) -> tuple[IntegrityRecord, ...]:
    """Integrity records for every snapshot referenced by a manifest."""
    return tuple(
        IntegrityRecord(
            label=f'{kind} snapshot',
            relative_path=record['path'],
            sha256=record['sha256'],
        )
        for kind, record in manifest['snapshots'].items()
    )


def _artifact_integrity_records(
    artifacts: list[dict[str, Any]],
) -> tuple[IntegrityRecord, ...]:
    """Integrity records for every cached artifact."""
    return tuple(
        IntegrityRecord(
            label=(
                f'{record["kind"]} artifact for'
                f' {record["segment"] or "the job"}'
            ),
            relative_path=record['path'],
            sha256=record['sha256'],
        )
        for record in artifacts
    )


def _integrity_failure_labels(
    failures: tuple[IntegrityRecord, ...],
) -> frozenset[str]:
    """Return the labels whose records failed verification."""
    return frozenset(failure.label for failure in failures)


def _artifact_label(record: dict[str, Any]) -> str:
    """Return the integrity label of one artifact record."""
    return f'{record["kind"]} artifact for {record["segment"] or "the job"}'


def _candidate_segments(
    document: TranslationDocument,
    windows: dict[str, SegmentWindow],
    settings: Settings,
) -> list[dict[str, Any]]:
    """Build manifest segment entries with per stage signatures."""
    segments: list[dict[str, Any]] = []
    for segment in document.segments:
        window = windows[segment.id]
        segments.append(
            {
                'id': segment.id,
                'source_cues': list(segment.source_cues),
                'window': {
                    'start_ms': window.start_ms,
                    'end_ms': window.end_ms,
                },
                'translation_text_sha256': _segment_text_hash(
                    segment.translation
                ),
                'narration_text_sha256': _segment_text_hash(
                    segment.resolved_narration
                ),
                'synthesis_signature': _synthesis_signature(segment),
                'fitting_signature': _fitting_signature(
                    segment, window, settings.fitting_max_speedup
                ),
            }
        )
    return segments


def _audio_identity(stream: Any) -> dict[str, Any]:
    """Return the identity fields of the selected original audio."""
    return {
        'index': stream.index,
        'codec': stream.codec,
        'profile': stream.profile,
        'channels': stream.channels,
        'sample_rate_hz': stream.sample_rate_hz,
        'language': stream.language,
    }


def _subtitle_signature(
    document: TranslationDocument,
    windows: dict[str, SegmentWindow],
    srt_hash: str,
) -> str:
    """Signature of the Russian subtitle text and timing identity."""
    return _signature(
        {
            'kind': 'subtitle-v1',
            'srt_sha256': srt_hash,
            'segments': [
                {
                    'id': segment.id,
                    'translation': segment.translation,
                    'source_cues': list(segment.source_cues),
                    'start_ms': windows[segment.id].start_ms,
                    'end_ms': windows[segment.id].end_ms,
                }
                for segment in document.segments
            ],
        }
    )


def _container_signature(
    subtitle_sig: str,
    fitting_sigs: list[str],
    selected: Any,
    policy: dict[str, Any],
    video_hash: str,
) -> str:
    """Signature of the container assembly work.

    The source video digest is part of the identity: container assembly
    copies the actual original stream packets, so a different input file
    with equal codec facts is still different container work.
    """
    return _signature(
        {
            'kind': 'container-v1',
            'subtitle': subtitle_sig,
            'narration_track': _signature(
                {
                    'kind': 'narration-track-v1',
                    'fitting': fitting_sigs,
                }
            ),
            'original_audio': _audio_identity(selected),
            'narration_audio': NARRATION_AUDIO_SETTINGS,
            'startup_defaults': {
                key: policy[key]
                for key in sorted(policy)
                if key.startswith('startup_')
            },
            'source_video_sha256': video_hash,
        }
    )


@dataclass(frozen=True)
class PrepareResult:
    """Outcome of one prepare run over a job directory."""

    manifest: dict[str, Any]
    reuse: dict[str, Any]
    invalidated: dict[str, Any]
    created: bool
    unchanged: bool


_PROJECT_DECLARATION = re.compile(r'^name = "lecture-localizer"', re.MULTILINE)


def _application_source_root() -> Path | None:
    """Locate this application source repository root when running from one.

    A candidate ancestor owns this application only when its
    pyproject.toml declares this exact project name, so an installed
    wheel inside an unrelated host project never classifies that host
    as the source checkout. No Git command is ever invoked; source
    checkouts and worktrees with a .git file are covered because their
    root pyproject.toml carries the declaration.
    """
    current = Path(__file__).resolve().parent
    for candidate in (current, *current.parents):
        marker = candidate / 'pyproject.toml'
        if not marker.is_file():
            continue
        try:
            declared = (
                _PROJECT_DECLARATION.search(marker.read_text(encoding='utf-8'))
                is not None
            )
        except OSError:
            declared = False
        if declared:
            return candidate
    return None


def _reject_source_tree_paths(job_dir: Path, output_path: Path) -> None:
    """Reject job runtime data inside this application source tree.

    The approved technical basis keeps job data outside the source
    repository; runtime data under the package root can otherwise be
    packaged as product content by a local build.
    """
    root = _application_source_root()
    if root is None:
        return
    resolved_root = root.resolve()
    for name, path in (('job directory', job_dir), ('output', output_path)):
        resolved = path.resolve()
        if resolved == resolved_root or resolved_root in resolved.parents:
            raise ContractError(
                f'the {name} must stay outside the application source'
                f' repository at {resolved_root}; job data is not product'
                ' content and must not be packaged or committed'
            )


def prepare_job(
    video_path: Path,
    srt_path: Path,
    translation_path: Path,
    job_dir: Path,
    output_path: Path,
    source_audio: int | None,
    settings: Settings,
) -> PrepareResult:
    """Validate all inputs and create or resume the persistent job.

    All persisted locations are canonicalized with resolve so the
    recorded identities are stable across working directories and
    symlink spellings of the same destination.
    """
    video_path = Path(video_path).expanduser().resolve()
    srt_path = Path(srt_path).expanduser().resolve()
    translation_path = Path(translation_path).expanduser().resolve()
    job_dir = Path(job_dir).expanduser().resolve()
    output_path = Path(output_path).expanduser().resolve()
    for label, path in (
        ('video', video_path),
        ('source SRT', srt_path),
        ('translation', translation_path),
    ):
        if not path.is_file():
            raise ContractError(f'{label} input not found: {path}')
    _reject_source_tree_paths(job_dir, output_path)
    ensure_distinct_files(
        {
            'video': video_path,
            'source SRT': srt_path,
            'translation': translation_path,
            'output': output_path,
        }
    )
    if output_path.exists():
        raise DestinationExistsError(
            f'refusing to overwrite an existing output: {output_path}'
        )
    ensure_outside(
        job_dir,
        {
            'video': video_path,
            'source SRT': srt_path,
            'translation': translation_path,
            'output': output_path,
        },
    )
    video_hash = sha256_file(video_path)
    srt_hash = sha256_file(srt_path)
    document = load_translation_document(translation_path)
    verify_binding(document, video_hash, srt_hash)
    try:
        srt_text = srt_path.read_text(encoding='utf-8')
    except UnicodeDecodeError as error:
        raise ContractError(
            f'source SRT is not valid UTF-8: {srt_path}'
        ) from error
    cues = parse_srt(srt_text)
    windows = derive_windows(cues, document.segments)
    facts = inspect_media(video_path, settings)
    _validate_video(facts)
    selected, mode, disposition = _select_audio(facts, source_audio)
    manifest_path = job_dir / MANIFEST_NAME
    with JobLock.acquire(job_dir):
        _check_operations_journal(job_dir)
        stored: dict[str, Any] | None = None
        if manifest_path.exists():
            stored = load_job_manifest(manifest_path)
            snapshot_failures = verify_integrity(
                job_dir, _snapshot_integrity_records(stored)
            )
            if snapshot_failures:
                names = ', '.join(r.label for r in snapshot_failures)
                raise ContractError(
                    'job snapshot integrity check failed for:'
                    f' {names}; the referenced immutable snapshots are'
                    ' missing or corrupted; inspect the job directory'
                    ' before any recovery'
                )
            artifact_failures = verify_integrity(
                job_dir, _artifact_integrity_records(stored['artifacts'])
            )
            if artifact_failures:
                names = ', '.join(r.label for r in artifact_failures)
                raise ContractError(
                    'job artifact integrity check failed for:'
                    f' {names}; recorded cached bytes are corrupt and'
                    ' reusable work cannot be decided until the job'
                    ' directory is inspected'
                )
        else:
            _ensure_create_ready(job_dir)
        manifest, invalidated, created, unchanged = _build_job_state(
            job_dir=job_dir,
            stored=stored,
            document=document,
            cues=cues,
            windows=windows,
            facts=facts,
            selected=selected,
            mode=mode,
            disposition=disposition,
            video_path=video_path,
            srt_path=srt_path,
            translation_path=translation_path,
            output_path=output_path,
            settings=settings,
            video_hash=video_hash,
            srt_hash=srt_hash,
        )
        if not unchanged:
            atomic_write_json(manifest_path, manifest)
    failed_labels = _integrity_failure_labels(
        verify_integrity(
            job_dir, _artifact_integrity_records(manifest['artifacts'])
        )
    )
    return PrepareResult(
        manifest=manifest,
        reuse=_reuse_view(manifest, failed_labels),
        invalidated=invalidated,
        created=created,
        unchanged=unchanged,
    )


def _stored_binding_changed(
    stored: dict[str, Any], video_hash: str, srt_hash: str
) -> bool:
    """Return True when the job was prepared for different source bytes.

    A supplied translation that already verifies against the current
    inputs is an explicit rebind; the job then recomputes affected work
    from the changed inputs and preserves the old revisions and
    artifacts. Mismatched translations never reach this point because
    verify_binding rejects them first.
    """
    binding = stored['binding']
    video_value = binding['video_sha256']
    srt_value = binding['srt_sha256']
    return (
        str(video_value).lower() != video_hash.lower()
        or str(srt_value).lower() != srt_hash.lower()
    )


def _ensure_create_ready(job_dir: Path) -> None:
    """Refuse to create a job over foreign files left in the directory.

    Entries the application owns, such as orphaned snapshot or artifact
    directories from an interrupted first prepare, stay visible and do
    not block recovery.
    """
    managed = {
        LOCK_NAME,
        SNAPSHOT_DIR,
        'artifacts',
        OPERATIONS_NAME,
        EVIDENCE_DIR,
    }
    foreign = sorted(
        entry.name for entry in job_dir.iterdir() if entry.name not in managed
    )
    if foreign:
        raise DestinationExistsError(
            f'job directory {job_dir} contains no manifest but holds'
            f' unrelated files ({", ".join(foreign)}); choose a fresh job'
            ' directory'
        )


def _build_job_state(
    job_dir: Path,
    stored: dict[str, Any] | None,
    document: TranslationDocument,
    cues: tuple[SourceCue, ...],
    windows: dict[str, SegmentWindow],
    facts: MediaStreamFacts,
    selected: AudioStreamFacts,
    mode: str,
    disposition: str,
    video_path: Path,
    srt_path: Path,
    translation_path: Path,
    output_path: Path,
    settings: Settings,
    video_hash: str,
    srt_hash: str,
) -> tuple[dict[str, Any], dict[str, Any], bool, bool]:
    """Compute the next job state, invalidations and the reuse view."""
    now = utc_now_iso()
    policy = _policy_dict(settings)
    translation_hash = sha256_bytes(document.raw_bytes)
    segments = _candidate_segments(document, windows, settings)
    subtitle_sig = _subtitle_signature(document, windows, srt_hash)
    container_sig = _container_signature(
        subtitle_sig,
        [s['fitting_signature'] for s in segments],
        selected,
        policy,
        video_hash,
    )
    rebind = stored is not None and _stored_binding_changed(
        stored, video_hash, srt_hash
    )
    if stored is None:
        invalidated: dict[str, Any] = {
            'synthesis': sorted(s['id'] for s in segments),
            'fitting': sorted(s['id'] for s in segments),
            'subtitle': True,
            'container': True,
        }
        previous_artifacts: list[dict[str, Any]] = []
        created_at = now
        base_revision = 1
        base_history: list[dict[str, Any]] = []
        snapshots = _write_snapshots(job_dir, 1, document, cues)
    else:
        old_segments = {s['id']: s for s in stored['segments']}
        invalidated = {
            'synthesis': sorted(
                s['id']
                for s in segments
                if s['id'] not in old_segments
                or old_segments[s['id']]['synthesis_signature']
                != s['synthesis_signature']
            ),
            'fitting': sorted(
                s['id']
                for s in segments
                if s['id'] not in old_segments
                or old_segments[s['id']]['fitting_signature']
                != s['fitting_signature']
            ),
            'subtitle': stored['subtitle_signature'] != subtitle_sig,
            'container': stored['container_signature'] != container_sig,
        }
        previous_artifacts = list(stored['artifacts'])
        created_at = stored['created_at']
        base_revision = stored['revision']
        base_history = list(stored['history'])
        cues_hash = sha256_bytes(
            _snapshot_bytes('source_cues', document, cues)
        )
        content_changed = (
            stored['snapshots']['translation']['sha256'] != translation_hash
            or stored['snapshots']['source_cues']['sha256'] != cues_hash
        )
        if content_changed:
            snapshots = _write_snapshots(
                job_dir,
                stored['snapshots']['translation']['revision'] + 1,
                document,
                cues,
            )
        else:
            snapshots = dict(stored['snapshots'])
    artifact_failures: tuple[IntegrityRecord, ...] = ()
    failed_labels = _integrity_failure_labels(artifact_failures)
    view = _cache_view(
        {
            'segments': segments,
            'subtitle_signature': subtitle_sig,
            'container_signature': container_sig,
        },
        previous_artifacts,
        failed_labels,
    )
    manifest: dict[str, Any] = {
        'schema_version': 1,
        'created_at': created_at,
        'updated_at': now,
        'revision': base_revision,
        'binding': {
            'video_sha256': video_hash,
            'srt_sha256': srt_hash,
            'translation_sha256': translation_hash,
            'video_path': str(video_path),
            'srt_path': str(srt_path),
            'translation_path': str(translation_path),
        },
        'media': {
            'container_format': facts.container_format,
            'duration_seconds': facts.duration_seconds,
            'video_stream': facts.video.to_dict(),
            'audio_stream': selected.to_dict(),
            'audio_selection': {'mode': mode, 'index': selected.index},
            'audio_disposition': disposition,
        },
        'output_path': str(output_path),
        'policy': policy,
        'segments': segments,
        'subtitle_signature': subtitle_sig,
        'container_signature': container_sig,
        'snapshots': snapshots,
        'artifacts': previous_artifacts,
        'stages': view['stages'],
        'history': base_history,
        'blocked': view['blocked'],
    }
    history_entry = {
        'revision': base_revision + (0 if stored is None else 1),
        'timestamp': now,
        'invalidated': invalidated,
    }
    if rebind:
        history_entry['rebind'] = True
    if stored is not None and _content_view(manifest) == _content_view(stored):
        return stored, invalidated, False, True
    manifest['revision'] = base_revision + (0 if stored is None else 1)
    manifest['history'] = [*base_history, history_entry]
    return manifest, invalidated, stored is None, False


def _classify_records(
    kind: str,
    expected: dict[str | None, str],
    artifacts: list[dict[str, Any]],
    failed_labels: frozenset[str],
) -> dict[str | None, str]:
    """Classify one artifact kind against expected unit signatures.

    A unit is ok when any retained record for it carries the current
    signature and verifies: history is scanned, not just the last row.
    Records for other signatures stay as valid history and never decide
    current eligibility.
    """
    result: dict[str | None, str] = {}
    for unit, signature in expected.items():
        records = [
            record
            for record in artifacts
            if record['kind'] == kind and record['segment'] == unit
        ]
        if not records:
            result[unit] = 'missing'
        elif any(
            _artifact_label(record) not in failed_labels
            and record['signature'] == signature
            for record in records
        ):
            result[unit] = 'ok'
        elif any(
            _artifact_label(record) in failed_labels for record in records
        ):
            result[unit] = 'corrupt'
        else:
            result[unit] = 'stale'
    return result


def _unit_classifications(
    manifest: dict[str, Any],
    artifacts: list[dict[str, Any]],
    failed_labels: frozenset[str],
) -> dict[str, Any]:
    """Classify every required cache unit against the current inputs."""
    segment_ids = [s['id'] for s in manifest['segments']]
    raw = _classify_records(
        'raw_response',
        {s['id']: s['synthesis_signature'] for s in manifest['segments']},
        artifacts,
        failed_labels,
    )
    fitted = _classify_records(
        'fitted_audio',
        {s['id']: s['fitting_signature'] for s in manifest['segments']},
        artifacts,
        failed_labels,
    )
    subtitle = _classify_records(
        'subtitles',
        {None: manifest['subtitle_signature']},
        artifacts,
        failed_labels,
    )[None]
    container = _classify_records(
        'container_track',
        {None: manifest['container_signature']},
        artifacts,
        failed_labels,
    )[None]
    return {
        'units': {
            'raw_response': raw,
            'fitted_audio': fitted,
            'subtitles': subtitle,
            'container_track': container,
        },
        'segment_ids': segment_ids,
    }


def _timeline_total_frames(manifest: dict[str, Any]) -> int:
    """Derive the timeline frame count from the measured duration.

    The measured video duration is converted with explicit nearest
    sample, half up rounding; the quantization is reported by the
    pipeline report instead of being silently absorbed.
    """
    duration = manifest['media']['duration_seconds']
    if duration is None:
        raise ContractError(
            'the recorded media carries no duration, so the assembly'
            ' timeline length cannot be derived'
        )
    frames = Fraction(str(duration)) * TIMELINE_SAMPLE_RATE
    return int(frames + Fraction(1, 2))


def _timeline_duration_ms(total_frames: int) -> int:
    """Convert an exact timeline frame count to half up milliseconds."""
    return (
        total_frames + TIMELINE_SAMPLES_PER_MS // 2
    ) // TIMELINE_SAMPLES_PER_MS


def _duration_to_ms(duration: float) -> int:
    """Round one measured duration to half up integer milliseconds."""
    return int(Fraction(str(duration)) * 1000 + Fraction(1, 2))


def _timeline_signature(manifest: dict[str, Any]) -> str:
    """Signature of the complete assembled PCM timeline."""
    return _signature(
        {
            'kind': 'timeline-v1',
            'fitting': [s['fitting_signature'] for s in manifest['segments']],
            'total_frames': _timeline_total_frames(manifest),
        }
    )


def _narration_track_signature(timeline_signature: str) -> str:
    """Signature of the encoded narration track."""
    return _signature(
        {
            'kind': 'narration-track-v1',
            'timeline': timeline_signature,
            'settings': NARRATION_AUDIO_SETTINGS,
        }
    )


def _original_track_signature(manifest: dict[str, Any]) -> str:
    """Signature of the converted original audio track."""
    return _signature(
        {
            'kind': 'original-track-v1',
            'video_sha256': manifest['binding']['video_sha256'],
            'audio_stream': manifest['media']['audio_stream'],
            'settings': ORIGINAL_AUDIO_SETTINGS,
        }
    )


def _assembly_model(
    manifest: dict[str, Any],
    artifacts: list[dict[str, Any]],
    failed_labels: frozenset[str],
) -> dict[str, Any]:
    """Classify every prepared assembly unit of the current inputs."""
    synthesis = {
        s['id']: s['synthesis_signature'] for s in manifest['segments']
    }
    fitting = {s['id']: s['fitting_signature'] for s in manifest['segments']}
    timeline_signature = _timeline_signature(manifest)
    narration_signature = _narration_track_signature(timeline_signature)
    original_signature: str | None = None
    units: dict[str, Any] = {
        'normalized_audio': _classify_records(
            'normalized_audio', synthesis, artifacts, failed_labels
        ),
        'fitted_audio': _classify_records(
            'fitted_audio', fitting, artifacts, failed_labels
        ),
        'timeline_audio': _classify_records(
            'timeline_audio',
            {None: timeline_signature},
            artifacts,
            failed_labels,
        )[None],
        'narration_audio': _classify_records(
            'narration_audio',
            {None: narration_signature},
            artifacts,
            failed_labels,
        )[None],
    }
    if manifest['media']['audio_disposition'] == 'convert':
        original_signature = _original_track_signature(manifest)
        units['original_audio'] = _classify_records(
            'original_audio',
            {None: original_signature},
            artifacts,
            failed_labels,
        )[None]
    return {
        'units': units,
        'signatures': {
            'timeline': timeline_signature,
            'narration': narration_signature,
            'original': original_signature,
        },
    }


def _assembly_view(
    manifest: dict[str, Any],
    artifacts: list[dict[str, Any]],
    failed_labels: frozenset[str],
) -> dict[str, Any]:
    """Summarize downstream assembly readiness with actionable blockers.

    A recorded missing duration keeps the job readable: assembly is
    reported as unplannable instead of raising, and no duration value
    is ever invented.
    """
    blocked: list[str] = []
    if manifest['media']['duration_seconds'] is None:
        blocked.append(
            'the recorded media carries no duration, so the assembly'
            ' timeline cannot be planned; supply a source whose'
            ' duration ffprobe can report and prepare again'
        )
        per_segment: dict[str, Any] = {
            'normalized_audio': _classify_records(
                'normalized_audio',
                {
                    s['id']: s['synthesis_signature']
                    for s in manifest['segments']
                },
                artifacts,
                failed_labels,
            ),
            'fitted_audio': _classify_records(
                'fitted_audio',
                {
                    s['id']: s['fitting_signature']
                    for s in manifest['segments']
                },
                artifacts,
                failed_labels,
            ),
        }
        return {
            'complete': False,
            'blocked': blocked,
            'units': {
                **per_segment,
                'timeline_audio': 'missing',
                'narration_audio': 'missing',
                **(
                    {'original_audio': 'missing'}
                    if manifest['media']['audio_disposition'] == 'convert'
                    else {}
                ),
            },
            'signatures': {
                'timeline': None,
                'narration': None,
                'original': None,
            },
        }
    model = _assembly_model(manifest, artifacts, failed_labels)
    units = model['units']
    normalized = units['normalized_audio']
    fitted = units['fitted_audio']
    for segment in manifest['segments']:
        segment_id = segment['id']
        if (
            fitted.get(segment_id) == 'ok'
            and normalized.get(segment_id) != 'ok'
        ):
            blocked.append(
                f'fitted audio for {segment_id} is present while its'
                ' normalized source is missing; inspect the job'
                ' directory before any recovery'
            )
        if (
            normalized.get(segment_id) not in (None, 'ok', 'missing')
            and fitted.get(segment_id) == 'missing'
        ):
            blocked.append(
                f'normalized audio for {segment_id} is damaged while no'
                ' fitted work exists; the damaged unit blocks assembly'
            )
    timeline = units['timeline_audio']
    narration = units['narration_audio']
    if narration == 'ok' and timeline != 'ok':
        blocked.append(
            'the encoded narration track is present while its PCM'
            ' timeline is missing or not current; inspect the job'
            ' directory before any recovery'
        )
    if timeline == 'ok' and (
        any(status != 'ok' for status in fitted.values())
        or any(status != 'ok' for status in normalized.values())
    ):
        blocked.append(
            'the PCM timeline is present while required per segment'
            ' audio units are missing or damaged; processed audio'
            ' cannot replace the missing units'
        )
    if manifest['media']['audio_disposition'] == 'convert':
        original = units['original_audio']
        if narration == 'ok' and original != 'ok':
            blocked.append(
                'the converted original audio track is missing or not'
                ' current while downstream container work exists'
            )
    complete = (
        all(status == 'ok' for status in normalized.values())
        and all(status == 'ok' for status in fitted.values())
        and timeline == 'ok'
        and narration == 'ok'
        and (
            manifest['media']['audio_disposition'] != 'convert'
            or units['original_audio'] == 'ok'
        )
    )
    return {
        'complete': complete,
        'blocked': blocked,
        'units': units,
        'signatures': model['signatures'],
    }


def _cache_view(
    manifest: dict[str, Any],
    artifacts: list[dict[str, Any]],
    failed_labels: frozenset[str],
) -> dict[str, Any]:
    """Compute one consistent stage and blocker view of the current cache.

    Stage completeness requires every required current unit, verified
    integrity and complete upstream provenance: fitted audio never
    stands in for a missing raw response, and container work requires
    complete upstream stages.
    """
    model = _unit_classifications(manifest, artifacts, failed_labels)
    raw = model['units']['raw_response']
    fitted = model['units']['fitted_audio']
    subtitle = model['units']['subtitles']
    container = model['units']['container_track']
    segment_ids: list[str] = model['segment_ids']

    blocked: list[str] = []
    provenance_missing = [
        segment_id
        for segment_id in segment_ids
        if raw.get(segment_id) != 'ok'
        and fitted.get(segment_id) not in (None, 'missing')
    ]
    for segment_id in provenance_missing:
        blocked.append(
            f'required synthesis artifact for {segment_id} is missing or'
            ' not current while downstream fitted work exists; processed'
            ' audio cannot replace the raw response'
        )
    raw_state: str
    if any(status == 'corrupt' for status in raw.values()):
        raw_state = 'blocked'
    elif all(raw.get(segment_id) == 'ok' for segment_id in segment_ids):
        raw_state = 'complete'
    else:
        raw_state = 'pending'
    fitted_state: str
    corrupt_fitted = any(status == 'corrupt' for status in fitted.values())
    if corrupt_fitted or provenance_missing:
        fitted_state = 'blocked'
    elif (
        all(fitted.get(segment_id) == 'ok' for segment_id in segment_ids)
        and raw_state == 'complete'
    ):
        fitted_state = 'complete'
    else:
        fitted_state = 'pending'
    subtitle_state: str
    if subtitle == 'corrupt':
        subtitle_state = 'blocked'
    elif subtitle == 'ok':
        subtitle_state = 'complete'
    else:
        subtitle_state = 'pending'
    container_state: str
    if container == 'corrupt':
        container_state = 'blocked'
    elif container == 'ok' and (
        subtitle_state != 'complete'
        or raw_state != 'complete'
        or fitted_state != 'complete'
    ):
        container_state = 'blocked'
        blocked.append(
            'container artifact matches the current inputs but required'
            ' upstream stages are not complete'
        )
    elif container == 'ok':
        container_state = 'complete'
    else:
        container_state = 'pending'
    return {
        'stages': {
            'synthesis': {'state': raw_state},
            'fitting': {'state': fitted_state},
            'subtitle': {'state': subtitle_state},
            'container': {'state': container_state},
        },
        'blocked': blocked,
        'units': model['units'],
    }


def _content_view(manifest: dict[str, Any]) -> dict[str, Any]:
    """Manifest view compared for unchanged detection."""
    return {
        key: value for key, value in manifest.items() if key != 'updated_at'
    }


def _reuse_view(
    manifest: dict[str, Any],
    failed_labels: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Summarize preserved, invalidated and pending work from a manifest.

    The reuse view consumes the same current eligibility model as the
    stage rollup: only records matching the current inputs with verified
    bytes count as preserved work.
    """
    latest = manifest['history'][-1]['invalidated']
    artifacts = manifest.get('artifacts', [])
    initial = manifest['revision'] == 1 and len(manifest['history']) == 1
    model = _unit_classifications(manifest, artifacts, failed_labels)
    units = model['units']
    reuse: dict[str, Any] = {}
    for stage, kind in (
        ('synthesis', 'raw_response'),
        ('fitting', 'fitted_audio'),
    ):
        preserved: list[str] = []
        invalidated: dict[str, str] = {}
        pending: list[str] = []
        for segment in manifest['segments']:
            segment_id = segment['id']
            status = units[kind].get(segment_id, 'missing')
            if status == 'ok':
                if stage == 'fitting' and units['raw_response'].get(
                    segment_id
                ) not in (None, 'ok'):
                    invalidated[segment_id] = 'missing_provenance'
                else:
                    preserved.append(segment_id)
            elif status == 'corrupt':
                invalidated[segment_id] = 'artifact_corrupt'
            elif status == 'stale':
                invalidated[segment_id] = 'signature_changed'
            elif segment_id in latest[stage]:
                invalidated[segment_id] = (
                    'initial_revision' if initial else 'signature_changed'
                )
            else:
                pending.append(segment_id)
        reuse[stage] = {
            'preserved': preserved,
            'invalidated': invalidated,
            'pending': pending,
        }
    for stage, kind, flag in (
        ('subtitle', 'subtitles', latest['subtitle']),
        ('container', 'container_track', latest['container']),
    ):
        status = units[kind]
        if status == 'ok' and stage == 'container':
            view = _cache_view(manifest, artifacts, failed_labels)
            complete = view['stages']['container']['state'] == 'complete'
        else:
            complete = status == 'ok'
        if status == 'ok' and complete:
            reuse[stage] = {
                'preserved': True,
                'invalidated': False,
                'pending': False,
            }
        elif status == 'corrupt':
            reuse[stage] = {
                'preserved': False,
                'invalidated': 'artifact_corrupt',
                'pending': False,
            }
        elif status == 'stale' or flag:
            reuse[stage] = {
                'preserved': False,
                'invalidated': (
                    'signature_changed'
                    if status == 'stale' or not initial
                    else 'initial_revision'
                ),
                'pending': False,
            }
        else:
            reuse[stage] = {
                'preserved': False,
                'invalidated': False,
                'pending': True,
            }
    return reuse


def read_job_status(job_dir: Path) -> dict[str, Any]:
    """Read one job directory and build its read only status report."""
    manifest_path = job_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise ContractError(f'no job manifest found at {manifest_path}')
    manifest = load_job_manifest(manifest_path)
    snapshot_failures = verify_integrity(
        job_dir, _snapshot_integrity_records(manifest)
    )
    artifacts = manifest['artifacts']
    artifact_failures = verify_integrity(
        job_dir, _artifact_integrity_records(artifacts)
    )
    failed_labels = _integrity_failure_labels(artifact_failures)
    blocked = list(manifest['blocked'])
    for failure in snapshot_failures:
        blocked.append(
            f'snapshot integrity failure: {failure.label} at'
            f' {failure.relative_path}'
        )
    for failure in artifact_failures:
        blocked.append(
            f'artifact integrity failure: {failure.label} at'
            f' {failure.relative_path}'
        )
    cache = _cache_view(manifest, artifacts, failed_labels)
    blocked.extend(cache['blocked'])
    assembly = _assembly_view(manifest, artifacts, failed_labels)
    blocked.extend(assembly['blocked'])
    operations, journal_blocked = _status_operations_view(job_dir)
    blocked.extend(journal_blocked)
    publication = _publication_status_view(
        job_dir, manifest, artifacts, failed_labels
    )
    blocked.extend(publication.get('blocked', []))
    return {
        'command': 'status',
        'job_dir': str(job_dir),
        'schema_version': manifest['schema_version'],
        'revision': manifest['revision'],
        'created_at': manifest['created_at'],
        'updated_at': manifest['updated_at'],
        'binding': manifest['binding'],
        'output_path': manifest['output_path'],
        'media': manifest['media'],
        'segment_count': len(manifest['segments']),
        'stages': cache['stages'],
        'assembly': {
            'complete': assembly['complete'],
            'units': assembly['units'],
        },
        'reuse': _reuse_view(manifest, failed_labels),
        'operations': operations,
        'publication': {
            key: value
            for key, value in publication.items()
            if key != 'blocked'
        },
        'latest_report': _latest_report_name(job_dir),
        'blocked': blocked,
    }


def _publication_status_view(
    job_dir: Path,
    manifest: dict[str, Any],
    artifacts: list[dict[str, Any]],
    failed_labels: frozenset[str],
) -> dict[str, Any]:
    """Derive the read only publication state of one job.

    The candidate classification consumes the actual integrity
    results, and recognizing a published output needs the full
    receipt chain: the exact destination, the recorded file identity
    and intact bytes hashing to the recorded digest.
    """
    container = _classify_records(
        'container_track',
        {None: manifest['container_signature']},
        artifacts,
        failed_labels,
    )[None]
    output_path = Path(manifest['output_path'])
    view: dict[str, Any] = {
        'candidate': container,
        'output_path': str(output_path),
        'state': 'pending',
        'recognized': False,
        'blocked': [],
    }
    receipt: dict[str, Any] | None = None
    try:
        receipt = _load_publication_receipt(job_dir)
    except ContractError as error:
        view['state'] = 'blocked'
        view['blocked'].append(
            f'publication receipt is malformed and blocks recovery: {error}'
        )
        return view
    if container == 'corrupt':
        view['state'] = 'blocked'
        view['blocked'].append(
            'the registered container candidate is missing or corrupt;'
            ' publication state cannot be trusted until inspected'
        )
        return view
    if not output_path.exists():
        view['state'] = 'pending'
        return view
    candidate_record = _current_record(
        manifest, 'container_track', None, manifest['container_signature']
    )
    if candidate_record is not None and _owned_publication_matches(
        receipt, output_path, candidate_record
    ):
        view['state'] = 'published'
        view['recognized'] = True
        view['sha256'] = candidate_record['sha256']
        return view
    view['state'] = 'collision'
    view['blocked'].append(
        'the output path holds a file without owned publication'
        f' provenance for the current candidate: {output_path}; it is'
        ' preserved untouched and never accepted by byte equality or'
        ' file identity alone'
    )
    return view


SUPPORTED_OPERATIONS_VERSION = 1


class PaidExecutionDenied(RuntimeError):
    """A paid speech request was refused before any HTTP attempt."""

    def __init__(self, kind: str, detail: str) -> None:
        """Record the stable refusal kind and its explanation."""
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


@dataclass(frozen=True)
class SpendingLimits:
    """Explicit numeric spending limits with their price basis."""

    max_requests: int
    max_text_chars: int
    max_cost_estimate: float
    price_per_request: float
    price_basis: str

    def __post_init__(self) -> None:
        """Reject malformed or non numeric limit values."""
        for name in ('max_requests', 'max_text_chars'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f'{name} must be an integer')
            if value < 1:
                raise ValueError(f'{name} must be at least 1')
        for name in ('max_cost_estimate', 'price_per_request'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f'{name} must be a number')
            if value < 0 or value != value or value == float('inf'):
                raise ValueError(f'{name} must be finite and nonnegative')
        if not isinstance(self.price_basis, str) or not self.price_basis:
            raise ValueError('price_basis must be a nonempty string')

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON serializable view."""
        return {
            'max_requests': self.max_requests,
            'max_text_chars': self.max_text_chars,
            'max_cost_estimate': self.max_cost_estimate,
            'price_per_request': self.price_per_request,
            'price_basis': self.price_basis,
        }


@dataclass(frozen=True)
class SpeechExecutionResult:
    """Outcome of one guarded speech execution pass."""

    planned: tuple[str, ...]
    reused: tuple[str, ...]
    completed: tuple[str, ...]
    failed: dict[str, str]
    uncertain: dict[str, str]
    blocked_unresolved: tuple[dict[str, str], ...]
    request_count: int


def load_operations_journal(job_dir: Path) -> dict[str, Any] | None:
    """Load the operations journal or return None when it is absent."""
    path = Path(job_dir) / OPERATIONS_NAME
    if not path.is_file():
        return None
    data = load_strict_json_file(path)
    if not isinstance(data, dict):
        raise ContractError('operations journal root must be a JSON object')
    if data.get('schema_version') != SUPPORTED_OPERATIONS_VERSION:
        version = data.get('schema_version')
        raise ContractError(
            f'unsupported operations journal schema_version {version!r};'
            f' this version understands only {SUPPORTED_OPERATIONS_VERSION}'
        )
    validate_instance(data, OPERATIONS_SCHEMA, 'operations journal')
    seen: set[str] = set()
    for operation in data['operations']:
        operation_id = operation['operation_id']
        if operation_id in seen:
            raise ContractError(
                f'duplicate operation id in the journal:'
                f' {operation_id}; inspect operations.json manually'
            )
        seen.add(operation_id)
    return data


def _check_operations_journal(job_dir: Path) -> None:
    """Refuse preparation while the journal is malformed or unknown."""
    try:
        load_operations_journal(job_dir)
    except ContractError as error:
        raise ContractError(
            f'operations journal check failed: {error};'
            f' {OPERATIONS_NAME} is preserved untouched and paid work'
            ' stays blocked until the journal is inspected manually'
        ) from error


def _operations_summary(journal: dict[str, Any] | None) -> dict[str, Any]:
    """Summarize the journal for the read only status report."""
    if journal is None:
        return {
            'state': 'absent',
            'operations': 0,
            'by_state': {},
            'unresolved': [],
        }
    by_state: dict[str, int] = {}
    unresolved: list[str] = []
    for operation in journal['operations']:
        state = operation['state']
        by_state[state] = by_state.get(state, 0) + 1
        if state in UNRESOLVED_OPERATION_STATES:
            unresolved.append(operation['operation_id'])
    return {
        'state': 'ok',
        'operations': len(journal['operations']),
        'by_state': by_state,
        'unresolved': unresolved,
    }


def _status_operations_view(job_dir: Path) -> tuple[dict[str, Any], list[str]]:
    """Build the journal view and its blocked messages for status."""
    try:
        journal = load_operations_journal(job_dir)
    except ContractError as error:
        return (
            {
                'state': 'malformed',
                'operations': 0,
                'by_state': {},
                'unresolved': [],
            },
            [
                'operations journal is malformed or of an unknown version'
                f' and blocks paid execution: {error}'
            ],
        )
    blocked: list[str] = []
    if journal is not None:
        for operation in journal['operations']:
            state = operation['state']
            if state in UNRESOLVED_OPERATION_STATES:
                blocked.append(
                    f'operation {operation["operation_id"]} for segment'
                    f' {operation["segment_id"]} is {state} and requires'
                    ' an explicit retry decision before any new paid'
                    ' request'
                )
    return _operations_summary(journal), blocked


def _verify_job_integrity(job_dir: Path, manifest: dict[str, Any]) -> None:
    """Refuse execution when any retained provenance bytes are damaged."""
    snapshot_failures = verify_integrity(
        job_dir, _snapshot_integrity_records(manifest)
    )
    if snapshot_failures:
        names = ', '.join(r.label for r in snapshot_failures)
        raise ContractError(
            'job snapshot integrity check failed for:'
            f' {names}; damaged provenance stops paid execution before'
            ' any request'
        )
    artifact_failures = verify_integrity(
        job_dir, _artifact_integrity_records(manifest['artifacts'])
    )
    if artifact_failures:
        names = ', '.join(r.label for r in artifact_failures)
        raise ContractError(
            'job artifact integrity check failed for:'
            f' {names}; damaged cached provenance stops paid execution'
            ' before any request'
        )


def _verify_window_feasibility(manifest: dict[str, Any]) -> None:
    """Refuse execution when a source window leaves the video."""
    duration = manifest['media']['duration_seconds']
    if duration is None:
        raise ContractError(
            'the recorded media carries no duration, so window'
            ' feasibility inside the video cannot be established before'
            ' paid requests'
        )
    limit_ms = float(duration) * 1000.0
    for segment in manifest['segments']:
        end_ms = segment['window']['end_ms']
        if end_ms > limit_ms:
            raise ContractError(
                f'window of segment {segment["id"]} ends at {end_ms} ms,'
                f' beyond the recorded video duration {limit_ms} ms'
            )


def _planned_segments(
    manifest: dict[str, Any],
    artifacts: list[dict[str, Any]],
    failed_labels: frozenset[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Split manifest segments into planned and fully cached sets."""
    model = _unit_classifications(manifest, artifacts, failed_labels)
    raw = model['units']['raw_response']
    planned = [
        segment
        for segment in manifest['segments']
        if raw.get(segment['id']) != 'ok'
    ]
    planned_ids = {segment['id'] for segment in planned}
    reused = [
        segment['id']
        for segment in manifest['segments']
        if segment['id'] not in planned_ids
    ]
    return planned, reused


def _posted_text(
    job_dir: Path, manifest: dict[str, Any], segment_id: str
) -> str:
    """Read the frozen posted text of one segment from its snapshot."""
    record = manifest['snapshots']['narration']
    path = job_dir / record['path']
    actual = sha256_file(path)
    if actual != record['sha256']:
        raise ContractError(
            f'narration snapshot {record["path"]} no longer matches its'
            ' recorded digest; provenance is damaged and no paid request'
            ' may start'
        )
    payload = load_strict_json_file(path)
    for entry in payload['segments']:
        if entry['id'] == segment_id:
            return str(entry['text'])
    raise ContractError(
        f'narration snapshot {record["path"]} carries no text for'
        f' segment {segment_id}'
    )


def _require_execution_permission(
    allow_paid: bool,
    limits: SpendingLimits | None,
    planned_count: int,
    text_chars: int,
) -> float:
    """Apply the paid execution guards before any key or HTTP use."""
    if not allow_paid:
        raise PaidExecutionDenied(
            'permission',
            'paid speech execution requires an explicit permission flag;'
            ' no request was sent and no key was read',
        )
    if limits is None:
        raise PaidExecutionDenied(
            'limits',
            'explicit numeric spending limits are required before paid'
            ' execution; no request was sent and no key was read',
        )
    estimated_cost = Fraction(str(limits.price_per_request)) * planned_count
    if planned_count > limits.max_requests:
        raise PaidExecutionDenied(
            'limits_exceeded',
            f'planning {planned_count} requests exceeds the explicit'
            f' limit of {limits.max_requests}; no request was sent',
        )
    if text_chars > limits.max_text_chars:
        raise PaidExecutionDenied(
            'limits_exceeded',
            f'planning {text_chars} text characters exceeds the explicit'
            f' limit of {limits.max_text_chars}; no request was sent',
        )
    if estimated_cost > Fraction(str(limits.max_cost_estimate)):
        raise PaidExecutionDenied(
            'limits_exceeded',
            f'estimated cost {float(estimated_cost)} above the explicit limit'
            f' {limits.max_cost_estimate} on basis'
            f' {limits.price_basis!r}; no request was sent',
        )
    return float(estimated_cost)


def _read_speech_key(reader: Callable[[str], str | None]) -> str:
    """Read the provider key only after every earlier guard passed."""
    key = reader(xai_client.KEY_ENV_VAR)
    if not key:
        raise PaidExecutionDenied(
            'key',
            f'no provider key was found in {xai_client.KEY_ENV_VAR};'
            ' no request was sent',
        )
    return key


def _unresolved_by_segment(
    journal: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Map segment ids to their unresolved journal operations."""
    result: dict[str, dict[str, Any]] = {}
    for operation in (journal or {}).get('operations', []):
        if operation['state'] in UNRESOLVED_OPERATION_STATES:
            result[operation['segment_id']] = operation
    return result


def _new_journal(journal: dict[str, Any] | None, now: str) -> dict[str, Any]:
    """Return a writable journal payload, creating a fresh one."""
    if journal is None:
        return {
            'schema_version': SUPPORTED_OPERATIONS_VERSION,
            'operations': [],
            'updated_at': now,
        }
    return journal


def _write_journal(path: Path, journal: dict[str, Any], now: str) -> None:
    """Durably persist the journal payload."""
    journal['updated_at'] = now
    atomic_write_json(path, journal)


def _operation_for_segment(
    segment: dict[str, Any],
    manifest: dict[str, Any],
    limits: SpendingLimits,
    text: str,
    now: str,
) -> dict[str, Any]:
    """Build one reserved operation record for one segment."""
    return {
        'operation_id': uuid.uuid4().hex,
        'segment_id': segment['id'],
        'synthesis_signature': segment['synthesis_signature'],
        'request_signature': request_signature(text),
        'posted_text_sha256': _segment_text_hash(text),
        'posted_text_chars': len(text),
        'posted_text_snapshot': {
            'path': manifest['snapshots']['narration']['path'],
            'sha256': manifest['snapshots']['narration']['sha256'],
        },
        'reserved_at': now,
        'state': 'reserved',
        'limits': limits.to_dict(),
        'estimated_cost': limits.price_per_request,
        'attempts': [],
    }


def _fresh_attempt(
    operation: dict[str, Any],
    limits: SpendingLimits,
    linked: bool,
    now: str,
) -> dict[str, Any]:
    """Append one in flight attempt record to the operation."""
    attempt = {
        'attempt': len(operation['attempts']) + 1,
        'linked_retry': linked,
        'limits': limits.to_dict(),
        'started_at': now,
        'finished_at': None,
        'verdict': None,
        'request_signature': operation['request_signature'],
        'failure': None,
        'evidence': None,
        'response': None,
        'artifact': None,
    }
    operation['attempts'].append(attempt)
    return attempt


def _finish_attempt(
    attempt: dict[str, Any],
    verdict: str,
    now: str,
    *,
    failure: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
    response: dict[str, Any] | None = None,
    artifact: dict[str, Any] | None = None,
) -> None:
    """Record the final verdict fields on one attempt."""
    attempt['finished_at'] = now
    attempt['verdict'] = verdict
    attempt['failure'] = failure
    attempt['evidence'] = evidence
    attempt['response'] = response
    attempt['artifact'] = artifact


def _retain_evidence(
    job_dir: Path,
    sink: Path,
    operation_id: str,
    attempt_number: int,
) -> dict[str, Any] | None:
    """Move received bytes into the evidence directory when possible."""
    if not sink.is_file():
        return None
    relative = f'{EVIDENCE_DIR}/{operation_id}-a{attempt_number}.bin'
    target = job_dir / relative
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        move_new_file(sink, target)
    except (DestinationExistsError, OSError):
        return None
    return {
        'path': relative,
        'sha256': sha256_file(target),
        'bytes': target.stat().st_size,
    }


def _redact_secrets(text: str, secrets: tuple[str, ...]) -> str:
    """Remove known credential values from persisted diagnostics."""
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, '[redacted]')
    return redacted


def _publish_raw_artifact(
    job_dir: Path,
    manifest: dict[str, Any],
    manifest_path: Path,
    segment: dict[str, Any],
    sink: Path,
    receipt: Any,
    now: str,
) -> dict[str, Any]:
    """Publish the validated response as an immutable raw artifact.

    The artifact name is an opaque safe token derived from the segment
    id; the original id is retained as the record metadata so the
    supported nonempty unique id contract stays unrestricted.
    """
    ordinal = len(manifest['artifacts'])
    safe_token = sha256_bytes(segment['id'].encode('utf-8'))[:16]
    relative = f'artifacts/{ordinal:03d}-raw_response-{safe_token}.wav'
    move_new_file(sink, job_dir / relative)
    artifact = {
        'kind': 'raw_response',
        'segment': segment['id'],
        'signature': segment['synthesis_signature'],
        'path': relative,
        'sha256': sha256_file(job_dir / relative),
        'created_at': now,
    }
    manifest['artifacts'].append(artifact)
    view = _cache_view(
        {
            'segments': manifest['segments'],
            'subtitle_signature': manifest['subtitle_signature'],
            'container_signature': manifest['container_signature'],
        },
        manifest['artifacts'],
        frozenset(),
    )
    manifest['stages'] = view['stages']
    manifest['blocked'] = view['blocked']
    manifest['updated_at'] = now
    atomic_write_json(manifest_path, manifest)
    return artifact


def _run_operation_attempt(
    job_dir: Path,
    journal_path: Path,
    journal: dict[str, Any],
    operation: dict[str, Any],
    manifest: dict[str, Any],
    manifest_path: Path,
    segment: dict[str, Any],
    limits: SpendingLimits,
    client: SpeechClient,
    now_fn: Callable[[], str],
    linked: bool,
    redactor: Callable[[str], str],
) -> str:
    """Reserve the attempt durably, perform HTTP once and record it.

    Returns the operation verdict: completed, failed or uncertain.
    Persisted failure diagnostics pass through the redactor so no
    configured credential value can survive into the journal.
    """
    text = _posted_text(job_dir, manifest, segment['id'])
    validate_posted_text(text)
    operation_id = operation['operation_id']
    attempt = _fresh_attempt(operation, limits, linked, now_fn())
    attempt_number = attempt['attempt']
    operation['state'] = 'started'
    _write_journal(journal_path, journal, now_fn())
    sink = (
        job_dir
        / 'artifacts'
        / (f'.partial-{operation_id}-a{attempt_number}.tmp')
    )
    started = time.monotonic()
    retained = False
    try:
        try:
            receipt = client.synthesize(text=text, sink=sink)
            elapsed = time.monotonic() - started
        except DefinitiveSpeechFailure as failure:
            elapsed = time.monotonic() - started
            evidence = _retain_evidence(
                job_dir, sink, operation_id, attempt_number
            )
            retained = evidence is not None
            response_record: dict[str, Any] | None = None
            if failure.evidence.status_code is not None:
                response_record = {
                    'status': failure.evidence.status_code,
                    'content_type': redactor(
                        failure.evidence.content_type or 'unknown'
                    ),
                    'bytes': failure.evidence.byte_count,
                    'sha256': failure.evidence.sha256,
                    'wav': None,
                    'elapsed_seconds': elapsed,
                }
            _finish_attempt(
                attempt,
                'failed',
                now_fn(),
                failure={
                    'kind': failure.kind,
                    'detail': redactor(failure.detail),
                },
                evidence=evidence,
                response=response_record,
            )
            operation['state'] = 'failed'
            _write_journal(journal_path, journal, now_fn())
            return 'failed'
        except (UncertainSpeechFailure, OSError) as failure:
            elapsed = time.monotonic() - started
            kind = (
                failure.kind
                if isinstance(failure, UncertainSpeechFailure)
                else 'storage_error'
            )
            detail = redactor(str(failure))
            evidence = _retain_evidence(
                job_dir, sink, operation_id, attempt_number
            )
            retained = evidence is not None
            _finish_attempt(
                attempt,
                'uncertain',
                now_fn(),
                failure={'kind': kind, 'detail': detail},
                evidence=evidence,
            )
            operation['state'] = 'uncertain'
            _write_journal(journal_path, journal, now_fn())
            return 'uncertain'
        except BaseException:
            try:
                evidence = _retain_evidence(
                    job_dir, sink, operation_id, attempt_number
                )
                retained = evidence is not None
                _finish_attempt(
                    attempt,
                    'uncertain',
                    now_fn(),
                    failure={
                        'kind': 'cancelled',
                        'detail': 'the attempt was interrupted before a'
                        ' verified complete response',
                    },
                    evidence=evidence,
                )
                operation['state'] = 'uncertain'
                _write_journal(journal_path, journal, now_fn())
            except Exception:
                pass
            raise
    finally:
        if sink.exists() and (retained or sink.stat().st_size == 0):
            sink.unlink(missing_ok=True)
    artifact = _publish_raw_artifact(
        job_dir,
        manifest,
        manifest_path,
        segment,
        sink,
        receipt,
        now_fn(),
    )
    _finish_attempt(
        attempt,
        'completed',
        now_fn(),
        response={
            'status': receipt.status_code,
            'content_type': receipt.content_type,
            'bytes': receipt.byte_count,
            'sha256': receipt.sha256,
            'wav': receipt.wav.to_dict(),
            'elapsed_seconds': elapsed,
        },
        artifact={
            'path': artifact['path'],
            'sha256': artifact['sha256'],
        },
    )
    operation['state'] = 'completed'
    _write_journal(journal_path, journal, now_fn())
    return 'completed'


def _execute_one_segment(
    job_dir: Path,
    journal_path: Path,
    journal: dict[str, Any],
    manifest: dict[str, Any],
    manifest_path: Path,
    segment: dict[str, Any],
    limits: SpendingLimits,
    client: SpeechClient,
    now_fn: Callable[[], str],
    redactor: Callable[[str], str],
) -> str:
    """Reserve one new operation and run its first attempt."""
    text = _posted_text(job_dir, manifest, segment['id'])
    operation = _operation_for_segment(
        segment, manifest, limits, text, now_fn()
    )
    journal['operations'].append(operation)
    _write_journal(journal_path, journal, now_fn())
    return _run_operation_attempt(
        job_dir,
        journal_path,
        journal,
        operation,
        manifest,
        manifest_path,
        segment,
        limits,
        client,
        now_fn,
        linked=False,
        redactor=redactor,
    )


def _run_guarded_planning(
    job_dir: Path,
    *,
    allow_paid: bool,
    limits: SpendingLimits | None,
    require_permission: bool = True,
) -> tuple[
    dict[str, Any],
    dict[str, Any] | None,
    list[dict[str, Any]],
    list[str],
    int,
]:
    """Verify provenance, plan segments and apply the spending guards.

    Every newly planned fragment passes provider text validation before
    any permission decision, key access or journal mutation, so
    invalid input never becomes a recorded provider operation.
    """
    manifest = load_job_manifest(job_dir / MANIFEST_NAME)
    journal = load_operations_journal(job_dir)
    _verify_job_integrity(job_dir, manifest)
    _verify_window_feasibility(manifest)
    planned, reused = _planned_segments(
        manifest, manifest['artifacts'], frozenset()
    )
    total_chars = 0
    for segment in planned:
        text = _posted_text(job_dir, manifest, segment['id'])
        validate_posted_text(text)
        total_chars += len(text)
    if planned and require_permission:
        _require_execution_permission(
            allow_paid, limits, len(planned), total_chars
        )
    return manifest, journal, planned, reused, total_chars


def preview_speech_operations(job_dir: Path) -> dict[str, Any]:
    """Report the exact planned count and text volume without requests."""
    job_dir = Path(job_dir).expanduser().resolve()
    manifest = load_job_manifest(job_dir / MANIFEST_NAME)
    journal = load_operations_journal(job_dir)
    _verify_job_integrity(job_dir, manifest)
    planned, reused = _planned_segments(
        manifest, manifest['artifacts'], frozenset()
    )
    unresolved = _unresolved_by_segment(journal)
    blocked = [
        {
            'segment': segment_id,
            'operation_id': operation['operation_id'],
            'state': operation['state'],
        }
        for segment_id, operation in unresolved.items()
        if any(s['id'] == segment_id for s in planned)
    ]
    total_chars = sum(
        len(_posted_text(job_dir, manifest, segment['id']))
        for segment in planned
    )
    return {
        'planned': [segment['id'] for segment in planned],
        'planned_count': len(planned),
        'planned_text_chars': total_chars,
        'reused': reused,
        'blocked_unresolved': blocked,
    }


def execute_speech_operations(
    job_dir: Path,
    *,
    allow_paid: bool,
    limits: SpendingLimits | None = None,
    transport: xai_client.Transport | None = None,
    key_reader: Callable[[str], str | None] | None = None,
    now: Callable[[], str] | None = None,
) -> SpeechExecutionResult:
    """Run guarded speech synthesis for every uncached segment.

    Complete cache reuse makes zero requests and never reads the key.
    Guards run in order before any key access or HTTP: job validity,
    journal validity, provenance integrity, window feasibility,
    explicit permission, explicit numeric limits and the key itself.
    """
    job_dir = Path(job_dir).expanduser().resolve()
    now_fn = now or utc_now_iso
    reader = key_reader or os.environ.get
    with JobLock.acquire(job_dir):
        return _execute_speech_operations_locked(
            job_dir,
            allow_paid=allow_paid,
            limits=limits,
            transport=transport,
            key_reader=reader,
            now_fn=now_fn,
        )


def _execute_speech_operations_locked(
    job_dir: Path,
    *,
    allow_paid: bool,
    limits: SpendingLimits | None,
    transport: xai_client.Transport | None,
    key_reader: Callable[[str], str | None],
    now_fn: Callable[[], str],
) -> SpeechExecutionResult:
    """Speech execution body that requires the held job lock."""
    manifest, journal, planned, reused, _total = _run_guarded_planning(
        job_dir, allow_paid=allow_paid, limits=limits
    )
    if not planned:
        return SpeechExecutionResult(
            planned=(),
            reused=tuple(reused),
            completed=(),
            failed={},
            uncertain={},
            blocked_unresolved=(),
            request_count=0,
        )
    if limits is None:
        raise PaidExecutionDenied(
            'limits',
            'explicit numeric spending limits are required before'
            ' paid execution; no request was sent and no key was read',
        )
    key = _read_speech_key(key_reader)
    client = SpeechClient(key, transport=transport)

    def redactor(detail: str, _key: str = key) -> str:
        """Keep the configured credential out of persisted text."""
        return _redact_secrets(detail, (_key,))

    manifest_path = job_dir / MANIFEST_NAME
    journal_path = job_dir / OPERATIONS_NAME
    writable_journal = _new_journal(journal, now_fn())
    unresolved = _unresolved_by_segment(writable_journal)
    completed: list[str] = []
    failed: dict[str, str] = {}
    uncertain: dict[str, str] = {}
    blocked: list[dict[str, str]] = []
    executed = 0
    for segment in planned:
        segment_id = segment['id']
        pending = unresolved.get(segment_id)
        if pending is not None:
            blocked.append(
                {
                    'segment': segment_id,
                    'operation_id': pending['operation_id'],
                    'state': pending['state'],
                }
            )
            continue
        verdict = _execute_one_segment(
            job_dir,
            journal_path,
            writable_journal,
            manifest,
            manifest_path,
            segment,
            limits,
            client,
            now_fn,
            redactor,
        )
        executed += 1
        if verdict == 'completed':
            completed.append(segment_id)
        elif verdict == 'failed':
            failed[segment_id] = _latest_failure_kind(
                writable_journal, segment_id
            )
        else:
            uncertain[segment_id] = _latest_failure_kind(
                writable_journal, segment_id
            )
    return SpeechExecutionResult(
        planned=tuple(s['id'] for s in planned),
        reused=tuple(reused),
        completed=tuple(completed),
        failed=failed,
        uncertain=uncertain,
        blocked_unresolved=tuple(blocked),
        request_count=executed,
    )


def _latest_failure_kind(journal: dict[str, Any], segment_id: str) -> str:
    """Return the failure kind of the newest attempt for a segment."""
    for operation in reversed(journal['operations']):
        if operation['segment_id'] != segment_id:
            continue
        for attempt in reversed(operation['attempts']):
            failure = attempt['failure']
            if failure is not None:
                return str(failure['kind'])
            if attempt['verdict'] == 'completed':
                return 'completed'
    return 'unknown'


def _validate_operation_provenance(
    job_dir: Path,
    operation: dict[str, Any],
    text: str,
) -> None:
    """Verify the recorded posted text provenance of one operation.

    The recorded immutable snapshot file must still exist with its
    recorded digest, the recorded posted text hash and character count
    must match the actual current frozen text, and the request
    signature binding the segment to that text is checked by the
    caller. Any mismatch is damaged provenance that blocks the retry
    while keeping the journal and all receipts preserved.
    """
    operation_id = operation['operation_id']
    recorded = operation['posted_text_snapshot']
    snapshot_path = job_dir / recorded['path']
    if sha256_file(snapshot_path) != recorded['sha256']:
        raise ContractError(
            f'the recorded narration snapshot {recorded["path"]} of'
            f' operation {operation_id} is missing or no longer matches'
            ' its recorded digest; the journal provenance is damaged'
            ' and the operation stays unresolved'
        )
    if operation['posted_text_sha256'] != _segment_text_hash(text):
        raise ContractError(
            f'operation {operation_id} recorded posted text hash'
            f' {operation["posted_text_sha256"]} but the current text'
            ' hashes differently; the journal provenance is damaged'
            ' and the operation stays unresolved'
        )
    if operation['posted_text_chars'] != len(text):
        raise ContractError(
            f'operation {operation_id} recorded'
            f' {operation["posted_text_chars"]} posted text characters'
            f' but the current text has {len(text)}; the journal'
            ' provenance is damaged and the operation stays unresolved'
        )


def retry_uncertain_operation(
    job_dir: Path,
    operation_id: str,
    *,
    allow_paid: bool,
    limits: SpendingLimits | None = None,
    transport: xai_client.Transport | None = None,
    key_reader: Callable[[str], str | None] | None = None,
    now: Callable[[], str] | None = None,
) -> SpeechExecutionResult:
    """Retry one named unresolved operation after explicit permission.

    The retry requires the current request signature to match the
    recorded one, appends a linked attempt and preserves the original
    receipt unchanged.
    """
    job_dir = Path(job_dir).expanduser().resolve()
    now_fn = now or utc_now_iso
    reader = key_reader or os.environ.get
    with JobLock.acquire(job_dir):
        return _retry_uncertain_operation_locked(
            job_dir,
            operation_id,
            allow_paid=allow_paid,
            limits=limits,
            transport=transport,
            key_reader=reader,
            now_fn=now_fn,
        )


def _retry_uncertain_operation_locked(
    job_dir: Path,
    operation_id: str,
    *,
    allow_paid: bool,
    limits: SpendingLimits | None,
    transport: xai_client.Transport | None,
    key_reader: Callable[[str], str | None],
    now_fn: Callable[[], str],
) -> SpeechExecutionResult:
    """Retry body that requires the held job lock."""
    manifest, journal, _planned, reused, _total = _run_guarded_planning(
        job_dir,
        allow_paid=allow_paid,
        limits=limits,
        require_permission=False,
    )
    writable_journal = _new_journal(journal, now_fn())
    operation = None
    for candidate in writable_journal.get('operations', []):
        if candidate['operation_id'] == operation_id:
            operation = candidate
            break
    if operation is None:
        raise ContractError(
            f'no operation {operation_id} exists in the journal of'
            f' {job_dir}; name the exact uncertain operation'
        )
    if operation['state'] not in UNRESOLVED_OPERATION_STATES:
        raise ContractError(
            f'operation {operation_id} is {operation["state"]}, not'
            ' unresolved; only unresolved operations accept an'
            ' explicit retry'
        )
    segment = None
    for candidate in manifest['segments']:
        if candidate['id'] == operation['segment_id']:
            segment = candidate
            break
    if segment is None:
        raise ContractError(
            f'segment {operation["segment_id"]} of operation'
            f' {operation_id} no longer exists in the manifest'
        )
    if segment['synthesis_signature'] != operation['synthesis_signature']:
        raise ContractError(
            f'operation {operation_id} was reserved for synthesis'
            f' signature {operation["synthesis_signature"]} but the'
            f' current signature is'
            f' {segment["synthesis_signature"]}; the changed inputs'
            ' require fresh planning instead of a retry'
        )
    text = _posted_text(job_dir, manifest, segment['id'])
    if request_signature(text) != operation['request_signature']:
        raise ContractError(
            f'operation {operation_id} recorded request signature'
            f' {operation["request_signature"]} but the current text'
            ' hashes differently; changed narration requires fresh'
            ' planning instead of a retry'
        )
    _validate_operation_provenance(job_dir, operation, text)
    validate_posted_text(text)
    _require_execution_permission(allow_paid, limits, 1, len(text))
    if limits is None:
        raise PaidExecutionDenied(
            'limits',
            'explicit numeric spending limits are required before'
            ' a retry; no request was sent and no key was read',
        )
    key = _read_speech_key(key_reader)
    client = SpeechClient(key, transport=transport)

    def redactor(detail: str, _key: str = key) -> str:
        """Keep the configured credential out of persisted text."""
        return _redact_secrets(detail, (_key,))

    verdict = _run_operation_attempt(
        job_dir,
        job_dir / OPERATIONS_NAME,
        writable_journal,
        operation,
        manifest,
        job_dir / MANIFEST_NAME,
        segment,
        limits,
        client,
        now_fn,
        linked=True,
        redactor=redactor,
    )
    failed: dict[str, str] = {}
    uncertain: dict[str, str] = {}
    completed: tuple[str, ...] = ()
    if verdict == 'completed':
        completed = (segment['id'],)
    elif verdict == 'failed':
        failed[segment['id']] = _latest_failure_kind(
            writable_journal, segment['id']
        )
    else:
        uncertain[segment['id']] = _latest_failure_kind(
            writable_journal, segment['id']
        )
    return SpeechExecutionResult(
        planned=(segment['id'],),
        reused=tuple(reused),
        completed=completed,
        failed=failed,
        uncertain=uncertain,
        blocked_unresolved=(),
        request_count=1,
    )


class PipelineFailure(RuntimeError):
    """A pipeline stage failed with its stable kind and exit code."""

    def __init__(
        self,
        kind: str,
        detail: str,
        exit_code: int,
        report: dict[str, Any] | None = None,
    ) -> None:
        """Record the kind, explanation, exit code and named report."""
        super().__init__(detail)
        self.kind = kind
        self.detail = detail
        self.exit_code = exit_code
        self.report = report


def describe_pipeline_failure(
    error: BaseException,
) -> tuple[int, str, str]:
    """Map one pipeline error to its approved exit code and kind."""
    if isinstance(error, PipelineFailure):
        return error.exit_code, error.kind, error.detail
    if isinstance(error, PaidExecutionDenied):
        return 4, 'paid_denied', error.detail
    if isinstance(error, DestinationExistsError):
        return 3, 'destination', str(error)
    if isinstance(error, (ContractError, SpeechRequestError)):
        return 3, 'contract', str(error)
    if isinstance(error, SubtitlesError):
        return 3, 'contract', str(error)
    if isinstance(error, LockUnavailableError):
        return 7, 'tooling', str(error)
    if isinstance(error, MediaToolError):
        return 7, 'tooling', str(error)
    if isinstance(error, audio.AudioError):
        if error.kind in ('window', 'fit'):
            return 6, 'timing', str(error)
        if error.kind == 'speech':
            return 3, 'contract', str(error)
        return 7, 'tooling', str(error)
    if isinstance(error, mux.MuxError):
        if error.kind == 'destination':
            return 3, 'destination', str(error)
        if error.kind in ('profile', 'input'):
            return 3, 'contract', str(error)
        return 7, 'tooling', str(error)
    if isinstance(error, verification.VerificationError):
        return 6, 'verification', str(error)
    if isinstance(error, KeyboardInterrupt):
        return (
            130,
            'cancelled',
            'interrupted; job state stays at its last checkpoint',
        )
    if isinstance(error, (StorageError, OSError)):
        return 7, 'tooling', str(error)
    return 7, 'tooling', str(error)


@dataclass(frozen=True)
class RunResult:
    """Outcome of one complete run or assemble pipeline pass."""

    command: str
    revision: int
    speech: SpeechExecutionResult | None
    built: tuple[str, ...]
    reused: tuple[str, ...]
    verification: dict[str, Any] | None
    publication: dict[str, Any]
    report_path: str


def _pcm_frames_of(path: Path) -> int:
    """Return the mono s16 frame count of one retained PCM file."""
    size = path.stat().st_size
    if size % TIMELINE_BYTES_PER_SAMPLE:
        raise PipelineFailure(
            'contract',
            f'PCM artifact {path} is not a whole number of frames',
            3,
        )
    return size // TIMELINE_BYTES_PER_SAMPLE


def _placed_speech(record: dict[str, Any], job_dir: Path) -> NormalizedSpeech:
    """Build a verified placement speech view from one PCM record."""
    path = job_dir / record['path']
    return NormalizedSpeech(
        raw_path=path,
        raw_sha256=record['sha256'],
        path=path,
        sha256=record['sha256'],
        frames=_pcm_frames_of(path),
    )


def _current_record(
    manifest: dict[str, Any],
    kind: str,
    unit: str | None,
    signature: str,
) -> dict[str, Any] | None:
    """Return the newest current signature record of one kind."""
    for record in reversed(manifest['artifacts']):
        if (
            record['kind'] == kind
            and record['segment'] == unit
            and record['signature'] == signature
        ):
            found: dict[str, Any] = record
            return found
    return None


def _speech_tools(settings: Settings) -> dict[str, Any]:
    """Resolve the media tools and timeout for one pipeline pass."""
    return {
        'ffmpeg': str(media.discover_tool('ffmpeg', settings.ffmpeg_path)),
        'ffprobe': str(media.discover_tool('ffprobe', settings.ffprobe_path)),
        'timeout_seconds': settings.subprocess_timeout_seconds,
    }


def _rebuild_manifest_view(manifest: dict[str, Any]) -> None:
    """Refresh the derived stage and blocker views of the manifest."""
    view = _cache_view(
        {
            'segments': manifest['segments'],
            'subtitle_signature': manifest['subtitle_signature'],
            'container_signature': manifest['container_signature'],
        },
        manifest['artifacts'],
        frozenset(),
    )
    manifest['stages'] = view['stages']
    manifest['blocked'] = view['blocked']


def _register_pipeline_artifact(
    job_dir: Path,
    manifest: dict[str, Any],
    manifest_path: Path,
    *,
    kind: str,
    segment: str | None,
    signature: str,
    source: Path,
    suffix: str,
    now: str,
) -> dict[str, Any]:
    """Register one produced artifact immutably and checkpoint it."""
    ordinal = len(manifest['artifacts'])
    token = sha256_bytes(f'{kind}|{segment or ""}'.encode())[:16]
    relative = f'artifacts/{ordinal:03d}-{kind}-{token}.{suffix}'
    copy_new_file(source, job_dir / relative)
    record = {
        'kind': kind,
        'segment': segment,
        'signature': signature,
        'path': relative,
        'sha256': sha256_file(job_dir / relative),
        'created_at': now,
    }
    manifest['artifacts'].append(record)
    _rebuild_manifest_view(manifest)
    manifest['updated_at'] = now
    atomic_write_json(manifest_path, manifest)
    return record


def _unit_label(kind: str, segment: str | None) -> str:
    """Return one report label for a built or reused unit."""
    return f'{kind}:{segment or "job"}'


def _build_normalized_unit(
    job_dir: Path,
    manifest: dict[str, Any],
    manifest_path: Path,
    segment: dict[str, Any],
    tools: dict[str, Any],
    now: str,
) -> dict[str, Any]:
    """Normalize one retained raw response into canonical PCM."""
    raw_record = _current_record(
        manifest,
        'raw_response',
        segment['id'],
        segment['synthesis_signature'],
    )
    if raw_record is None:
        raise PipelineFailure(
            'contract',
            f'retained raw response for {segment["id"]} is missing;'
            ' processed audio cannot replace raw provenance',
            3,
        )
    temp = job_dir / 'artifacts' / f'.normalized-{uuid.uuid4().hex[:12]}.pcm'
    try:
        audio.normalize_wav(
            job_dir / raw_record['path'],
            temp,
            ffmpeg_path=tools['ffmpeg'],
            timeout_seconds=tools['timeout_seconds'],
        )
        record = _register_pipeline_artifact(
            job_dir,
            manifest,
            manifest_path,
            kind='normalized_audio',
            segment=segment['id'],
            signature=segment['synthesis_signature'],
            source=temp,
            suffix='pcm',
            now=now,
        )
    finally:
        temp.unlink(missing_ok=True)
    return record


def _refusal_failure(segment: dict[str, Any], fit: Any) -> PipelineFailure:
    """Build the timing refusal failure for one segment fit."""
    detail = (
        f'segment {segment["id"]} refused fitting in its'
        f' {fit.window_frames} frame window:'
        f' {fit.refusal}'
    )
    return PipelineFailure('timing', detail, 6)


def _build_fitted_unit(
    job_dir: Path,
    manifest: dict[str, Any],
    manifest_path: Path,
    segment: dict[str, Any],
    normalized: dict[str, Any],
    tools: dict[str, Any],
    now: str,
) -> dict[str, Any]:
    """Fit one normalized speech into its window without trimming."""
    normalized_path = job_dir / normalized['path']
    speech = NormalizedSpeech(
        raw_path=normalized_path,
        raw_sha256=normalized['sha256'],
        path=normalized_path,
        sha256=normalized['sha256'],
        frames=_pcm_frames_of(normalized_path),
    )
    placement = SegmentPlacement(
        segment_id=segment['id'],
        start_ms=segment['window']['start_ms'],
        end_ms=segment['window']['end_ms'],
        speech=speech,
    )
    with tempfile.TemporaryDirectory(
        dir=job_dir, prefix='.fitting-'
    ) as work_dir:
        fit = audio.fit_segment(
            placement,
            max_speedup=manifest['policy']['fitting_max_speedup'],
            work_dir=Path(work_dir),
            ffmpeg_path=tools['ffmpeg'],
            timeout_seconds=tools['timeout_seconds'],
        )
        if fit.disposition == 'refused':
            raise _refusal_failure(segment, fit)
        assert fit.pcm_path is not None
        record = _register_pipeline_artifact(
            job_dir,
            manifest,
            manifest_path,
            kind='fitted_audio',
            segment=segment['id'],
            signature=segment['fitting_signature'],
            source=fit.pcm_path,
            suffix='pcm',
            now=now,
        )
        if fit.disposition == 'fitted':
            fit.pcm_path.unlink(missing_ok=True)
    return record


def _build_timeline_unit(
    job_dir: Path,
    manifest: dict[str, Any],
    manifest_path: Path,
    model: dict[str, Any],
    tools: dict[str, Any],
    now: str,
) -> dict[str, Any]:
    """Stream the complete timeline from retained placed speech."""
    placements = []
    for segment in manifest['segments']:
        record = _current_record(
            manifest,
            'fitted_audio',
            segment['id'],
            segment['fitting_signature'],
        )
        if record is None:
            raise PipelineFailure(
                'contract',
                f'placed audio for {segment["id"]} is missing before'
                ' timeline assembly',
                3,
            )
        placements.append(
            SegmentPlacement(
                segment_id=segment['id'],
                start_ms=segment['window']['start_ms'],
                end_ms=segment['window']['end_ms'],
                speech=_placed_speech(record, job_dir),
            )
        )
    total_frames = _timeline_total_frames(manifest)
    temp = job_dir / 'artifacts' / f'.timeline-{uuid.uuid4().hex[:12]}.pcm'
    try:
        audio.stream_timeline(
            placements,
            total_frames=total_frames,
            destination=temp,
            max_speedup=manifest['policy']['fitting_max_speedup'],
            work_dir=job_dir / 'artifacts',
            ffmpeg_path=tools['ffmpeg'],
            timeout_seconds=tools['timeout_seconds'],
        )
        record = _register_pipeline_artifact(
            job_dir,
            manifest,
            manifest_path,
            kind='timeline_audio',
            segment=None,
            signature=model['signatures']['timeline'],
            source=temp,
            suffix='pcm',
            now=now,
        )
    finally:
        temp.unlink(missing_ok=True)
    return record


def _build_narration_unit(
    job_dir: Path,
    manifest: dict[str, Any],
    manifest_path: Path,
    timeline_record: dict[str, Any],
    model: dict[str, Any],
    tools: dict[str, Any],
    now: str,
) -> dict[str, Any]:
    """Encode the timeline as the approved narration AAC track."""
    temp = job_dir / 'artifacts' / f'.narration-{uuid.uuid4().hex[:12]}.m4a'
    try:
        audio.encode_narration_aac(
            job_dir / timeline_record['path'],
            temp,
            ffmpeg_path=tools['ffmpeg'],
            ffprobe_path=tools['ffprobe'],
            timeout_seconds=tools['timeout_seconds'],
        )
        record = _register_pipeline_artifact(
            job_dir,
            manifest,
            manifest_path,
            kind='narration_audio',
            segment=None,
            signature=model['signatures']['narration'],
            source=temp,
            suffix='m4a',
            now=now,
        )
    finally:
        temp.unlink(missing_ok=True)
    return record


def _build_original_unit(
    job_dir: Path,
    manifest: dict[str, Any],
    manifest_path: Path,
    model: dict[str, Any],
    tools: dict[str, Any],
    now: str,
) -> dict[str, Any]:
    """Prepare the converted original audio as a separate AAC track."""
    temp = job_dir / 'artifacts' / f'.original-{uuid.uuid4().hex[:12]}.m4a'
    try:
        audio.prepare_original_audio(
            Path(manifest['binding']['video_path']),
            manifest['media']['audio_stream']['index'],
            temp,
            ffmpeg_path=tools['ffmpeg'],
            ffprobe_path=tools['ffprobe'],
            timeout_seconds=tools['timeout_seconds'],
        )
        record = _register_pipeline_artifact(
            job_dir,
            manifest,
            manifest_path,
            kind='original_audio',
            segment=None,
            signature=model['signatures']['original'] or '',
            source=temp,
            suffix='m4a',
            now=now,
        )
    finally:
        temp.unlink(missing_ok=True)
    return record


def _snapshot_cues(
    job_dir: Path, manifest: dict[str, Any]
) -> tuple[SourceCue, ...]:
    """Rebuild source cues from the immutable cue snapshot."""
    record = manifest['snapshots']['source_cues']
    payload = load_strict_json_file(job_dir / record['path'])
    return tuple(
        SourceCue(
            position=entry['position'],
            start_ms=entry['start_ms'],
            end_ms=entry['end_ms'],
            text=entry['text'],
        )
        for entry in payload['cues']
    )


def _build_subtitles_unit(
    job_dir: Path,
    manifest: dict[str, Any],
    manifest_path: Path,
    now: str,
) -> dict[str, Any]:
    """Render the complete Russian subtitle file from the snapshot."""
    translation_record = manifest['snapshots']['translation']
    document = load_translation_document(job_dir / translation_record['path'])
    cues = _snapshot_cues(job_dir, manifest)
    text = russian_srt(cues, document.segments)
    temp = job_dir / 'artifacts' / f'.subtitles-{uuid.uuid4().hex[:12]}.srt'
    try:
        create_new_file(temp, text.encode())
        record = _register_pipeline_artifact(
            job_dir,
            manifest,
            manifest_path,
            kind='subtitles',
            segment=None,
            signature=manifest['subtitle_signature'],
            source=temp,
            suffix='srt',
            now=now,
        )
    finally:
        temp.unlink(missing_ok=True)
    return record


def _verify_source_binding(manifest: dict[str, Any]) -> None:
    """Refuse pipeline work when the bound sources changed on disk.

    The check runs before any paid request and again before the
    container consumes the source packets, so a source changed,
    truncated or removed after prepare never reaches synthesis or a
    candidate built from foreign bytes.
    """
    binding = manifest['binding']
    video_path = Path(binding['video_path'])
    srt_path = Path(binding['srt_path'])
    if not video_path.is_file():
        raise ContractError(
            f'bound source video is missing: {video_path}; supply the'
            ' original input or prepare the job again'
        )
    actual_video = sha256_file(video_path)
    if actual_video.lower() != str(binding['video_sha256']).lower():
        raise ContractError(
            'bound source video changed since prepare; container work'
            ' copies its exact packets and requires a fresh prepare'
        )
    if not srt_path.is_file():
        raise ContractError(
            f'bound source SRT is missing: {srt_path}; supply the'
            ' original input or prepare the job again'
        )
    actual_srt = sha256_file(srt_path)
    if actual_srt.lower() != str(binding['srt_sha256']).lower():
        raise ContractError(
            'bound source SRT changed since prepare; the English track'
            ' requires a fresh prepare with the matching translation'
        )


def _verify_source_decodability(
    manifest: dict[str, Any], settings: Settings
) -> None:
    """Refuse execution when the bound source fails a full decode.

    Matching hashes alone cannot prove a decodable source: a damaged
    video packet keeps every byte hash while stopping real playback.
    The approved criterion requires a read or decode failure to stop
    processing before paid synthesis, so the selected streams are
    fully decoded here with strict error propagation.
    """
    video_path = Path(manifest['binding']['video_path'])
    selected = manifest['media']['audio_stream']['index']
    try:
        ffmpeg = media.discover_tool('ffmpeg', settings.ffmpeg_path)
    except MediaToolError as error:
        raise PipelineFailure('tooling', str(error), 7) from error
    argv = [
        str(ffmpeg),
        '-hide_banner',
        '-loglevel',
        'error',
        '-nostdin',
        '-xerror',
        '-i',
        str(video_path),
        '-map',
        '0:v:0',
        '-map',
        f'0:{selected}',
        '-f',
        'null',
        '-',
    ]
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=settings.subprocess_timeout_seconds,
            env=media.scrubbed_environment(),
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise PipelineFailure(
            'tooling',
            f'the source decode check timed out after'
            f' {settings.subprocess_timeout_seconds}s; raise'
            ' subprocess_timeout_seconds or inspect the local tools'
            f' before running again: {error}',
            7,
        ) from error
    except OSError as error:
        raise PipelineFailure(
            'tooling',
            f'the decode tool failed to run: {error}',
            7,
        ) from error
    if result.returncode < 0:
        raise PipelineFailure(
            'tooling',
            f'the decode tool was terminated by signal'
            f' {-result.returncode}; inspect local tools and available'
            ' resources before running again',
            7,
        )
    if result.returncode != 0:
        tail = (
            result.stderr.strip().splitlines()[-3:]
            if result.stderr.strip()
            else []
        )
        raise ContractError(
            'the bound source video does not decode completely, which'
            ' stops processing before any paid synthesis; supply a'
            f' decodable source and prepare again: {" | ".join(tail)}'
        )


def _reconcile_registered_operations(
    job_dir: Path, manifest: dict[str, Any], now_fn: Callable[[], str]
) -> None:
    """Resolve unresolved operations that already carry durable evidence.

    Ownership is proven before any reconciliation: the operation must
    carry the current synthesis and request signatures of its segment
    and the exact posted text digest, the segment must hold the
    current verified raw response artifact with the same synthesis
    signature, and exactly one attempt with an unknown verdict must
    exist. That interrupted attempt is then recorded truthfully as
    completed with a reference to the evidence bytes. A later attempt
    with a known outcome is never touched, the operation keeps its
    uncertainty when its latest attempt stays uncertain, nothing is
    retried and no provider receipt is fabricated; anything without
    full proof keeps its unresolved state.
    """
    journal = load_operations_journal(job_dir)
    if journal is None:
        return
    unresolved = [
        operation
        for operation in journal['operations']
        if operation['state'] in UNRESOLVED_OPERATION_STATES
    ]
    if not unresolved:
        return
    writable = _new_journal(journal, now_fn())
    now = now_fn()
    for operation in unresolved:
        segment_id = operation['segment_id']
        segment = next(
            (
                candidate
                for candidate in manifest['segments']
                if candidate['id'] == segment_id
            ),
            None,
        )
        if segment is None:
            continue
        if operation['synthesis_signature'] != segment['synthesis_signature']:
            continue
        record = _current_record(
            manifest,
            'raw_response',
            segment_id,
            segment['synthesis_signature'],
        )
        if (
            record is None
            or record['signature'] != operation['synthesis_signature']
        ):
            continue
        try:
            text = _posted_text(job_dir, manifest, segment_id)
        except ContractError:
            continue
        if operation['posted_text_sha256'] != _segment_text_hash(text):
            continue
        if operation['request_signature'] != request_signature(text):
            continue
        unknown = [
            attempt
            for attempt in operation['attempts']
            if attempt['verdict'] is None
        ]
        if len(unknown) != 1:
            continue
        attempt = unknown[0]
        attempt['verdict'] = 'completed'
        attempt['finished_at'] = now
        attempt['artifact'] = {
            'path': record['path'],
            'sha256': record['sha256'],
        }
        attempt['evidence'] = {
            'path': record['path'],
            'sha256': record['sha256'],
            'bytes': (job_dir / record['path']).stat().st_size,
        }
        if operation['attempts'][-1] is attempt:
            operation['state'] = 'completed'
    _write_journal(job_dir / OPERATIONS_NAME, writable, now)


def _require_resolved_completed_cache(
    manifest: dict[str, Any], job_dir: Path
) -> None:
    """Keep an unresolved required operation from publishing silently.

    After evidence based reconciliation, an operation that is still
    unresolved while its segment already counts as cached stops the
    run as a non success: the completed cache must not hide an
    explicit pending paid decision.
    """
    journal = load_operations_journal(job_dir)
    unresolved = [
        operation
        for operation in (journal or {}).get('operations', [])
        if operation['state'] in UNRESOLVED_OPERATION_STATES
    ]
    if not unresolved:
        return
    planned, _reused = _planned_segments(
        manifest, manifest['artifacts'], frozenset()
    )
    planned_ids = {segment['id'] for segment in planned}
    stuck = [
        operation
        for operation in unresolved
        if operation['segment_id'] not in planned_ids
    ]
    if stuck:
        raise PipelineFailure(
            'provider',
            'unresolved paid operations block the completed cache'
            ' without registered raw evidence:'
            f' {[operation["operation_id"] for operation in stuck]};'
            ' an explicit retry decision is required',
            5,
        )


def _prepared_audio_facts(
    artifact_path: Path, settings: Settings
) -> tuple[int, int]:
    """Measure the channels and sample rate of one prepared AAC file.

    The converted original track is the verification ground truth, so
    its declared facts come from probing the actual prepared artifact
    instead of the source stream it was converted from.
    """
    ffprobe = media.discover_tool('ffprobe', settings.ffprobe_path)
    result = media.checked_run_strict(
        [
            str(ffprobe),
            '-v',
            'error',
            '-select_streams',
            'a:0',
            '-show_entries',
            'stream=channels,sample_rate',
            '-of',
            'json',
            str(artifact_path),
        ],
        settings.subprocess_timeout_seconds,
    )
    try:
        payload = json.loads(result.stdout)
        stream = payload['streams'][0]
        channels = int(stream['channels'])
        rate = int(stream['sample_rate'])
    except (
        json.JSONDecodeError,
        KeyError,
        IndexError,
        TypeError,
        ValueError,
    ) as error:
        raise ContractError(
            f'the prepared original audio artifact {artifact_path}'
            ' carries unusable stream facts and cannot anchor'
            f' verification expectations: {error}'
        ) from error
    if channels < 1 or rate < 1:
        raise ContractError(
            f'the prepared original audio artifact {artifact_path}'
            ' declares unusable channel or sample rate values'
        )
    return channels, rate


def _candidate_expectation(
    job_dir: Path,
    manifest: dict[str, Any],
    narration_record: dict[str, Any],
    subtitles_record: dict[str, Any],
    original_record: dict[str, Any] | None,
    settings: Settings,
) -> CandidateExpectation:
    """Build the declared source facts for candidate verification."""
    binding = manifest['binding']
    media_view = manifest['media']
    audio_stream = media_view['audio_stream']
    copied = media_view['audio_disposition'] == 'copy'
    policy = manifest['policy']
    duration = media_view['duration_seconds']
    if duration is None:
        raise ContractError(
            'the recorded media carries no duration; verification'
            ' expectations cannot declare complete intervals'
        )
    original_path = (
        Path(binding['video_path'])
        if copied
        else job_dir / (original_record or {}).get('path', '')
    )
    if copied:
        original_channels = audio_stream['channels']
        original_sample_rate = audio_stream['sample_rate_hz']
    else:
        assert original_record is not None
        original_channels, original_sample_rate = _prepared_audio_facts(
            job_dir / original_record['path'], settings
        )
    narration_record_path = job_dir / narration_record['path']
    return CandidateExpectation(
        video_source=Path(binding['video_path']),
        original_packet_source=original_path,
        original_packet_stream_index=audio_stream['index'],
        original_is_copied=copied,
        narration_packet_source=narration_record_path,
        original_channels=original_channels,
        original_sample_rate=original_sample_rate,
        original_end_ms=_duration_to_ms(duration),
        narration_end_ms=_timeline_duration_ms(
            _timeline_total_frames(manifest)
        ),
        english_srt_path=Path(binding['srt_path']),
        russian_srt_path=job_dir / subtitles_record['path'],
        profile=TrackProfile(
            narration_default=policy['startup_narration_default'],
            original_default=policy['startup_original_default'],
            subtitles_default=policy['startup_subtitles_default'],
            subtitles_forced=policy['startup_subtitles_forced'],
        ),
    )


def _load_publication_receipt(job_dir: Path) -> dict[str, Any] | None:
    """Load the durable publication receipt or return None."""
    path = job_dir / PUBLICATION_RECEIPT
    if not path.is_file():
        return None
    data = load_strict_json_file(path)
    if not isinstance(data, dict) or data.get('schema') != 1:
        raise ContractError(
            f'publication receipt {path} is malformed or of an unknown'
            ' schema; inspect it manually before any recovery'
        )
    receipt: dict[str, Any] = data
    return receipt


def _identity_of(path: Path) -> dict[str, Any]:
    """Return the actual filesystem identity of one existing file."""
    status = path.stat()
    return {
        'device': status.st_dev,
        'inode': status.st_ino,
        'size': status.st_size,
    }


def _identity_matches(receipt: dict[str, Any] | None, path: Path) -> bool:
    """Report whether one file carries the receipt recorded identity."""
    if receipt is None:
        return False
    identity = receipt.get('identity')
    if not isinstance(identity, dict):
        return False
    try:
        status = path.stat()
    except OSError:
        return False
    return (
        status.st_dev == identity.get('device')
        and status.st_ino == identity.get('inode')
        and status.st_size == identity.get('size')
    )


def _owned_publication_matches(
    receipt: dict[str, Any] | None,
    output_path: Path,
    candidate_record: dict[str, Any],
) -> bool:
    """Recognize the published result of this job.

    Recognition needs the full durable provenance chain: the receipt
    must name this exact destination, the actual file must still carry
    the recorded device, inode and size, and its intact bytes must
    hash to the recorded digest and to the current registered
    candidate. Byte equality alone, a registered candidate alone, or
    an unchanged stat identity alone never prove ownership.
    """
    if receipt is None:
        return False
    if receipt.get('output_path') != str(output_path):
        return False
    recorded = receipt.get('sha256')
    if not isinstance(recorded, str) or not recorded:
        return False
    if recorded != candidate_record['sha256']:
        return False
    if not _identity_matches(receipt, output_path):
        return False
    try:
        return sha256_file(output_path) == recorded
    except OSError:
        return False


def _write_publication_receipt(job_dir: Path, payload: dict[str, Any]) -> None:
    """Durably persist the publication receipt."""
    (job_dir / EVIDENCE_DIR).mkdir(parents=True, exist_ok=True)
    atomic_write_json(job_dir / PUBLICATION_RECEIPT, payload)


def _publish_candidate(
    job_dir: Path,
    candidate_record: dict[str, Any],
    output_path: Path,
    now: str,
) -> dict[str, Any]:
    """Publish the registered verified candidate without clobbering.

    An existing output is accepted only when the durable receipt
    proves it is the actual file this job linked, by device, inode
    and size. Byte equality alone, and a registered candidate alone,
    never prove ownership of a preexisting destination file.
    """
    candidate_path = job_dir / candidate_record['path']
    if output_path.exists():
        receipt = _load_publication_receipt(job_dir)
        if _owned_publication_matches(receipt, output_path, candidate_record):
            if receipt is not None and receipt.get('state') != 'completed':
                completed = dict(receipt)
                completed['state'] = 'completed'
                completed['completed_at'] = now
                _write_publication_receipt(job_dir, completed)
            return {
                'state': 'published',
                'path': str(output_path),
                'sha256': candidate_record['sha256'],
                'recognized': True,
            }
        raise PipelineFailure(
            'destination',
            f'refusing to overwrite an existing output without owned'
            f' publication provenance: {output_path}; a file with'
            ' matching bytes, or one keeping the recorded size and'
            ' file identity after an in place edit, is still a'
            ' foreign destination',
            3,
        )
    fd, temp_name = tempfile.mkstemp(
        dir=output_path.parent,
        prefix=f'.{output_path.name}.publish-',
        suffix='.mp4',
    )
    temp = Path(temp_name)
    try:
        try:
            reader = open(candidate_path, 'rb')  # noqa: SIM115
        except BaseException:
            os.close(fd)
            raise
        with (
            reader,
            os.fdopen(fd, 'wb') as writer,
        ):
            shutil.copyfileobj(reader, writer, length=COPY_CHUNK_BYTES)
            writer.flush()
            os.fsync(writer.fileno())
        _write_publication_receipt(
            job_dir,
            {
                'schema': 1,
                'state': 'linking',
                'output_path': str(output_path),
                'identity': _identity_of(temp),
                'sha256': candidate_record['sha256'],
                'candidate': {
                    'path': candidate_record['path'],
                    'sha256': candidate_record['sha256'],
                },
                'created_at': now,
                'completed_at': None,
            },
        )
        try:
            os.link(temp, output_path)
        except FileExistsError as error:
            raise PipelineFailure(
                'destination',
                f'the output destination appeared during publication:'
                f' {output_path}; it is preserved untouched',
                3,
            ) from error
    finally:
        temp.unlink(missing_ok=True)
    fsync_directory(output_path.parent)
    _write_publication_receipt(
        job_dir,
        {
            'schema': 1,
            'state': 'completed',
            'output_path': str(output_path),
            'identity': _identity_of(output_path),
            'sha256': candidate_record['sha256'],
            'candidate': {
                'path': candidate_record['path'],
                'sha256': candidate_record['sha256'],
            },
            'created_at': now,
            'completed_at': now,
        },
    )
    return {
        'state': 'published',
        'path': str(output_path),
        'sha256': candidate_record['sha256'],
        'recognized': False,
    }


def _publish_extra_copy(
    job_dir: Path,
    candidate_record: dict[str, Any],
    extra_output: Path,
) -> dict[str, Any]:
    """Publish one additional fresh copy of the verified candidate."""
    try:
        copy_new_file(job_dir / candidate_record['path'], extra_output)
    except DestinationExistsError:
        raise PipelineFailure(
            'destination',
            f'the requested assemble output already exists:'
            f' {extra_output}; it is preserved untouched',
            3,
        ) from None
    return {
        'state': 'published',
        'path': str(extra_output),
        'sha256': candidate_record['sha256'],
        'recognized': False,
    }


def _container_stage(
    job_dir: Path,
    manifest: dict[str, Any],
    manifest_path: Path,
    model: dict[str, Any],
    settings: Settings,
    tools: dict[str, Any],
    now: str,
    extra_output: Path | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Assemble, verify, register and publish the final candidate."""
    _verify_source_binding(manifest)
    container_signature = manifest['container_signature']
    candidate_record = _current_record(
        manifest, 'container_track', None, container_signature
    )
    output_path = Path(manifest['output_path'])
    verification_view: dict[str, Any] | None = None
    if candidate_record is not None:
        integrity = verify_integrity(
            job_dir,
            (
                IntegrityRecord(
                    label='container_track artifact for the job',
                    relative_path=candidate_record['path'],
                    sha256=candidate_record['sha256'],
                ),
            ),
        )
        if integrity:
            candidate_record = None
    if candidate_record is None:
        if output_path.exists():
            raise PipelineFailure(
                'destination',
                f'refusing to build a candidate while an unowned or'
                f' non-current file occupies the output path:'
                f' {output_path}; it is preserved untouched, and a'
                ' receipt from another revision never recognizes it'
                ' for the current one',
                3,
            )
        candidate_record, verification_view = _assemble_candidate(
            job_dir,
            manifest,
            manifest_path,
            model,
            settings,
            tools,
            now,
            output_path,
        )
    publication = _publish_candidate(
        job_dir, candidate_record, output_path, now
    )
    if extra_output is not None:
        _publish_extra_copy(job_dir, candidate_record, extra_output)
    return publication, verification_view


def _assemble_candidate(
    job_dir: Path,
    manifest: dict[str, Any],
    manifest_path: Path,
    model: dict[str, Any],
    settings: Settings,
    tools: dict[str, Any],
    now: str,
    output_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build one fresh staging candidate, verify and register it."""
    narration_record = _current_record(
        manifest,
        'narration_audio',
        None,
        model['signatures']['narration'],
    )
    subtitles_record = _current_record(
        manifest, 'subtitles', None, manifest['subtitle_signature']
    )
    if narration_record is None or subtitles_record is None:
        raise PipelineFailure(
            'contract',
            'the prepared narration and subtitle tracks are required'
            ' before container assembly',
            3,
        )
    original_record = None
    if manifest['media']['audio_disposition'] == 'convert':
        original_record = _current_record(
            manifest,
            'original_audio',
            None,
            model['signatures']['original'] or '',
        )
        if original_record is None:
            raise PipelineFailure(
                'contract',
                'the converted original audio track is required before'
                ' container assembly',
                3,
            )
    expectation = _candidate_expectation(
        job_dir,
        manifest,
        narration_record,
        subtitles_record,
        original_record,
        settings,
    )
    if manifest['media']['audio_disposition'] == 'copy':
        original_audio: CopiedAudioStream | ConvertedAudioTrack = (
            CopiedAudioStream(
                stream_index=manifest['media']['audio_stream']['index']
            )
        )
    else:
        assert original_record is not None
        original_audio = ConvertedAudioTrack(
            path=job_dir / original_record['path']
        )
    staging = output_path.parent / (
        f'.{output_path.name}.staging-{uuid.uuid4().hex}.mp4'
    )
    request = MuxRequest(
        video_path=Path(manifest['binding']['video_path']),
        original_audio=original_audio,
        narration_path=job_dir / narration_record['path'],
        english_srt_path=Path(manifest['binding']['srt_path']),
        russian_srt_path=job_dir / subtitles_record['path'],
        destination=staging,
        profile=TrackProfile(
            narration_default=manifest['policy']['startup_narration_default'],
            original_default=manifest['policy']['startup_original_default'],
            subtitles_default=manifest['policy']['startup_subtitles_default'],
            subtitles_forced=manifest['policy']['startup_subtitles_forced'],
        ),
    )
    try:
        mux.build_candidate(request, settings)
        report = verification.verify_candidate(staging, expectation, settings)
        if not report.ok:
            raise PipelineFailure(
                'verification',
                'candidate verification failed:'
                f' {", ".join(report.failed_checks)}',
                6,
                report=report.to_dict(),
            )
        record = _register_pipeline_artifact(
            job_dir,
            manifest,
            manifest_path,
            kind='container_track',
            segment=None,
            signature=manifest['container_signature'],
            source=staging,
            suffix='mp4',
            now=now,
        )
    finally:
        staging.unlink(missing_ok=True)
    return record, report.to_dict()


def _raw_complete(manifest: dict[str, Any]) -> bool:
    """Report whether every retained raw response is current."""
    raw = _classify_records(
        'raw_response',
        {s['id']: s['synthesis_signature'] for s in manifest['segments']},
        manifest['artifacts'],
        frozenset(),
    )
    return all(raw.get(s['id']) == 'ok' for s in manifest['segments'])


def _speech_view(speech: SpeechExecutionResult | None) -> dict[str, Any]:
    """Return the JSON view of one speech execution outcome."""
    if speech is None:
        return {'executed': False, 'request_count': 0}
    return {
        'executed': True,
        'planned': list(speech.planned),
        'reused': list(speech.reused),
        'completed': list(speech.completed),
        'failed': dict(speech.failed),
        'uncertain': dict(speech.uncertain),
        'blocked_unresolved': [
            dict(item) for item in speech.blocked_unresolved
        ],
        'request_count': speech.request_count,
    }


def _failure_view(error: BaseException) -> dict[str, Any]:
    """Return the durable failure view of one pipeline error."""
    exit_code, kind, detail = describe_pipeline_failure(error)
    return {'kind': kind, 'detail': detail, 'exit_code': exit_code}


def _report_stamp(now: str) -> str:
    """Build one filename safe stamp from an ISO timestamp."""
    return re.sub(r'[^0-9A-Za-z]', '', now)[:14]


def _write_run_report(
    job_dir: Path,
    manifest: dict[str, Any] | None,
    *,
    command: str,
    speech: SpeechExecutionResult | None,
    built: list[str],
    reused: list[str],
    verification_view: dict[str, Any] | None,
    publication: dict[str, Any] | None,
    failure: dict[str, Any] | None,
    failure_report: dict[str, Any] | None = None,
    now: str,
) -> str:
    """Persist one durable run report and return its relative path."""
    text_outputs: dict[str, Any] = {}
    if manifest is not None:
        text_outputs['full_translation'] = {
            'path': manifest['snapshots']['translation']['path'],
            'sha256': manifest['snapshots']['translation']['sha256'],
        }
        text_outputs['narration_text'] = {
            'path': manifest['snapshots']['narration']['path'],
            'sha256': manifest['snapshots']['narration']['sha256'],
        }
        subtitles_record = _current_record(
            manifest, 'subtitles', None, manifest['subtitle_signature']
        )
        if subtitles_record is not None:
            text_outputs['russian_subtitles'] = {
                'path': subtitles_record['path'],
                'sha256': subtitles_record['sha256'],
            }
    payload = {
        'command': command,
        'created_at': now,
        'job_revision': None if manifest is None else manifest['revision'],
        'speech': _speech_view(speech),
        'built': built,
        'reused': reused,
        'verification': verification_view,
        'failed_verification': failure_report,
        'publication': publication,
        'text_outputs': text_outputs,
        'automated_checks': [
            'job manifest and retained artifact integrity verified',
            'translation binding and source cue coverage validated at'
            ' prepare time',
            'stage reuse decided from current signatures and verified bytes',
            'publication governed by the no clobber link and durable receipt',
        ],
        'pending_human_checks': [
            'human listening acceptance of the synthesized narration is'
            ' pending',
            'startup audio and subtitle behavior in QuickTime Player'
            ' and IINA is pending manual verification',
            'translation meaning and speech intelligibility remain'
            ' human review responsibilities',
        ],
        'failure': failure,
    }
    evidence = job_dir / EVIDENCE_DIR
    evidence.mkdir(parents=True, exist_ok=True)
    name = f'run-{_report_stamp(now)}-{uuid.uuid4().hex[:8]}.json'
    relative = f'{EVIDENCE_DIR}/{name}'
    atomic_write_json(job_dir / relative, payload)
    return relative


def _latest_report_name(job_dir: Path) -> str | None:
    """Return the newest durable run report by its recorded time.

    The selection reads the durable created_at field of every report
    instead of trusting filename suffixes, so same second invocations
    and their random name tokens cannot reorder the actual history;
    failed invocations write reports too and take part in the order.
    """
    evidence = job_dir / EVIDENCE_DIR
    if not evidence.is_dir():
        return None
    best: tuple[str, str] | None = None
    for entry in sorted(evidence.glob('run-*.json')):
        with contextlib.suppress(ContractError, OSError, ValueError):
            payload = load_strict_json_file(entry)
            if not isinstance(payload, dict):
                continue
            created_at = payload.get('created_at')
            if not isinstance(created_at, str) or not created_at:
                continue
            key = (created_at, entry.name)
            if best is None or key > best:
                best = key
    return best[1] if best is not None else None


def run_job_pipeline(
    job_dir: Path,
    *,
    allow_paid: bool = False,
    limits: SpendingLimits | None = None,
    transport: xai_client.Transport | None = None,
    key_reader: Callable[[str], str | None] | None = None,
    settings: Settings | None = None,
    now: Callable[[], str] | None = None,
    retry_uncertain: str | None = None,
) -> RunResult:
    """Run the complete guarded pipeline over one prepared job.

    One writer lock covers speech synthesis through publication. An
    unchanged complete cache makes zero provider calls and reads no
    key; any failed, uncertain or blocked speech unit stops the
    pipeline before assembly with a non success outcome.
    """
    job_dir = Path(job_dir).expanduser().resolve()
    now_fn = now or utc_now_iso
    reader = key_reader or os.environ.get
    resolved = settings or load_settings()
    with JobLock.acquire(job_dir):
        return _run_pipeline_locked(
            job_dir,
            assemble_only=False,
            allow_paid=allow_paid,
            limits=limits,
            transport=transport,
            key_reader=reader,
            settings=resolved,
            now_fn=now_fn,
            retry_operation_id=retry_uncertain,
            extra_output=None,
        )


def assemble_job_pipeline(
    job_dir: Path,
    *,
    settings: Settings | None = None,
    extra_output: Path | None = None,
    now: Callable[[], str] | None = None,
) -> RunResult:
    """Assemble and publish from intact retained artifacts offline.

    No provider transport, credential access or synthesis happens
    here by construction: the job must already carry complete
    prepared speech, audio and subtitle artifacts.
    """
    job_dir = Path(job_dir).expanduser().resolve()
    now_fn = now or utc_now_iso
    resolved = settings or load_settings()
    with JobLock.acquire(job_dir):
        return _run_pipeline_locked(
            job_dir,
            assemble_only=True,
            allow_paid=False,
            limits=None,
            transport=None,
            key_reader=os.environ.get,
            settings=resolved,
            now_fn=now_fn,
            retry_operation_id=None,
            extra_output=extra_output,
        )


def _run_pipeline_locked(
    job_dir: Path,
    *,
    assemble_only: bool,
    allow_paid: bool,
    limits: SpendingLimits | None,
    transport: xai_client.Transport | None,
    key_reader: Callable[[str], str | None],
    settings: Settings,
    now_fn: Callable[[], str],
    retry_operation_id: str | None,
    extra_output: Path | None,
) -> RunResult:
    """Pipeline body that requires the held job lock."""
    command = 'assemble' if assemble_only else 'run'
    manifest: dict[str, Any] | None = None
    speech: SpeechExecutionResult | None = None
    built: list[str] = []
    reused: list[str] = []
    verification_view: dict[str, Any] | None = None
    publication: dict[str, Any] | None = None
    try:
        manifest = load_job_manifest(job_dir / MANIFEST_NAME)
        _verify_job_integrity(job_dir, manifest)
        _verify_window_feasibility(manifest)
        if not assemble_only:
            _check_operations_journal(job_dir)
            _reconcile_registered_operations(job_dir, manifest, now_fn)
            _require_resolved_completed_cache(manifest, job_dir)
        _verify_source_binding(manifest)
        _verify_source_decodability(manifest, settings)
        if not assemble_only:
            if retry_operation_id is not None:
                speech = _retry_uncertain_operation_locked(
                    job_dir,
                    retry_operation_id,
                    allow_paid=allow_paid,
                    limits=limits,
                    transport=transport,
                    key_reader=key_reader,
                    now_fn=now_fn,
                )
                if speech.failed or speech.uncertain:
                    raise PipelineFailure(
                        'provider',
                        'the named uncertain operation did not'
                        ' complete; the explicit retry outcome is'
                        ' recorded in the journal',
                        5,
                    )
                manifest = load_job_manifest(job_dir / MANIFEST_NAME)
            _planned, _reused_units = _planned_segments(
                manifest, manifest['artifacts'], frozenset()
            )
            if _planned:
                speech = _execute_speech_operations_locked(
                    job_dir,
                    allow_paid=allow_paid,
                    limits=limits,
                    transport=transport,
                    key_reader=key_reader,
                    now_fn=now_fn,
                )
                manifest = load_job_manifest(job_dir / MANIFEST_NAME)
                if speech.failed or speech.uncertain:
                    kinds = {
                        **speech.failed,
                        **speech.uncertain,
                    }
                    raise PipelineFailure(
                        'provider',
                        'speech execution stopped with non success'
                        f' outcomes: {kinds}; the job preserves all'
                        ' receipts and stays non successful',
                        5,
                    )
                if speech.blocked_unresolved:
                    raise PipelineFailure(
                        'provider',
                        'speech execution is blocked by unresolved'
                        ' operations requiring an explicit retry:'
                        f' {list(speech.blocked_unresolved)}',
                        5,
                    )
        failed_labels: frozenset[str] = frozenset()
        model = _assembly_model(manifest, manifest['artifacts'], failed_labels)
        units = model['units']
        tools = _speech_tools(settings)
        manifest_path = job_dir / MANIFEST_NAME
        for segment in manifest['segments']:
            segment_id = segment['id']
            record: dict[str, Any] | None
            if units['normalized_audio'].get(segment_id) != 'ok':
                record = _build_normalized_unit(
                    job_dir,
                    manifest,
                    manifest_path,
                    segment,
                    tools,
                    now_fn(),
                )
                built.append(_unit_label('normalized_audio', segment_id))
            else:
                record = _current_record(
                    manifest,
                    'normalized_audio',
                    segment_id,
                    segment['synthesis_signature'],
                )
                reused.append(_unit_label('normalized_audio', segment_id))
            assert record is not None
            if units['fitted_audio'].get(segment_id) != 'ok':
                _build_fitted_unit(
                    job_dir,
                    manifest,
                    manifest_path,
                    segment,
                    record,
                    tools,
                    now_fn(),
                )
                built.append(_unit_label('fitted_audio', segment_id))
            else:
                reused.append(_unit_label('fitted_audio', segment_id))
        if units['timeline_audio'] != 'ok':
            _build_timeline_unit(
                job_dir, manifest, manifest_path, model, tools, now_fn()
            )
            built.append(_unit_label('timeline_audio', None))
        else:
            reused.append(_unit_label('timeline_audio', None))
        timeline_record = _current_record(
            manifest,
            'timeline_audio',
            None,
            model['signatures']['timeline'],
        )
        assert timeline_record is not None
        if units['narration_audio'] != 'ok':
            _build_narration_unit(
                job_dir,
                manifest,
                manifest_path,
                timeline_record,
                model,
                tools,
                now_fn(),
            )
            built.append(_unit_label('narration_audio', None))
        else:
            reused.append(_unit_label('narration_audio', None))
        if manifest['media']['audio_disposition'] == 'convert':
            if units['original_audio'] != 'ok':
                _build_original_unit(
                    job_dir,
                    manifest,
                    manifest_path,
                    model,
                    tools,
                    now_fn(),
                )
                built.append(_unit_label('original_audio', None))
            else:
                reused.append(_unit_label('original_audio', None))
        subtitle_record = _current_record(
            manifest, 'subtitles', None, manifest['subtitle_signature']
        )
        if subtitle_record is None:
            _build_subtitles_unit(job_dir, manifest, manifest_path, now_fn())
            built.append(_unit_label('subtitles', None))
        else:
            reused.append(_unit_label('subtitles', None))
        publication, verification_view = _container_stage(
            job_dir,
            manifest,
            manifest_path,
            model,
            settings,
            tools,
            now_fn(),
            extra_output,
        )
    except BaseException as error:
        failure = _failure_view(error)
        failure_report = getattr(error, 'report', None)
        with contextlib.suppress(OSError):
            if manifest is not None:
                _write_run_report(
                    job_dir,
                    manifest,
                    command=command,
                    speech=speech,
                    built=built,
                    reused=reused,
                    verification_view=verification_view,
                    publication=publication,
                    failure=failure,
                    failure_report=failure_report,
                    now=now_fn(),
                )
        raise
    report_relative = _write_run_report(
        job_dir,
        manifest,
        command=command,
        speech=speech,
        built=built,
        reused=reused,
        verification_view=verification_view,
        publication=publication,
        failure=None,
        now=now_fn(),
    )
    assert publication is not None
    return RunResult(
        command=command,
        revision=manifest['revision'],
        speech=speech,
        built=tuple(built),
        reused=tuple(reused),
        verification=verification_view,
        publication=publication,
        report_path=report_relative,
    )

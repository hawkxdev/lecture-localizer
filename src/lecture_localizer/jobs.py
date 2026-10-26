"""Job orchestration: validated preparation, persistence and reuse."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lecture_localizer import media
from lecture_localizer.contracts import (
    ContractError,
    TranslationDocument,
    TranslationSegment,
    load_job_manifest,
    load_translation_document,
    verify_binding,
)
from lecture_localizer.media import MediaToolError
from lecture_localizer.settings import Settings
from lecture_localizer.storage import (
    DestinationExistsError,
    IntegrityRecord,
    JobLock,
    atomic_write_json,
    create_new_file,
    ensure_distinct_files,
    ensure_outside,
    sha256_bytes,
    sha256_file,
    utc_now_iso,
    verify_integrity,
)
from lecture_localizer.subtitles import (
    SegmentWindow,
    SourceCue,
    derive_windows,
    parse_srt,
)

MANIFEST_NAME = 'manifest.json'
SNAPSHOT_DIR = 'snapshots'
LOCK_NAME = 'lock'
STAGE_NAMES = ('synthesis', 'fitting', 'subtitle', 'container')
SUPPORTED_CONTAINERS = frozenset({'mp4', 'mov', 'matroska'})
SUPPORTED_AUDIO_CODECS = frozenset({'aac', 'pcm_s16le', 'mp3', 'opus'})
HDR_TRANSFERS = frozenset({'smpte2084', 'arib-std-b67'})
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
    managed = {LOCK_NAME, SNAPSHOT_DIR, 'artifacts'}
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


def _unit_classifications(
    manifest: dict[str, Any],
    artifacts: list[dict[str, Any]],
    failed_labels: frozenset[str],
) -> dict[str, Any]:
    """Classify every required cache unit against the current inputs.

    A unit is ok when any retained record for it carries the current
    signature and verifies: history is scanned, not just the last row,
    so returning to an earlier signature reuses its intact response.
    Records for other signatures stay as valid history and never decide
    current eligibility.
    """
    segment_ids = [s['id'] for s in manifest['segments']]

    def classify(
        kind: str, expected: dict[str | None, str]
    ) -> dict[str | None, str]:
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

    raw = classify(
        'raw_response',
        {s['id']: s['synthesis_signature'] for s in manifest['segments']},
    )
    fitted = classify(
        'fitted_audio',
        {s['id']: s['fitting_signature'] for s in manifest['segments']},
    )
    subtitle = classify('subtitles', {None: manifest['subtitle_signature']})[
        None
    ]
    container = classify(
        'container_track', {None: manifest['container_signature']}
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
        'reuse': _reuse_view(manifest, failed_labels),
        'blocked': blocked,
    }

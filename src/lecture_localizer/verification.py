"""Persisted candidate verification with named check results."""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import selectors
import subprocess
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lecture_localizer.media import (
    MediaToolError,
    checked_run_strict,
    discover_tool,
    scrubbed_environment,
)
from lecture_localizer.mux import TrackProfile
from lecture_localizer.settings import Settings
from lecture_localizer.subtitles import SourceCue, parse_srt

NARRATION_SAMPLE_RATE = 48000
NARRATION_CHANNELS = 1
AAC_FRAME_SAMPLES = 1024
PRIMING_TOLERANCE_FRAMES = 2
ARTIFACT_ROUNDING_MS = 1
FALLBACK_TICK_S = 0.001
_PACKET_FIELDS = ('pts_time', 'dts_time', 'duration_time', 'size', 'data')


def _deregister(
    selector_object: selectors.BaseSelector, descriptor: int
) -> None:
    """Unregister one descriptor when it is still registered."""
    with contextlib.suppress(KeyError):
        selector_object.unregister(descriptor)


_ABSENT_TIMING = ('n/a', '', 'none', 'nan', '?')

_EXPECTED_ROLES = (
    ('video', 'h264'),
    ('audio', 'aac'),
    ('audio', 'aac'),
    ('subtitle', 'mov_text'),
    ('subtitle', 'mov_text'),
)
_EXPECTED_LANGUAGES = ('eng', 'rus', 'eng', 'rus')


def _padding_tolerance_ms(frames: int) -> int:
    """Convert AAC frame counts into a millisecond tolerance bound."""
    return math.ceil(frames * AAC_FRAME_SAMPLES * 1000 / NARRATION_SAMPLE_RATE)


PRIMING_TOLERANCE_MS = _padding_tolerance_ms(PRIMING_TOLERANCE_FRAMES)


class VerificationError(RuntimeError):
    """Verification inputs are unusable for a meaningful report."""


@dataclass(frozen=True)
class CandidateExpectation:
    """Declared source facts the saved candidate must persist.

    Every copied stream is bound to its exact prepared artifact: the
    video to video_source, the selected original audio to
    original_packet_source, and the narration to
    narration_packet_source, so substituted or shortened prepared
    inputs cannot produce a successful report.
    """

    video_source: Path
    original_packet_source: Path
    original_packet_stream_index: int
    original_is_copied: bool
    narration_packet_source: Path
    original_channels: int
    original_sample_rate: int
    original_end_ms: int
    narration_end_ms: int
    english_srt_path: Path
    russian_srt_path: Path
    profile: TrackProfile


@dataclass(frozen=True)
class CheckResult:
    """One named verification check with measured evidence."""

    name: str
    ok: bool
    detail: str = ''
    measured: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VerificationReport:
    """Complete named check results for one saved candidate."""

    candidate: Path
    checks: tuple[CheckResult, ...]
    measured: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """Return True when every named check passed."""
        return all(check.ok for check in self.checks)

    @property
    def failed_checks(self) -> tuple[str, ...]:
        """Return the names of every failed check."""
        return tuple(check.name for check in self.checks if not check.ok)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON serializable view of the report."""
        return {
            'candidate': str(self.candidate),
            'ok': self.ok,
            'checks': [
                {
                    'name': check.name,
                    'ok': check.ok,
                    'detail': check.detail,
                    'measured': check.measured,
                }
                for check in self.checks
            ],
            'measured': self.measured,
        }


def _not_evaluated(name: str, reason: str) -> CheckResult:
    """Build an explicit failed placeholder for a blocked check."""
    return CheckResult(name=name, ok=False, detail=f'not evaluated: {reason}')


def _probe_streams(
    ffprobe: Path, media_path: Path, timeout_seconds: float
) -> dict[str, Any]:
    """Return the ffprobe stream and format payload of one file.

    Stream data is limited to the small extradata configuration dump;
    media payloads never enter this call.
    """
    result = checked_run_strict(
        [
            str(ffprobe),
            '-v',
            'error',
            '-show_streams',
            '-show_data',
            '-show_entries',
            'stream=index,codec_name,codec_type,profile,channels,'
            'sample_rate,duration,width,height,extradata,time_base',
            '-show_entries',
            'format=duration',
            '-of',
            'json',
            str(media_path),
        ],
        timeout_seconds,
    )
    try:
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise TypeError('payload is not an object')
        return payload
    except (json.JSONDecodeError, TypeError) as error:
        raise MediaToolError(
            f'unusable ffprobe stream output for {media_path}: {error}'
        ) from error


def _extradata_digest(hexdump: str) -> str:
    """Hash the decoded bytes of one ffprobe extradata dump."""
    cleaned = hexdump.replace('\\n', '\n')
    chunks: list[bytes] = []
    for line in cleaned.splitlines():
        body = line.split('  ', 1)[0]
        parts = body.split(':')[1:] if ':' in body else [body]
        chunks.append(''.join(parts).encode('ascii'))
    return hashlib.sha256(b''.join(chunks)).hexdigest()


def _stream_packet_fingerprints(
    ffprobe: Path,
    media_path: Path,
    selector: str,
    timeout_seconds: float,
) -> Iterator[tuple[str, ...]]:
    """Yield bounded packet identity rows one packet at a time.

    Each row carries presentation timestamps, duration, size and the
    digest of the packet payload, so whole media data is never retained
    in memory. The read enforces the subprocess deadline during the
    blocking stream itself, every packet must carry the complete field
    set, and a stream that ends without packets is a tool error rather
    than an empty success. The child is killed and reaped on every
    abnormal exit.
    """
    argv = [
        str(ffprobe),
        '-v',
        'error',
        '-select_streams',
        selector,
        '-show_packets',
        '-show_data',
        '-of',
        'flat',
        str(media_path),
    ]
    with tempfile.TemporaryFile() as stderr_file:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            bufsize=0,
            env=scrubbed_environment(),
        )
        assert process.stdout is not None
        descriptor = process.stdout.fileno()
        selector_object = selectors.DefaultSelector()
        selector_object.register(descriptor, selectors.EVENT_READ)
        registered = True
        deadline = time.monotonic() + timeout_seconds
        fields: dict[str, str] = {}
        current_index: str | None = None
        packet_count = 0
        buffer = b''

        def fail(message: str) -> MediaToolError:
            """Kill and reap the child, then raise the tool error."""
            _deregister(selector_object, descriptor)
            nonlocal registered
            registered = False
            process.kill()
            process.wait()
            return MediaToolError(f'{message}: {media_path}')

        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise fail(
                        f'stalled packet output, no data within'
                        f' {timeout_seconds}s'
                    )
                events = selector_object.select(remaining)
                if not events:
                    raise fail(
                        f'stalled packet output, no data within'
                        f' {timeout_seconds}s'
                    )
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    break
                buffer += chunk
                while b'\n' in buffer:
                    raw_line, buffer = buffer.split(b'\n', 1)
                    line = raw_line.decode('utf-8', 'replace').strip()
                    if not line or '=' not in line:
                        continue
                    key, _, value = line.partition('=')
                    parts = key.split('.')
                    if len(parts) < 4 or parts[:2] != ['packets', 'packet']:
                        continue
                    field_name = parts[3]
                    if current_index is None:
                        current_index = parts[2]
                    elif parts[2] != current_index:
                        packet_count += 1
                        yield _fingerprint(fields, media_path)
                        fields = {}
                        current_index = parts[2]
                    fields[field_name] = value.strip('"')
            if buffer.strip():
                raise fail('unusable packet output, trailing partial row')
            if fields:
                packet_count += 1
                yield _fingerprint(fields, media_path)
            if packet_count == 0:
                raise fail('unusable packet output, no packets were reported')
            _deregister(selector_object, descriptor)
            registered = False
            process.stdout.close()
            return_code = process.wait(timeout=timeout_seconds)
            stderr_file.seek(0)
            noise = stderr_file.read().decode('utf-8', 'replace').strip()
            if return_code != 0 or noise:
                raise fail(
                    f'unusable packet output, exit {return_code}'
                    f' {noise.splitlines()[-1:]}'
                )
        except BaseException:
            if registered:
                _deregister(selector_object, descriptor)
            process.kill()
            process.wait()
            raise


def _fingerprint(fields: dict[str, str], media_path: Path) -> tuple[str, ...]:
    """Return one compared packet row with a bounded payload digest.

    Every packet must carry the complete compared field set; a packet
    missing a field is unusable tool evidence rather than a gap.
    """
    for field_name in _PACKET_FIELDS:
        if field_name not in fields:
            raise MediaToolError(
                f'unusable packet output for {media_path}: packet is'
                f' missing {field_name}'
            )
    return (
        fields['pts_time'],
        fields['dts_time'],
        fields['duration_time'],
        fields['size'],
        hashlib.sha256(fields['data'].encode('utf-8')).hexdigest(),
    )


def _compare_streamed_packets(
    check_name: str,
    candidate_packets: Iterator[tuple[str, ...]],
    source_packets: Iterator[tuple[str, ...]],
    tick_bound_s: float,
) -> CheckResult:
    """Compare two packet streams one row at a time.

    Payload and size must match exactly. Timing is compared with the
    measured rational clock precision of the two streams: the bound is
    the sum of both reported time base ticks, so a millisecond clock
    like Matroska covers its own quantization while real shifts stay
    visible. Presentation timestamps are required on both sides; DTS is
    compared only when both containers actually store it.
    """
    index = 0
    skipped_dts = 0
    while True:
        candidate = next(candidate_packets, None)
        source = next(source_packets, None)
        if candidate is None and source is None:
            return CheckResult(
                name=check_name,
                ok=True,
                detail='copied packets match the source stream',
                measured={'packets': index, 'dts_skipped': skipped_dts},
            )
        if candidate is None or source is None:
            return CheckResult(
                name=check_name,
                ok=False,
                detail=(
                    f'packet count differs at packet {index}: candidate has'
                    f' {"at least" if candidate is not None else "only"}'
                    f' {index} packets, source has'
                    f' {"at least" if source is not None else "only"} {index}'
                ),
                measured={'packets': index, 'dts_skipped': skipped_dts},
            )
        if candidate[3] != source[3] or candidate[4] != source[4]:
            return CheckResult(
                name=check_name,
                ok=False,
                detail=(
                    f'packet {index} payload or size differs from the'
                    ' copied source; copied streams must keep identical'
                    ' packets'
                ),
                measured={'packets': index, 'dts_skipped': skipped_dts},
            )
        if (
            candidate[1].lower() in _ABSENT_TIMING
            or source[1].lower() in _ABSENT_TIMING
        ):
            skipped_dts += 1
        else:
            result = _compare_timing(
                check_name,
                index,
                1,
                'dts_time',
                candidate,
                source,
                tick_bound_s,
            )
            if result is not None:
                return result
        result = _compare_timing(
            check_name,
            index,
            0,
            'pts_time',
            candidate,
            source,
            tick_bound_s,
        )
        if result is not None:
            return result
        try:
            duration_difference = abs(float(candidate[2]) - float(source[2]))
        except ValueError:
            result = _timing_mismatch(
                check_name,
                index,
                'duration_time',
                candidate[2],
                source[2],
                tick_bound_s,
            )
            if result is not None:
                return result
        else:
            if duration_difference > tick_bound_s:
                return _timing_mismatch(
                    check_name,
                    index,
                    'duration_time',
                    candidate[2],
                    source[2],
                    tick_bound_s,
                )
        index += 1


def _compare_timing(
    check_name: str,
    index: int,
    position: int,
    field_name: str,
    candidate: tuple[str, ...],
    source: tuple[str, ...],
    tick_bound_s: float,
) -> CheckResult | None:
    """Compare one timing field within the measured clock bound."""
    try:
        difference = abs(float(candidate[position]) - float(source[position]))
    except ValueError:
        if candidate[position] != source[position]:
            return _timing_mismatch(
                check_name,
                index,
                field_name,
                candidate[position],
                source[position],
                tick_bound_s,
            )
        return None
    if difference > tick_bound_s:
        return _timing_mismatch(
            check_name,
            index,
            field_name,
            candidate[position],
            source[position],
            tick_bound_s,
        )
    return None


def _timing_mismatch(
    check_name: str,
    index: int,
    field_name: str,
    candidate_value: str,
    source_value: str,
    tick_bound_s: float,
) -> CheckResult:
    """Build the named failure for one timing field outside the bound."""
    return CheckResult(
        name=check_name,
        ok=False,
        detail=(
            f'packet {index} {field_name} moved from {source_value} to'
            f' {candidate_value}, above the measured clock bound of'
            f' {tick_bound_s} s'
        ),
        measured={'packets': index},
    )


def _stream_row(stream: dict[str, Any]) -> dict[str, Any]:
    """Return the compared presentation fields of one stream."""
    disposition = stream.get('disposition') or {}
    tags = stream.get('tags') or {}
    return {
        'index': stream.get('index'),
        'codec_type': stream.get('codec_type'),
        'codec_name': stream.get('codec_name'),
        'language': tags.get('language'),
        'default': disposition.get('default'),
        'forced': disposition.get('forced'),
    }


def _check_candidate_file(candidate: Path) -> CheckResult:
    """Require an existing nonempty candidate file."""
    if not candidate.is_file():
        return CheckResult(
            name='candidate_file',
            ok=False,
            detail=f'candidate file is missing: {candidate}',
        )
    if candidate.stat().st_size == 0:
        return CheckResult(
            name='candidate_file',
            ok=False,
            detail=f'candidate file is empty: {candidate}',
        )
    return CheckResult(
        name='candidate_file',
        ok=True,
        detail=f'{candidate.stat().st_size} bytes',
        measured={'size_bytes': candidate.stat().st_size},
    )


def _check_roles(streams: list[dict[str, Any]]) -> CheckResult:
    """Require the five stream roles, codecs and languages in order."""
    measured = {'streams': [_stream_row(stream) for stream in streams]}
    if len(streams) != len(_EXPECTED_ROLES):
        return CheckResult(
            name='stream_roles',
            ok=False,
            detail=(
                f'candidate carries {len(streams)} streams, expected'
                f' {len(_EXPECTED_ROLES)} (one video, two audio, two'
                ' subtitles)'
            ),
            measured=measured,
        )
    for index, (stream, (role, codec)) in enumerate(
        zip(streams, _EXPECTED_ROLES, strict=True)
    ):
        if (
            stream.get('codec_type') != role
            or stream.get('codec_name') != codec
        ):
            return CheckResult(
                name='stream_roles',
                ok=False,
                detail=(
                    f'stream {index} is'
                    f' {stream.get("codec_type")}/{stream.get("codec_name")},'
                    f' expected {role}/{codec}'
                ),
                measured=measured,
            )
    languages = tuple(
        str((stream.get('tags') or {}).get('language'))
        for stream in streams[1:]
    )
    if languages != _EXPECTED_LANGUAGES:
        return CheckResult(
            name='stream_roles',
            ok=False,
            detail=(
                f'persisted languages {languages} do not match'
                f' {_EXPECTED_LANGUAGES}'
            ),
            measured=measured,
        )
    return CheckResult(
        name='stream_roles',
        ok=True,
        detail='five stream roles, codecs and languages match the profile',
        measured=measured,
    )


def _int_or_none(value: Any) -> int | None:
    """Return one value as int when it is numeric."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _check_narration_format(stream: dict[str, Any]) -> CheckResult:
    """Require the approved narration AAC profile."""
    measured = {
        'profile': stream.get('profile'),
        'channels': stream.get('channels'),
        'sample_rate': stream.get('sample_rate'),
    }
    problems: list[str] = []
    if stream.get('profile') != 'LC':
        problems.append(f'audio profile {stream.get("profile")!r} is not LC')
    if _int_or_none(stream.get('channels')) != NARRATION_CHANNELS:
        problems.append(
            f'narration must be mono, found'
            f' {stream.get("channels")!r} channels'
        )
    if _int_or_none(stream.get('sample_rate')) != NARRATION_SAMPLE_RATE:
        problems.append(
            f'narration sample rate {stream.get("sample_rate")!r} is not'
            f' {NARRATION_SAMPLE_RATE}'
        )
    if problems:
        return CheckResult(
            name='narration_format',
            ok=False,
            detail='; '.join(problems),
            measured=measured,
        )
    return CheckResult(
        name='narration_format',
        ok=True,
        detail='narration is AAC LC mono at 48000 Hz',
        measured=measured,
    )


def _check_original_format(
    stream: dict[str, Any], expectation: CandidateExpectation
) -> CheckResult:
    """Require the declared original audio channels and sample rate."""
    measured = {
        'channels': stream.get('channels'),
        'sample_rate': stream.get('sample_rate'),
        'declared_channels': expectation.original_channels,
        'declared_sample_rate': expectation.original_sample_rate,
    }
    problems: list[str] = []
    actual_channels = _int_or_none(stream.get('channels'))
    if actual_channels != expectation.original_channels:
        problems.append(
            f'original audio channels {actual_channels!r} do not match the'
            f' declared {expectation.original_channels}'
        )
    actual_rate = _int_or_none(stream.get('sample_rate'))
    if actual_rate != expectation.original_sample_rate:
        problems.append(
            f'original audio sample rate {actual_rate!r} does not match the'
            f' declared {expectation.original_sample_rate}'
        )
    if problems:
        return CheckResult(
            name='original_format',
            ok=False,
            detail='; '.join(problems),
            measured=measured,
        )
    return CheckResult(
        name='original_format',
        ok=True,
        detail='original audio matches the declared channel layout',
        measured=measured,
    )


def _check_flags(
    streams: list[dict[str, Any]], profile: TrackProfile
) -> CheckResult:
    """Require the persisted default and forced flags of every role."""
    measured = {
        'flags': [
            {
                'index': stream.get('index'),
                'default': (stream.get('disposition') or {}).get('default'),
                'forced': (stream.get('disposition') or {}).get('forced'),
            }
            for stream in streams
        ]
    }
    expected_flags = (
        ('video', 1, 0),
        ('original audio', 1 if profile.original_default else 0, 0),
        ('narration', 1 if profile.narration_default else 0, 0),
        (
            'english subtitles',
            1 if profile.subtitles_default else 0,
            1 if profile.subtitles_forced else 0,
        ),
        (
            'russian subtitles',
            1 if profile.subtitles_default else 0,
            1 if profile.subtitles_forced else 0,
        ),
    )
    problems: list[str] = []
    for index, (role, expected_default, expected_forced) in enumerate(
        expected_flags
    ):
        disposition = streams[index].get('disposition') or {}
        actual_default = _int_or_none(disposition.get('default'))
        actual_forced = _int_or_none(disposition.get('forced'))
        if (
            actual_default != expected_default
            or actual_forced != expected_forced
        ):
            problems.append(
                f'{role} persisted default={actual_default!r}'
                f' forced={actual_forced!r}, expected default='
                f'{expected_default} forced={expected_forced}'
            )
    if problems:
        return CheckResult(
            name='track_flags',
            ok=False,
            detail='; '.join(problems),
            measured=measured,
        )
    return CheckResult(
        name='track_flags',
        ok=True,
        detail='persisted flags match the requested startup profile',
        measured=measured,
    )


def _check_video_profile(
    candidate_stream: dict[str, Any], source_stream: dict[str, Any]
) -> CheckResult:
    """Require unchanged video decoder configuration and dimensions.

    Packet equality alone cannot see bitstream configuration edits
    such as an h264_metadata crop, which preserves every packet while
    changing width, height and the avcC extradata.
    """
    measured: dict[str, Any] = {}
    problems: list[str] = []
    for field_name in ('width', 'height'):
        candidate_value = candidate_stream.get(field_name)
        source_value = source_stream.get(field_name)
        measured[field_name] = {
            'candidate': candidate_value,
            'source': source_value,
        }
        if candidate_value != source_value:
            problems.append(
                f'video {field_name} changed from {source_value!r} to'
                f' {candidate_value!r}'
            )
    candidate_extradata = str(candidate_stream.get('extradata') or '')
    source_extradata = str(source_stream.get('extradata') or '')
    candidate_digest = _extradata_digest(candidate_extradata)
    source_digest = _extradata_digest(source_extradata)
    measured['extradata_digest'] = {
        'candidate': candidate_digest,
        'source': source_digest,
    }
    measured['extradata_size'] = {
        'candidate': candidate_stream.get('extradata_size'),
        'source': source_stream.get('extradata_size'),
    }
    if candidate_digest != source_digest:
        problems.append(
            'video decoder configuration extradata differs from the'
            ' copied source while packets may still match'
        )
    if problems:
        return CheckResult(
            name='video_profile',
            ok=False,
            detail='; '.join(problems),
            measured=measured,
        )
    return CheckResult(
        name='video_profile',
        ok=True,
        detail='video dimensions and decoder configuration are unchanged',
        measured=measured,
    )


def _stream_duration_ms(stream: dict[str, Any]) -> float | None:
    """Return one stream duration in milliseconds when reported."""
    duration = stream.get('duration')
    if duration is None:
        return None
    try:
        return float(duration) * 1000
    except (TypeError, ValueError):
        return None


def _duration_mismatch(candidate_ms: float, artifact_ms: float) -> str | None:
    """Compare a copied stream against its prepared artifact duration."""
    if abs(candidate_ms - artifact_ms) > ARTIFACT_ROUNDING_MS:
        return (
            f'copied stream duration {round(candidate_ms, 3)} ms differs'
            f' from its prepared artifact duration'
            f' {round(artifact_ms, 3)} ms'
        )
    return None


def _check_narration_duration(
    candidate_stream: dict[str, Any],
    artifact_stream: dict[str, Any],
    expectation: CandidateExpectation,
) -> CheckResult:
    """Check narration duration against its prepared artifact.

    The prepared artifact carries its own measured codec delay, so it
    is the ground truth for the copied track: the candidate must match
    it within edit list rounding. Missing content can never pass as
    negative padding. The padding against the declared narration end is
    always reported, and the declared end plus the documented two frame
    AAC priming tolerance bounds the upper side.
    """
    candidate_ms = _stream_duration_ms(candidate_stream)
    artifact_ms = _stream_duration_ms(artifact_stream)
    measured: dict[str, Any] = {
        'narration_stream_duration_ms': candidate_ms,
        'artifact_stream_duration_ms': artifact_ms,
        'declared_narration_end_ms': expectation.narration_end_ms,
        'priming_tolerance_ms': PRIMING_TOLERANCE_MS,
        'artifact_rounding_ms': ARTIFACT_ROUNDING_MS,
    }
    if candidate_ms is None:
        return CheckResult(
            name='narration_duration',
            ok=False,
            detail='ffprobe reported no duration for the narration stream',
            measured=measured,
        )
    if artifact_ms is None:
        return CheckResult(
            name='narration_duration',
            ok=False,
            detail=(
                'ffprobe reported no duration for the prepared narration'
                ' artifact'
            ),
            measured=measured,
        )
    measured['narration_padding_ms'] = round(
        candidate_ms - expectation.narration_end_ms, 3
    )
    mismatch = _duration_mismatch(candidate_ms, artifact_ms)
    if mismatch is not None:
        return CheckResult(
            name='narration_duration',
            ok=False,
            detail=mismatch,
            measured=measured,
        )
    if candidate_ms < expectation.narration_end_ms - PRIMING_TOLERANCE_MS:
        return CheckResult(
            name='narration_duration',
            ok=False,
            detail=(
                f'narration is {abs(measured["narration_padding_ms"])} ms'
                ' short of the declared narration end; the prepared'
                ' content is missing from the timeline, which measured'
                ' codec padding cannot explain'
            ),
            measured=measured,
        )
    if candidate_ms > expectation.narration_end_ms + PRIMING_TOLERANCE_MS:
        return CheckResult(
            name='narration_duration',
            ok=False,
            detail=(
                f'narration stream carries {measured["narration_padding_ms"]}'
                f' ms of padding, above the documented AAC priming'
                f' tolerance of {PRIMING_TOLERANCE_MS} ms'
            ),
            measured=measured,
        )
    return CheckResult(
        name='narration_duration',
        ok=True,
        detail=(
            f'narration matches its artifact with'
            f' {measured["narration_padding_ms"]} ms padding versus the'
            ' declared end'
        ),
        measured=measured,
    )


def _check_original_duration(
    candidate_stream: dict[str, Any],
    artifact_stream: dict[str, Any],
    expectation: CandidateExpectation,
) -> CheckResult:
    """Check a converted original track against its prepared artifact."""
    candidate_ms = _stream_duration_ms(candidate_stream)
    artifact_ms = _stream_duration_ms(artifact_stream)
    measured: dict[str, Any] = {
        'original_stream_duration_ms': candidate_ms,
        'artifact_stream_duration_ms': artifact_ms,
        'declared_original_end_ms': expectation.original_end_ms,
        'priming_tolerance_ms': PRIMING_TOLERANCE_MS,
        'artifact_rounding_ms': ARTIFACT_ROUNDING_MS,
    }
    if candidate_ms is None:
        return CheckResult(
            name='original_duration',
            ok=False,
            detail='ffprobe reported no duration for the original stream',
            measured=measured,
        )
    if artifact_ms is None:
        return CheckResult(
            name='original_duration',
            ok=False,
            detail=(
                'ffprobe reported no duration for the prepared original'
                ' artifact'
            ),
            measured=measured,
        )
    measured['original_padding_ms'] = round(
        candidate_ms - expectation.original_end_ms, 3
    )
    mismatch = _duration_mismatch(candidate_ms, artifact_ms)
    if mismatch is not None:
        return CheckResult(
            name='original_duration',
            ok=False,
            detail=mismatch,
            measured=measured,
        )
    if candidate_ms < expectation.original_end_ms - PRIMING_TOLERANCE_MS:
        return CheckResult(
            name='original_duration',
            ok=False,
            detail=(
                f'original is {abs(measured["original_padding_ms"])} ms'
                ' short of the declared original end; the prepared'
                ' content is missing from the timeline, which measured'
                ' codec padding cannot explain'
            ),
            measured=measured,
        )
    if candidate_ms > expectation.original_end_ms + PRIMING_TOLERANCE_MS:
        return CheckResult(
            name='original_duration',
            ok=False,
            detail=(
                f'original stream carries {measured["original_padding_ms"]}'
                f' ms of padding, above the documented AAC priming'
                f' tolerance of {PRIMING_TOLERANCE_MS} ms'
            ),
            measured=measured,
        )
    return CheckResult(
        name='original_duration',
        ok=True,
        detail=(
            f'original matches its artifact with'
            f' {measured["original_padding_ms"]} ms padding versus the'
            ' declared end'
        ),
        measured=measured,
    )


def _check_container_duration(
    format_payload: dict[str, Any], expectation: CandidateExpectation
) -> CheckResult:
    """Require the container to cover the declared narration end."""
    duration = format_payload.get('duration')
    measured = {'format_duration_s': duration}
    if duration is None:
        return CheckResult(
            name='container_duration',
            ok=False,
            detail='ffprobe reported no container duration',
            measured=measured,
        )
    try:
        duration_ms = float(duration) * 1000
    except (TypeError, ValueError):
        return CheckResult(
            name='container_duration',
            ok=False,
            detail=f'unusable container duration {duration!r}',
            measured=measured,
        )
    measured['format_duration_ms'] = round(duration_ms, 3)
    if duration_ms + ARTIFACT_ROUNDING_MS < expectation.narration_end_ms:
        return CheckResult(
            name='container_duration',
            ok=False,
            detail=(
                f'container duration {round(duration_ms, 3)} ms does not'
                f' cover the declared narration end'
                f' {expectation.narration_end_ms} ms'
            ),
            measured=measured,
        )
    return CheckResult(
        name='container_duration',
        ok=True,
        detail='container covers the declared timeline',
        measured=measured,
    )


def _read_source_cues(path: Path) -> tuple[SourceCue, ...]:
    """Parse one prepared source subtitle file into cues."""
    return parse_srt(path.read_text(encoding='utf-8'))


def _extract_subtitles(
    ffmpeg: Path,
    candidate: Path,
    stream_index: int,
    destination: Path,
    timeout_seconds: float,
) -> tuple[SourceCue, ...]:
    """Extract one mov_text track back into parsed SRT cues."""
    checked_run_strict(
        [
            str(ffmpeg),
            '-hide_banner',
            '-loglevel',
            'error',
            '-i',
            str(candidate),
            '-map',
            f'0:{stream_index}',
            '-f',
            'srt',
            str(destination),
        ],
        timeout_seconds,
    )
    return _read_source_cues(destination)


def _compare_cues(
    name: str, extracted: tuple[SourceCue, ...], source: tuple[SourceCue, ...]
) -> CheckResult:
    """Require complete extracted text and times of one language."""
    measured = {
        'extracted_cues': len(extracted),
        'source_cues': len(source),
    }
    if len(extracted) != len(source):
        return CheckResult(
            name=name,
            ok=False,
            detail=(
                f'extracted {len(extracted)} cues but the source carries'
                f' {len(source)}; subtitle text is incomplete'
            ),
            measured=measured,
        )
    for index, (actual, expected) in enumerate(
        zip(extracted, source, strict=True)
    ):
        if (
            actual.start_ms != expected.start_ms
            or actual.end_ms != expected.end_ms
            or actual.text != expected.text
        ):
            return CheckResult(
                name=name,
                ok=False,
                detail=(
                    f'extracted cue {index + 1} differs from the source:'
                    f' {actual.start_ms}..{actual.end_ms}'
                    f' {actual.text!r} versus'
                    f' {expected.start_ms}..{expected.end_ms}'
                    f' {expected.text!r}'
                ),
                measured=measured,
            )
    return CheckResult(
        name=name,
        ok=True,
        detail=f'all {len(source)} cues match text and times exactly',
        measured=measured,
    )


def _check_full_decode(
    ffmpeg: Path, candidate: Path, timeout_seconds: float
) -> CheckResult:
    """Decode video and both audio tracks completely and cleanly.

    The subtitle tracks are proven decodable by the text extraction
    checks, which fully decode both mov_text streams back to SRT.
    """
    result = checked_run_strict(
        [
            str(ffmpeg),
            '-hide_banner',
            '-loglevel',
            'error',
            '-i',
            str(candidate),
            '-map',
            '0:v:0',
            '-map',
            '0:a:0',
            '-map',
            '0:a:1',
            '-f',
            'null',
            '-',
        ],
        timeout_seconds,
    )
    noise = (result.stderr or '').strip()
    if noise:
        return CheckResult(
            name='full_decode',
            ok=False,
            detail=f'decoding reported errors: {noise.splitlines()[-1]}',
            measured={'stderr_tail': noise.splitlines()[-3:]},
        )
    return CheckResult(
        name='full_decode',
        ok=True,
        detail=(
            'video and both audio tracks decode fully; both subtitle'
            ' tracks decode through the text extraction checks'
        ),
    )


def _audio_stream_row(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the first audio stream row of one payload."""
    for stream in payload.get('streams', []):
        if stream.get('codec_type') == 'audio':
            return dict(stream)
    return {}


def _video_stream_row(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the first video stream row of one payload."""
    for stream in payload.get('streams', []):
        if stream.get('codec_type') == 'video':
            return dict(stream)
    return {}


def _stream_tick(stream: dict[str, Any]) -> float | None:
    """Return one stream time base tick in seconds when reported."""
    time_base = stream.get('time_base')
    if not isinstance(time_base, str) or '/' not in time_base:
        return None
    denominator = time_base.rsplit('/', 1)[1]
    if not denominator.isdigit() or int(denominator) <= 0:
        return None
    return 1.0 / int(denominator)


def _pairwise_tick_bound(
    candidate_stream: dict[str, Any], source_stream: dict[str, Any]
) -> float:
    """Bound timing differences by the measured rational clocks.

    The bound is the sum of both reported time base ticks, the exact
    rational precision each container actually stores. A millisecond
    clock like Matroska therefore covers its own quantization without
    any universal epsilon, and containers that report no clock fall
    back to the one millisecond resolution such formats store.
    """
    candidate_tick = _stream_tick(candidate_stream)
    source_tick = _stream_tick(source_stream)
    if candidate_tick is None or source_tick is None:
        return FALLBACK_TICK_S
    return candidate_tick + source_tick


def _row_by_index(
    payload: dict[str, Any], stream_index: int
) -> dict[str, Any]:
    """Return one stream row of a payload by absolute index."""
    for stream in payload.get('streams', []):
        if stream.get('index') == stream_index:
            return dict(stream)
    return {}


def _run_packet_check(
    check_name: str,
    ffprobe: Path,
    timeout_seconds: float,
    candidate_path: Path,
    candidate_selector: str,
    candidate_stream: dict[str, Any],
    source_path: Path,
    source_selector: str,
    source_stream: dict[str, Any],
) -> CheckResult:
    """Stream and compare one copied candidate stream with its source."""
    try:
        return _compare_streamed_packets(
            check_name,
            _stream_packet_fingerprints(
                ffprobe, candidate_path, candidate_selector, timeout_seconds
            ),
            _stream_packet_fingerprints(
                ffprobe, source_path, source_selector, timeout_seconds
            ),
            _pairwise_tick_bound(candidate_stream, source_stream),
        )
    except MediaToolError as error:
        return _not_evaluated(check_name, str(error))


def _verify_with_tools(
    candidate: Path,
    expectation: CandidateExpectation,
    ffmpeg: Path,
    ffprobe: Path,
    timeout_seconds: float,
) -> VerificationReport:
    """Run all checks with resolved tools against the saved file."""
    checks: list[CheckResult] = []
    measured: dict[str, Any] = {
        'priming_tolerance_ms': PRIMING_TOLERANCE_MS,
        'fallback_tick_s': FALLBACK_TICK_S,
        'artifact_rounding_ms': ARTIFACT_ROUNDING_MS,
    }
    checks.append(_check_candidate_file(candidate))
    try:
        payload = _probe_streams(ffprobe, candidate, timeout_seconds)
        source_payload = _probe_streams(
            ffprobe, expectation.video_source, timeout_seconds
        )
        artifact_payload = _probe_streams(
            ffprobe, expectation.narration_packet_source, timeout_seconds
        )
        original_artifact_payload = _probe_streams(
            ffprobe, expectation.original_packet_source, timeout_seconds
        )
        streams = [dict(stream) for stream in payload.get('streams', [])]
        stream_error: str | None = None
    except MediaToolError as error:
        streams = []
        stream_error = str(error)
    if stream_error is not None:
        names = [
            'stream_roles',
            'video_profile',
            'narration_format',
            'original_format',
            'track_flags',
            'narration_duration',
            'container_duration',
            'video_packets',
            'narration_packets',
        ]
        names.append(
            'copied_original_packets'
            if expectation.original_is_copied
            else 'converted_original_packets'
        )
        names.extend(['english_text', 'russian_text', 'full_decode'])
        for name in names:
            checks.append(_not_evaluated(name, stream_error))
        return VerificationReport(
            candidate=candidate,
            checks=tuple(checks),
            measured={**measured, 'probe_error': stream_error},
        )
    checks.append(_check_roles(streams))
    if len(streams) == len(_EXPECTED_ROLES):
        checks.append(_check_narration_format(streams[2]))
        checks.append(_check_original_format(streams[1], expectation))
        checks.append(_check_flags(streams, expectation.profile))
        checks.append(
            _check_video_profile(streams[0], _video_stream_row(source_payload))
        )
        checks.append(
            _check_narration_duration(
                streams[2],
                _audio_stream_row(artifact_payload),
                expectation,
            )
        )
        if not expectation.original_is_copied:
            checks.append(
                _check_original_duration(
                    streams[1],
                    _audio_stream_row(original_artifact_payload),
                    expectation,
                )
            )
        checks.append(
            _check_container_duration(payload.get('format') or {}, expectation)
        )
    source_video_row = _video_stream_row(source_payload)
    artifact_audio_row = _audio_stream_row(artifact_payload)
    original_audio_row = _audio_stream_row(original_artifact_payload)
    checks.append(
        _run_packet_check(
            'video_packets',
            ffprobe,
            timeout_seconds,
            candidate,
            '0',
            streams[0],
            expectation.video_source,
            str(_video_stream_index_of(source_payload)),
            source_video_row,
        )
    )
    checks.append(
        _run_packet_check(
            'narration_packets',
            ffprobe,
            timeout_seconds,
            candidate,
            '2',
            streams[2],
            expectation.narration_packet_source,
            str(_audio_stream_index_of(artifact_payload)),
            artifact_audio_row,
        )
    )
    if expectation.original_is_copied:
        checks.append(
            _run_packet_check(
                'copied_original_packets',
                ffprobe,
                timeout_seconds,
                candidate,
                '1',
                streams[1],
                expectation.original_packet_source,
                str(expectation.original_packet_stream_index),
                _row_by_index(
                    original_artifact_payload,
                    expectation.original_packet_stream_index,
                ),
            )
        )
    else:
        checks.append(
            _run_packet_check(
                'converted_original_packets',
                ffprobe,
                timeout_seconds,
                candidate,
                '1',
                streams[1],
                expectation.original_packet_source,
                str(_audio_stream_index_of(original_artifact_payload)),
                original_audio_row,
            )
        )
    with tempfile.TemporaryDirectory(
        prefix='lecture-localizer-verify-'
    ) as temp_dir:
        temp_path = Path(temp_dir)
        for name, stream_index, source_path in (
            ('english_text', 3, expectation.english_srt_path),
            ('russian_text', 4, expectation.russian_srt_path),
        ):
            try:
                extracted = _extract_subtitles(
                    ffmpeg,
                    candidate,
                    stream_index,
                    temp_path / f'{name}.srt',
                    timeout_seconds,
                )
                checks.append(
                    _compare_cues(
                        name, extracted, _read_source_cues(source_path)
                    )
                )
            except MediaToolError as error:
                checks.append(_not_evaluated(name, str(error)))
            except (OSError, UnicodeDecodeError) as error:
                checks.append(_not_evaluated(name, str(error)))
    try:
        checks.append(_check_full_decode(ffmpeg, candidate, timeout_seconds))
    except MediaToolError as error:
        checks.append(_not_evaluated('full_decode', str(error)))
    return VerificationReport(
        candidate=candidate,
        checks=tuple(checks),
        measured=measured,
    )


def _video_stream_index_of(payload: dict[str, Any]) -> int:
    """Return the first video stream index of one probe payload."""
    for stream in payload.get('streams', []):
        if stream.get('codec_type') == 'video':
            return int(stream.get('index', 0))
    raise MediaToolError('source payload carries no video stream')


def _audio_stream_index_of(payload: dict[str, Any]) -> int:
    """Return the first audio stream index of one probe payload."""
    for stream in payload.get('streams', []):
        if stream.get('codec_type') == 'audio':
            return int(stream.get('index', 0))
    raise MediaToolError('source payload carries no audio stream')


def verify_candidate(
    candidate: Path,
    expectation: CandidateExpectation,
    settings: Settings,
) -> VerificationReport:
    """Verify the saved candidate against the declared source facts.

    Every failure mode is returned as a named failed check instead of a
    successful report. Tool and input errors degrade to explicit not
    evaluated checks so a broken toolchain can never produce a
    successful candidate report.
    """
    try:
        ffmpeg = discover_tool('ffmpeg', settings.ffmpeg_path)
        ffprobe = discover_tool('ffprobe', settings.ffprobe_path)
    except MediaToolError as error:
        return VerificationReport(
            candidate=candidate,
            checks=(
                CheckResult(name='toolchain', ok=False, detail=str(error)),
            ),
            measured={},
        )
    return _verify_with_tools(
        candidate,
        expectation,
        ffmpeg,
        ffprobe,
        settings.subprocess_timeout_seconds,
    )

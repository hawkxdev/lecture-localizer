"""Persisted candidate verification with named check results."""

from __future__ import annotations

import json
import math
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lecture_localizer.media import (
    MediaToolError,
    checked_run_strict,
    discover_tool,
)
from lecture_localizer.mux import TrackProfile
from lecture_localizer.settings import Settings
from lecture_localizer.subtitles import SourceCue, parse_srt

NARRATION_SAMPLE_RATE = 48000
NARRATION_CHANNELS = 1
AAC_FRAME_SAMPLES = 1024
PRIMING_TOLERANCE_FRAMES = 2
NEGATIVE_TOLERANCE_FRAMES = 1

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
NEGATIVE_TOLERANCE_MS = _padding_tolerance_ms(NEGATIVE_TOLERANCE_FRAMES)


class VerificationError(RuntimeError):
    """Verification inputs are unusable for a meaningful report."""


@dataclass(frozen=True)
class CandidateExpectation:
    """Declared source facts the saved candidate must persist."""

    video_source: Path
    original_packet_source: Path
    original_packet_stream_index: int
    original_is_copied: bool
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
    ffprobe: Path, candidate: Path, timeout_seconds: float
) -> dict[str, Any]:
    """Return the full ffprobe stream and format payload."""
    result = checked_run_strict(
        [
            str(ffprobe),
            '-v',
            'error',
            '-show_entries',
            'stream=index,codec_name,codec_type,profile,channels,'
            'sample_rate,duration:stream_tags=language:stream_disposition:'
            'format=duration',
            '-of',
            'json',
            str(candidate),
        ],
        timeout_seconds,
    )
    try:
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise TypeError('payload is not an object')
        return payload
    except json.JSONDecodeError as error:
        raise MediaToolError(
            f'unusable ffprobe stream output for {candidate}: {error}'
        ) from error
    except TypeError as error:
        raise MediaToolError(
            f'unusable ffprobe stream output for {candidate}: {error}'
        ) from error


def _probe_packets(
    ffprobe: Path,
    media_path: Path,
    selector: str,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    """Return packet rows with data for one selected stream."""
    result = checked_run_strict(
        [
            str(ffprobe),
            '-v',
            'error',
            '-select_streams',
            selector,
            '-show_packets',
            '-show_data',
            '-of',
            'json',
            str(media_path),
        ],
        timeout_seconds,
    )
    try:
        payload = json.loads(result.stdout)
        packets = payload['packets']
        if not isinstance(packets, list):
            raise TypeError('packets is not a list')
        return [dict(packet) for packet in packets]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise MediaToolError(
            f'unusable ffprobe packet output for {media_path}: {error}'
        ) from error


def _packet_fingerprint(packet: dict[str, Any]) -> tuple[str, ...]:
    """Return the compared identity fields of one packet."""
    return (
        str(packet.get('pts_time')),
        str(packet.get('dts_time')),
        str(packet.get('duration_time')),
        str(packet.get('size')),
        str(packet.get('data')),
    )


def _compare_packets(
    actual: list[dict[str, Any]], expected: list[dict[str, Any]]
) -> CheckResult:
    """Require exact packet identity between two selected streams."""
    measured = {'actual_packets': len(actual), 'source_packets': len(expected)}
    if len(actual) != len(expected):
        return CheckResult(
            name='packets',
            ok=False,
            detail=(
                f'packet count differs: candidate has {len(actual)},'
                f' source has {len(expected)}'
            ),
            measured=measured,
        )
    for index, (candidate_packet, source_packet) in enumerate(
        zip(actual, expected, strict=True)
    ):
        candidate_fields = _packet_fingerprint(candidate_packet)
        source_fields = _packet_fingerprint(source_packet)
        if candidate_fields != source_fields:
            differing = [
                field_name
                for field_name, left, right in zip(
                    ('pts_time', 'dts_time', 'duration_time', 'size', 'data'),
                    candidate_fields,
                    source_fields,
                    strict=True,
                )
                if left != right
            ]
            return CheckResult(
                name='packets',
                ok=False,
                detail=(
                    f'packet {index} differs in {differing}; copied streams'
                    ' must keep identical packets'
                ),
                measured=measured,
            )
    return CheckResult(
        name='packets',
        ok=True,
        detail='copied packets are identical to the source',
        measured=measured,
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


def _stream_duration_ms(stream: dict[str, Any]) -> float | None:
    """Return one stream duration in milliseconds when reported."""
    duration = stream.get('duration')
    if duration is None:
        return None
    try:
        return float(duration) * 1000
    except (TypeError, ValueError):
        return None


def _check_narration_duration(
    stream: dict[str, Any], expectation: CandidateExpectation
) -> CheckResult:
    """Check narration duration and report the codec padding explicitly.

    AAC encoding adds priming samples that are not speech content. The
    persisted stream may exceed the declared narration end by at most
    PRIMING_TOLERANCE_MS (two AAC frames) and must not fall below it by
    more than NEGATIVE_TOLERANCE_MS (one frame of edit list rounding).
    The measured padding is always reported rather than hidden.
    """
    duration_ms = _stream_duration_ms(stream)
    measured = {
        'narration_stream_duration_ms': duration_ms,
        'declared_narration_end_ms': expectation.narration_end_ms,
        'priming_tolerance_ms': PRIMING_TOLERANCE_MS,
        'negative_tolerance_ms': NEGATIVE_TOLERANCE_MS,
    }
    if duration_ms is None:
        return CheckResult(
            name='narration_duration',
            ok=False,
            detail='ffprobe reported no duration for the narration stream',
            measured=measured,
        )
    padding_ms = round(duration_ms - expectation.narration_end_ms, 3)
    measured['narration_padding_ms'] = padding_ms
    if padding_ms < -NEGATIVE_TOLERANCE_MS:
        return CheckResult(
            name='narration_duration',
            ok=False,
            detail=(
                f'narration stream is {abs(padding_ms)} ms shorter than the'
                ' declared narration end; audio appears truncated'
            ),
            measured=measured,
        )
    if padding_ms > PRIMING_TOLERANCE_MS:
        return CheckResult(
            name='narration_duration',
            ok=False,
            detail=(
                f'narration stream carries {padding_ms} ms of padding, above'
                f' the documented AAC priming tolerance of'
                f' {PRIMING_TOLERANCE_MS} ms'
            ),
            measured=measured,
        )
    return CheckResult(
        name='narration_duration',
        ok=True,
        detail=(
            f'narration duration fits with {padding_ms} ms reported padding'
        ),
        measured=measured,
    )


def _check_original_duration(
    stream: dict[str, Any], expectation: CandidateExpectation
) -> CheckResult:
    """Check a converted original track against its declared end."""
    duration_ms = _stream_duration_ms(stream)
    measured = {
        'original_stream_duration_ms': duration_ms,
        'declared_original_end_ms': expectation.original_end_ms,
        'priming_tolerance_ms': PRIMING_TOLERANCE_MS,
        'negative_tolerance_ms': NEGATIVE_TOLERANCE_MS,
    }
    if duration_ms is None:
        return CheckResult(
            name='original_duration',
            ok=False,
            detail='ffprobe reported no duration for the original stream',
            measured=measured,
        )
    padding_ms = round(duration_ms - expectation.original_end_ms, 3)
    measured['original_padding_ms'] = padding_ms
    if padding_ms < -NEGATIVE_TOLERANCE_MS:
        return CheckResult(
            name='original_duration',
            ok=False,
            detail=(
                f'original stream is {abs(padding_ms)} ms shorter than the'
                ' declared original end; audio appears truncated'
            ),
            measured=measured,
        )
    if padding_ms > PRIMING_TOLERANCE_MS:
        return CheckResult(
            name='original_duration',
            ok=False,
            detail=(
                f'original stream carries {padding_ms} ms of padding, above'
                f' the documented AAC priming tolerance of'
                f' {PRIMING_TOLERANCE_MS} ms'
            ),
            measured=measured,
        )
    return CheckResult(
        name='original_duration',
        ok=True,
        detail=f'original duration fits with {padding_ms} ms reported padding',
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
    if duration_ms + NEGATIVE_TOLERANCE_MS < expectation.narration_end_ms:
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
        'negative_tolerance_ms': NEGATIVE_TOLERANCE_MS,
    }
    checks.append(_check_candidate_file(candidate))
    try:
        payload = _probe_streams(ffprobe, candidate, timeout_seconds)
        streams = [dict(stream) for stream in payload.get('streams', [])]
        stream_error: str | None = None
    except MediaToolError as error:
        streams = []
        stream_error = str(error)
    if stream_error is not None:
        names = [
            'stream_roles',
            'narration_format',
            'original_format',
            'track_flags',
            'narration_duration',
            'container_duration',
            'video_packets',
        ]
        names.append(
            'copied_original_packets'
            if expectation.original_is_copied
            else 'original_duration'
        )
        names.extend(['english_text', 'russian_text', 'full_decode'])
        for name in names:
            checks.append(_not_evaluated(name, stream_error))
        return VerificationReport(
            candidate=candidate,
            checks=tuple(checks),
            measured={**measured, 'probe_error': stream_error},
        )
    roles = _check_roles(streams)
    checks.append(roles)
    if len(streams) == len(_EXPECTED_ROLES):
        checks.append(_check_narration_format(streams[2]))
        checks.append(_check_original_format(streams[1], expectation))
        checks.append(_check_flags(streams, expectation.profile))
        checks.append(_check_narration_duration(streams[2], expectation))
        if not expectation.original_is_copied:
            checks.append(_check_original_duration(streams[1], expectation))
        checks.append(
            _check_container_duration(payload.get('format') or {}, expectation)
        )
    try:
        video_packets = _probe_packets(
            ffprobe, candidate, 'v:0', timeout_seconds
        )
        source_packets = _probe_packets(
            ffprobe,
            expectation.video_source,
            'v:0',
            timeout_seconds,
        )
        comparison = _compare_packets(video_packets, source_packets)
        checks.append(
            CheckResult(
                name='video_packets',
                ok=comparison.ok,
                detail=comparison.detail,
                measured=comparison.measured,
            )
        )
    except MediaToolError as error:
        checks.append(_not_evaluated('video_packets', str(error)))
    if expectation.original_is_copied:
        try:
            candidate_packets = _probe_packets(
                ffprobe, candidate, 'a:0', timeout_seconds
            )
            source_packets = _probe_packets(
                ffprobe,
                expectation.original_packet_source,
                str(expectation.original_packet_stream_index),
                timeout_seconds,
            )
            comparison = _compare_packets(candidate_packets, source_packets)
            checks.append(
                CheckResult(
                    name='copied_original_packets',
                    ok=comparison.ok,
                    detail=comparison.detail,
                    measured=comparison.measured,
                )
            )
        except MediaToolError as error:
            checks.append(
                _not_evaluated('copied_original_packets', str(error))
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

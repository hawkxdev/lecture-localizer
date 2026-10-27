"""Tests for persisted candidate verification."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from container_fixtures import (
    build_cropped_candidate,
    candidate_request,
    encode_narration_aac,
    encode_original_aac,
    make_shifted_audio,
    make_shifted_source,
    prepared_tracks,
    write_fake_tools,
    write_srt_entries,
)

from conftest import make_media, require_real_media
from lecture_localizer import verification as verification_module
from lecture_localizer.mux import TrackProfile, build_candidate
from lecture_localizer.settings import Settings
from lecture_localizer.verification import (
    CandidateExpectation,
    VerificationReport,
    verify_candidate,
)


def _candidate(tmp_path: Path) -> tuple[dict[str, Any], Path]:
    """Build one real five stream candidate from prepared tracks."""
    tracks = prepared_tracks(tmp_path)
    destination = tmp_path / 'staging' / 'candidate.mp4'
    destination.parent.mkdir(parents=True, exist_ok=True)
    build_candidate(candidate_request(tracks, destination), Settings())
    return tracks, destination


def _expectation(
    tracks: dict[str, Any],
    *,
    original_is_copied: bool = True,
    original_end_ms: int = 2000,
    narration_end_ms: int = 2000,
    profile: TrackProfile | None = None,
    russian_srt_path: Path | None = None,
    video_source: Path | None = None,
    original_packet_stream_index: int = 1,
) -> CandidateExpectation:
    """Build the declared expectation for the standard candidate."""
    return CandidateExpectation(
        video_source=video_source or tracks['video'],
        original_packet_source=tracks['video'],
        original_packet_stream_index=original_packet_stream_index,
        original_is_copied=original_is_copied,
        narration_packet_source=tracks['narration'],
        original_channels=1,
        original_sample_rate=44100,
        original_end_ms=original_end_ms,
        narration_end_ms=narration_end_ms,
        english_srt_path=tracks['english'],
        russian_srt_path=(
            russian_srt_path
            if russian_srt_path is not None
            else tracks['russian']
        ),
        profile=profile if profile is not None else TrackProfile(),
    )


def _check(report: VerificationReport, name: str) -> Any:
    """Return one named check result from a report."""
    return next(check for check in report.checks if check.name == name)


def test_verify_real_candidate_reports_ok(tmp_path: Path) -> None:
    require_real_media()
    tracks, destination = _candidate(tmp_path)
    report = verify_candidate(destination, _expectation(tracks), Settings())
    assert report.ok, report.failed_checks
    names = {check.name for check in report.checks}
    assert {
        'candidate_file',
        'stream_roles',
        'video_profile',
        'narration_format',
        'original_format',
        'track_flags',
        'narration_duration',
        'container_duration',
        'video_packets',
        'copied_original_packets',
        'narration_packets',
        'english_text',
        'russian_text',
        'full_decode',
    } <= names
    duration_check = _check(report, 'narration_duration')
    assert 'narration_padding_ms' in duration_check.measured
    assert 'narration_stream_duration_ms' in duration_check.measured
    packets_check = _check(report, 'video_packets')
    assert packets_check.measured['packets'] > 0
    profile_check = _check(report, 'video_profile')
    assert profile_check.ok


def test_verify_real_candidate_keeps_multiline_text(
    tmp_path: Path,
) -> None:
    require_real_media()
    tracks, destination = _candidate(tmp_path)
    report = verify_candidate(destination, _expectation(tracks), Settings())
    assert report.ok, report.failed_checks
    english = _check(report, 'english_text')
    assert english.ok
    assert english.measured['extracted_cues'] == 2


def test_verify_flags_mismatch_is_a_failed_check(tmp_path: Path) -> None:
    require_real_media()
    tracks, destination = _candidate(tmp_path)
    report = verify_candidate(
        destination,
        _expectation(tracks, profile=TrackProfile(narration_default=False)),
        Settings(),
    )
    assert not report.ok
    assert 'track_flags' in report.failed_checks


def test_verify_video_packet_mismatch_is_detected(tmp_path: Path) -> None:
    require_real_media()
    tracks, destination = _candidate(tmp_path)
    other_video = make_media(tmp_path / 'other.mp4', seconds=2.2)
    report = verify_candidate(
        destination,
        _expectation(tracks, video_source=other_video),
        Settings(),
    )
    assert not report.ok
    assert 'video_packets' in report.failed_checks


def test_verify_wrong_copied_stream_is_detected(tmp_path: Path) -> None:
    require_real_media()
    tracks, destination = _candidate(tmp_path)
    report = verify_candidate(
        destination,
        _expectation(tracks, original_packet_stream_index=0),
        Settings(),
    )
    assert not report.ok
    assert 'copied_original_packets' in report.failed_checks


def test_verify_truncated_russian_text_is_detected(tmp_path: Path) -> None:
    require_real_media()
    tracks, destination = _candidate(tmp_path)
    truncated = tmp_path / 'truncated.srt'
    write_srt_entries(
        truncated,
        [(0, 1000, 'Первая полная реплика перевода.')],
    )
    report = verify_candidate(
        destination,
        _expectation(tracks, russian_srt_path=truncated),
        Settings(),
    )
    assert not report.ok
    assert 'russian_text' in report.failed_checks


def test_verify_corrupt_candidate_fails_decoding(tmp_path: Path) -> None:
    require_real_media()
    tracks, destination = _candidate(tmp_path)
    corrupt = tmp_path / 'staging' / 'corrupt.mp4'
    raw = destination.read_bytes()
    corrupt.write_bytes(raw[: len(raw) // 2])
    report = verify_candidate(corrupt, _expectation(tracks), Settings())
    assert not report.ok
    assert 'full_decode' in report.failed_checks


def test_verify_missing_candidate_is_a_failed_report(
    tmp_path: Path,
) -> None:
    require_real_media()
    tracks, _ = _candidate(tmp_path)
    report = verify_candidate(
        tmp_path / 'staging' / 'absent.mp4',
        _expectation(tracks),
        Settings(),
    )
    assert not report.ok
    assert 'candidate_file' in report.failed_checks


@pytest.mark.parametrize(
    ('declared_end_ms', 'should_pass'),
    [
        (2000, True),
        (2005, True),
        (1985, True),
        (1950, False),
        (1900, False),
    ],
)
def test_verify_narration_padding_boundaries(
    tmp_path: Path, declared_end_ms: int, should_pass: bool
) -> None:
    require_real_media()
    tracks, destination = _candidate(tmp_path)
    report = verify_candidate(
        destination,
        _expectation(tracks, narration_end_ms=declared_end_ms),
        Settings(),
    )
    duration_check = _check(report, 'narration_duration')
    assert duration_check.ok is should_pass
    assert 'narration_padding_ms' in duration_check.measured
    if not should_pass:
        assert not report.ok


def test_verify_converted_original_artifact_binding(
    tmp_path: Path,
) -> None:
    require_real_media()
    tracks = prepared_tracks(tmp_path)
    destination = tmp_path / 'staging' / 'converted.mp4'
    destination.parent.mkdir(parents=True, exist_ok=True)
    build_candidate(
        candidate_request(tracks, destination, copied_stream_index=None),
        Settings(),
    )
    right = CandidateExpectation(
        video_source=tracks['video'],
        original_packet_source=tracks['converted'],
        original_packet_stream_index=0,
        original_is_copied=False,
        narration_packet_source=tracks['narration'],
        original_channels=2,
        original_sample_rate=48000,
        original_end_ms=2000,
        narration_end_ms=2000,
        english_srt_path=tracks['english'],
        russian_srt_path=tracks['russian'],
        profile=TrackProfile(),
    )
    report = verify_candidate(destination, right, Settings())
    assert report.ok, report.failed_checks
    converted = _check(report, 'converted_original_packets')
    assert converted.ok
    assert converted.measured['packets'] > 0
    substitute = encode_original_aac(
        tmp_path / 'prepared' / 'substitute.m4a',
        stereo=True,
        frequency=520,
    )
    wrong = CandidateExpectation(
        video_source=tracks['video'],
        original_packet_source=substitute,
        original_packet_stream_index=0,
        original_is_copied=False,
        narration_packet_source=tracks['narration'],
        original_channels=2,
        original_sample_rate=48000,
        original_end_ms=2000,
        narration_end_ms=2000,
        english_srt_path=tracks['english'],
        russian_srt_path=tracks['russian'],
        profile=TrackProfile(),
    )
    report = verify_candidate(destination, wrong, Settings())
    assert not report.ok
    assert 'converted_original_packets' in report.failed_checks


def test_verify_probe_failure_degrades_to_not_evaluated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    require_real_media()
    tracks, destination = _candidate(tmp_path)
    tools = write_fake_tools(tmp_path / 'tooling')
    monkeypatch.setenv('FAKE_FFPROBE_EXIT', '1')
    settings = Settings(
        ffmpeg_path=shutil.which('ffmpeg'),
        ffprobe_path=str(tools['ffprobe']),
    )
    report = verify_candidate(destination, _expectation(tracks), settings)
    assert not report.ok
    assert any(
        check.detail.startswith('not evaluated:') for check in report.checks
    )
    failing = {
        check.name
        for check in report.checks
        if not check.ok and check.name != 'candidate_file'
    }
    assert 'stream_roles' in failing
    assert 'full_decode' in failing


def _canned_streams_payload() -> dict[str, Any]:
    """Return a five stream payload with one wrong language tag."""
    return {
        'streams': [
            {
                'index': 0,
                'codec_type': 'video',
                'codec_name': 'h264',
                'disposition': {'default': 1, 'forced': 0},
            },
            {
                'index': 1,
                'codec_type': 'audio',
                'codec_name': 'aac',
                'profile': 'LC',
                'channels': 1,
                'sample_rate': '44100',
                'duration': '2.000000',
                'tags': {'language': 'eng'},
                'disposition': {'default': 0, 'forced': 0},
            },
            {
                'index': 2,
                'codec_type': 'audio',
                'codec_name': 'aac',
                'profile': 'LC',
                'channels': 1,
                'sample_rate': '48000',
                'duration': '2.020000',
                'tags': {'language': 'deu'},
                'disposition': {'default': 1, 'forced': 0},
            },
            {
                'index': 3,
                'codec_type': 'subtitle',
                'codec_name': 'mov_text',
                'tags': {'language': 'eng'},
                'disposition': {'default': 0, 'forced': 0},
            },
            {
                'index': 4,
                'codec_type': 'subtitle',
                'codec_name': 'mov_text',
                'tags': {'language': 'rus'},
                'disposition': {'default': 0, 'forced': 0},
            },
        ],
        'format': {'duration': '2.020000'},
    }


def test_verify_language_mismatch_is_a_failed_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    require_real_media()
    tracks, destination = _candidate(tmp_path)
    tooling = tmp_path / 'tooling'
    tools = write_fake_tools(tooling)
    payloads = tooling / 'payloads'
    payloads.mkdir(parents=True, exist_ok=True)
    streams_path = payloads / 'streams.json'
    streams_path.write_text(
        json.dumps(_canned_streams_payload()), encoding='utf-8'
    )
    packets_path = payloads / 'packets.json'
    packets_path.write_text('{"packets": []}', encoding='utf-8')
    monkeypatch.setenv('FAKE_STREAMS_PAYLOAD', str(streams_path))
    monkeypatch.setenv('FAKE_PACKETS_PAYLOAD', str(packets_path))
    settings = Settings(
        ffmpeg_path=shutil.which('ffmpeg'),
        ffprobe_path=str(tools['ffprobe']),
    )
    report = verify_candidate(destination, _expectation(tracks), settings)
    assert not report.ok
    assert 'stream_roles' in report.failed_checks
    narration = _check(report, 'narration_format')
    assert narration.ok


def test_verify_report_serialization_shape(tmp_path: Path) -> None:
    require_real_media()
    tracks, destination = _candidate(tmp_path)
    report = verify_candidate(destination, _expectation(tracks), Settings())
    payload = report.to_dict()
    assert payload['ok'] is True
    assert payload['candidate'] == str(destination)
    assert isinstance(payload['checks'], list)
    assert all('name' in check for check in payload['checks'])
    assert isinstance(report, VerificationReport)


def test_verify_detects_wrong_narration_artifact(tmp_path: Path) -> None:
    require_real_media()
    tracks = prepared_tracks(tmp_path)
    wrong_narration = encode_narration_aac(
        tmp_path / 'prepared' / 'wrong-narration.m4a', frequency=660
    )
    destination = tmp_path / 'staging' / 'wrong-narr-candidate.mp4'
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = candidate_request(tracks, destination)
    build_candidate(
        request.__class__(
            video_path=request.video_path,
            original_audio=request.original_audio,
            narration_path=wrong_narration,
            english_srt_path=request.english_srt_path,
            russian_srt_path=request.russian_srt_path,
            destination=request.destination,
            profile=request.profile,
        ),
        Settings(),
    )
    report = verify_candidate(destination, _expectation(tracks), Settings())
    assert not report.ok
    assert 'narration_packets' in report.failed_checks


def test_verify_detects_shortened_narration(tmp_path: Path) -> None:
    require_real_media()
    tracks = prepared_tracks(tmp_path)
    shortened = encode_narration_aac(
        tmp_path / 'prepared' / 'narration-short.m4a', seconds=1.0
    )
    destination = tmp_path / 'staging' / 'short-candidate.mp4'
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = candidate_request(tracks, destination)
    build_candidate(
        request.__class__(
            video_path=request.video_path,
            original_audio=request.original_audio,
            narration_path=shortened,
            english_srt_path=request.english_srt_path,
            russian_srt_path=request.russian_srt_path,
            destination=request.destination,
            profile=request.profile,
        ),
        Settings(),
    )
    report = verify_candidate(destination, _expectation(tracks), Settings())
    assert not report.ok
    assert 'narration_packets' in report.failed_checks
    assert 'narration_duration' in report.failed_checks


def test_verify_detects_shifted_narration(tmp_path: Path) -> None:
    require_real_media()
    tracks = prepared_tracks(tmp_path)
    shifted = make_shifted_audio(
        tracks['narration'],
        tmp_path / 'prepared' / 'narration-shifted.m4a',
    )
    destination = tmp_path / 'staging' / 'shifted-candidate.mp4'
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = candidate_request(tracks, destination)
    build_candidate(
        request.__class__(
            video_path=request.video_path,
            original_audio=request.original_audio,
            narration_path=shifted,
            english_srt_path=request.english_srt_path,
            russian_srt_path=request.russian_srt_path,
            destination=request.destination,
            profile=request.profile,
        ),
        Settings(),
    )
    report = verify_candidate(destination, _expectation(tracks), Settings())
    assert not report.ok
    assert 'narration_packets' in report.failed_checks
    shifted_check = _check(report, 'narration_packets')
    assert 'pts_time' in shifted_check.detail


def test_verify_detects_video_configuration_crop(tmp_path: Path) -> None:
    require_real_media()
    tracks = prepared_tracks(tmp_path)
    destination = tmp_path / 'staging' / 'cropped.mp4'
    build_cropped_candidate(tracks, destination)
    report = verify_candidate(destination, _expectation(tracks), Settings())
    assert not report.ok
    profile_check = _check(report, 'video_profile')
    assert not profile_check.ok
    assert 'width' in profile_check.detail
    assert 'extradata' in profile_check.detail
    packets_check = _check(report, 'video_packets')
    assert packets_check.ok


def test_verify_preserves_nonzero_input_timeline(tmp_path: Path) -> None:
    require_real_media()
    tracks = prepared_tracks(tmp_path)
    shifted_video = make_shifted_source(
        tracks['video'], tmp_path / 'prepared' / 'shifted.mp4'
    )
    shifted_tracks = {
        **tracks,
        'video': shifted_video,
    }
    destination = tmp_path / 'staging' / 'shifted-candidate.mp4'
    destination.parent.mkdir(parents=True, exist_ok=True)
    build_candidate(
        candidate_request(shifted_tracks, destination),
        Settings(),
    )
    report = verify_candidate(
        destination,
        _expectation(shifted_tracks, original_packet_stream_index=1),
        Settings(),
    )
    assert report.ok, report.failed_checks
    video_packets = _check(report, 'video_packets')
    assert video_packets.ok
    assert video_packets.measured['packets'] > 0


class _RecordingStream:
    """Line iterator that records how much was consumed."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self.consumed = 0

    def __iter__(self) -> _RecordingStream:
        return self

    def __next__(self) -> str:
        if self.consumed >= len(self._lines):
            raise StopIteration
        line = self._lines[self.consumed]
        self.consumed += 1
        return line


class _FakeProcess:
    """Popen stand in with a recorded stdout stream."""

    def __init__(self, lines: list[str]) -> None:
        self.stdout = _RecordingStream(lines)

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:
        return None


def test_packet_fingerprint_streaming_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_packet = [
        'packets.packet.0.codec_type="video"',
        'packets.packet.0.pts_time="0.000000"',
        'packets.packet.0.dts_time="0.000000"',
        'packets.packet.0.duration_time="0.100000"',
        'packets.packet.0.size="256"',
        'packets.packet.0.data="\\n00000000: 00ff 00ff  first.\\n"',
    ]
    second_packet = [
        'packets.packet.1.codec_type="video"',
        'packets.packet.1.pts_time="0.100000"',
        'packets.packet.1.dts_time="0.100000"',
        'packets.packet.1.duration_time="0.100000"',
        'packets.packet.1.size="256"',
        'packets.packet.1.data="\\n00000000: 11ee 11ee  second.\\n"',
    ]
    lines = [*first_packet, *second_packet]
    fake_process = _FakeProcess(lines)
    monkeypatch.setattr(
        subprocess,
        'Popen',
        lambda *args, **kwargs: fake_process,
    )
    fingerprints = verification_module._stream_packet_fingerprints(
        Path('ffprobe'),
        Path('media'),
        '0',
        1.0,
    )
    first = next(fingerprints)
    assert first[0] == '0.000000'
    assert len(first[4]) == 64
    consumed = fake_process.stdout.consumed
    assert consumed == len(first_packet) + 1
    assert consumed < len(lines)
    second = next(fingerprints)
    assert second[0] == '0.100000'
    assert second[4] != first[4]

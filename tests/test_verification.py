"""Tests for persisted candidate verification."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from container_fixtures import (
    candidate_request,
    prepared_tracks,
    write_fake_tools,
    write_srt_entries,
)

from conftest import make_media, require_real_media
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
        'narration_format',
        'original_format',
        'track_flags',
        'narration_duration',
        'container_duration',
        'video_packets',
        'copied_original_packets',
        'english_text',
        'russian_text',
        'full_decode',
    } <= names
    duration_check = _check(report, 'narration_duration')
    assert 'narration_padding_ms' in duration_check.measured
    assert 'narration_stream_duration_ms' in duration_check.measured
    packets_check = _check(report, 'video_packets')
    assert packets_check.measured['actual_packets'] > 0


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
        (2080, False),
        (1950, False),
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


def test_verify_converted_original_duration_check(tmp_path: Path) -> None:
    require_real_media()
    tracks = prepared_tracks(tmp_path)
    destination = tmp_path / 'staging' / 'converted.mp4'
    destination.parent.mkdir(parents=True, exist_ok=True)
    build_candidate(
        candidate_request(tracks, destination, copied_stream_index=None),
        Settings(),
    )
    wrong = CandidateExpectation(
        video_source=tracks['video'],
        original_packet_source=tracks['converted'],
        original_packet_stream_index=0,
        original_is_copied=False,
        original_channels=2,
        original_sample_rate=48000,
        original_end_ms=10000,
        narration_end_ms=2000,
        english_srt_path=tracks['english'],
        russian_srt_path=tracks['russian'],
        profile=TrackProfile(),
    )
    report = verify_candidate(destination, wrong, Settings())
    assert not report.ok
    assert 'original_duration' in report.failed_checks
    right = CandidateExpectation(
        video_source=tracks['video'],
        original_packet_source=tracks['converted'],
        original_packet_stream_index=0,
        original_is_copied=False,
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

"""Tests for persisted candidate verification."""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any, cast

import pytest
from container_fixtures import (
    build_cropped_candidate,
    candidate_request,
    encode_narration_aac,
    encode_original_aac,
    make_container_copy,
    make_shifted_audio,
    make_shifted_source,
    prepared_tracks,
    write_fake_tools,
    write_packet_wrapper,
    write_srt_entries,
)

from conftest import make_media, require_real_media
from lecture_localizer.media import MediaToolError
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
        (2005, False),
        (2001, True),
        (2002, False),
        (1985, True),
        (2080, False),
        (2500, False),
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
    assert 'moved from' in shifted_check.detail
    assert shifted_check.measured['packets'] == 0


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


def test_packet_fingerprint_streaming_is_bounded(
    tmp_path: Path,
) -> None:
    slow_ffprobe = tmp_path / 'bounded-ffprobe'
    fields = {
        'pts_time': '0.000000',
        'dts_time': '0.000000',
        'duration_time': '0.100000',
        'size': '256',
        'data_hash': 'SHA256:' + 'a' * 64,
    }
    rows = ''.join(
        f'packets.packet.0.{name}={json.dumps(value)}\n'
        for name, value in fields.items()
    )
    slow_ffprobe.write_text(
        '#!/usr/bin/env python3\n'
        'import sys, time\n'
        f'sys.stdout.write({rows!r})\n'
        'print(\'packets.packet.1.pts_time="1.000000"\', flush=True)\n'
        'time.sleep(3)\n',
        encoding='utf-8',
    )
    slow_ffprobe.chmod(0o755)
    from lecture_localizer.verification import _stream_packet_fingerprints

    started = time.monotonic()
    fingerprints = _stream_packet_fingerprints(
        slow_ffprobe,
        Path('ignored-media'),
        '0',
        30.0,
    )
    first = next(fingerprints)
    elapsed = time.monotonic() - started
    assert first == (
        '0.000000',
        '0.000000',
        '0.100000',
        '256',
        'a' * 64,
    )
    assert elapsed < 2.0, elapsed
    cast(Generator[tuple[str, ...], None, None], fingerprints).close()


def test_verify_detects_missing_narration_against_declared_timeline(
    tmp_path: Path,
) -> None:
    require_real_media()
    staging = tmp_path / 'prepared'
    staging.mkdir()
    video = make_media(staging / 'lecture4.mp4', seconds=4.0, use_cache=False)
    narration = encode_narration_aac(staging / 'narration.m4a', seconds=2.0)
    english = write_srt_entries(
        staging / 'english.srt',
        [(0, 2000, 'First half line.'), (2000, 4000, 'Second half line.')],
    )
    russian = write_srt_entries(
        staging / 'russian.srt',
        [
            (0, 2000, 'Первая половина перевода целиком.'),
            (2000, 4000, 'Вторая половина перевода целиком.'),
        ],
    )
    tracks = {
        'video': video,
        'narration': narration,
        'english': english,
        'russian': russian,
    }
    destination = tmp_path / 'staging' / 'short-timeline.mp4'
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = candidate_request(tracks, destination)
    build_candidate(request, Settings())
    report = verify_candidate(
        destination,
        _expectation(tracks, narration_end_ms=4000),
        Settings(),
    )
    assert not report.ok
    duration_check = _check(report, 'narration_duration')
    assert not duration_check.ok
    assert 'short of the declared' in duration_check.detail
    assert duration_check.measured['narration_padding_ms'] == -2000.0


def test_verify_accepts_full_four_second_timeline(tmp_path: Path) -> None:
    require_real_media()
    staging = tmp_path / 'prepared'
    staging.mkdir()
    video = make_media(staging / 'lecture4.mp4', seconds=4.0, use_cache=False)
    narration = encode_narration_aac(staging / 'narration.m4a', seconds=4.0)
    english = write_srt_entries(
        staging / 'english.srt',
        [(0, 2000, 'First half line.'), (2000, 4000, 'Second half line.')],
    )
    russian = write_srt_entries(
        staging / 'russian.srt',
        [
            (0, 2000, 'Первая половина перевода целиком.'),
            (2000, 4000, 'Вторая половина перевода целиком.'),
        ],
    )
    tracks = {
        'video': video,
        'narration': narration,
        'english': english,
        'russian': russian,
    }
    destination = tmp_path / 'staging' / 'full-timeline.mp4'
    destination.parent.mkdir(parents=True, exist_ok=True)
    build_candidate(candidate_request(tracks, destination), Settings())
    report = verify_candidate(
        destination,
        _expectation(tracks, narration_end_ms=4000),
        Settings(),
    )
    assert report.ok, report.failed_checks


def _packet_wrapper(tmp_path: Path, mode: str) -> tuple[Settings, str]:
    """Return settings whose ffprobe faults only packet probes."""
    real_ffprobe = shutil.which('ffprobe')
    assert real_ffprobe is not None
    import os

    wrapper = write_packet_wrapper(tmp_path / 'tooling', real_ffprobe)
    os.environ['FAKE_PACKET_MODE'] = mode
    settings = Settings(
        ffmpeg_path=shutil.which('ffmpeg'),
        ffprobe_path=str(wrapper),
    )
    return settings, mode


_PACKET_CHECKS = (
    'video_packets',
    'narration_packets',
    'copied_original_packets',
)


def test_verify_garbage_packet_output_is_rejected(tmp_path: Path) -> None:
    require_real_media()
    tracks, destination = _candidate(tmp_path)
    settings, _ = _packet_wrapper(tmp_path, 'garbage')
    try:
        report = verify_candidate(destination, _expectation(tracks), settings)
    finally:
        import os

        del os.environ['FAKE_PACKET_MODE']
    assert not report.ok
    for name in _PACKET_CHECKS:
        check = _check(report, name)
        assert not check.ok
        assert 'unusable packet output' in check.detail


def test_verify_empty_packet_output_is_rejected(tmp_path: Path) -> None:
    require_real_media()
    tracks, destination = _candidate(tmp_path)
    settings, _ = _packet_wrapper(tmp_path, 'empty')
    try:
        report = verify_candidate(destination, _expectation(tracks), settings)
    finally:
        import os

        del os.environ['FAKE_PACKET_MODE']
    assert not report.ok
    for name in _PACKET_CHECKS:
        check = _check(report, name)
        assert not check.ok
        assert 'no packets were reported' in check.detail


def test_verify_stalled_packet_output_times_out(tmp_path: Path) -> None:
    require_real_media()
    tracks, destination = _candidate(tmp_path)
    import os
    import time as time_module

    wrapper = write_packet_wrapper(
        tmp_path / 'tooling', shutil.which('ffprobe') or 'ffprobe'
    )
    os.environ['FAKE_PACKET_MODE'] = 'stall'
    settings = Settings(
        ffmpeg_path=shutil.which('ffmpeg'),
        ffprobe_path=str(wrapper),
        subprocess_timeout_seconds=1.0,
    )
    try:
        started = time_module.monotonic()
        report = verify_candidate(destination, _expectation(tracks), settings)
        elapsed = time_module.monotonic() - started
    finally:
        del os.environ['FAKE_PACKET_MODE']
    # Three packet probes hit the 1s deadline each; a stalled child that
    # finished naturally would need at least 15s for the 5s sleeps.
    assert elapsed < 8.0, elapsed
    assert not report.ok
    for name in _PACKET_CHECKS:
        check = _check(report, name)
        assert not check.ok
        assert 'stalled packet output' in check.detail


def test_verify_accepts_mkv_and_mov_sources(tmp_path: Path) -> None:
    require_real_media()
    for extension in ('mkv', 'mov'):
        tracks = prepared_tracks(tmp_path / f'prepared-{extension}')
        alt_video = make_container_copy(
            tracks['video'],
            tmp_path / f'prepared-{extension}' / f'source.{extension}',
        )
        alt_tracks = {**tracks, 'video': alt_video}
        destination = tmp_path / f'staging-{extension}' / 'candidate.mp4'
        destination.parent.mkdir(parents=True, exist_ok=True)
        build_candidate(
            candidate_request(alt_tracks, destination),
            Settings(),
        )
        report = verify_candidate(
            destination,
            _expectation(alt_tracks),
            Settings(),
        )
        assert report.ok, (extension, report.failed_checks)
        for name in ('video_packets', 'copied_original_packets'):
            check = _check(report, name)
            assert check.ok, (extension, check.detail)


@pytest.mark.parametrize('seconds', [1.99, 1.97])
def test_verify_rejects_small_missing_narration_extent(
    tmp_path: Path, seconds: float
) -> None:
    require_real_media()
    tracks = prepared_tracks(tmp_path)
    encode_narration_aac(tracks['narration'], seconds=seconds)
    destination = tmp_path / 'candidate.mp4'
    build_candidate(candidate_request(tracks, destination), Settings())
    report = verify_candidate(destination, _expectation(tracks), Settings())
    assert not report.ok
    assert 'narration_duration' in report.failed_checks


@pytest.mark.parametrize('seconds', [1.99, 1.97])
def test_verify_rejects_small_missing_converted_original_extent(
    tmp_path: Path, seconds: float
) -> None:
    require_real_media()
    tracks = prepared_tracks(tmp_path)
    encode_original_aac(tracks['converted'], seconds=seconds)
    destination = tmp_path / 'candidate.mp4'
    build_candidate(
        candidate_request(tracks, destination, copied_stream_index=None),
        Settings(),
    )
    expectation = CandidateExpectation(
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
    report = verify_candidate(destination, expectation, Settings())
    assert not report.ok
    assert 'original_duration' in report.failed_checks


@pytest.mark.parametrize(
    ('field', 'value'),
    [
        ('pts_time', 'nan'),
        ('pts_time', 'N/A'),
        ('duration_time', 'nan'),
        ('size', '0'),
        ('payload', ''),
        ('payload', 'invalid digest'),
    ],
)
def test_verify_rejects_unusable_packet_values(
    tmp_path: Path, field: str, value: str
) -> None:
    require_real_media()
    tracks, destination = _candidate(tmp_path)
    real_ffprobe = shutil.which('ffprobe')
    assert real_ffprobe is not None
    fields = {
        'pts_time': '0.0',
        'dts_time': '0.0',
        'duration_time': '0.1',
        'size': '256',
        'data': 'packet bytes',
        'data_hash': 'SHA256:' + 'a' * 64,
    }
    if field == 'payload':
        fields['data'] = value
        fields['data_hash'] = value
    else:
        fields[field] = value
    lines = ''.join(
        f'packets.packet.0.{name}={json.dumps(content)}\n'
        for name, content in fields.items()
    )
    wrapper = tmp_path / 'unusable-packet-probe'
    wrapper.write_text(
        '#!/usr/bin/env python3\n'
        'import os, sys\n'
        'if "-show_packets" in sys.argv:\n'
        f'    sys.stdout.write({lines!r})\n'
        'else:\n'
        f'    os.execv({real_ffprobe!r}, '
        f'[{real_ffprobe!r}] + sys.argv[1:])\n',
        encoding='utf-8',
    )
    wrapper.chmod(0o755)
    report = verify_candidate(
        destination,
        _expectation(tracks),
        Settings(ffprobe_path=str(wrapper)),
    )
    assert not report.ok
    for name in _PACKET_CHECKS:
        check = _check(report, name)
        assert not check.ok
        assert 'unusable packet output' in check.detail


def test_packet_deadline_covers_child_after_stdout_closes(
    tmp_path: Path,
) -> None:
    import os

    from lecture_localizer.verification import _stream_packet_fingerprints

    pid_file = tmp_path / 'child.pid'
    fields = {
        'pts_time': '0.0',
        'dts_time': 'N/A',
        'duration_time': '0.1',
        'size': '256',
        'data_hash': 'SHA256:' + 'a' * 64,
    }
    rows = ''.join(
        f'packets.packet.0.{name}={json.dumps(value)}\n'
        for name, value in fields.items()
    )
    wrapper = tmp_path / 'closed-stdout-probe'
    wrapper.write_text(
        '#!/usr/bin/env python3\n'
        'import os, sys, time\n'
        'from pathlib import Path\n'
        f'Path({str(pid_file)!r}).write_text(str(os.getpid()))\n'
        f'sys.stdout.write({rows!r})\n'
        'sys.stdout.flush()\n'
        'os.close(sys.stdout.fileno())\n'
        'time.sleep(5)\n',
        encoding='utf-8',
    )
    wrapper.chmod(0o755)
    started = time.monotonic()
    with pytest.raises(MediaToolError, match='stalled packet'):
        list(_stream_packet_fingerprints(wrapper, Path('ignored'), '0', 1.0))
    assert time.monotonic() - started < 3.0
    assert pid_file.is_file(), 'child startup must be observed by this control'
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)

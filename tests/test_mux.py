"""Tests for explicit five stream candidate assembly."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from container_fixtures import (
    CANNED_TRACK_INFO,
    EN_CUES,
    RU_CUES,
    candidate_request,
    encode_narration_aac,
    make_two_audio_video,
    packet_fingerprints,
    prepared_tracks,
    read_stream_table,
    stream_probe_payload,
    write_fake_tools,
    write_srt_entries,
)

from conftest import require_real_media
from lecture_localizer.media import MediaToolError
from lecture_localizer.mux import (
    ConvertedAudioTrack,
    CopiedAudioStream,
    MuxError,
    MuxRequest,
    TrackIds,
    TrackProfile,
    build_candidate,
)
from lecture_localizer.settings import Settings


def _stub_inputs(tmp_path: Path) -> dict[str, Path]:
    """Create empty stand in input files for command building tests."""
    staging = tmp_path / 'inputs'
    staging.mkdir(exist_ok=True)
    paths = {
        'video': staging / 'video.mp4',
        'converted': staging / 'original.m4a',
        'narration': staging / 'narration.m4a',
        'english': staging / 'english.srt',
        'russian': staging / 'russian.srt',
    }
    for path in paths.values():
        path.write_bytes(b'stub input bytes')
    return paths


def _stub_request(paths: dict[str, Path], destination: Path) -> MuxRequest:
    """Build a copied audio request over stub inputs."""
    return MuxRequest(
        video_path=paths['video'],
        original_audio=CopiedAudioStream(stream_index=2),
        narration_path=paths['narration'],
        english_srt_path=paths['english'],
        russian_srt_path=paths['russian'],
        destination=destination,
        profile=TrackProfile(),
    )


def _fake_settings(tools: dict[str, Path]) -> Settings:
    """Return settings that resolve the fake tool scripts."""
    return Settings(
        ffmpeg_path=str(tools['ffmpeg']),
        ffprobe_path=str(tools['ffprobe']),
        mp4box_path=str(tools['MP4Box']),
    )


def _write_payloads(
    tmp_path: Path,
    *,
    streams: dict[str, object] | None = None,
    info: str = CANNED_TRACK_INFO,
) -> dict[str, Path]:
    """Write the canned ffprobe and MP4Box payload files."""
    payload_dir = tmp_path / 'payloads'
    payload_dir.mkdir(exist_ok=True)
    stream_path = payload_dir / 'streams.json'
    stream_path.write_text(
        json.dumps(
            streams
            if streams is not None
            else stream_probe_payload(video_index=1, audio_indexes=(0, 2))
        ),
        encoding='utf-8',
    )
    packets_path = payload_dir / 'packets.json'
    packets_path.write_text('{"packets": []}', encoding='utf-8')
    info_path = payload_dir / 'info.txt'
    info_path.write_text(info, encoding='utf-8')
    return {
        'streams': stream_path,
        'packets': packets_path,
        'info': info_path,
    }


@pytest.fixture
def fake_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """Wire fake tools, payloads and argv dumps into the environment."""
    tools = write_fake_tools(tmp_path)
    payloads = _write_payloads(tmp_path)
    dump = tmp_path / 'argv-dump'
    dump.write_text('', encoding='utf-8')
    monkeypatch.setenv('FAKE_ARGV_DUMP', str(dump))
    monkeypatch.setenv('FAKE_STREAMS_PAYLOAD', str(payloads['streams']))
    monkeypatch.setenv('FAKE_PACKETS_PAYLOAD', str(payloads['packets']))
    monkeypatch.setenv('FAKE_INFO_PAYLOAD', str(payloads['info']))
    return {'tools': tools, 'dump': dump}


def _invocations(dump: Path, tool: str) -> list[list[str]]:
    """Return every dumped argv of one tool in call order."""
    invocations: list[tuple[str, list[str]]] = []
    for line in dump.read_text(encoding='utf-8').splitlines():
        name = Path(line).name
        if name in ('ffmpeg', 'ffprobe', 'MP4Box'):
            invocations.append((name, []))
            continue
        assert invocations, f'argv outside a tool call: {line!r}'
        invocations[-1][1].append(line)
    return [argv for name, argv in invocations if name == tool]


def _flag_pass(dump: Path) -> list[str]:
    """Return the MP4Box invocation that applies track flags."""
    passes = [
        argv
        for argv in _invocations(dump, 'MP4Box')
        if '-enable' in argv or '-disable' in argv
    ]
    assert len(passes) == 1
    return passes[0]


def _mapped_values(argv: list[str], flag: str) -> list[str]:
    """Return every value that follows one flag."""
    return [
        argv[index + 1] for index, value in enumerate(argv) if value == flag
    ]


def test_build_candidate_maps_explicit_streams(
    tmp_path: Path, fake_env: dict[str, Any]
) -> None:
    paths = _stub_inputs(tmp_path)
    destination = tmp_path / 'candidate.mp4'
    result = build_candidate(
        _stub_request(paths, destination), _fake_settings(fake_env['tools'])
    )
    ffmpeg = _invocations(fake_env['dump'], 'ffmpeg')[0]
    assert _mapped_values(ffmpeg, '-map') == [
        '0:1',
        '0:2',
        '1:0',
        '2:0',
        '3:0',
    ]
    assert _mapped_values(ffmpeg, '-c:v') == ['copy']
    assert _mapped_values(ffmpeg, '-c:a') == ['copy']
    assert _mapped_values(ffmpeg, '-c:s') == ['mov_text']
    assert _mapped_values(ffmpeg, '-metadata:s:a:0') == ['language=eng']
    assert _mapped_values(ffmpeg, '-metadata:s:a:1') == ['language=rus']
    assert _mapped_values(ffmpeg, '-metadata:s:s:0') == ['language=eng']
    assert _mapped_values(ffmpeg, '-metadata:s:s:1') == ['language=rus']
    assert _mapped_values(ffmpeg, '-disposition:a:0') == ['0']
    assert _mapped_values(ffmpeg, '-disposition:a:1') == ['0']
    assert _mapped_values(ffmpeg, '-disposition:s:0') == ['0']
    assert _mapped_values(ffmpeg, '-disposition:s:1') == ['0']
    assert ffmpeg[-1] == str(destination)
    flag_pass = _flag_pass(fake_env['dump'])
    assert _mapped_values(flag_pass, '-enable') == ['5']
    assert _mapped_values(flag_pass, '-disable') == ['3', '9', '2']
    assert result.track_ids == TrackIds(
        video=7,
        original_audio=3,
        narration=5,
        english_subtitles=9,
        russian_subtitles=2,
    )
    assert destination.is_file()
    assert destination.read_bytes() == b''


def test_build_candidate_maps_converted_original_input(
    tmp_path: Path, fake_env: dict[str, Any]
) -> None:
    paths = _stub_inputs(tmp_path)
    destination = tmp_path / 'candidate.mp4'
    request = MuxRequest(
        video_path=paths['video'],
        original_audio=ConvertedAudioTrack(path=paths['converted']),
        narration_path=paths['narration'],
        english_srt_path=paths['english'],
        russian_srt_path=paths['russian'],
        destination=destination,
        profile=TrackProfile(),
    )
    build_candidate(request, _fake_settings(fake_env['tools']))
    ffmpeg = _invocations(fake_env['dump'], 'ffmpeg')[0]
    assert _mapped_values(ffmpeg, '-map') == [
        '0:1',
        '1:0',
        '2:0',
        '3:0',
        '4:0',
    ]
    assert _mapped_values(ffmpeg, '-i') == [
        str(paths['video']),
        str(paths['converted']),
        str(paths['narration']),
        str(paths['english']),
        str(paths['russian']),
    ]


def test_build_candidate_honors_profile_flags(
    tmp_path: Path, fake_env: dict[str, Any]
) -> None:
    paths = _stub_inputs(tmp_path)
    destination = tmp_path / 'candidate.mp4'
    request = MuxRequest(
        video_path=paths['video'],
        original_audio=CopiedAudioStream(stream_index=2),
        narration_path=paths['narration'],
        english_srt_path=paths['english'],
        russian_srt_path=paths['russian'],
        destination=destination,
        profile=TrackProfile(
            narration_default=False,
            original_default=True,
            subtitles_default=True,
        ),
    )
    build_candidate(request, _fake_settings(fake_env['tools']))
    flag_pass = _flag_pass(fake_env['dump'])
    assert _mapped_values(flag_pass, '-enable') == ['3', '9', '2']
    assert _mapped_values(flag_pass, '-disable') == ['5']


def test_build_candidate_rejects_existing_destination(
    tmp_path: Path, fake_env: dict[str, Any]
) -> None:
    paths = _stub_inputs(tmp_path)
    destination = tmp_path / 'candidate.mp4'
    destination.write_bytes(b'existing destination bytes')
    with pytest.raises(MuxError) as error:
        build_candidate(
            _stub_request(paths, destination),
            _fake_settings(fake_env['tools']),
        )
    assert error.value.kind == 'destination'
    assert destination.read_bytes() == b'existing destination bytes'
    assert _invocations(fake_env['dump'], 'ffmpeg') == []


def test_build_candidate_rejects_missing_input(
    tmp_path: Path, fake_env: dict[str, Any]
) -> None:
    paths = _stub_inputs(tmp_path)
    paths['narration'].unlink()
    with pytest.raises(MuxError) as error:
        build_candidate(
            _stub_request(paths, tmp_path / 'candidate.mp4'),
            _fake_settings(fake_env['tools']),
        )
    assert error.value.kind == 'input'


def test_build_candidate_rejects_absent_copied_stream(
    tmp_path: Path, fake_env: dict[str, Any]
) -> None:
    paths = _stub_inputs(tmp_path)
    request = MuxRequest(
        video_path=paths['video'],
        original_audio=CopiedAudioStream(stream_index=9),
        narration_path=paths['narration'],
        english_srt_path=paths['english'],
        russian_srt_path=paths['russian'],
        destination=tmp_path / 'candidate.mp4',
        profile=TrackProfile(),
    )
    with pytest.raises(MuxError) as error:
        build_candidate(request, _fake_settings(fake_env['tools']))
    assert error.value.kind == 'input'


def test_build_candidate_cleans_partial_candidate_on_ffmpeg_failure(
    tmp_path: Path,
    fake_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _stub_inputs(tmp_path)
    destination = tmp_path / 'candidate.mp4'
    monkeypatch.setenv('FAKE_FFMPEG_EXIT', '2')
    with pytest.raises(MediaToolError):
        build_candidate(
            _stub_request(paths, destination),
            _fake_settings(fake_env['tools']),
        )
    assert not destination.exists()


def test_build_candidate_cleans_partial_candidate_on_mp4box_failure(
    tmp_path: Path,
    fake_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _stub_inputs(tmp_path)
    destination = tmp_path / 'candidate.mp4'
    monkeypatch.setenv('FAKE_MP4BOX_EXIT', '3')
    with pytest.raises(MediaToolError):
        build_candidate(
            _stub_request(paths, destination),
            _fake_settings(fake_env['tools']),
        )
    assert not destination.exists()


def test_build_candidate_cleans_partial_candidate_on_ffprobe_failure(
    tmp_path: Path,
    fake_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _stub_inputs(tmp_path)
    destination = tmp_path / 'candidate.mp4'
    monkeypatch.setenv('FAKE_FFPROBE_EXIT', '1')
    with pytest.raises(MediaToolError):
        build_candidate(
            _stub_request(paths, destination),
            _fake_settings(fake_env['tools']),
        )
    assert not destination.exists()


def test_build_candidate_rejects_unexpected_track_layout(
    tmp_path: Path, fake_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _stub_inputs(tmp_path)
    destination = tmp_path / 'candidate.mp4'
    info_path = tmp_path / 'payloads' / 'info.txt'
    info_path.write_text(
        '# Track 1 Info - ID 1 - TimeScale 100\nMedia Type: vide:avc1\n'
        '# Track 2 Info - ID 2 - TimeScale 100\nMedia Type: soun:mp4a\n',
        encoding='utf-8',
    )
    with pytest.raises(MuxError) as error:
        build_candidate(
            _stub_request(paths, destination),
            _fake_settings(fake_env['tools']),
        )
    assert error.value.kind == 'track-flags'
    assert not destination.exists()


def test_build_candidate_rejects_forced_subtitle_profile(
    tmp_path: Path, fake_env: dict[str, Any]
) -> None:
    paths = _stub_inputs(tmp_path)
    request = MuxRequest(
        video_path=paths['video'],
        original_audio=CopiedAudioStream(stream_index=2),
        narration_path=paths['narration'],
        english_srt_path=paths['english'],
        russian_srt_path=paths['russian'],
        destination=tmp_path / 'candidate.mp4',
        profile=TrackProfile(subtitles_forced=True),
    )
    with pytest.raises(MuxError) as error:
        build_candidate(request, _fake_settings(fake_env['tools']))
    assert error.value.kind == 'profile'


def test_build_candidate_real_persists_five_streams_and_flags(
    tmp_path: Path,
) -> None:
    require_real_media()
    tracks = prepared_tracks(tmp_path)
    destination = tmp_path / 'staging' / 'candidate.mp4'
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = build_candidate(
        candidate_request(tracks, destination), Settings()
    )
    assert destination.is_file()
    rows = read_stream_table(destination)
    assert [(row['codec_type'], row['codec_name']) for row in rows] == [
        ('video', 'h264'),
        ('audio', 'aac'),
        ('audio', 'aac'),
        ('subtitle', 'mov_text'),
        ('subtitle', 'mov_text'),
    ]
    assert [row['language'] for row in rows[1:]] == [
        'eng',
        'rus',
        'eng',
        'rus',
    ]
    assert [row['default'] for row in rows] == [1, 0, 1, 0, 0]
    assert [row['forced'] for row in rows] == [0, 0, 0, 0, 0]
    narration_row = rows[2]
    assert narration_row['profile'] == 'LC'
    assert narration_row['channels'] == 1
    assert narration_row['sample_rate'] == '48000'
    assert result.track_ids == TrackIds(
        video=1,
        original_audio=2,
        narration=3,
        english_subtitles=4,
        russian_subtitles=5,
    )


def test_build_candidate_real_copies_selected_second_audio_stream(
    tmp_path: Path,
) -> None:
    require_real_media()
    staging = tmp_path / 'staging'
    staging.mkdir()
    video = make_two_audio_video(staging / 'two-audio.mp4')
    narration = encode_narration_aac(staging / 'narration.m4a')
    english = write_srt_entries(staging / 'english.srt', EN_CUES)
    russian = write_srt_entries(staging / 'russian.srt', RU_CUES)
    request = MuxRequest(
        video_path=video,
        original_audio=CopiedAudioStream(stream_index=2),
        narration_path=narration,
        english_srt_path=english,
        russian_srt_path=russian,
        destination=staging / 'candidate.mp4',
        profile=TrackProfile(),
    )
    build_candidate(request, Settings())
    selected = packet_fingerprints(staging / 'candidate.mp4', 'a:0')
    assert selected == packet_fingerprints(video, '2')
    assert selected != packet_fingerprints(video, '1')


def test_build_candidate_real_uses_converted_original_track(
    tmp_path: Path,
) -> None:
    require_real_media()
    tracks = prepared_tracks(tmp_path)
    destination = tmp_path / 'staging' / 'converted-candidate.mp4'
    destination.parent.mkdir(parents=True, exist_ok=True)
    build_candidate(
        candidate_request(tracks, destination, copied_stream_index=None),
        Settings(),
    )
    rows = read_stream_table(destination)
    assert rows[1]['codec_name'] == 'aac'
    assert rows[1]['channels'] == 2
    assert rows[1]['sample_rate'] == '48000'
    assert [row['default'] for row in rows] == [1, 0, 1, 0, 0]


def test_build_candidate_real_preserves_inputs_and_special_paths(
    tmp_path: Path,
) -> None:
    require_real_media()
    tracks = prepared_tracks(tmp_path)
    input_hashes = {
        name: path.read_bytes()
        for name, path in tracks.items()
        if isinstance(path, Path)
    }
    staging = tmp_path / 'staging dir #42 и пробел'
    staging.mkdir()
    destination = staging / 'кандидат #1.mp4'
    build_candidate(candidate_request(tracks, destination), Settings())
    assert destination.is_file()
    for name, path in tracks.items():
        if isinstance(path, Path):
            assert path.read_bytes() == input_hashes[name], name

"""Tests for normalization, fitting, timeline streaming and AAC."""

from __future__ import annotations

import errno
import shutil
from pathlib import Path
from typing import Any, cast

import audio_testsupport as ats
import pytest

from conftest import require_real_media, sha256
from lecture_localizer import audio as audio_module
from lecture_localizer.audio import (
    PRIMING_TOLERANCE_FRAMES,
    AudioError,
    NormalizedSpeech,
    SegmentPlacement,
    encode_narration_aac,
    fit_segment,
    normalize_wav,
    prepare_original_audio,
    stream_timeline,
)
from lecture_localizer.settings import DEFAULT_FITTING_MAX_SPEEDUP

_TIMEOUT = 120.0


@pytest.fixture(scope='session')
def tools() -> dict[str, str]:
    """Resolve real ffmpeg and ffprobe paths for native checks."""
    require_real_media()
    return {
        'ffmpeg_path': str(shutil.which('ffmpeg')),
        'ffprobe_path': str(shutil.which('ffprobe')),
    }


def _speech(
    tmp_path: Path,
    name: str,
    frames: int,
    *,
    decay: bool = False,
    noise: bool = False,
) -> NormalizedSpeech:
    """Write deterministic WAV and PCM fixtures for one speech unit."""
    if noise:
        pcm = ats.noise_pcm_bytes(frames)
    else:
        pcm = ats.pcm_bytes(frames, decay_to_quiet=decay)
    wav = ats.write_wav(tmp_path / f'{name}.wav', pcm)
    raw = ats.write_pcm(tmp_path / f'{name}.pcm', pcm)
    return ats.speech_record(wav, raw)


def _place(
    segment_id: str,
    start_ms: int,
    end_ms: int,
    speech: NormalizedSpeech,
) -> SegmentPlacement:
    """Build one placement request."""
    return SegmentPlacement(
        segment_id=segment_id,
        start_ms=start_ms,
        end_ms=end_ms,
        speech=speech,
    )


def _expected_timeline(
    total_frames: int, parts: list[tuple[int, bytes]]
) -> bytes:
    """Build the exact expected timeline bytes from placements."""
    out = bytearray()
    cursor = 0
    for start_frame, pcm in parts:
        out += b'\x00' * ((start_frame - cursor) * 2)
        out += pcm
        cursor = start_frame + len(pcm) // 2
    out += b'\x00' * ((total_frames - cursor) * 2)
    return bytes(out)


def _assert_no_partials(directory: Path) -> None:
    """Assert a directory holds no disposable partial outputs."""
    leftovers = [
        entry.name
        for entry in directory.iterdir()
        if entry.name.endswith('.partial')
    ]
    assert leftovers == []


def test_normalize_wav_reports_exact_frames_and_preserves_raw_bytes(
    tools: dict[str, str], tmp_path: Path
) -> None:
    pcm = ats.pcm_bytes(48_000)
    raw = ats.write_wav(tmp_path / 'raw.wav', pcm)
    raw_digest = sha256(raw)
    record = normalize_wav(
        raw,
        tmp_path / 'norm.pcm',
        ffmpeg_path=tools['ffmpeg_path'],
        timeout_seconds=_TIMEOUT,
    )
    assert record.frames == 48_000
    assert record.sample_rate == 48_000
    assert record.channels == 1
    assert record.sample_format == 's16le'
    assert record.path.stat().st_size == 96_000
    assert record.raw_sha256 == raw_digest
    assert sha256(raw) == raw_digest
    assert record.sha256 == sha256(record.path)


def test_normalize_wav_rejects_missing_corrupt_and_empty_speech(
    tools: dict[str, str], tmp_path: Path
) -> None:
    destination = tmp_path / 'norm.pcm'
    with pytest.raises(AudioError, match='missing') as missing:
        normalize_wav(
            tmp_path / 'absent.wav',
            destination,
            ffmpeg_path=tools['ffmpeg_path'],
            timeout_seconds=_TIMEOUT,
        )
    assert missing.value.kind == 'speech'
    corrupt = tmp_path / 'corrupt.wav'
    corrupt.write_bytes(b'definitely not a wave file payload')
    with pytest.raises(AudioError, match='not a RIFF WAVE') as broken:
        normalize_wav(
            corrupt,
            destination,
            ffmpeg_path=tools['ffmpeg_path'],
            timeout_seconds=_TIMEOUT,
        )
    assert broken.value.kind == 'speech'
    empty = ats.write_wav(tmp_path / 'empty.wav', b'')
    with pytest.raises(AudioError, match='zero frames') as silent:
        normalize_wav(
            empty,
            destination,
            ffmpeg_path=tools['ffmpeg_path'],
            timeout_seconds=_TIMEOUT,
        )
    assert silent.value.kind == 'speech'
    assert not destination.exists()
    _assert_no_partials(tmp_path)


def test_normalize_wav_requires_complete_wav_structure(
    tools: dict[str, str], tmp_path: Path
) -> None:
    pcm = ats.pcm_bytes(48_000)
    complete = ats.write_wav(tmp_path / 'complete.wav', pcm)
    record = normalize_wav(
        complete,
        tmp_path / 'norm-complete.pcm',
        ffmpeg_path=tools['ffmpeg_path'],
        timeout_seconds=_TIMEOUT,
    )
    assert record.frames == 48_000
    destination = tmp_path / 'norm-truncated.pcm'
    truncated = tmp_path / 'truncated.wav'
    truncated.write_bytes(complete.read_bytes()[:-48_000])
    with pytest.raises(AudioError, match='truncated') as broken:
        normalize_wav(
            truncated,
            destination,
            ffmpeg_path=tools['ffmpeg_path'],
            timeout_seconds=_TIMEOUT,
        )
    assert broken.value.kind == 'speech'
    assert not destination.exists()


def test_normalize_wav_refuses_existing_destination_and_alias(
    tools: dict[str, str], tmp_path: Path
) -> None:
    raw = ats.write_wav(tmp_path / 'raw.wav', ats.pcm_bytes(4_800))
    destination = tmp_path / 'norm.pcm'
    destination.write_bytes(b'existing bytes')
    with pytest.raises(AudioError, match='already exists') as taken:
        normalize_wav(
            raw,
            destination,
            ffmpeg_path=tools['ffmpeg_path'],
            timeout_seconds=_TIMEOUT,
        )
    assert taken.value.kind == 'destination'
    assert destination.read_bytes() == b'existing bytes'
    with pytest.raises(AudioError, match='one location') as aliased:
        normalize_wav(
            raw,
            raw,
            ffmpeg_path=tools['ffmpeg_path'],
            timeout_seconds=_TIMEOUT,
        )
    assert aliased.value.kind == 'destination'
    _assert_no_partials(tmp_path)


def test_window_rejects_nonpositive_and_noninteger_milliseconds(
    tmp_path: Path,
) -> None:
    speech = _speech(tmp_path, 'short', 2_400)
    work_dir = tmp_path / 'work'
    cases: list[tuple[Any, int, str]] = [
        (-1, 1_000, 'nonnegative'),
        (1_000, 1_000, 'positive span'),
        (2_000, 1_000, 'positive span'),
        (10.5, 1_000, 'integer millisecond'),
        (True, 1_000, 'integer millisecond'),
    ]
    for start_ms, end_ms, expected in cases:
        placement = _place(
            's001',
            cast(Any, start_ms),
            end_ms,
            speech,
        )
        with pytest.raises(AudioError, match=expected) as bad:
            fit_segment(
                placement,
                max_speedup=DEFAULT_FITTING_MAX_SPEEDUP,
                work_dir=work_dir,
                ffmpeg_path='unused-ffmpeg',
                timeout_seconds=_TIMEOUT,
            )
        assert bad.value.kind == 'window'
    assert not work_dir.exists()


def test_fit_shorter_speech_keeps_normal_speed(tmp_path: Path) -> None:
    speech = _speech(tmp_path, 'half', 24_000)
    fit = fit_segment(
        _place('s001', 1_000, 2_000, speech),
        max_speedup=DEFAULT_FITTING_MAX_SPEEDUP,
        work_dir=tmp_path / 'work',
        ffmpeg_path='unused-ffmpeg',
        timeout_seconds=_TIMEOUT,
    )
    assert fit.disposition == 'normal'
    assert fit.required_speedup == 1.0
    assert fit.window_frames == 48_000
    assert fit.placed_frames == 24_000
    assert fit.pcm_path == speech.path
    assert fit.pcm_sha256 == speech.sha256
    equal = _speech(tmp_path, 'exact', 48_000)
    fit_equal = fit_segment(
        _place('s002', 0, 1_000, equal),
        max_speedup=DEFAULT_FITTING_MAX_SPEEDUP,
        work_dir=tmp_path / 'work',
        ffmpeg_path='unused-ffmpeg',
        timeout_seconds=_TIMEOUT,
    )
    assert fit_equal.disposition == 'normal'
    assert fit_equal.placed_frames == 48_000


def test_fit_refuses_excessive_speedup_before_fitting(
    tmp_path: Path,
) -> None:
    speech = _speech(tmp_path, 'too-long', 58_000)
    work_dir = tmp_path / 'work'
    fit = fit_segment(
        _place('s001', 0, 1_000, speech),
        max_speedup=DEFAULT_FITTING_MAX_SPEEDUP,
        work_dir=work_dir,
        ffmpeg_path='unused-ffmpeg',
        timeout_seconds=_TIMEOUT,
    )
    assert fit.disposition == 'refused'
    assert fit.required_speedup > DEFAULT_FITTING_MAX_SPEEDUP
    assert fit.measured_frames is None
    assert fit.refusal is not None
    assert 'exceeds the configured maximum' in fit.refusal
    assert fit.refusal.startswith('required speedup 1.2')
    assert not work_dir.exists()


def test_fit_at_exact_limit_overrun_refuses_without_trimming(
    tools: dict[str, str], tmp_path: Path
) -> None:
    speech = _speech(tmp_path, 'limit', 57_600)
    work_dir = tmp_path / 'work'
    fit = fit_segment(
        _place('s001', 0, 1_000, speech),
        max_speedup=DEFAULT_FITTING_MAX_SPEEDUP,
        work_dir=work_dir,
        ffmpeg_path=tools['ffmpeg_path'],
        timeout_seconds=_TIMEOUT,
    )
    assert fit.disposition == 'refused'
    assert fit.required_speedup == DEFAULT_FITTING_MAX_SPEEDUP
    assert fit.measured_frames is not None
    assert 48_000 < fit.measured_frames <= 48_600
    assert fit.refusal is not None
    assert 'overruns the 48000 frame window' in fit.refusal
    assert 'without trimming' in fit.refusal
    assert list(work_dir.iterdir()) == []
    assert speech.path.stat().st_size == 115_200


def test_fit_small_post_fit_overrun_refuses_at_limit(
    tools: dict[str, str], tmp_path: Path
) -> None:
    speech = _speech(tmp_path, 'near', 50_400)
    work_dir = tmp_path / 'work'
    fit = fit_segment(
        _place('s001', 0, 1_000, speech),
        max_speedup=DEFAULT_FITTING_MAX_SPEEDUP,
        work_dir=work_dir,
        ffmpeg_path=tools['ffmpeg_path'],
        timeout_seconds=_TIMEOUT,
    )
    assert fit.disposition == 'refused'
    assert fit.required_speedup == 1.05
    assert fit.measured_frames is not None
    assert 48_000 < fit.measured_frames <= 48_100
    assert fit.refusal is not None
    assert 'without trimming' in fit.refusal
    assert list(work_dir.iterdir()) == []


def test_fit_within_limit_places_fitted_artifact_with_exact_hash(
    tools: dict[str, str], tmp_path: Path
) -> None:
    speech = _speech(tmp_path, 'long', 52_000)
    work_dir = tmp_path / 'work'
    fit = fit_segment(
        _place('s001', 0, 1_000, speech),
        max_speedup=DEFAULT_FITTING_MAX_SPEEDUP,
        work_dir=work_dir,
        ffmpeg_path=tools['ffmpeg_path'],
        timeout_seconds=_TIMEOUT,
    )
    assert fit.disposition == 'fitted'
    assert fit.required_speedup == pytest.approx(52_000 / 48_000)
    assert fit.placed_frames <= fit.window_frames == 48_000
    assert fit.placed_frames >= 47_000
    assert fit.pcm_path is not None
    assert fit.pcm_path.stat().st_size == fit.placed_frames * 2
    assert fit.pcm_sha256 == sha256(fit.pcm_path)


def test_single_segment_timeline_preserves_quiet_end_exactly(
    tmp_path: Path,
) -> None:
    pcm = ats.pcm_bytes(24_000, decay_to_quiet=True)
    wav = ats.write_wav(tmp_path / 's1.wav', pcm)
    raw = ats.write_pcm(tmp_path / 's1.pcm', pcm)
    speech = ats.speech_record(wav, raw)
    destination = tmp_path / 'timeline.pcm'
    result = stream_timeline(
        [_place('s001', 1_000, 2_000, speech)],
        total_frames=96_000,
        destination=destination,
        max_speedup=DEFAULT_FITTING_MAX_SPEEDUP,
        work_dir=tmp_path / 'work',
        ffmpeg_path='unused-ffmpeg',
        timeout_seconds=_TIMEOUT,
    )
    assert result.frames == 96_000
    assert result.sha256 == sha256(destination)
    expected = _expected_timeline(96_000, [(48_000, pcm)])
    placed = destination.read_bytes()
    assert placed == expected
    assert placed[48_000 * 2 : 48_000 * 2 + len(pcm)] == pcm


def test_multiple_segment_timeline_preserves_gaps_and_boundaries(
    tmp_path: Path,
) -> None:
    pcm_one = ats.pcm_bytes(20_000, frequency_hz=440)
    pcm_two = ats.pcm_bytes(30_000, frequency_hz=550)
    speech_one = ats.speech_record(
        ats.write_wav(tmp_path / 'one.wav', pcm_one),
        ats.write_pcm(tmp_path / 'one.pcm', pcm_one),
    )
    speech_two = ats.speech_record(
        ats.write_wav(tmp_path / 'two.wav', pcm_two),
        ats.write_pcm(tmp_path / 'two.pcm', pcm_two),
    )
    destination = tmp_path / 'timeline.pcm'
    result = stream_timeline(
        [
            _place('s001', 0, 1_000, speech_one),
            _place('s002', 1_500, 2_500, speech_two),
        ],
        total_frames=144_000,
        destination=destination,
        max_speedup=DEFAULT_FITTING_MAX_SPEEDUP,
        work_dir=tmp_path / 'work',
        ffmpeg_path='unused-ffmpeg',
        timeout_seconds=_TIMEOUT,
    )
    assert result.frames == 144_000
    expected = _expected_timeline(
        144_000,
        [(0, pcm_one), (72_000, pcm_two)],
    )
    assert destination.read_bytes() == expected
    assert [fit.segment_id for fit in result.placements] == ['s001', 's002']


def test_timeline_rejects_bad_windows_and_timeline_lengths(
    tmp_path: Path,
) -> None:
    speech = _speech(tmp_path, 'unit', 4_800)
    destination = tmp_path / 'timeline.pcm'
    overlapping = [
        _place('s001', 0, 1_000, speech),
        _place('s002', 500, 2_000, speech),
    ]
    with pytest.raises(AudioError, match='overlaps or precedes') as bad:
        stream_timeline(
            overlapping,
            total_frames=144_000,
            destination=destination,
            max_speedup=DEFAULT_FITTING_MAX_SPEEDUP,
            work_dir=tmp_path / 'work',
            ffmpeg_path='unused-ffmpeg',
            timeout_seconds=_TIMEOUT,
        )
    assert bad.value.kind == 'window'
    beyond = [_place('s001', 1_000, 2_000, speech)]
    with pytest.raises(AudioError, match='beyond the 91200 frame') as far:
        stream_timeline(
            beyond,
            total_frames=91_200,
            destination=destination,
            max_speedup=DEFAULT_FITTING_MAX_SPEEDUP,
            work_dir=tmp_path / 'work',
            ffmpeg_path='unused-ffmpeg',
            timeout_seconds=_TIMEOUT,
        )
    assert far.value.kind == 'window'
    for total in (0, -5, 96000.5, True):
        with pytest.raises(
            AudioError, match='timeline frame count'
        ) as invalid:
            stream_timeline(
                [],
                total_frames=cast(Any, total),
                destination=destination,
                max_speedup=DEFAULT_FITTING_MAX_SPEEDUP,
                work_dir=tmp_path / 'work',
                ffmpeg_path='unused-ffmpeg',
                timeout_seconds=_TIMEOUT,
            )
        assert invalid.value.kind == 'window'
    assert not destination.exists()


def test_timeline_refusal_reports_every_segment_without_destination(
    tools: dict[str, str], tmp_path: Path
) -> None:
    first = _speech(tmp_path, 'first', 58_000)
    second = _speech(tmp_path, 'second', 59_000)
    work_dir = tmp_path / 'work'
    destination = tmp_path / 'timeline.pcm'
    with pytest.raises(AudioError, match='timeline refused') as refused:
        stream_timeline(
            [
                _place('s001', 0, 1_000, first),
                _place('s002', 1_000, 2_000, second),
            ],
            total_frames=144_000,
            destination=destination,
            max_speedup=DEFAULT_FITTING_MAX_SPEEDUP,
            work_dir=work_dir,
            ffmpeg_path=tools['ffmpeg_path'],
            timeout_seconds=_TIMEOUT,
        )
    assert refused.value.kind == 'fit'
    message = str(refused.value)
    assert 's001' in message and 's002' in message
    assert not destination.exists()
    _assert_no_partials(tmp_path)
    assert not work_dir.exists() or list(work_dir.iterdir()) == []
    assert first.raw_sha256 == sha256(first.raw_path)


def test_timeline_refuses_existing_destination_without_touching_it(
    tmp_path: Path,
) -> None:
    speech = _speech(tmp_path, 'unit', 4_800)
    destination = tmp_path / 'timeline.pcm'
    destination.write_bytes(b'untouched')
    with pytest.raises(AudioError, match='already exists'):
        stream_timeline(
            [_place('s001', 0, 100, speech)],
            total_frames=4_800,
            destination=destination,
            max_speedup=DEFAULT_FITTING_MAX_SPEEDUP,
            work_dir=tmp_path / 'work',
            ffmpeg_path='unused-ffmpeg',
            timeout_seconds=_TIMEOUT,
        )
    assert destination.read_bytes() == b'untouched'
    _assert_no_partials(tmp_path)


def test_enospc_streaming_cleans_partial_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    speech = _speech(tmp_path, 'unit', 4_800)

    def failing_silence(handle: Any, frames: int) -> None:
        raise OSError(errno.ENOSPC, 'No space left on device')

    monkeypatch.setattr(audio_module, '_write_silence', failing_silence)
    destination = tmp_path / 'timeline.pcm'
    with pytest.raises(AudioError, match='streaming failed') as full:
        stream_timeline(
            [_place('s001', 0, 100, speech)],
            total_frames=4_800,
            destination=destination,
            max_speedup=DEFAULT_FITTING_MAX_SPEEDUP,
            work_dir=tmp_path / 'work',
            ffmpeg_path='unused-ffmpeg',
            timeout_seconds=_TIMEOUT,
        )
    assert full.value.kind == 'storage'
    assert 'No space left' in str(full.value)
    assert not destination.exists()
    _assert_no_partials(tmp_path)
    assert sha256(speech.raw_path) == speech.raw_sha256


def test_interrupted_streaming_cleans_partial_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    speech = _speech(tmp_path, 'unit', 4_800)

    def interrupting_copy(handle: Any, source: Any, frames: int) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(audio_module, '_copy_pcm', interrupting_copy)
    destination = tmp_path / 'timeline.pcm'
    with pytest.raises(KeyboardInterrupt):
        stream_timeline(
            [_place('s001', 0, 100, speech)],
            total_frames=4_800,
            destination=destination,
            max_speedup=DEFAULT_FITTING_MAX_SPEEDUP,
            work_dir=tmp_path / 'work',
            ffmpeg_path='unused-ffmpeg',
            timeout_seconds=_TIMEOUT,
        )
    assert not destination.exists()
    _assert_no_partials(tmp_path)


def test_large_timeline_streams_exact_frame_count(tmp_path: Path) -> None:
    speech = _speech(tmp_path, 'unit', 480)
    destination = tmp_path / 'timeline.pcm'
    total = 14_400_000
    result = stream_timeline(
        [_place('s001', 60_000, 60_010, speech)],
        total_frames=total,
        destination=destination,
        max_speedup=DEFAULT_FITTING_MAX_SPEEDUP,
        work_dir=tmp_path / 'work',
        ffmpeg_path='unused-ffmpeg',
        timeout_seconds=_TIMEOUT,
    )
    assert result.frames == total
    data = destination.read_bytes()
    assert len(data) == total * 2
    start_frame = 60_000 * 48
    assert data[: start_frame * 2] == b'\x00' * (start_frame * 2)
    assert data[start_frame * 2 : start_frame * 2 + 960] == ats.pcm_bytes(480)


def test_encode_narration_aac_meets_profile_with_priming_accounting(
    tools: dict[str, str], tmp_path: Path
) -> None:
    pcm = ats.noise_pcm_bytes(144_000)
    source = ats.write_pcm(tmp_path / 'narration.pcm', pcm)
    destination = tmp_path / 'narration.m4a'
    result = encode_narration_aac(
        source,
        destination,
        ffmpeg_path=tools['ffmpeg_path'],
        ffprobe_path=tools['ffprobe_path'],
        timeout_seconds=_TIMEOUT,
    )
    assert result.disposition == 'converted'
    assert result.codec == 'aac'
    assert result.profile == 'LC'
    assert result.channels == 1
    assert result.sample_rate == 48_000
    assert result.bit_rate is not None
    assert abs(result.bit_rate - 192_000) <= 192_000 * 0.05
    assert result.input_frames == 144_000
    assert result.sha256 == sha256(destination)
    facts = ats.audio_facts(tools['ffprobe_path'], destination)
    assert facts['codec_name'] == 'aac'
    assert facts['profile'] == 'LC'
    decoded = ats.decoded_frames(
        tools['ffmpeg_path'], destination, tmp_path / 'decode'
    )
    assert decoded >= 144_000
    assert decoded <= 144_000 + PRIMING_TOLERANCE_FRAMES


def test_encode_narration_refuses_existing_destination(
    tools: dict[str, str], tmp_path: Path
) -> None:
    source = ats.write_pcm(tmp_path / 'narration.pcm', ats.pcm_bytes(480))
    destination = tmp_path / 'narration.m4a'
    destination.write_bytes(b'taken')
    with pytest.raises(AudioError, match='already exists'):
        encode_narration_aac(
            source,
            destination,
            ffmpeg_path=tools['ffmpeg_path'],
            ffprobe_path=tools['ffprobe_path'],
            timeout_seconds=_TIMEOUT,
        )
    assert destination.read_bytes() == b'taken'


@pytest.mark.parametrize('channels', [1, 2])
def test_original_aac_lc_stays_on_copy_path(
    tools: dict[str, str], tmp_path: Path, channels: int
) -> None:
    source = ats.render_original(
        tools['ffmpeg_path'],
        tmp_path / f'original-{channels}ch.mp4',
        codec_args=(
            '-c:a',
            'aac',
            '-b:a',
            '96k',
            '-ac',
            str(channels),
            '-ar',
            '48000',
        ),
    )
    destination = tmp_path / f'prepared-{channels}ch.m4a'
    result = prepare_original_audio(
        source,
        0,
        destination,
        ffmpeg_path=tools['ffmpeg_path'],
        ffprobe_path=tools['ffprobe_path'],
        timeout_seconds=_TIMEOUT,
    )
    assert result.disposition == 'copied'
    assert result.codec == 'aac'
    assert result.profile == 'LC'
    assert result.channels == channels
    assert result.sample_rate == 48_000
    facts = ats.audio_facts(tools['ffprobe_path'], destination)
    assert facts['codec_name'] == 'aac'
    assert facts['channels'] == channels


@pytest.mark.parametrize(
    ('suffix', 'codec_args'),
    [
        ('.wav', ('-c:a', 'pcm_s16le')),
        ('.mp3', ('-c:a', 'libmp3lame', '-b:a', '96k')),
        ('.opus', ('-c:a', 'libopus', '-b:a', '64k')),
    ],
)
@pytest.mark.parametrize('channels', [1, 2])
def test_convertible_originals_convert_preserving_channels(
    tools: dict[str, str],
    tmp_path: Path,
    suffix: str,
    codec_args: tuple[str, ...],
    channels: int,
) -> None:
    source = ats.render_original(
        tools['ffmpeg_path'],
        tmp_path / f'original-{channels}{suffix}',
        codec_args=(*codec_args, '-ac', str(channels)),
    )
    destination = tmp_path / f'prepared-{channels}{suffix}.m4a'
    result = prepare_original_audio(
        source,
        0,
        destination,
        ffmpeg_path=tools['ffmpeg_path'],
        ffprobe_path=tools['ffprobe_path'],
        timeout_seconds=_TIMEOUT,
    )
    assert result.disposition == 'converted'
    assert result.profile == 'LC'
    assert result.channels == channels
    assert result.sample_rate == 48_000
    facts = ats.audio_facts(tools['ffprobe_path'], destination)
    assert facts['codec_name'] == 'aac'
    assert facts['profile'] == 'LC'
    assert facts['channels'] == channels


def test_original_unsupported_missing_stream_and_profile_refused(
    tools: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    float_source = ats.render_original(
        tools['ffmpeg_path'],
        tmp_path / 'float.wav',
        codec_args=('-c:a', 'pcm_f32le'),
    )
    destination = tmp_path / 'prepared.m4a'
    with pytest.raises(AudioError, match='pcm_f32le') as unsupported:
        prepare_original_audio(
            float_source,
            0,
            destination,
            ffmpeg_path=tools['ffmpeg_path'],
            ffprobe_path=tools['ffprobe_path'],
            timeout_seconds=_TIMEOUT,
        )
    assert unsupported.value.kind == 'profile'
    aac_source = ats.render_original(
        tools['ffmpeg_path'],
        tmp_path / 'aac.m4a',
        codec_args=('-c:a', 'aac', '-b:a', '96k'),
    )
    with pytest.raises(AudioError, match='selection') as absent:
        prepare_original_audio(
            aac_source,
            7,
            destination,
            ffmpeg_path=tools['ffmpeg_path'],
            ffprobe_path=tools['ffprobe_path'],
            timeout_seconds=_TIMEOUT,
        )
    assert absent.value.kind == 'profile'
    monkeypatch.setattr(
        audio_module,
        '_probe_audio_facts',
        lambda *args: (
            {
                'codec_name': 'aac',
                'profile': 'HE-AAC',
                'channels': 2,
                'sample_rate': 48_000,
                'bit_rate': None,
            },
            None,
        ),
    )
    with pytest.raises(AudioError, match='HE-AAC') as he:
        prepare_original_audio(
            aac_source,
            0,
            destination,
            ffmpeg_path=tools['ffmpeg_path'],
            ffprobe_path=tools['ffprobe_path'],
            timeout_seconds=_TIMEOUT,
        )
    assert he.value.kind == 'profile'
    assert not destination.exists()


def test_unicode_and_space_paths_flow_through_all_stages(
    tools: dict[str, str], tmp_path: Path
) -> None:
    work = tmp_path / 'Обработка 12' / 'речь и тишина'
    work.mkdir(parents=True)
    pcm = ats.noise_pcm_bytes(9_600)
    raw = ats.write_wav(work / 'исходная речь.wav', pcm)
    normalized = normalize_wav(
        raw,
        work / 'нормализованный звук.pcm',
        ffmpeg_path=tools['ffmpeg_path'],
        timeout_seconds=_TIMEOUT,
    )
    assert normalized.frames == 9_600
    timeline_destination = work / 'звуковая дорожка.pcm'
    result = stream_timeline(
        [_place('s001', 250, 500, normalized)],
        total_frames=24_000,
        destination=timeline_destination,
        max_speedup=DEFAULT_FITTING_MAX_SPEEDUP,
        work_dir=work,
        ffmpeg_path=tools['ffmpeg_path'],
        timeout_seconds=_TIMEOUT,
    )
    assert result.frames == 24_000
    narration = encode_narration_aac(
        timeline_destination,
        work / 'рассказ.m4a',
        ffmpeg_path=tools['ffmpeg_path'],
        ffprobe_path=tools['ffprobe_path'],
        timeout_seconds=_TIMEOUT,
    )
    assert narration.profile == 'LC'
    assert narration.path.is_file()

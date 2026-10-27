"""Deterministic audio lane fixtures and real tool helpers."""

from __future__ import annotations

import json
import math
import os
import random
import struct
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from lecture_localizer.audio import NormalizedSpeech
from lecture_localizer.media import checked_run_strict, scrubbed_environment
from lecture_localizer.storage import sha256_file

SAMPLE_RATE = 48_000
BYTES_PER_SAMPLE = 2
_DECAY_START = 0.6


def pcm_bytes(
    frames: int,
    frequency_hz: int = 440,
    amplitude: int = 12_000,
    decay_to_quiet: bool = False,
) -> bytes:
    """Build deterministic sine PCM frames at the working rate."""
    values = bytearray()
    for position in range(frames):
        level = amplitude
        if decay_to_quiet and position >= frames * _DECAY_START:
            level = max(1, int(amplitude * 0.01))
        value = int(
            level
            * math.sin(2 * math.pi * frequency_hz * position / SAMPLE_RATE)
        )
        values += struct.pack('<h', value)
    return bytes(values)


def noise_pcm_bytes(
    frames: int, amplitude: int = 12_000, seed: int = 7
) -> bytes:
    """Build deterministic noise PCM frames at the working rate."""
    rng = random.Random(seed)
    return b''.join(
        struct.pack('<h', rng.randint(-amplitude, amplitude))
        for _ in range(frames)
    )


def wav_bytes(
    pcm: bytes, sample_rate: int = SAMPLE_RATE, channels: int = 1
) -> bytes:
    """Wrap PCM bytes in a canonical 44 byte RIFF WAVE header."""
    byte_rate = sample_rate * channels * BYTES_PER_SAMPLE
    header = b'RIFF'
    header += struct.pack('<I', 36 + len(pcm))
    header += b'WAVEfmt '
    header += struct.pack(
        '<IHHIIHH',
        16,
        1,
        channels,
        sample_rate,
        byte_rate,
        channels * BYTES_PER_SAMPLE,
        16,
    )
    header += b'data'
    header += struct.pack('<I', len(pcm))
    return header + pcm


def write_pcm(path: Path, pcm: bytes) -> Path:
    """Write raw PCM bytes to one file."""
    path.write_bytes(pcm)
    return path


def write_wav(path: Path, pcm: bytes) -> Path:
    """Write PCM bytes as a canonical WAV file."""
    path.write_bytes(wav_bytes(pcm))
    return path


def speech_record(wav_path: Path, pcm_path: Path) -> NormalizedSpeech:
    """Build a normalized speech record from written fixture files."""
    return NormalizedSpeech(
        raw_path=wav_path,
        raw_sha256=sha256_file(wav_path),
        path=pcm_path,
        sha256=sha256_file(pcm_path),
        frames=pcm_path.stat().st_size // BYTES_PER_SAMPLE,
    )


def audio_facts(
    ffprobe_path: str, media: Path, stream: int = 0
) -> dict[str, Any]:
    """Return codec facts of one audio stream for assertions."""
    result = subprocess.run(
        [
            ffprobe_path,
            '-v',
            'error',
            '-select_streams',
            str(stream),
            '-show_entries',
            'stream=codec_name,profile,channels,sample_rate,bit_rate',
            '-of',
            'json',
            str(media),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=scrubbed_environment(),
    )
    assert result.returncode == 0, result.stderr
    payload: dict[str, Any] = json.loads(result.stdout)
    row: dict[str, Any] = payload['streams'][0]
    return row


def decoded_frames(ffmpeg_path: str, media: Path, work_dir: Path) -> int:
    """Fully decode one media file and return its PCM frame count."""
    work_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=work_dir, suffix='.pcm')
    os.close(fd)
    decoded = Path(name)
    try:
        checked_run_strict(
            [
                ffmpeg_path,
                '-hide_banner',
                '-loglevel',
                'error',
                '-y',
                '-i',
                str(media),
                '-vn',
                '-f',
                's16le',
                '-acodec',
                'pcm_s16le',
                str(decoded),
            ],
            120.0,
        )
        return decoded.stat().st_size // BYTES_PER_SAMPLE
    finally:
        decoded.unlink(missing_ok=True)


def render_original(
    ffmpeg_path: str,
    destination: Path,
    *,
    codec_args: tuple[str, ...],
    seconds: float = 1.0,
    frequency: int = 330,
) -> Path:
    """Render one synthetic original audio file with real FFmpeg."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    checked_run_strict(
        [
            ffmpeg_path,
            '-hide_banner',
            '-loglevel',
            'error',
            '-f',
            'lavfi',
            '-i',
            f'sine=frequency={frequency}:duration={seconds}',
            *codec_args,
            '-y',
            str(destination),
        ],
        120.0,
    )
    return destination

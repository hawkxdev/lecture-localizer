"""Lane C container test helpers with fake and real media tools."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from conftest import make_media
from lecture_localizer.media import scrubbed_environment
from lecture_localizer.mux import (
    ConvertedAudioTrack,
    CopiedAudioStream,
    MuxRequest,
    TrackProfile,
)
from lecture_localizer.settings import Settings
from lecture_localizer.subtitles import SubtitleEntry

FAKE_MUX_FFMPEG = """#!/bin/sh
if [ -n "$FAKE_ARGV_DUMP" ]; then
  printf '%s\\n' "$0" "$@" >> "$FAKE_ARGV_DUMP"
fi
mode=""
out=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "-f" ]; then mode="$arg"; fi
  if [ "$prev" = "-y" ] || [ "$prev" = "-out" ]; then out="$arg"; fi
  prev="$arg"
done
case "$mode" in
  null) exit ${FAKE_DECODE_EXIT:-0} ;;
  srt) cat "$FAKE_SRT_PAYLOAD" > "$out" ;;
  *) [ -n "$out" ] && : > "$out" ;;
esac
exit ${FAKE_FFMPEG_EXIT:-0}
"""

FAKE_MUX_MP4BOX = """#!/bin/sh
if [ -n "$FAKE_ARGV_DUMP" ]; then
  printf '%s\\n' "$0" "$@" >> "$FAKE_ARGV_DUMP"
fi
if [ "$1" = "-info" ]; then
  cat "$FAKE_INFO_PAYLOAD"
  exit ${FAKE_INFO_EXIT:-0}
fi
src=""
dst=""
prev=""
while [ $# -gt 0 ]; do
  if [ "$1" = "-out" ]; then dst="$2"; src="$prev"; fi
  prev="$1"
  shift
done
cat "$src" > "$dst"
exit ${FAKE_MP4BOX_EXIT:-0}
"""

FAKE_MUX_FFPROBE = """#!/bin/sh
if [ -n "$FAKE_ARGV_DUMP" ]; then
  printf '%s\\n' "$0" "$@" >> "$FAKE_ARGV_DUMP"
fi
mode="streams"
for arg in "$@"; do
  if [ "$arg" = "-show_packets" ]; then mode="packets"; fi
done
if [ "$mode" = "packets" ]; then
  cat "$FAKE_PACKETS_PAYLOAD"
else
  cat "$FAKE_STREAMS_PAYLOAD"
fi
exit ${FAKE_FFPROBE_EXIT:-0}
"""

CANNED_TRACK_INFO = """# Movie Info - 5 tracks - TimeScale 1000
Duration 00:00:02.000
# Track 1 Info - ID 7 - TimeScale 10240
Track flags: Enabled In Movie
Media Type: vide:avc1
# Track 2 Info - ID 3 - TimeScale 48000
Track flags: Enabled In Movie
Media Type: soun:mp4a
# Track 3 Info - ID 5 - TimeScale 48000
Track flags: Disabled In Movie
Media Type: soun:mp4a
# Track 4 Info - ID 9 - TimeScale 1000000
Track flags: Enabled In Movie
Media Type: sbtl:tx3g
# Track 5 Info - ID 2 - TimeScale 1000000
Track flags: Disabled In Movie
Media Type: sbtl:tx3g
"""

EN_CUES = [
    (0, 1000, 'First spoken line.'),
    (1000, 2000, 'Second spoken line.\nWith a second line.'),
]

RU_CUES = [
    (0, 1000, 'Первая полная реплика перевода.'),
    (1000, 2000, 'Вторая полная реплика перевода.'),
]

_MP4BOX_INFO = CANNED_TRACK_INFO


def write_fake_tools(tmp_path: Path) -> dict[str, Path]:
    """Write the lane C fake media tool scripts."""
    bin_dir = tmp_path / 'fake-bin'
    bin_dir.mkdir(parents=True, exist_ok=True)
    tools: dict[str, Path] = {}
    for name, content in (
        ('ffmpeg', FAKE_MUX_FFMPEG),
        ('ffprobe', FAKE_MUX_FFPROBE),
        ('MP4Box', FAKE_MUX_MP4BOX),
    ):
        path = bin_dir / name
        path.write_text(content, encoding='utf-8')
        path.chmod(0o755)
        tools[name] = path
    return tools


def stream_probe_payload(
    *, video_index: int = 0, audio_indexes: tuple[int, ...] = (1,)
) -> dict[str, Any]:
    """Build an ffprobe index and codec type payload."""
    streams: list[dict[str, Any]] = [
        {'index': video_index, 'codec_type': 'video'}
    ]
    streams.extend(
        {'index': index, 'codec_type': 'audio'} for index in audio_indexes
    )
    return {'streams': streams}


def write_srt_entries(path: Path, entries: list[tuple[int, int, str]]) -> Path:
    """Write one SRT file from millisecond rows, preserving newlines."""
    blocks: list[str] = []
    for number, (start_ms, end_ms, text) in enumerate(entries, start=1):
        blocks.append(
            f'{number}\n{_stamp(start_ms)} --> {_stamp(end_ms)}\n{text}\n'
        )
    path.write_text('\n'.join(blocks), encoding='utf-8')
    return path


def _stamp(ms: int) -> str:
    """Render milliseconds as an SRT timestamp."""
    hour, rest = divmod(ms, 3_600_000)
    minute, rest = divmod(rest, 60_000)
    second, millis = divmod(rest, 1000)
    return f'{hour:02d}:{minute:02d}:{second:02d},{millis:03d}'


def _encode_aac(
    destination: Path,
    frequency: int,
    seconds: float,
    channels: str,
) -> Path:
    """Encode one synthetic AAC LC 48 kHz file with the real tool."""
    ffmpeg = shutil.which('ffmpeg')
    if ffmpeg is None:
        raise RuntimeError('required setup prerequisite: ffmpeg absent')
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            ffmpeg,
            '-hide_banner',
            '-loglevel',
            'error',
            '-f',
            'lavfi',
            '-i',
            f'sine=frequency={frequency}:duration={seconds}',
            '-c:a',
            'aac',
            '-b:a',
            '192k',
            '-ar',
            '48000',
            '-ac',
            channels,
            '-y',
            str(destination),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=scrubbed_environment(),
    )
    if result.returncode != 0:
        raise RuntimeError(f'ffmpeg failed: {result.stderr}')
    return destination


def encode_narration_aac(destination: Path, seconds: float = 2.0) -> Path:
    """Encode one synthetic mono AAC LC 48 kHz narration file."""
    return _encode_aac(destination, 440, seconds, '1')


def encode_original_aac(
    destination: Path, seconds: float = 2.0, stereo: bool = True
) -> Path:
    """Encode one prepared original audio AAC file with given layout."""
    channels = '2' if stereo else '1'
    return _encode_aac(destination, 330, seconds, channels)


def prepared_tracks(tmp_path: Path) -> dict[str, Any]:
    """Create the complete synthetic prepared track set with real tools.

    Returns the video, narration, subtitle and converted audio paths
    plus the declared expectation numbers used across lane C tests.
    """
    staging = tmp_path / 'prepared'
    staging.mkdir(exist_ok=True)
    video = make_media(staging / 'lecture.mp4')
    narration = encode_narration_aac(staging / 'narration.m4a')
    english = write_srt_entries(staging / 'english.srt', EN_CUES)
    russian = write_srt_entries(staging / 'russian.srt', RU_CUES)
    return {
        'video': video,
        'narration': narration,
        'english': english,
        'russian': russian,
        'converted': encode_original_aac(staging / 'original.m4a'),
        'narration_end_ms': 2000,
        'original_end_ms': 2000,
    }


def candidate_request(
    tracks: dict[str, Any],
    destination: Path,
    *,
    copied_stream_index: int | None = 1,
    profile: TrackProfile | None = None,
) -> MuxRequest:
    """Build a lane C mux request from prepared tracks."""
    original_audio: CopiedAudioStream | ConvertedAudioTrack
    if copied_stream_index is not None:
        original_audio = CopiedAudioStream(stream_index=copied_stream_index)
    else:
        original_audio = ConvertedAudioTrack(path=tracks['converted'])
    return MuxRequest(
        video_path=tracks['video'],
        original_audio=original_audio,
        narration_path=tracks['narration'],
        english_srt_path=tracks['english'],
        russian_srt_path=tracks['russian'],
        destination=destination,
        profile=profile if profile is not None else TrackProfile(),
    )


def default_settings() -> Settings:
    """Return settings that resolve the local real tools."""
    return Settings()


def read_stream_table(path: Path) -> list[dict[str, Any]]:
    """Return the compact per stream rows of one media file."""
    ffprobe = shutil.which('ffprobe')
    if ffprobe is None:
        raise RuntimeError('required setup prerequisite: ffprobe absent')
    result = subprocess.run(
        [
            ffprobe,
            '-v',
            'error',
            '-show_entries',
            'stream=index,codec_name,codec_type,profile,channels,'
            'sample_rate:stream_tags=language:stream_disposition',
            '-of',
            'json',
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=scrubbed_environment(),
    )
    if result.returncode != 0:
        raise RuntimeError(f'ffprobe failed: {result.stderr}')
    payload = json.loads(result.stdout)
    rows: list[dict[str, Any]] = []
    for stream in payload['streams']:
        tags = stream.get('tags') or {}
        disposition = stream.get('disposition') or {}
        rows.append(
            {
                'index': stream['index'],
                'codec_type': stream['codec_type'],
                'codec_name': stream['codec_name'],
                'profile': stream.get('profile'),
                'channels': stream.get('channels'),
                'sample_rate': stream.get('sample_rate'),
                'language': tags.get('language'),
                'default': disposition.get('default'),
                'forced': disposition.get('forced'),
            }
        )
    return rows


def packet_fingerprints(path: Path, selector: str) -> list[tuple[str, ...]]:
    """Return the packet identity rows of one selected stream."""
    ffprobe = shutil.which('ffprobe')
    if ffprobe is None:
        raise RuntimeError('required setup prerequisite: ffprobe absent')
    result = subprocess.run(
        [
            ffprobe,
            '-v',
            'error',
            '-select_streams',
            selector,
            '-show_packets',
            '-show_data',
            '-of',
            'json',
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=scrubbed_environment(),
    )
    if result.returncode != 0:
        raise RuntimeError(f'ffprobe failed: {result.stderr}')
    payload = json.loads(result.stdout)
    return [
        (
            str(packet.get('pts_time')),
            str(packet.get('duration_time')),
            str(packet.get('size')),
            str(packet.get('data')),
        )
        for packet in payload['packets']
    ]


def make_two_audio_video(destination: Path) -> Path:
    """Render one synthetic video carrying two mono audio tracks."""
    ffmpeg = shutil.which('ffmpeg')
    if ffmpeg is None:
        raise RuntimeError('required setup prerequisite: ffmpeg absent')
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            ffmpeg,
            '-hide_banner',
            '-loglevel',
            'error',
            '-f',
            'lavfi',
            '-i',
            'color=c=black:s=128x96:r=10:d=2.0',
            '-f',
            'lavfi',
            '-i',
            'sine=frequency=440:duration=2.0',
            '-f',
            'lavfi',
            '-i',
            'sine=frequency=880:duration=2.0',
            '-map',
            '0:v',
            '-map',
            '1:a',
            '-map',
            '2:a',
            '-c:v',
            'libx264',
            '-pix_fmt',
            'yuv420p',
            '-c:a',
            'aac',
            '-b:a',
            '96k',
            '-y',
            str(destination),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=scrubbed_environment(),
    )
    if result.returncode != 0:
        raise RuntimeError(f'ffmpeg failed: {result.stderr}')
    return destination


def subtitle_entries(cues: list[tuple[int, int, str]]) -> list[SubtitleEntry]:
    """Convert millisecond rows into subtitle entries."""
    return [
        SubtitleEntry(start_ms=start, end_ms=end, text=text)
        for start, end, text in cues
    ]

"""Shared helpers for the stage tests."""

from __future__ import annotations

import hashlib
import json
import shlex
import shutil
import subprocess
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from lecture_localizer.media import (
    CREDENTIAL_ENV_PATTERN,
    scrubbed_environment,
)

FAKE_FFMPEG = """#!/bin/sh
case "$*" in
  *-version*) echo "ffmpeg version 7.1 Fake" ;;
  *-decoders*) printf ' V....D h264   H264 decoder\\n' ;;
  *-encoders*) printf ' A....D aac AAC\\n S..... mov_text MOV\\n' ;;
  *-filters*) printf ' .. atempo     A->A Adjust tempo.\\n' ;;
  *) echo "unexpected args: $*" >&2; exit 64 ;;
esac
"""

FAKE_FFMPEG_NO_ATEMPO = """#!/bin/sh
case "$*" in
  *-version*) echo "ffmpeg version 7.1 Fake" ;;
  *-decoders*) printf ' V....D h264   H264 decoder\\n' ;;
  *-encoders*) printf ' A....D aac AAC\\n S..... mov_text MOV\\n' ;;
  *-filters*) printf ' .. volume     A->A Adjust volume.\\n' ;;
  *) echo "unexpected args: $*" >&2; exit 64 ;;
esac
"""

FAKE_FFPROBE = """#!/bin/sh
case "$*" in
  *-version*) echo "ffprobe version 7.1 Fake" ;;
  *) echo "unexpected args: $*" >&2; exit 64 ;;
esac
"""

FAKE_MP4BOX = """#!/bin/sh
case "$*" in
  *-version*) echo "MP4Box - GPAC version fake-test" ;;
  *) echo "unexpected args: $*" >&2; exit 64 ;;
esac
"""

ENV_DUMP_TOOL = """#!/bin/sh
echo "xai_api_key_seen=$XAI_API_KEY"
echo "path_present=${PATH:+yes}"
"""


def write_executable(path: Path, content: str) -> Path:
    """Write a shell script file and mark it executable."""
    path.write_text(content, encoding='utf-8')
    path.chmod(0o755)
    return path


def fake_tool_dir(
    tmp_path: Path, ffmpeg_script: str = FAKE_FFMPEG
) -> dict[str, Path]:
    """Create a fake tool directory with ffmpeg, ffprobe and MP4Box."""
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir(exist_ok=True)
    return {
        'bin': bin_dir,
        'ffmpeg': write_executable(bin_dir / 'ffmpeg', ffmpeg_script),
        'ffprobe': write_executable(bin_dir / 'ffprobe', FAKE_FFPROBE),
        'mp4box': write_executable(bin_dir / 'MP4Box', FAKE_MP4BOX),
    }


def real_tool_paths() -> dict[str, str | None]:
    """Return resolved paths of the real local media tools."""
    return {
        'ffmpeg': shutil.which('ffmpeg'),
        'ffprobe': shutil.which('ffprobe'),
        'mp4box': shutil.which('MP4Box') or shutil.which('mp4box'),
    }


class CredentialValueTrap(Mapping[str, str]):
    """Mapping that forbids reading values of credential named keys.

    Writes and deletions stay permitted because only value reads can
    disclose a secret.
    """

    def __init__(self, data: dict[str, str]) -> None:
        """Store the backing environment data."""
        self._data = data

    def __getitem__(self, key: str) -> str:
        """Return a value and forbid reading credential named values."""
        if CREDENTIAL_ENV_PATTERN.search(key):
            raise AssertionError(f'credential value read attempted for {key}')
        return self._data[key]

    def __setitem__(self, key: str, value: str) -> None:
        """Store a key and its value."""
        self._data[key] = value

    def __delitem__(self, key: str) -> None:
        """Remove a key and its value."""
        del self._data[key]

    def __iter__(self) -> Iterator[str]:
        """Iterate over the stored keys."""
        return iter(self._data)

    def __len__(self) -> int:
        """Return the number of stored keys."""
        return len(self._data)


def sha256(path: Path) -> str:
    """Return the hex digest of one file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_srt(path: Path, cues: list[tuple[int, int, str]]) -> Path:
    """Write cues as start and end milliseconds plus text lines."""
    blocks = []
    for index, (start_ms, end_ms, text) in enumerate(cues, start=1):
        blocks.append(
            f'{index}\n{_stamp(start_ms)} --> {_stamp(end_ms)}\n{text}\n'
        )
    path.write_text('\n'.join(blocks), encoding='utf-8')
    return path


def _stamp(ms: int) -> str:
    """Render milliseconds as an SRT timestamp."""
    hour, rest = divmod(ms, 3_600_000)
    minute, rest = divmod(rest, 60_000)
    second, millis = divmod(rest, 1000)
    return f'{hour:02d}:{minute:02d}:{second:02d},{millis:03d}'


def translation_payload(
    video_sha256: str,
    srt_sha256: str,
    segments: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a minimal valid translation payload."""
    return {
        'schema_version': 1,
        'source': {
            'video_sha256': video_sha256,
            'srt_sha256': srt_sha256,
        },
        'source_language': 'en',
        'target_language': 'ru',
        'segments': segments,
    }


def write_translation(
    path: Path, video: Path, srt: Path, segments: list[dict[str, Any]]
) -> Path:
    """Write a translation bound to the actual input hashes."""
    payload = translation_payload(sha256(video), sha256(srt), segments)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    return path


def rebind_translation(path: Path, video: Path, srt: Path) -> Path:
    """Update the source hashes of an existing translation file."""
    payload = json.loads(path.read_text(encoding='utf-8'))
    payload['source'] = {
        'video_sha256': sha256(video),
        'srt_sha256': sha256(srt),
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    return path


def default_segments() -> list[dict[str, Any]]:
    """Return the two segment rows used by most fixtures."""
    return [
        {
            'id': 's001',
            'source_cues': [1],
            'translation': 'Первая реплика.',
            'narration': 'Первая реплика для речи.',
        },
        {
            'id': 's002',
            'source_cues': [2],
            'translation': 'Вторая реплика.',
        },
    ]


DEFAULT_CUES = [
    (0, 1000, 'First spoken line.'),
    (1000, 2000, 'Second spoken line.'),
]


def make_media(
    destination: Path,
    *,
    video_codec: str = 'libx264',
    pix_fmt: str = 'yuv420p',
    audio_args: tuple[str, ...] = ('-c:a', 'aac', '-b:a', '96k'),
    extra_audio_inputs: int = 0,
    extension: str = 'mp4',
    seconds: float = 2.0,
    extra_args: tuple[str, ...] = (),
) -> Path:
    """Render one small synthetic media fixture with the real FFmpeg."""
    ffmpeg = shutil.which('ffmpeg')
    if ffmpeg is None:
        raise RuntimeError('required setup prerequisite: ffmpeg absent')
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        '-hide_banner',
        '-loglevel',
        'error',
        '-f',
        'lavfi',
        '-i',
        f'color=c=black:s=128x96:r=10:d={seconds}',
        '-f',
        'lavfi',
        '-i',
        f'sine=frequency=440:duration={seconds}',
    ]
    for extra in range(extra_audio_inputs):
        command.extend(
            [
                '-f',
                'lavfi',
                '-i',
                f'sine=frequency={880 + extra * 110}:duration={seconds}',
            ]
        )
    command.extend(
        [
            '-c:v',
            video_codec,
            '-pix_fmt',
            pix_fmt,
            *audio_args,
            *extra_args,
        ]
    )
    if extra_audio_inputs:
        command.extend(['-map', '0:v'])
        for audio_input in range(1, extra_audio_inputs + 2):
            command.extend(['-map', f'{audio_input}:a'])
    command.extend(['-shortest', '-y', str(destination)])
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=scrubbed_environment(),
    )
    if result.returncode != 0:
        raise RuntimeError(f'ffmpeg failed: {result.stderr}')
    return destination


def probe_payload(
    *,
    container: str = 'mov,mp4,m4a,3gp,3g2,mj2',
    duration: str | None = '2.0',
    video: dict[str, Any] | None = None,
    audio: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build an ffprobe style JSON payload."""
    if video is None:
        video = {
            'index': 0,
            'codec_name': 'h264',
            'codec_type': 'video',
            'pix_fmt': 'yuv420p',
            'width': 320,
            'height': 240,
            'color_transfer': 'bt709',
        }
    if audio is None:
        audio = [
            {
                'index': 1,
                'codec_name': 'aac',
                'codec_type': 'audio',
                'profile': 'LC',
                'channels': 2,
                'sample_rate': '48000',
                'tags': {'language': 'eng'},
            }
        ]
    media_format: dict[str, Any] = {'format_name': container}
    if duration is not None:
        media_format['duration'] = duration
    return {'streams': [video, *audio], 'format': media_format}


def fake_ffprobe(
    tmp_path: Path, payload: dict[str, Any] | str
) -> dict[str, Path]:
    """Create a fake ffprobe that prints one canned JSON payload."""
    bin_dir = tmp_path / 'probe-bin'
    bin_dir.mkdir(exist_ok=True)
    printed = payload if isinstance(payload, str) else json.dumps(payload)
    script = f"#!/bin/sh\nprintf '%s' {shlex.quote(printed)}\nexit 0\n"
    return {
        'bin': bin_dir,
        'ffprobe': write_executable(bin_dir / 'ffprobe', script),
    }


VALID_PROBE_JSON = json.dumps(
    {
        'streams': [
            {
                'index': 0,
                'codec_name': 'h264',
                'codec_type': 'video',
                'pix_fmt': 'yuv420p',
                'width': 320,
                'height': 240,
                'color_transfer': 'bt709',
            },
            {
                'index': 1,
                'codec_name': 'aac',
                'codec_type': 'audio',
                'profile': 'LC',
                'channels': 2,
                'sample_rate': '48000',
                'tags': {'language': 'eng'},
            },
        ],
        'format': {
            'format_name': 'mov,mp4,m4a,3gp,3g2,mj2',
            'duration': '2.0',
        },
    }
)


def env_dump_ffprobe(tmp_path: Path, dump_file: Path) -> dict[str, Path]:
    """Create a fake ffprobe that records its environment to a file."""
    bin_dir = tmp_path / 'probe-bin'
    bin_dir.mkdir(exist_ok=True)
    script = (
        '#!/bin/sh\n'
        '{\n'
        '  printf \'canary_seen=%s\\n\' "$XAI_API_KEY"\n'
        '  printf \'path_present=%s\\n\' "${PATH:+yes}"\n'
        '} > "$DUMP_FILE"\n'
        f"printf '%s' {shlex.quote(VALID_PROBE_JSON)}\n"
        'exit 0\n'
    )
    return {
        'bin': bin_dir,
        'ffprobe': write_executable(bin_dir / 'ffprobe', script),
    }


def require_real_media() -> None:
    """Fail the test when the real local media tools are absent."""
    missing = [
        name
        for name, path in (
            ('ffmpeg', shutil.which('ffmpeg')),
            ('ffprobe', shutil.which('ffprobe')),
        )
        if path is None
    ]
    if missing:
        from pytest import fail

        fail(
            f'required setup prerequisite: local media tools absent: {missing}'
        )


class NoNetwork:
    """Replace socket construction to prove no provider call is made."""

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        """Fail any attempt to open a socket."""
        raise AssertionError('network socket creation attempted')

    def __getattr__(self, name: str) -> Any:
        """Fail any other socket module attribute use."""
        raise AssertionError(f'network access attempted: {name}')


def plant_artifact_record(
    job_dir: Path,
    kind: str,
    segment: str | None,
    signature: str,
    data: bytes = b'synthetic cached bytes',
) -> str:
    """Write one synthetic cached artifact and record it in the manifest.

    The bytes are clearly synthetic test data; the record ties them to
    a stage signature so reuse decisions can be exercised without any
    later stage execution.
    """
    from lecture_localizer.storage import atomic_write_json

    manifest_path = job_dir / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    relative = f'artifacts/{kind}-{segment or "job"}.bin'
    target = job_dir / relative
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(data)
    manifest['artifacts'].append(
        {
            'kind': kind,
            'segment': segment,
            'signature': signature,
            'path': relative,
            'sha256': hashlib.sha256(data).hexdigest(),
            'created_at': '2026-10-26T12:00:00+00:00',
        }
    )
    atomic_write_json(manifest_path, manifest)
    return relative


def plant_complete_cache(job_dir: Path) -> None:
    """Plant raw, fitted, subtitle and container records for a manifest."""
    manifest = json.loads(
        (job_dir / 'manifest.json').read_text(encoding='utf-8')
    )
    for segment in manifest['segments']:
        plant_artifact_record(
            job_dir,
            'raw_response',
            segment['id'],
            segment['synthesis_signature'],
        )
        plant_artifact_record(
            job_dir,
            'fitted_audio',
            segment['id'],
            segment['fitting_signature'],
        )
    plant_artifact_record(
        job_dir, 'subtitles', None, manifest['subtitle_signature']
    )
    plant_artifact_record(
        job_dir, 'container_track', None, manifest['container_signature']
    )

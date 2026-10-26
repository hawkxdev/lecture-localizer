"""Shared helpers for the stage tests."""

from __future__ import annotations

import shutil
from collections.abc import Iterator, Mapping
from pathlib import Path

from lecture_localizer.media import CREDENTIAL_ENV_PATTERN

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

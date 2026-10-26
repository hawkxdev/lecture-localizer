"""Shared test helpers for the lecture-localizer stage tests."""

from __future__ import annotations

import shutil
from pathlib import Path

from pytest import MonkeyPatch

FAKE_FFMPEG = """#!/bin/sh
case "$*" in
  *-version*) echo "ffmpeg version 7.1 Fake" ;;
  *-decoders*) printf ' V....D h264      H264 decoder\\n' ;;
  *-encoders*) printf ' A....D aac       AAC encoder\\n S..... mov_text MOV text\\n' ;;
  *-filters*) printf ' .. atempo        A->A Adjust audio tempo.\\n' ;;
  *) echo "unexpected args: $*" >&2; exit 64 ;;
esac
"""

FAKE_FFMPEG_NO_ATEMPO = """#!/bin/sh
case "$*" in
  *-version*) echo "ffmpeg version 7.1 Fake" ;;
  *-decoders*) printf ' V....D h264      H264 decoder\\n' ;;
  *-encoders*) printf ' A....D aac       AAC encoder\\n S..... mov_text MOV text\\n' ;;
  *-filters*) printf ' .. volume        A->A Adjust volume.\\n' ;;
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
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


def fake_tool_dir(tmp_path: Path, ffmpeg_script: str = FAKE_FFMPEG) -> dict[str, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    return {
        "bin": bin_dir,
        "ffmpeg": write_executable(bin_dir / "ffmpeg", ffmpeg_script),
        "ffprobe": write_executable(bin_dir / "ffprobe", FAKE_FFPROBE),
        "mp4box": write_executable(bin_dir / "MP4Box", FAKE_MP4BOX),
    }


def real_tool_paths() -> dict[str, str | None]:
    return {
        "ffmpeg": shutil.which("ffmpeg"),
        "ffprobe": shutil.which("ffprobe"),
        "mp4box": shutil.which("MP4Box") or shutil.which("mp4box"),
    }


def isolated_path(monkeypatch: MonkeyPatch, directory: Path) -> None:
    monkeypatch.setenv("PATH", str(directory))
    monkeypatch.delenv("LECTURE_LOCALIZER_SETTINGS", raising=False)

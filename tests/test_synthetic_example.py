"""Tests for the synthetic fixture generator."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / 'scripts'
    / 'make-synthetic-example.py'
)

CANARY = 'sk-test-canary-not-real'

FAKE_FFMPEG_ENV_DUMP = """#!/bin/sh
{
  printf 'xai_api_key_seen=%s\\n' "$XAI_API_KEY"
  printf 'path_present=%s\\n' "${PATH:+yes}"
} > "$DUMP_FILE"
out=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "-y" ]; then out="$arg"; fi
  prev="$arg"
done
printf fake > "$out"
exit 0
"""

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / 'scripts'
    / 'make-synthetic-example.py'
)

EXPECTED_SRT = """1
00:00:00,000 --> 00:00:01,000
Synthetic cue one for technical checks.

2
00:00:01,000 --> 00:00:02,000
Synthetic cue two for technical checks.
"""

SPECIAL_DIR = "fixtures dir#with 'apostrophe' and юникод"


def run_script(
    *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the fixture generator as a user would."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=env,
    )


def sha256(path: Path) -> str:
    """Return the hex digest of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_creates_fixtures_in_directory_with_special_characters(
    tmp_path: Path,
) -> None:
    destination = tmp_path / SPECIAL_DIR
    result = run_script(str(destination))
    assert result.returncode == 0, result.stderr
    media = destination / 'synthetic-example.mp4'
    srt = destination / 'synthetic-example.srt'
    assert media.is_file() and media.stat().st_size > 0
    assert srt.read_text(encoding='utf-8') == EXPECTED_SRT
    for line in result.stdout.splitlines():
        digest, _, created = line.partition('  ')
        assert Path(created) in (media, srt)
        assert digest == sha256(Path(created))


def test_refuses_existing_destination(tmp_path: Path) -> None:
    existing = tmp_path / 'already-there'
    existing.mkdir()
    result = run_script(str(existing))
    assert result.returncode == 1
    assert 'refusing to write' in result.stderr


def test_creates_missing_parent_directories(tmp_path: Path) -> None:
    destination = tmp_path / SPECIAL_DIR / 'nested'
    result = run_script(str(destination))
    assert result.returncode == 0, result.stderr
    assert (destination / 'synthetic-example.mp4').is_file()


def test_fixtures_are_deterministic_across_runs(tmp_path: Path) -> None:
    first = tmp_path / 'first-run'
    second = tmp_path / 'second-run'
    assert run_script(str(first)).returncode == 0
    assert run_script(str(second)).returncode == 0
    for name in ('synthetic-example.mp4', 'synthetic-example.srt'):
        assert sha256(first / name) == sha256(second / name)


def test_generator_does_not_forward_credentials_to_children(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    dump_file = tmp_path / 'env-dump.txt'
    fake = bin_dir / 'ffmpeg'
    fake.write_text(FAKE_FFMPEG_ENV_DUMP, encoding='utf-8')
    fake.chmod(0o755)
    destination = tmp_path / "fixtures destination#'quote' юникод"
    result = run_script(
        str(destination),
        env={
            'PATH': str(bin_dir),
            'XAI_API_KEY': CANARY,
            'DUMP_FILE': str(dump_file),
            'HOME': str(tmp_path),
        },
    )
    assert result.returncode == 0, result.stderr
    assert (destination / 'synthetic-example.srt').is_file()
    dump = dump_file.read_text(encoding='utf-8')
    assert 'xai_api_key_seen=' in dump
    assert CANARY not in dump
    assert 'path_present=yes' in dump


def test_media_matches_the_approved_profile(tmp_path: Path) -> None:
    if shutil.which('ffprobe') is None or shutil.which('ffmpeg') is None:
        pytest.fail('required setup prerequisite: ffmpeg and ffprobe absent')
    destination = tmp_path / 'profile check#dir'
    result = run_script(str(destination))
    assert result.returncode == 0, result.stderr
    probe = subprocess.run(
        [
            'ffprobe',
            '-v',
            'error',
            '-show_entries',
            'stream=codec_name,codec_type,width,height:stream_disposition',
            '-of',
            'json',
            str(destination / 'synthetic-example.mp4'),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    streams = json.loads(probe.stdout)['streams']
    video = next(s for s in streams if s['codec_type'] == 'video')
    audio = next(s for s in streams if s['codec_type'] == 'audio')
    assert video['codec_name'] == 'h264'
    assert video['width'] == 320 and video['height'] == 240
    assert audio['codec_name'] == 'aac'

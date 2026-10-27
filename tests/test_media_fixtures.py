"""Checks for isolated reusable media fixtures."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

import conftest
from conftest import make_media


@pytest.fixture
def media_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Count actual renders with isolated seeds."""
    calls: list[list[str]] = []
    original = subprocess.run

    def counted(
        command: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        """Run the actual media command."""
        calls.append(command)
        return original(command, **kwargs)

    monkeypatch.setattr(conftest, '_MEDIA_SEEDS', {})
    monkeypatch.setattr(subprocess, 'run', counted)
    return calls


def test_cached_media_copies_are_independent(
    tmp_path: Path, media_calls: list[list[str]]
) -> None:
    first = make_media(tmp_path / 'first.mp4', use_cache=True)
    expected = first.read_bytes()
    second = make_media(tmp_path / 'second.mp4', use_cache=True)
    assert second.read_bytes() == expected
    assert first.stat().st_ino != second.stat().st_ino
    first.write_bytes(b'changed writable fixture')
    third = make_media(tmp_path / 'third.mp4', use_cache=True)
    assert second.read_bytes() == third.read_bytes() == expected
    assert len(media_calls) == 1


def test_cached_media_distinguishes_duration(
    tmp_path: Path, media_calls: list[list[str]]
) -> None:
    short = make_media(tmp_path / 'short.mp4', use_cache=True)
    longer = make_media(tmp_path / 'longer.mp4', seconds=2.2, use_cache=True)
    repeated = make_media(tmp_path / 'repeated.mp4', use_cache=True)
    assert short.read_bytes() != longer.read_bytes()
    assert repeated.read_bytes() == short.read_bytes()
    assert len(media_calls) == 2


def test_media_cache_can_be_bypassed(
    tmp_path: Path, media_calls: list[list[str]]
) -> None:
    cached = make_media(tmp_path / 'cached.mp4', use_cache=True)
    fresh = make_media(tmp_path / 'fresh.mp4', use_cache=False)
    assert fresh.read_bytes() == cached.read_bytes()
    assert len(media_calls) == 2


def test_other_media_profiles_are_rendered_each_time(
    tmp_path: Path, media_calls: list[list[str]]
) -> None:
    first = make_media(tmp_path / 'first.mp4', seconds=2.4, use_cache=True)
    second = make_media(tmp_path / 'second.mp4', seconds=2.4, use_cache=True)
    assert first.stat().st_size > 0 and second.stat().st_size > 0
    assert len(media_calls) == 2


def test_cached_media_still_requires_available_tool(
    tmp_path: Path,
    media_calls: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_media(tmp_path / 'first.mp4', use_cache=True)
    monkeypatch.setattr(shutil, 'which', lambda name: None)
    with pytest.raises(RuntimeError, match='prerequisite: ffmpeg absent'):
        make_media(tmp_path / 'second.mp4', use_cache=True)
    assert len(media_calls) == 1


def test_cached_media_tracks_tool_replacement(
    tmp_path: Path,
    media_calls: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = shutil.which('ffmpeg')
    assert executable is not None
    wrapper = tmp_path / 'ffmpeg'
    wrapper.write_text(
        '#!/bin/sh\nexec ' + shlex.quote(executable) + ' "$@"\n',
        encoding='utf-8',
    )
    wrapper.chmod(0o755)
    monkeypatch.setattr(shutil, 'which', lambda name: str(wrapper))
    first = make_media(tmp_path / 'first.mp4', use_cache=True)
    previous = wrapper.stat()
    os.utime(
        wrapper,
        ns=(previous.st_atime_ns, previous.st_mtime_ns + 1_000_000_000),
    )
    second = make_media(tmp_path / 'second.mp4', use_cache=True)
    assert second.read_bytes() == first.read_bytes()
    assert len(media_calls) == 2

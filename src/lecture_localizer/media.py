"""Local media tool discovery, capability probing and checked subprocesses."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lecture_localizer.settings import Settings

CREDENTIAL_ENV_PATTERN = re.compile(
    r'API_KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL', re.IGNORECASE
)

FFMPEG_REQUIREMENTS: tuple[tuple[str, str], ...] = (
    ('decoder', 'h264'),
    ('encoder', 'aac'),
    ('encoder', 'mov_text'),
    ('filter', 'atempo'),
)

BINARY_NAMES: dict[str, tuple[str, ...]] = {
    'ffmpeg': ('ffmpeg',),
    'ffprobe': ('ffprobe',),
    'mp4box': ('MP4Box', 'mp4box'),
}

_LISTING_NAME_RE = re.compile(r'^\s*[A-Za-z.|]{1,8}\s+(\S+)\s')
_VERSION_RE = re.compile(r'version\s+(\S+)')


class MediaToolError(RuntimeError):
    """A required media tool is missing or unusable."""


@dataclass(frozen=True)
class Capability:
    """Result of one required feature probe."""

    requirement: str
    ok: bool
    detail: str = ''


@dataclass(frozen=True)
class ToolReport:
    """Diagnostics for one external media tool."""

    name: str
    path: str | None
    version: str | None
    ok: bool
    capabilities: tuple[Capability, ...] = ()
    detail: str = ''

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON serializable view."""
        return {
            'name': self.name,
            'path': self.path,
            'version': self.version,
            'ok': self.ok,
            'detail': self.detail,
            'capabilities': [
                {'requirement': c.requirement, 'ok': c.ok, 'detail': c.detail}
                for c in self.capabilities
            ],
        }


@dataclass(frozen=True)
class DoctorReport:
    """Combined tool diagnostics for the doctor command."""

    tools: tuple[ToolReport, ...]

    @property
    def ok(self) -> bool:
        """Return True when every tool passed."""
        return all(tool.ok for tool in self.tools)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON serializable view."""
        return {
            'command': 'doctor',
            'ok': self.ok,
            'tools': [tool.to_dict() for tool in self.tools],
        }


def scrubbed_environment(
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an environment copy without credential variables.

    Credential names are filtered before any value is looked up, so a
    credential entry can never be read here.
    """
    source = os.environ if base is None else base
    return {
        key: source[key]
        for key in source
        if not CREDENTIAL_ENV_PATTERN.search(key)
    }


def checked_run(
    argv: Sequence[str], timeout_seconds: float
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess with a scrubbed environment and timeout."""
    try:
        return subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=scrubbed_environment(),
            check=False,
        )
    except FileNotFoundError as error:
        raise MediaToolError(f'executable not found: {argv[0]}') from error
    except subprocess.TimeoutExpired as error:
        raise MediaToolError(
            f'timed out after {timeout_seconds}s: {argv[0]}'
        ) from error
    except OSError as error:
        raise MediaToolError(f'failed to run {argv[0]}: {error}') from error


def checked_run_strict(
    argv: Sequence[str], timeout_seconds: float
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess and treat any nonzero exit as a tool failure."""
    result = checked_run(argv, timeout_seconds)
    if result.returncode != 0:
        tail = (
            result.stderr.strip().splitlines()[-3:]
            if result.stderr.strip()
            else []
        )
        detail = ' | '.join(tail)
        raise MediaToolError(
            f'{argv[0]} exited with code {result.returncode}: {detail}'
        )
    return result


def discover_tool(name: str, configured_path: str | None) -> Path:
    """Locate a tool from an explicit configured path or PATH."""
    if configured_path is not None:
        candidate = Path(configured_path)
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise MediaToolError(
                f'configured {name} path is not executable: {configured_path}'
            )
        return candidate
    for binary in BINARY_NAMES.get(name, (name,)):
        found = shutil.which(binary)
        if found is not None:
            return Path(found)
    raise MediaToolError(f'{name} not found on PATH')


def probe_version(tool_path: Path, timeout_seconds: float) -> str:
    """Return the tool version reported by its version flag."""
    result = checked_run_strict([str(tool_path), '-version'], timeout_seconds)
    for stream in (result.stdout, result.stderr):
        for line in stream.splitlines():
            if not line.strip():
                continue
            match = _VERSION_RE.search(line)
            if match is not None:
                return match.group(1)
    raise MediaToolError(f'could not parse a version from {tool_path}')


def listing_contains(output: str, token: str) -> bool:
    """Check whether a tool listing names an exact feature token."""
    for line in output.splitlines():
        match = _LISTING_NAME_RE.match(line)
        if match is not None and match.group(1) == token:
            return True
    return False


def _listing_argv(tool_path: Path, kind: str) -> list[str]:
    """Build the listing command for one feature kind."""
    return [str(tool_path), '-hide_banner', f'-{kind}s']


def probe_ffmpeg_capabilities(
    ffmpeg_path: Path, timeout_seconds: float
) -> tuple[Capability, ...]:
    """Probe the media profile decoder, encoder and filter features."""
    listings: dict[str, str] = {}
    for kind in ('decoder', 'encoder', 'filter'):
        result = checked_run_strict(
            _listing_argv(ffmpeg_path, kind), timeout_seconds
        )
        listings[kind] = result.stdout
    capabilities = []
    for kind, token in FFMPEG_REQUIREMENTS:
        ok = listing_contains(listings[kind], token)
        detail = '' if ok else f'no {kind} named {token} in ffmpeg listing'
        capabilities.append(Capability(f'{kind} {token}', ok, detail))
    return tuple(capabilities)


def _probe_input_argv(ffmpeg_path: Path, destination: Path) -> list[str]:
    """Build the synthetic two track probe input command."""
    return [
        str(ffmpeg_path),
        '-hide_banner',
        '-loglevel',
        'error',
        '-f',
        'lavfi',
        '-i',
        'color=c=black:s=128x96:r=10:d=0.4',
        '-f',
        'lavfi',
        '-i',
        'sine=frequency=440:duration=0.4',
        '-c:v',
        'mpeg4',
        '-q:v',
        '28',
        '-c:a',
        'aac',
        '-shortest',
        '-y',
        str(destination),
    ]


def _probe_dispositions(
    ffprobe_path: Path, media_path: Path, timeout_seconds: float
) -> list[tuple[int, int]]:
    """Return sorted stream index and default flag rows."""
    result = checked_run_strict(
        [
            str(ffprobe_path),
            '-v',
            'error',
            '-show_entries',
            'stream=index:stream_disposition',
            '-of',
            'json',
            str(media_path),
        ],
        timeout_seconds,
    )
    try:
        payload = json.loads(result.stdout)
        streams = payload['streams']
        rows = [
            (int(s['index']), int(s['disposition']['default']))
            for s in streams
        ]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise MediaToolError(
            f'unusable ffprobe disposition output: {error}'
        ) from error
    rows.sort()
    return rows


def probe_mp4box_track_flags(
    ffmpeg_path: Path,
    ffprobe_path: Path,
    mp4box_path: Path,
    timeout_seconds: float,
) -> Capability:
    """Rewrite track flags on a synthetic MP4 and verify the result."""
    requirement = 'mp4box track flag rewrite'
    with tempfile.TemporaryDirectory(prefix='lecture-localizer-probe-') as tmp:
        source = Path(tmp) / 'probe-input.mp4'
        rewritten = Path(tmp) / 'probe-output.mp4'
        checked_run_strict(
            _probe_input_argv(ffmpeg_path, source), timeout_seconds
        )
        checked_run_strict(
            [
                str(mp4box_path),
                '-disable',
                '2',
                str(source),
                '-out',
                str(rewritten),
            ],
            timeout_seconds,
        )
        rows = _probe_dispositions(ffprobe_path, rewritten, timeout_seconds)
    expected = [(0, 1), (1, 0)]
    if rows != expected:
        return Capability(
            requirement,
            False,
            f'persisted default flags {rows} do not match expected {expected}',
        )
    return Capability(requirement, True, 'verified on synthetic input')


def _failed_tool(name: str, error: MediaToolError) -> ToolReport:
    """Build a failed report from a tool error."""
    return ToolReport(
        name=name, path=None, version=None, ok=False, detail=str(error)
    )


def _probe_ffmpeg(settings: Settings) -> ToolReport:
    """Probe FFmpeg discovery, version and required capabilities."""
    try:
        path = discover_tool('ffmpeg', settings.ffmpeg_path)
        version = probe_version(path, settings.subprocess_timeout_seconds)
    except MediaToolError as error:
        return _failed_tool('ffmpeg', error)
    capabilities = probe_ffmpeg_capabilities(
        path, settings.subprocess_timeout_seconds
    )
    ok = all(capability.ok for capability in capabilities)
    detail = '' if ok else 'required ffmpeg capabilities are missing'
    return ToolReport('ffmpeg', str(path), version, ok, capabilities, detail)


def _probe_simple(
    name: str, configured_path: str | None, settings: Settings
) -> ToolReport:
    """Probe discovery and version for one tool without feature listings."""
    try:
        path = discover_tool(name, configured_path)
        version = probe_version(path, settings.subprocess_timeout_seconds)
    except MediaToolError as error:
        return _failed_tool(name, error)
    return ToolReport(name=name, path=str(path), version=version, ok=True)


def run_doctor(settings: Settings) -> DoctorReport:
    """Discover required tools and probe media capabilities."""
    ffmpeg = _probe_ffmpeg(settings)
    ffprobe = _probe_simple('ffprobe', settings.ffprobe_path, settings)
    mp4box = _probe_simple('mp4box', settings.mp4box_path, settings)
    if ffmpeg.ok and ffprobe.ok and mp4box.ok:
        assert (
            ffmpeg.path is not None
            and ffprobe.path is not None
            and mp4box.path is not None
        )
        try:
            functional = probe_mp4box_track_flags(
                Path(ffmpeg.path),
                Path(ffprobe.path),
                Path(mp4box.path),
                settings.subprocess_timeout_seconds,
            )
        except MediaToolError as error:
            functional = Capability(
                'mp4box track flag rewrite', False, str(error)
            )
        mp4box = ToolReport(
            name=mp4box.name,
            path=mp4box.path,
            version=mp4box.version,
            ok=mp4box.ok and functional.ok,
            capabilities=(functional,),
            detail='' if functional.ok else functional.detail,
        )
    return DoctorReport(tools=(ffmpeg, ffprobe, mp4box))

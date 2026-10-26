"""Command line interface of the lecture_localizer package."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from lecture_localizer import __version__
from lecture_localizer.contracts import ContractError
from lecture_localizer.jobs import PrepareResult, prepare_job, read_job_status
from lecture_localizer.media import DoctorReport, MediaToolError, run_doctor
from lecture_localizer.settings import (
    SETTINGS_ENV_VAR,
    SettingsError,
    load_settings,
)
from lecture_localizer.storage import (
    DestinationExistsError,
    LockUnavailableError,
    StorageError,
)
from lecture_localizer.subtitles import SubtitlesError

EXIT_OK = 0
EXIT_INVALID_INPUT = 3
EXIT_TOOL_FAILURE = 7
EXIT_INTERRUPTED = 130


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser advertising implemented commands."""
    parser = argparse.ArgumentParser(
        prog='lecture-localizer',
        description='Localize lecture videos and preserve the original media.',
    )
    parser.add_argument(
        '--version', action='version', version=f'%(prog)s {__version__}'
    )
    commands = parser.add_subparsers(dest='command', required=True)
    doctor = commands.add_parser(
        'doctor',
        help='Check local media tools and capabilities',
    )
    doctor.add_argument(
        '--json',
        dest='as_json',
        action='store_true',
        help='Print the diagnostics report as JSON',
    )
    prepare = commands.add_parser(
        'prepare',
        help='Validate inputs and create or resume a persistent job',
    )
    prepare.add_argument(
        '--video', required=True, type=Path, help='Path to the lecture video'
    )
    prepare.add_argument(
        '--source-srt',
        required=True,
        type=Path,
        dest='source_srt',
        help='Path to the English SRT subtitles',
    )
    prepare.add_argument(
        '--translation',
        required=True,
        type=Path,
        help='Path to the reviewed translation JSON',
    )
    prepare.add_argument(
        '--job',
        required=True,
        type=Path,
        dest='job_dir',
        help='Fresh or existing job directory outside the repository',
    )
    prepare.add_argument(
        '--output',
        required=True,
        type=Path,
        help='Future localized output path; it must not exist yet',
    )
    prepare.add_argument(
        '--source-audio',
        type=int,
        default=None,
        dest='source_audio',
        help='Audio stream index to use when the input has several tracks',
    )
    prepare.add_argument(
        '--settings',
        type=Path,
        default=None,
        dest='settings_path',
        help='Optional settings JSON with tool paths and job policy',
    )
    status = commands.add_parser(
        'status',
        help='Read the persistent job state and its report',
    )
    status.add_argument(
        '--job',
        required=True,
        type=Path,
        dest='job_dir',
        help='Existing job directory',
    )
    status.add_argument(
        '--json',
        dest='as_json',
        action='store_true',
        help='Print the status report as JSON',
    )
    return parser


def _render_report(report: DoctorReport) -> str:
    """Render the doctor report as human readable text."""
    lines = []
    for tool in report.tools:
        status = 'OK' if tool.ok else 'FAILED'
        row = (
            f'{tool.name:8} {status:6} {tool.path or ""} {tool.version or ""}'
        )
        lines.append(row.rstrip())
        if tool.detail:
            lines.append(f'         detail: {tool.detail}')
        for capability in tool.capabilities:
            cap_status = 'OK' if capability.ok else 'FAILED'
            lines.append(f'  {capability.requirement:28} {cap_status}')
            if capability.detail:
                lines.append(f'    detail: {capability.detail}')
    verdict = 'OK' if report.ok else 'FAILED'
    lines.append(f'doctor: {verdict}')
    return '\n'.join(lines)


def _run_doctor(as_json: bool) -> int:
    """Run the doctor command and return its exit code."""
    try:
        settings = load_settings()
    except SettingsError as error:
        print(f'invalid settings: {error}', file=sys.stderr)
        return EXIT_INVALID_INPUT
    except OSError as error:
        print(f'settings file cannot be read: {error}', file=sys.stderr)
        return EXIT_TOOL_FAILURE
    try:
        report = run_doctor(settings)
    except MediaToolError as error:
        print(f'media tool failure: {error}', file=sys.stderr)
        return EXIT_TOOL_FAILURE
    if as_json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(_render_report(report))
    return EXIT_OK if report.ok else EXIT_TOOL_FAILURE


def _prepare_error_exit(error: Exception) -> int:
    """Map one prepare failure to its planned exit code."""
    if isinstance(error, DestinationExistsError):
        print(f'destination conflict: {error}', file=sys.stderr)
        return EXIT_INVALID_INPUT
    if isinstance(error, ContractError):
        print(f'invalid input: {error}', file=sys.stderr)
        return EXIT_INVALID_INPUT
    if isinstance(error, SubtitlesError):
        print(f'invalid subtitles: {error}', file=sys.stderr)
        return EXIT_INVALID_INPUT
    if isinstance(error, LockUnavailableError):
        print(f'job lock unavailable: {error}', file=sys.stderr)
        return EXIT_TOOL_FAILURE
    if isinstance(error, (StorageError, MediaToolError, OSError)):
        print(f'job preparation failed: {error}', file=sys.stderr)
        return EXIT_TOOL_FAILURE
    raise error


def _render_prepare(result: PrepareResult) -> str:
    """Render the prepare result as human readable text."""
    manifest = result.manifest
    media_view = manifest['media']
    disposition = (
        'unchanged'
        if result.unchanged
        else ('created' if result.created else 'updated')
    )
    lines = [
        f'state: revision {manifest["revision"]} ({disposition})',
        (
            'media: '
            f'{media_view["video_stream"]["codec"]}'
            f' {media_view["video_stream"]["pix_fmt"]}'
            f' {media_view["video_stream"]["width"]}x'
            f'{media_view["video_stream"]["height"]}; audio'
            f' {media_view["audio_stream"]["codec"]}'
            f' {media_view["audio_stream"]["profile"] or ""}'
            f' {media_view["audio_stream"]["channels"]}ch,'
            f' selection {media_view["audio_selection"]["mode"]}'
            f' index {media_view["audio_selection"]["index"]},'
            f' {media_view["audio_disposition"]}'
        ),
        f'segments: {len(manifest["segments"])}',
    ]
    reuse = result.reuse
    for stage in ('synthesis', 'fitting', 'subtitle', 'container'):
        view = reuse[stage]
        if isinstance(view['invalidated'], dict):
            summary = (
                f'{len(view["preserved"])} preserved,'
                f' {len(view["invalidated"])} invalidated,'
                f' {len(view["pending"])} pending'
            )
        elif view['invalidated']:
            summary = 'invalidated'
        else:
            summary = 'preserved'
        lines.append(f'reuse {stage}: {summary}')
    blocked = manifest['blocked']
    lines.append(f'blocked conditions: {len(blocked)}')
    return '\n'.join(lines)


def _run_prepare(args: argparse.Namespace) -> int:
    """Run the prepare command and return its exit code."""
    try:
        settings = load_settings(
            str(args.settings_path) if args.settings_path else None
        )
    except SettingsError as error:
        print(f'invalid settings: {error}', file=sys.stderr)
        return EXIT_INVALID_INPUT
    except OSError as error:
        print(f'settings file cannot be read: {error}', file=sys.stderr)
        return EXIT_TOOL_FAILURE
    try:
        result = prepare_job(
            video_path=args.video,
            srt_path=args.source_srt,
            translation_path=args.translation,
            job_dir=args.job_dir,
            output_path=args.output,
            source_audio=args.source_audio,
            settings=settings,
        )
    except (DestinationExistsError, ContractError) as error:
        return _prepare_error_exit(error)
    except (
        SubtitlesError,
        LockUnavailableError,
        StorageError,
        MediaToolError,
        OSError,
    ) as error:
        return _prepare_error_exit(error)
    print(f'prepared job: {args.job_dir}')
    print(_render_prepare(result))
    return EXIT_OK


def _run_status(args: argparse.Namespace) -> int:
    """Run the status command and return its exit code."""
    try:
        report = read_job_status(args.job_dir)
    except ContractError as error:
        print(f'invalid job: {error}', file=sys.stderr)
        return EXIT_INVALID_INPUT
    except OSError as error:
        print(f'job status read failed: {error}', file=sys.stderr)
        return EXIT_TOOL_FAILURE
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(_render_status(report))
    return EXIT_OK


def _render_status(report: dict[str, object]) -> str:
    """Render the status report as human readable text."""
    lines = [
        f'job: {report["job_dir"]}',
        f'schema_version: {report["schema_version"]}',
        f'revision: {report["revision"]}',
        f'segments: {report["segment_count"]}',
        f'output: {report["output_path"]}',
    ]
    stages = report['stages']
    assert isinstance(stages, dict)
    for stage in ('synthesis', 'fitting', 'subtitle', 'container'):
        state = stages[stage]
        assert isinstance(state, dict)
        lines.append(f'stage {stage}: {state["state"]}')
    blocked = report['blocked']
    assert isinstance(blocked, list)
    lines.append(f'blocked conditions: {len(blocked)}')
    for condition in blocked:
        lines.append(f'  {condition}')
    verdict = 'BLOCKED' if blocked else 'OK'
    lines.append(f'status: {verdict}')
    return '\n'.join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line interface and return the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == 'doctor':
            return _run_doctor(args.as_json)
        if args.command == 'prepare':
            return _run_prepare(args)
        if args.command == 'status':
            return _run_status(args)
    except KeyboardInterrupt:
        print(
            'interrupted; job state stays at its last checkpoint',
            file=sys.stderr,
        )
        return EXIT_INTERRUPTED
    parser.error(f'unhandled command: {args.command}')
    raise AssertionError('unreachable')


__all__ = [
    'EXIT_INTERRUPTED',
    'EXIT_INVALID_INPUT',
    'EXIT_OK',
    'EXIT_TOOL_FAILURE',
    'SETTINGS_ENV_VAR',
    'build_parser',
    'main',
]

"""Command line interface of the lecture_localizer package."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from lecture_localizer import __version__
from lecture_localizer.contracts import ContractError
from lecture_localizer.jobs import (
    PrepareResult,
    RunResult,
    SpendingLimits,
    assemble_job_pipeline,
    describe_pipeline_failure,
    prepare_job,
    read_job_status,
    recover_response,
    run_job_pipeline,
)
from lecture_localizer.media import DoctorReport, MediaToolError, run_doctor
from lecture_localizer.settings import (
    SETTINGS_ENV_VAR,
    SettingsError,
    load_settings,
    spending_limits_from_file,
)
from lecture_localizer.storage import (
    DestinationExistsError,
    LockUnavailableError,
    StorageError,
)
from lecture_localizer.subtitles import SubtitlesError

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_INVALID_INPUT = 3
EXIT_PAID_DENIED = 4
EXIT_PROVIDER = 5
EXIT_VERIFICATION = 6
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
    run = commands.add_parser(
        'run',
        help='Execute the authorized job and publish the verified result',
    )
    run.add_argument(
        '--job',
        required=True,
        type=Path,
        dest='job_dir',
        help='Existing prepared job directory',
    )
    run.add_argument(
        '--allow-paid',
        action='store_true',
        dest='allow_paid',
        help='Explicitly permit paid speech requests when needed',
    )
    run.add_argument(
        '--limits',
        type=Path,
        default=None,
        dest='limits_path',
        help='JSON file with explicit numeric spending limits',
    )
    run.add_argument(
        '--retry-uncertain',
        default=None,
        dest='retry_uncertain',
        help='Operation ID of one unresolved paid operation to retry',
    )
    assemble = commands.add_parser(
        'assemble',
        help='Assemble and publish from retained artifacts offline',
    )
    assemble.add_argument(
        '--job',
        required=True,
        type=Path,
        dest='job_dir',
        help='Existing prepared job directory',
    )
    assemble.add_argument(
        '--output',
        type=Path,
        default=None,
        dest='output_path',
        help='Additional fresh output path for a second copy',
    )
    recover = commands.add_parser(
        'recover',
        help='Revalidate a retained failed response offline',
    )
    recover.add_argument('--job', required=True, type=Path, dest='job_dir')
    recover.add_argument('--operation', required=True, dest='operation_id')
    recover.add_argument('--attempt', required=True, type=int)
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
    assembly = report.get('assembly')
    if isinstance(assembly, dict):
        lines.append(f'assembly complete: {assembly["complete"]}')
    publication = report.get('publication')
    if isinstance(publication, dict):
        lines.append(f'publication: {publication["state"]}')
    blocked = report['blocked']
    assert isinstance(blocked, list)
    lines.append(f'blocked conditions: {len(blocked)}')
    for condition in blocked:
        lines.append(f'  {condition}')
    verdict = 'BLOCKED' if blocked else 'OK'
    lines.append(f'status: {verdict}')
    return '\n'.join(lines)


def _render_run(result: RunResult) -> str:
    """Render the run or assemble outcome as human readable text."""
    lines = [
        f'{result.command}: revision {result.revision}',
    ]
    if result.speech is not None:
        lines.append(
            f'speech: {len(result.speech.completed)} completed,'
            f' {len(result.speech.reused)} reused,'
            f' {result.speech.request_count} requests'
        )
    lines.append(f'built units: {len(result.built)}')
    for unit in result.built:
        lines.append(f'  built {unit}')
    lines.append(f'reused units: {len(result.reused)}')
    verification = result.verification
    if verification is not None:
        verdict = 'passed' if verification.get('ok') else 'FAILED'
        checks = verification.get('checks', [])
        lines.append(f'verification: {verdict} ({len(checks)} checks)')
    else:
        lines.append('verification: registered candidate reused')
    publication = result.publication
    lines.append(f'publication: {publication["state"]} {publication["path"]}')
    lines.append(f'report: {result.report_path}')
    lines.append(
        'pending human checks: listening acceptance and reference'
        ' player startup remain manual'
    )
    return '\n'.join(lines)


def _load_run_limits(
    args: argparse.Namespace,
) -> tuple[SpendingLimits | None, int]:
    """Load the spending limits file for one run invocation."""
    if args.limits_path is None:
        return None, EXIT_OK
    try:
        return spending_limits_from_file(args.limits_path), EXIT_OK
    except SettingsError as error:
        print(f'invalid spending limits: {error}', file=sys.stderr)
        return None, EXIT_INVALID_INPUT
    except OSError as error:
        print(f'spending limits file cannot be read: {error}', file=sys.stderr)
        return None, EXIT_TOOL_FAILURE


def _run_run(args: argparse.Namespace) -> int:
    """Run the run command and return its exit code."""
    try:
        settings = load_settings()
    except SettingsError as error:
        print(f'invalid settings: {error}', file=sys.stderr)
        return EXIT_INVALID_INPUT
    except OSError as error:
        print(f'settings file cannot be read: {error}', file=sys.stderr)
        return EXIT_TOOL_FAILURE
    limits, limits_exit = _load_run_limits(args)
    if limits_exit != EXIT_OK:
        return limits_exit
    try:
        result = run_job_pipeline(
            args.job_dir,
            allow_paid=args.allow_paid,
            limits=limits,
            settings=settings,
            retry_uncertain=args.retry_uncertain,
        )
    except KeyboardInterrupt:
        print(
            'interrupted; job state stays at its last checkpoint',
            file=sys.stderr,
        )
        return EXIT_INTERRUPTED
    except Exception as error:
        exit_code, kind, detail = describe_pipeline_failure(error)
        print(f'{kind}: {detail}', file=sys.stderr)
        return exit_code
    print(_render_run(result))
    return EXIT_OK


def _run_assemble(args: argparse.Namespace) -> int:
    """Run the assemble command and return its exit code."""
    try:
        settings = load_settings()
    except SettingsError as error:
        print(f'invalid settings: {error}', file=sys.stderr)
        return EXIT_INVALID_INPUT
    except OSError as error:
        print(f'settings file cannot be read: {error}', file=sys.stderr)
        return EXIT_TOOL_FAILURE
    try:
        result = assemble_job_pipeline(
            args.job_dir,
            settings=settings,
            extra_output=args.output_path,
        )
    except KeyboardInterrupt:
        print(
            'interrupted; job state stays at its last checkpoint',
            file=sys.stderr,
        )
        return EXIT_INTERRUPTED
    except Exception as error:
        exit_code, kind, detail = describe_pipeline_failure(error)
        print(f'{kind}: {detail}', file=sys.stderr)
        return exit_code
    print(_render_run(result))
    return EXIT_OK


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
        if args.command == 'run':
            return _run_run(args)
        if args.command == 'assemble':
            return _run_assemble(args)
        if args.command == 'recover':
            return _run_recover(args)
    except KeyboardInterrupt:
        print(
            'interrupted; job state stays at its last checkpoint',
            file=sys.stderr,
        )
        return EXIT_INTERRUPTED
    parser.error(f'unhandled command: {args.command}')
    raise AssertionError('unreachable')


def _run_recover(args: argparse.Namespace) -> int:
    """Revalidate recorded response bytes without a paid operation."""
    try:
        receipt = recover_response(
            args.job_dir,
            operation_id=args.operation_id,
            attempt_number=args.attempt,
        )
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    except Exception as error:
        exit_code, kind, detail = describe_pipeline_failure(error)
        print(f'{kind}: {detail}', file=sys.stderr)
        return exit_code
    print(
        json.dumps(
            {'command': 'recover', 'receipt': receipt},
            ensure_ascii=False,
            indent=2,
        )
    )
    return EXIT_OK


__all__ = [
    'EXIT_INTERRUPTED',
    'EXIT_INVALID_INPUT',
    'EXIT_OK',
    'EXIT_PROVIDER',
    'EXIT_TOOL_FAILURE',
    'EXIT_USAGE',
    'EXIT_VERIFICATION',
    'SETTINGS_ENV_VAR',
    'build_parser',
    'main',
]

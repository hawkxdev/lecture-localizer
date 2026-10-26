"""Command line interface for lecture-localizer."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from lecture_localizer import __version__
from lecture_localizer.media import DoctorReport, MediaToolError, run_doctor
from lecture_localizer.settings import SETTINGS_ENV_VAR, SettingsError, load_settings

EXIT_OK = 0
EXIT_INVALID_INPUT = 3
EXIT_TOOL_FAILURE = 7


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser advertising implemented commands."""
    parser = argparse.ArgumentParser(
        prog="lecture-localizer",
        description="Localize lecture videos while preserving the original media.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser(
        "doctor",
        help="Check local media tools and capabilities without provider access",
    )
    doctor.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Print the diagnostics report as JSON",
    )
    return parser


def _render_report(report: DoctorReport) -> str:
    lines = []
    for tool in report.tools:
        status = "OK" if tool.ok else "FAILED"
        lines.append(
            f"{tool.name:8} {status:6} {tool.path or ''} {tool.version or ''}".rstrip()
        )
        if tool.detail:
            lines.append(f"         detail: {tool.detail}")
        for capability in tool.capabilities:
            cap_status = "OK" if capability.ok else "FAILED"
            lines.append(f"  {capability.requirement:28} {cap_status}")
            if capability.detail:
                lines.append(f"    detail: {capability.detail}")
    verdict = "OK" if report.ok else "FAILED"
    lines.append(f"doctor: {verdict}")
    return "\n".join(lines)


def _run_doctor(as_json: bool) -> int:
    try:
        settings = load_settings()
    except SettingsError as error:
        print(f"invalid settings: {error}", file=sys.stderr)
        return EXIT_INVALID_INPUT
    except OSError as error:
        print(f"settings file cannot be read: {error}", file=sys.stderr)
        return EXIT_TOOL_FAILURE
    try:
        report = run_doctor(settings)
    except MediaToolError as error:
        print(f"media tool failure: {error}", file=sys.stderr)
        return EXIT_TOOL_FAILURE
    if as_json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(_render_report(report))
    return EXIT_OK if report.ok else EXIT_TOOL_FAILURE


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line interface and return the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "doctor":
        return _run_doctor(args.as_json)
    parser.error(f"unhandled command: {args.command}")
    raise AssertionError("unreachable")


__all__ = [
    "EXIT_INVALID_INPUT",
    "EXIT_OK",
    "EXIT_TOOL_FAILURE",
    "SETTINGS_ENV_VAR",
    "build_parser",
    "main",
]

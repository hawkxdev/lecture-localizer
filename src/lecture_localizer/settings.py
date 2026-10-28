"""Validated nonsecret application settings."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lecture_localizer.jobs import SpendingLimits

SETTINGS_ENV_VAR = 'LECTURE_LOCALIZER_SETTINGS'
DEFAULT_TIMEOUT_SECONDS = 120.0
MAX_TIMEOUT_SECONDS = 3600.0
DEFAULT_FITTING_MAX_SPEEDUP = 1.2
MAX_FITTING_MAX_SPEEDUP = 10.0

_TOOL_PATH_FIELDS = ('ffmpeg_path', 'ffprobe_path', 'mp4box_path')
_BOOL_FIELDS = (
    'startup_narration_default',
    'startup_original_default',
    'startup_subtitles_default',
    'startup_subtitles_forced',
)
_BOOL_DEFAULTS = {
    'startup_narration_default': True,
    'startup_original_default': False,
    'startup_subtitles_default': False,
    'startup_subtitles_forced': False,
}


class SettingsError(ValueError):
    """Settings input is invalid."""


@dataclass(frozen=True)
class Settings:
    """Validated nonsecret tool, timing and job policy settings."""

    ffmpeg_path: str | None = None
    ffprobe_path: str | None = None
    mp4box_path: str | None = None
    subprocess_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    fitting_max_speedup: float = DEFAULT_FITTING_MAX_SPEEDUP
    startup_narration_default: bool = True
    startup_original_default: bool = False
    startup_subtitles_default: bool = False
    startup_subtitles_forced: bool = False


def _validate_tool_path(name: str, value: Any) -> str | None:
    """Validate one optional configured tool path."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SettingsError(f'{name} must be a nonempty string or null')
    return value


def _validate_timeout(value: Any) -> float:
    """Validate the subprocess timeout bound."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SettingsError('subprocess_timeout_seconds must be a number')
    try:
        timeout = float(value)
    except OverflowError as error:
        raise SettingsError(
            'subprocess_timeout_seconds is outside the representable range'
        ) from error
    if (
        not math.isfinite(timeout)
        or timeout <= 0
        or timeout > MAX_TIMEOUT_SECONDS
    ):
        raise SettingsError(
            'subprocess_timeout_seconds must be within (0, '
            f'{MAX_TIMEOUT_SECONDS}]'
        )
    return timeout


def _validate_bool(name: str, value: Any) -> bool:
    """Validate one boolean job policy flag."""
    if not isinstance(value, bool):
        raise SettingsError(f'{name} must be a boolean')
    return value


def _validate_speedup(value: Any) -> float:
    """Validate the fitting speedup cap."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SettingsError('fitting_max_speedup must be a number')
    try:
        speedup = float(value)
    except OverflowError as error:
        raise SettingsError(
            'fitting_max_speedup is outside the representable range'
        ) from error
    if (
        not math.isfinite(speedup)
        or speedup < 1.0
        or speedup > MAX_FITTING_MAX_SPEEDUP
    ):
        raise SettingsError(
            'fitting_max_speedup must be within [1, '
            f'{MAX_FITTING_MAX_SPEEDUP}]'
        )
    return speedup


def settings_from_mapping(data: Any) -> Settings:
    """Build validated settings from a parsed mapping."""
    if not isinstance(data, dict):
        raise SettingsError('settings root must be a JSON object')
    known = (
        set(_TOOL_PATH_FIELDS)
        | {'subprocess_timeout_seconds', 'fitting_max_speedup'}
        | set(_BOOL_FIELDS)
    )
    unknown = sorted(set(data) - known)
    if unknown:
        raise SettingsError(f'unknown settings fields: {", ".join(unknown)}')
    paths = {
        name: _validate_tool_path(name, data.get(name))
        for name in _TOOL_PATH_FIELDS
    }
    timeout = _validate_timeout(
        data.get('subprocess_timeout_seconds', DEFAULT_TIMEOUT_SECONDS)
    )
    speedup = _validate_speedup(
        data.get('fitting_max_speedup', DEFAULT_FITTING_MAX_SPEEDUP)
    )
    flags = {
        name: _validate_bool(name, data.get(name, _BOOL_DEFAULTS[name]))
        for name in _BOOL_FIELDS
    }
    return Settings(
        ffmpeg_path=paths['ffmpeg_path'],
        ffprobe_path=paths['ffprobe_path'],
        mp4box_path=paths['mp4box_path'],
        subprocess_timeout_seconds=timeout,
        fitting_max_speedup=speedup,
        startup_narration_default=flags['startup_narration_default'],
        startup_original_default=flags['startup_original_default'],
        startup_subtitles_default=flags['startup_subtitles_default'],
        startup_subtitles_forced=flags['startup_subtitles_forced'],
    )


def settings_from_file(path: str | Path) -> Settings:
    """Load validated settings from a JSON file."""
    with open(path, encoding='utf-8') as handle:
        try:
            data = json.load(handle)
        except json.JSONDecodeError as error:
            raise SettingsError(
                f'settings file is not valid JSON: {error}'
            ) from error
        except UnicodeDecodeError as error:
            raise SettingsError('settings file is not valid UTF-8') from error
    return settings_from_mapping(data)


def load_settings(explicit_path: str | None = None) -> Settings:
    """Load settings from an explicit path, env var, or defaults."""
    path = explicit_path or os.environ.get(SETTINGS_ENV_VAR)
    if path is None:
        return Settings()
    return settings_from_file(path)


_LIMITS_FIELDS = (
    'max_requests',
    'max_text_chars',
    'max_cost_estimate',
    'price_per_request',
    'price_basis',
)


def spending_limits_from_mapping(data: Any) -> SpendingLimits:
    """Build validated spending limits from a parsed mapping."""
    from lecture_localizer.jobs import SpendingLimits

    if not isinstance(data, dict):
        raise SettingsError('spending limits root must be a JSON object')
    unknown = sorted(set(data) - set(_LIMITS_FIELDS))
    if unknown:
        raise SettingsError(
            f'unknown spending limit fields: {", ".join(unknown)}'
        )
    missing = sorted(name for name in _LIMITS_FIELDS if name not in data)
    if missing:
        raise SettingsError(
            f'missing spending limit fields: {", ".join(missing)}'
        )
    try:
        return SpendingLimits(
            max_requests=data['max_requests'],
            max_text_chars=data['max_text_chars'],
            max_cost_estimate=data['max_cost_estimate'],
            price_per_request=data['price_per_request'],
            price_basis=data['price_basis'],
        )
    except ValueError as error:
        raise SettingsError(f'invalid spending limits: {error}') from error


def spending_limits_from_file(path: str | Path) -> SpendingLimits:
    """Load validated spending limits from one JSON file."""
    with open(path, encoding='utf-8') as handle:
        try:
            data = json.load(handle)
        except json.JSONDecodeError as error:
            raise SettingsError(
                f'spending limits file is not valid JSON: {error}'
            ) from error
        except UnicodeDecodeError as error:
            raise SettingsError(
                'spending limits file is not valid UTF-8'
            ) from error
    return spending_limits_from_mapping(data)

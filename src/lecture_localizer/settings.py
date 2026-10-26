"""Validated nonsecret application settings."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SETTINGS_ENV_VAR = "LECTURE_LOCALIZER_SETTINGS"
DEFAULT_TIMEOUT_SECONDS = 120.0
MAX_TIMEOUT_SECONDS = 3600.0

_TOOL_PATH_FIELDS = ("ffmpeg_path", "ffprobe_path", "mp4box_path")


class SettingsError(ValueError):
    """Settings input is invalid."""


@dataclass(frozen=True)
class Settings:
    """Validated nonsecret tool and timing settings."""

    ffmpeg_path: str | None = None
    ffprobe_path: str | None = None
    mp4box_path: str | None = None
    subprocess_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


def _validate_tool_path(name: str, value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SettingsError(f"{name} must be a nonempty string or null")
    return value


def _validate_timeout(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SettingsError("subprocess_timeout_seconds must be a number")
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0 or timeout > MAX_TIMEOUT_SECONDS:
        raise SettingsError(
            f"subprocess_timeout_seconds must be within (0, {MAX_TIMEOUT_SECONDS}]"
        )
    return timeout


def settings_from_mapping(data: Any) -> Settings:
    """Build validated settings from a parsed mapping."""
    if not isinstance(data, dict):
        raise SettingsError("settings root must be a JSON object")
    known = set(_TOOL_PATH_FIELDS) | {"subprocess_timeout_seconds"}
    unknown = sorted(set(data) - known)
    if unknown:
        raise SettingsError(f"unknown settings fields: {', '.join(unknown)}")
    paths = {
        name: _validate_tool_path(name, data.get(name)) for name in _TOOL_PATH_FIELDS
    }
    timeout = _validate_timeout(
        data.get("subprocess_timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    )
    return Settings(subprocess_timeout_seconds=timeout, **paths)


def settings_from_file(path: str | Path) -> Settings:
    """Load validated settings from a JSON file."""
    with open(path, encoding="utf-8") as handle:
        try:
            data = json.load(handle)
        except json.JSONDecodeError as error:
            raise SettingsError(f"settings file is not valid JSON: {error}") from error
        except UnicodeDecodeError as error:
            raise SettingsError("settings file is not valid UTF-8") from error
    return settings_from_mapping(data)


def load_settings(explicit_path: str | None = None) -> Settings:
    """Load settings from an explicit path, the settings env var, or defaults."""
    path = explicit_path or os.environ.get(SETTINGS_ENV_VAR)
    if path is None:
        return Settings()
    return settings_from_file(path)

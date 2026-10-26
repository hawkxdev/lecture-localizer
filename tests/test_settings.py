from __future__ import annotations

import json
from pathlib import Path

import pytest

from lecture_localizer.settings import (
    SETTINGS_ENV_VAR,
    Settings,
    SettingsError,
    load_settings,
    settings_from_file,
    settings_from_mapping,
)


def test_defaults_are_valid() -> None:
    settings = Settings()
    assert settings.ffmpeg_path is None
    assert settings.subprocess_timeout_seconds == 120.0


def test_mapping_builds_settings() -> None:
    settings = settings_from_mapping(
        {"ffmpeg_path": "/opt/tools/ffmpeg", "subprocess_timeout_seconds": 15}
    )
    assert settings.ffmpeg_path == "/opt/tools/ffmpeg"
    assert settings.subprocess_timeout_seconds == 15.0


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(SettingsError, match="unknown settings fields: api_key"):
        settings_from_mapping({"api_key": "value"})


def test_credential_like_field_is_rejected_as_unknown() -> None:
    with pytest.raises(SettingsError, match="unknown settings fields: xai_api_key"):
        settings_from_mapping({"xai_api_key": "sk-secret"})


def test_non_dict_root_is_rejected() -> None:
    with pytest.raises(SettingsError, match="JSON object"):
        settings_from_mapping([1, 2])


def test_empty_tool_path_is_rejected() -> None:
    with pytest.raises(SettingsError, match="ffmpeg_path"):
        settings_from_mapping({"ffmpeg_path": "   "})


def test_bool_timeout_is_rejected() -> None:
    with pytest.raises(SettingsError, match="must be a number"):
        settings_from_mapping({"subprocess_timeout_seconds": True})


def test_nonpositive_timeout_is_rejected() -> None:
    with pytest.raises(SettingsError, match="within"):
        settings_from_mapping({"subprocess_timeout_seconds": 0})


def test_oversized_timeout_is_rejected() -> None:
    with pytest.raises(SettingsError, match="within"):
        settings_from_mapping({"subprocess_timeout_seconds": 3601})


def test_non_finite_timeout_is_rejected() -> None:
    with pytest.raises(SettingsError, match="within"):
        settings_from_mapping({"subprocess_timeout_seconds": float("inf")})


def test_file_loading_supports_special_characters(tmp_path: Path) -> None:
    directory = tmp_path / "settings dir#with 'quote' юникод"
    directory.mkdir()
    path = directory / "settings.json"
    path.write_text(json.dumps({"ffprobe_path": "/opt/ffprobe"}), encoding="utf-8")
    settings = settings_from_file(path)
    assert settings.ffprobe_path == "/opt/ffprobe"


def test_invalid_json_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(SettingsError, match="not valid JSON"):
        settings_from_file(path)


def test_load_settings_without_sources_returns_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(SETTINGS_ENV_VAR, raising=False)
    assert load_settings() == Settings()


def test_load_settings_reads_env_var_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "with space#and#hash.json"
    path.write_text(json.dumps({"subprocess_timeout_seconds": 42}), encoding="utf-8")
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(path))
    assert load_settings().subprocess_timeout_seconds == 42.0


def test_load_settings_missing_env_file_raises_os_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(tmp_path / "absent.json"))
    with pytest.raises(FileNotFoundError):
        load_settings()

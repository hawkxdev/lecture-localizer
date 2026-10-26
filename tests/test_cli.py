from __future__ import annotations

import json
from importlib.metadata import version
from pathlib import Path

import pytest

from conftest import real_tool_paths
from lecture_localizer import __version__
from lecture_localizer.cli import main
from lecture_localizer.settings import SETTINGS_ENV_VAR


def test_help_advertises_only_doctor(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "{doctor}" in out
    assert "prepare" not in out
    assert "assemble" not in out
    assert "status" not in out


def test_version_flag_prints_package_version(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_unknown_command_exits_with_usage_code() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["no-such-command"])
    assert excinfo.value.code == 2


def test_missing_command_exits_with_usage_code() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2


def test_invalid_settings_exit_with_code_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "bad-settings.json"
    path.write_text(json.dumps({"api_key": "nope"}), encoding="utf-8")
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(path))
    assert main(["doctor"]) == 3
    assert "invalid settings" in capsys.readouterr().err


def test_unreadable_settings_exit_with_code_7(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(tmp_path / "absent.json"))
    assert main(["doctor"]) == 7
    assert "cannot be read" in capsys.readouterr().err


def test_installed_metadata_matches_package_version() -> None:
    assert version("lecture-localizer") == __version__


def _require_real_tools() -> None:
    paths = real_tool_paths()
    missing = [name for name, path in paths.items() if path is None]
    if missing:
        pytest.fail(f"required setup prerequisite: local media tools absent: {missing}")


def test_doctor_json_success_with_real_tools(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _require_real_tools()
    monkeypatch.delenv(SETTINGS_ENV_VAR, raising=False)
    assert main(["doctor", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["command"] == "doctor"
    names = {tool["name"] for tool in payload["tools"]}
    assert names == {"ffmpeg", "ffprobe", "mp4box"}


def test_doctor_human_success_with_real_tools(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _require_real_tools()
    monkeypatch.delenv(SETTINGS_ENV_VAR, raising=False)
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "doctor: OK" in out
    assert "mp4box track flag rewrite" in out


def test_doctor_json_failure_reports_missing_tools_exit_7(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.delenv(SETTINGS_ENV_VAR, raising=False)
    assert main(["doctor", "--json"]) == 7
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert all(tool["ok"] is False for tool in payload["tools"])


def test_doctor_json_reports_unusable_tool_exit_7(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    unusable = tmp_path / "not-executable-ffmpeg"
    unusable.write_text("data", encoding="utf-8")
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"ffmpeg_path": str(unusable)}), encoding="utf-8")
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings))
    assert main(["doctor", "--json"]) == 7
    payload = json.loads(capsys.readouterr().out)
    ffmpeg = payload["tools"][0]
    assert ffmpeg["name"] == "ffmpeg"
    assert ffmpeg["ok"] is False
    assert "not an executable file" in ffmpeg["detail"]


def test_doctor_never_reads_or_prints_provider_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _require_real_tools()
    monkeypatch.delenv(SETTINGS_ENV_VAR, raising=False)
    sentinel = "sk-test-doctor-sentinel"
    monkeypatch.setenv("XAI_API_KEY", sentinel)
    assert main(["doctor", "--json"]) == 0
    captured = capsys.readouterr()
    assert sentinel not in captured.out
    assert sentinel not in captured.err


def test_doctor_reports_mp4box_functional_capability(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _require_real_tools()
    monkeypatch.delenv(SETTINGS_ENV_VAR, raising=False)
    assert main(["doctor", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    mp4box = next(tool for tool in payload["tools"] if tool["name"] == "mp4box")
    rewrite = next(
        cap
        for cap in mp4box["capabilities"]
        if cap["requirement"] == "mp4box track flag rewrite"
    )
    assert rewrite["ok"] is True

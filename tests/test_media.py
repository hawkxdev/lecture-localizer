from __future__ import annotations

from pathlib import Path

import pytest

from conftest import (
    ENV_DUMP_TOOL,
    FAKE_FFMPEG_NO_ATEMPO,
    fake_tool_dir,
    write_executable,
)
from lecture_localizer.media import (
    Capability,
    MediaToolError,
    checked_run,
    checked_run_strict,
    discover_tool,
    listing_contains,
    probe_ffmpeg_capabilities,
    probe_version,
    run_doctor,
    scrubbed_environment,
)
from lecture_localizer.settings import Settings


def test_scrubbed_environment_removes_credential_variables() -> None:
    base = {
        "XAI_API_KEY": "secret",
        "MY_TOKEN": "t",
        "SECRET_VALUE": "s",
        "PASSWORD": "p",
        "PATH": "/bin",
    }
    scrubbed = scrubbed_environment(base)
    assert scrubbed == {"PATH": "/bin"}


def test_scrubbed_environment_keeps_regular_variables() -> None:
    scrubbed = scrubbed_environment({"PATH": "/bin", "HOME": "/root"})
    assert scrubbed == {"PATH": "/bin", "HOME": "/root"}


def test_checked_run_passes_scrubbed_env_to_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = write_executable(tmp_path / "envdump", ENV_DUMP_TOOL)
    monkeypatch.setenv("XAI_API_KEY", "sk-live-value")
    result = checked_run([str(tool)], timeout_seconds=10)
    assert "xai_api_key_seen=" in result.stdout
    assert "sk-live-value" not in result.stdout
    assert "path_present=yes" in result.stdout


def test_checked_run_timeout_raises(tmp_path: Path) -> None:
    tool = write_executable(tmp_path / "sleeper", "#!/bin/sh\nsleep 5\n")
    with pytest.raises(MediaToolError, match="timed out"):
        checked_run([str(tool)], timeout_seconds=0.2)


def test_checked_run_missing_binary_raises(tmp_path: Path) -> None:
    with pytest.raises(MediaToolError, match="executable not found"):
        checked_run([str(tmp_path / "absent")], timeout_seconds=5)


def test_checked_run_keeps_nonzero_exit(tmp_path: Path) -> None:
    tool = write_executable(tmp_path / "failer", "#!/bin/sh\nexit 3\n")
    result = checked_run([str(tool)], timeout_seconds=5)
    assert result.returncode == 3


def test_checked_run_strict_raises_on_nonzero(tmp_path: Path) -> None:
    tool = write_executable(tmp_path / "failer", "#!/bin/sh\necho boom >&2\nexit 3\n")
    with pytest.raises(MediaToolError, match="exited with code 3"):
        checked_run_strict([str(tool)], timeout_seconds=5)


def test_discover_tool_via_configured_path(tmp_path: Path) -> None:
    tool = write_executable(tmp_path / "ffmpeg", "#!/bin/sh\nexit 0\n")
    assert discover_tool("ffmpeg", str(tool)) == tool


def test_discover_tool_rejects_missing_configured_path(tmp_path: Path) -> None:
    absent = tmp_path / "absent"
    with pytest.raises(MediaToolError, match="not an executable file"):
        discover_tool("ffmpeg", str(absent))


def test_discover_tool_rejects_non_executable_path(tmp_path: Path) -> None:
    plain = tmp_path / "plain.txt"
    plain.write_text("data", encoding="utf-8")
    with pytest.raises(MediaToolError, match="not an executable file"):
        discover_tool("ffmpeg", str(plain))


def test_discover_tool_rejects_missing_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(MediaToolError, match="not found on PATH"):
        discover_tool("ffmpeg", None)


def test_discover_tool_finds_mp4box_by_exact_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = fake_tool_dir(tmp_path)
    monkeypatch.setenv("PATH", str(tools["bin"]))
    assert discover_tool("mp4box", None) == tools["mp4box"]


def test_probe_version_parses_real_style_output(tmp_path: Path) -> None:
    tools = fake_tool_dir(tmp_path)
    assert probe_version(tools["ffmpeg"], 5) == "7.1"


def test_probe_version_rejects_unparseable_output(tmp_path: Path) -> None:
    tool = write_executable(
        tmp_path / "ffmpeg", "#!/bin/sh\necho nothing useful\nexit 0\n"
    )
    with pytest.raises(MediaToolError, match="could not parse a version"):
        probe_version(tool, 5)


def test_probe_version_rejects_nonzero_exit(tmp_path: Path) -> None:
    tool = write_executable(tmp_path / "ffmpeg", "#!/bin/sh\nexit 1\n")
    with pytest.raises(MediaToolError, match="exited with code 1"):
        probe_version(tool, 5)


def test_listing_contains_exact_token() -> None:
    listing = (
        " V....D h264                 H.264 decoder\n"
        " A..... aac_at               aac (codec aac)\n"
    )
    assert listing_contains(listing, "h264")
    assert listing_contains(listing, "aac_at")
    assert not listing_contains(listing, "aac")
    assert not listing_contains(listing, "264")


def test_capability_probe_distinguishes_missing_filter(tmp_path: Path) -> None:
    tools = fake_tool_dir(tmp_path, ffmpeg_script=FAKE_FFMPEG_NO_ATEMPO)
    capabilities = probe_ffmpeg_capabilities(tools["ffmpeg"], 5)
    assert Capability("decoder h264", True) in capabilities
    missing = [c for c in capabilities if not c.ok]
    assert [c.requirement for c in missing] == ["filter atempo"]


def test_doctor_with_fake_tools_keeps_listings_but_unmet_functional_probe(
    tmp_path: Path,
) -> None:
    tools = fake_tool_dir(tmp_path)
    settings = Settings(
        ffmpeg_path=str(tools["ffmpeg"]),
        ffprobe_path=str(tools["ffprobe"]),
        mp4box_path=str(tools["mp4box"]),
    )
    report = run_doctor(settings)
    assert not report.ok
    ffmpeg, ffprobe, mp4box = report.tools
    assert ffmpeg.ok
    assert [c.ok for c in ffmpeg.capabilities] == [True, True, True, True]
    assert ffprobe.ok
    assert not mp4box.ok
    assert mp4box.capabilities[0].requirement == "mp4box track flag rewrite"
    assert mp4box.capabilities[0].ok is False
    assert mp4box.capabilities[0].detail


def test_doctor_with_broken_capability_fails_without_functional_probe(
    tmp_path: Path,
) -> None:
    tools = fake_tool_dir(tmp_path, ffmpeg_script=FAKE_FFMPEG_NO_ATEMPO)
    settings = Settings(
        ffmpeg_path=str(tools["ffmpeg"]),
        ffprobe_path=str(tools["ffprobe"]),
        mp4box_path=str(tools["mp4box"]),
    )
    report = run_doctor(settings)
    assert not report.ok
    ffmpeg = report.tools[0]
    assert not ffmpeg.ok
    assert ffmpeg.detail == "required ffmpeg capabilities are missing"
    assert report.tools[2].capabilities == ()


def test_doctor_with_missing_tool_reports_failure(tmp_path: Path) -> None:
    settings = Settings(mp4box_path=str(tmp_path / "absent"))
    report = run_doctor(settings)
    assert not report.ok
    assert report.tools[2].path is None
    assert "not an executable file" in report.tools[2].detail


def test_doctor_report_dict_is_json_serializable(tmp_path: Path) -> None:
    import json

    tools = fake_tool_dir(tmp_path)
    settings = Settings(
        ffmpeg_path=str(tools["ffmpeg"]),
        ffprobe_path=str(tools["ffprobe"]),
        mp4box_path=str(tools["mp4box"]),
    )
    payload = run_doctor(settings).to_dict()
    encoded = json.dumps(payload, ensure_ascii=False)
    assert '"command": "doctor"' in encoded
    assert payload["ok"] is False
    assert [tool["name"] for tool in payload["tools"]] == [
        "ffmpeg",
        "ffprobe",
        "mp4box",
    ]

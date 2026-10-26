"""Tests for the command line interface."""

from __future__ import annotations

import hashlib
import json
import os
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pytest

from conftest import CredentialValueTrap, real_tool_paths
from lecture_localizer import __version__
from lecture_localizer.cli import main
from lecture_localizer.media import CREDENTIAL_ENV_PATTERN
from lecture_localizer.settings import SETTINGS_ENV_VAR


def test_help_advertises_implemented_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(['--help'])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert '{doctor,prepare,status}' in out


def test_unimplemented_pipeline_commands_are_rejected() -> None:
    for command in ('run', 'assemble'):
        with pytest.raises(SystemExit) as excinfo:
            main([command])
        assert excinfo.value.code == 2


def test_prepare_requires_all_input_arguments(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(['prepare', '--video', 'v.mp4'])
    assert excinfo.value.code == 2


def test_version_flag_prints_package_version(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(['--version'])
    assert excinfo.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_unknown_command_exits_with_usage_code() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(['no-such-command'])
    assert excinfo.value.code == 2


def test_missing_command_exits_with_usage_code() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2


def test_invalid_settings_exit_with_code_3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / 'bad-settings.json'
    path.write_text(json.dumps({'api_key': 'nope'}), encoding='utf-8')
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(path))
    assert main(['doctor']) == 3
    assert 'invalid settings' in capsys.readouterr().err


def test_unreadable_settings_exit_with_code_7(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(tmp_path / 'absent.json'))
    assert main(['doctor']) == 7
    assert 'cannot be read' in capsys.readouterr().err


def test_huge_integer_timeout_exits_3_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / 'huge-timeout.json'
    path.write_text(
        json.dumps({'subprocess_timeout_seconds': 10**400}), encoding='utf-8'
    )
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(path))
    assert main(['doctor']) == 3
    captured = capsys.readouterr()
    assert 'representable' in captured.err
    assert 'Traceback' not in captured.err
    assert 'Traceback' not in captured.out


def test_installed_metadata_matches_package_version() -> None:
    assert version('lecture-localizer') == __version__


def _require_real_tools() -> None:
    """Fail the test when the real local media tools are absent."""
    paths = real_tool_paths()
    missing = [name for name, path in paths.items() if path is None]
    if missing:
        pytest.fail(
            f'required setup prerequisite: local media tools absent: {missing}'
        )


def test_doctor_json_success_with_real_tools(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _require_real_tools()
    monkeypatch.delenv(SETTINGS_ENV_VAR, raising=False)
    assert main(['doctor', '--json']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['ok'] is True
    assert payload['command'] == 'doctor'
    names = {tool['name'] for tool in payload['tools']}
    assert names == {'ffmpeg', 'ffprobe', 'mp4box'}


def test_doctor_human_success_with_real_tools(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _require_real_tools()
    monkeypatch.delenv(SETTINGS_ENV_VAR, raising=False)
    assert main(['doctor']) == 0
    out = capsys.readouterr().out
    assert 'doctor: OK' in out
    assert 'mp4box track flag rewrite' in out


def test_doctor_json_failure_reports_missing_tools_exit_7(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    empty = tmp_path / 'empty-bin'
    empty.mkdir()
    monkeypatch.setenv('PATH', str(empty))
    monkeypatch.delenv(SETTINGS_ENV_VAR, raising=False)
    assert main(['doctor', '--json']) == 7
    payload = json.loads(capsys.readouterr().out)
    assert payload['ok'] is False
    assert all(tool['ok'] is False for tool in payload['tools'])


def test_doctor_json_reports_unusable_tool_exit_7(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    unusable = tmp_path / 'not-executable-ffmpeg'
    unusable.write_text('data', encoding='utf-8')
    settings = tmp_path / 'settings.json'
    settings.write_text(
        json.dumps({'ffmpeg_path': str(unusable)}), encoding='utf-8'
    )
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings))
    assert main(['doctor', '--json']) == 7
    payload = json.loads(capsys.readouterr().out)
    ffmpeg = payload['tools'][0]
    assert ffmpeg['name'] == 'ffmpeg'
    assert ffmpeg['ok'] is False
    assert 'not executable' in ffmpeg['detail']


def test_doctor_never_reads_or_prints_provider_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _require_real_tools()
    monkeypatch.delenv(SETTINGS_ENV_VAR, raising=False)
    sentinel = 'sk-test-doctor-sentinel'
    monkeypatch.setenv('XAI_API_KEY', sentinel)
    assert main(['doctor', '--json']) == 0
    captured = capsys.readouterr()
    assert sentinel not in captured.out
    assert sentinel not in captured.err


def test_doctor_succeeds_without_reading_credential_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_real_tools()
    monkeypatch.delenv(SETTINGS_ENV_VAR, raising=False)
    canary = 'sk-test-canary-not-real'
    safe = {
        key: canary if CREDENTIAL_ENV_PATTERN.search(key) else value
        for key, value in os.environ.items()
    }
    safe.setdefault('XAI_API_KEY', canary)
    monkeypatch.setattr(os, 'environ', CredentialValueTrap(safe))
    assert main(['doctor', '--json']) == 0


def test_doctor_reports_mp4box_functional_capability(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _require_real_tools()
    monkeypatch.delenv(SETTINGS_ENV_VAR, raising=False)
    assert main(['doctor', '--json']) == 0
    payload = json.loads(capsys.readouterr().out)
    mp4box = next(
        tool for tool in payload['tools'] if tool['name'] == 'mp4box'
    )
    rewrite = next(
        cap
        for cap in mp4box['capabilities']
        if cap['requirement'] == 'mp4box track flag rewrite'
    )
    assert rewrite['ok'] is True


def _prepare_argv(
    video: Path, srt: Path, translation: Path, job: Path, output: Path
) -> list[str]:
    """Build the prepare argument vector for one kit."""
    return [
        'prepare',
        '--video',
        str(video),
        '--source-srt',
        str(srt),
        '--translation',
        str(translation),
        '--job',
        str(job),
        '--output',
        str(output),
    ]


def test_prepare_and_status_roundtrip_with_special_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _require_real_tools()
    monkeypatch.delenv(SETTINGS_ENV_VAR, raising=False)
    special = tmp_path / "source dir with 'quote', #hash and юникод"
    from conftest import (
        DEFAULT_CUES,
        default_segments,
        make_media,
        write_srt,
        write_translation,
    )

    special.mkdir(parents=True)
    video = make_media(special / 'lecture file.mp4')
    srt = write_srt(special / 'lecture file.srt', DEFAULT_CUES)
    translation = write_translation(
        special / 'translation file.json', video, srt, default_segments()
    )
    job = tmp_path / "job dir#'quoted' юникод"
    output = tmp_path / "output dir#with 'apostrophe'" / 'результат.mp4'
    argv = _prepare_argv(video, srt, translation, job, output)
    assert main(argv) == 0
    first = capsys.readouterr()
    assert 'revision 1 (created)' in first.out
    status_argv = ['status', '--job', str(job)]
    assert main([*status_argv, '--json']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['command'] == 'status'
    assert payload['segment_count'] == 2
    assert payload['stages']['synthesis']['state'] == 'pending'
    assert payload['blocked'] == []
    assert main(status_argv) == 0
    human = capsys.readouterr().out
    assert 'status: OK' in human
    assert main([*argv, '--settings', str(tmp_path / 'absent.json')]) == 7
    assert 'cannot be read' in capsys.readouterr().err


def test_prepare_rejects_invalid_translation_with_code_3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _require_real_tools()
    monkeypatch.delenv(SETTINGS_ENV_VAR, raising=False)
    from conftest import DEFAULT_CUES, make_media, write_srt

    special = tmp_path / 'kit'
    special.mkdir()
    video = make_media(special / 'lecture.mp4')
    srt = write_srt(special / 'lecture.srt', DEFAULT_CUES)
    broken = special / 'broken.json'
    broken.write_text(
        '{"schema_version": 1, "dup": 1, "dup": 2}', encoding='utf-8'
    )
    argv = _prepare_argv(
        video,
        srt,
        broken,
        tmp_path / 'job',
        tmp_path / 'out.mp4',
    )
    assert main(argv) == 3
    assert 'invalid input' in capsys.readouterr().err


def test_prepare_missing_input_file_exits_3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(SETTINGS_ENV_VAR, raising=False)
    argv = _prepare_argv(
        tmp_path / 'absent.mp4',
        tmp_path / 'absent.srt',
        tmp_path / 'absent.json',
        tmp_path / 'job',
        tmp_path / 'out.mp4',
    )
    assert main(argv) == 3
    assert 'not found' in capsys.readouterr().err


def test_status_missing_job_exits_3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(SETTINGS_ENV_VAR, raising=False)
    assert main(['status', '--job', str(tmp_path / 'absent')]) == 3
    assert 'no job manifest' in capsys.readouterr().err


def test_prepare_lock_contention_exits_7(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _require_real_tools()
    monkeypatch.delenv(SETTINGS_ENV_VAR, raising=False)
    from conftest import (
        DEFAULT_CUES,
        default_segments,
        make_media,
        write_srt,
        write_translation,
    )
    from lecture_localizer.storage import JobLock

    kit = tmp_path / 'kit'
    kit.mkdir()
    video = make_media(kit / 'lecture.mp4')
    srt = write_srt(kit / 'lecture.srt', DEFAULT_CUES)
    translation = write_translation(
        kit / 'translation.json', video, srt, default_segments()
    )
    job = tmp_path / 'job'
    holder = JobLock.acquire(job)
    try:
        argv = _prepare_argv(
            video, srt, translation, job, tmp_path / 'out.mp4'
        )
        assert main(argv) == 7
        assert 'job lock unavailable' in capsys.readouterr().err
    finally:
        holder.release()


def test_prepare_and_status_never_touch_the_network_or_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import os
    import socket

    _require_real_tools()
    monkeypatch.delenv(SETTINGS_ENV_VAR, raising=False)
    from conftest import (
        DEFAULT_CUES,
        CredentialValueTrap,
        NoNetwork,
        default_segments,
        make_media,
        write_srt,
        write_translation,
    )
    from lecture_localizer.media import CREDENTIAL_ENV_PATTERN

    monkeypatch.setattr(socket, 'socket', NoNetwork())
    monkeypatch.setattr(socket, 'create_connection', NoNetwork())
    sentinel = 'sk-cli-sentinel-not-real'
    canary = 'sk-canary-never-read'
    safe = {
        key: canary if CREDENTIAL_ENV_PATTERN.search(key) else value
        for key, value in os.environ.items()
    }
    safe.setdefault('XAI_API_KEY', canary)
    monkeypatch.setattr(os, 'environ', CredentialValueTrap(safe))
    kit = tmp_path / 'offline kit'
    kit.mkdir()
    video = make_media(kit / 'lecture.mp4')
    srt = write_srt(kit / 'lecture.srt', DEFAULT_CUES)
    translation = write_translation(
        kit / 'translation.json', video, srt, default_segments()
    )
    argv = _prepare_argv(
        video, srt, translation, tmp_path / 'offline job', tmp_path / 'out.mp4'
    )
    assert main(argv) == 0
    prepared = capsys.readouterr()
    assert sentinel not in prepared.out + prepared.err
    assert canary not in prepared.out + prepared.err
    assert (
        main(['status', '--job', str(tmp_path / 'offline job'), '--json']) == 0
    )
    reported = capsys.readouterr()
    assert sentinel not in reported.out + reported.err
    assert canary not in reported.out + reported.err


def test_prepare_media_child_gets_scrubbed_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _require_real_tools()
    from conftest import (
        DEFAULT_CUES,
        default_segments,
        env_dump_ffprobe,
        make_media,
        write_srt,
        write_translation,
    )

    monkeypatch.setenv('XAI_API_KEY', 'sk-child-canary')
    kit = tmp_path / 'kit'
    kit.mkdir()
    video = make_media(kit / 'lecture.mp4')
    srt = write_srt(kit / 'lecture.srt', DEFAULT_CUES)
    translation = write_translation(
        kit / 'translation.json', video, srt, default_segments()
    )
    dump = tmp_path / 'env-dump.txt'
    monkeypatch.setenv('DUMP_FILE', str(dump))
    fake = env_dump_ffprobe(tmp_path, dump)
    settings = tmp_path / 'probe-settings.json'
    settings.write_text(
        json.dumps({'ffprobe_path': str(fake['ffprobe'])}),
        encoding='utf-8',
    )
    argv = [
        *_prepare_argv(
            video, srt, translation, tmp_path / 'job', tmp_path / 'out.mp4'
        ),
        '--settings',
        str(settings),
    ]
    assert main(argv) == 0
    capsys.readouterr()
    dump_text = dump.read_text(encoding='utf-8')
    assert 'canary_seen=' in dump_text
    assert 'sk-child-canary' not in dump_text
    assert 'path_present=yes' in dump_text


def test_status_reports_blocked_job_with_exit_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _require_real_tools()
    monkeypatch.delenv(SETTINGS_ENV_VAR, raising=False)
    from conftest import (
        DEFAULT_CUES,
        default_segments,
        make_media,
        write_srt,
        write_translation,
    )
    from lecture_localizer.storage import atomic_write_json

    kit = tmp_path / 'kit'
    kit.mkdir()
    video = make_media(kit / 'lecture.mp4')
    srt = write_srt(kit / 'lecture.srt', DEFAULT_CUES)
    translation = write_translation(
        kit / 'translation.json', video, srt, default_segments()
    )
    job = tmp_path / 'job'
    assert (
        main(_prepare_argv(video, srt, translation, job, tmp_path / 'out.mp4'))
        == 0
    )
    capsys.readouterr()
    manifest_path = job / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    segment = manifest['segments'][0]
    artifact_dir = job / 'artifacts'
    artifact_dir.mkdir()
    target = artifact_dir / 'raw_response-s001.bin'
    target.write_bytes(b'original')
    manifest['artifacts'].append(
        {
            'kind': 'raw_response',
            'segment': segment['id'],
            'signature': segment['synthesis_signature'],
            'path': 'artifacts/raw_response-s001.bin',
            'sha256': hashlib.sha256(b'original').hexdigest(),
            'created_at': '2026-10-26T12:00:00+00:00',
        }
    )
    atomic_write_json(manifest_path, manifest)
    target.write_bytes(b'tampered')
    assert main(['status', '--job', str(job), '--json']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['blocked']
    assert payload['reuse']['synthesis']['invalidated']['s001'] == (
        'artifact_corrupt'
    )
    assert main(['status', '--job', str(job)]) == 0
    assert 'status: BLOCKED' in capsys.readouterr().out


def test_settings_drive_the_dependency_matrix_through_the_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _require_real_tools()
    monkeypatch.delenv(SETTINGS_ENV_VAR, raising=False)
    from conftest import (
        DEFAULT_CUES,
        default_segments,
        make_media,
        plant_complete_cache,
        write_srt,
        write_translation,
    )

    kit = tmp_path / 'kit'
    kit.mkdir()
    video = make_media(kit / 'lecture.mp4')
    srt = write_srt(kit / 'lecture.srt', DEFAULT_CUES)
    translation = write_translation(
        kit / 'translation.json', video, srt, default_segments()
    )
    job = tmp_path / 'job'
    argv = _prepare_argv(video, srt, translation, job, tmp_path / 'out.mp4')
    assert main(argv) == 0
    capsys.readouterr()
    plant_complete_cache(job)
    payload = _status_json(job, capsys)
    stages = payload['stages']
    assert all(stages[s]['state'] == 'complete' for s in stages)
    reuse = payload['reuse']
    assert reuse['synthesis']['preserved'] == ['s001', 's002']
    assert reuse['subtitle']['preserved'] is True
    assert reuse['container']['preserved'] is True

    fitting_settings = tmp_path / 'fitting.json'
    fitting_settings.write_text(
        json.dumps({'fitting_max_speedup': 1.15}), encoding='utf-8'
    )
    assert main([*argv, '--settings', str(fitting_settings)]) == 0
    capsys.readouterr()
    payload = _status_json(job, capsys)
    reuse = payload['reuse']
    assert reuse['synthesis']['invalidated'] == {}
    assert reuse['synthesis']['preserved'] == ['s001', 's002']
    assert reuse['fitting']['invalidated'] == {
        's001': 'signature_changed',
        's002': 'signature_changed',
    }
    assert reuse['container']['invalidated'] == 'signature_changed'
    assert reuse['subtitle']['preserved'] is True

    plant_complete_cache(job)
    defaults_settings = tmp_path / 'combined.json'
    defaults_settings.write_text(
        json.dumps(
            {
                'fitting_max_speedup': 1.15,
                'startup_original_default': True,
            }
        ),
        encoding='utf-8',
    )
    assert main([*argv, '--settings', str(defaults_settings)]) == 0
    capsys.readouterr()
    payload = _status_json(job, capsys)
    reuse = payload['reuse']
    assert reuse['synthesis']['invalidated'] == {}
    assert reuse['fitting']['invalidated'] == {}
    assert reuse['subtitle']['preserved'] is True
    assert reuse['container']['invalidated'] == 'signature_changed'
    assert reuse['container']['preserved'] is False


def _status_json(
    job: Path, capsys: pytest.CaptureFixture[str]
) -> dict[str, Any]:
    """Read one status JSON report through the CLI."""
    assert main(['status', '--job', str(job), '--json']) == 0
    payload: dict[str, Any] = json.loads(capsys.readouterr().out)
    return payload


def test_prepare_overlap_and_utf8_exit_3_with_positive_control(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _require_real_tools()
    monkeypatch.delenv(SETTINGS_ENV_VAR, raising=False)
    from conftest import default_segments, make_media

    kit = tmp_path / 'kit'
    kit.mkdir()
    video = make_media(kit / 'lecture.mp4')
    overlap_srt = kit / 'overlap.srt'
    overlap_srt.write_text(
        '1\n00:00:00,000 --> 00:00:01,500\nFirst.\n\n'
        '2\n00:00:00,500 --> 00:00:02,000\nOverlapping second.\n',
        encoding='utf-8',
    )
    from conftest import write_translation

    translation = write_translation(
        kit / 'overlap.json', video, overlap_srt, default_segments()
    )
    argv = _prepare_argv(
        video,
        overlap_srt,
        translation,
        tmp_path / 'overlap-job',
        tmp_path / 'overlap-out.mp4',
    )
    assert main(argv) == 3
    captured = capsys.readouterr()
    assert 'invalid subtitles' in captured.err
    assert 'overlaps segment' in captured.err
    assert 'Traceback' not in captured.out + captured.err

    broken_srt = kit / 'broken.srt'
    broken_srt.write_bytes(b'\xff\xfe not utf8')
    translation = write_translation(
        kit / 'broken.json', video, broken_srt, default_segments()
    )
    argv = _prepare_argv(
        video,
        broken_srt,
        translation,
        tmp_path / 'broken-job',
        tmp_path / 'broken-out.mp4',
    )
    assert main(argv) == 3
    assert 'not valid UTF-8' in capsys.readouterr().err


def test_prepare_huge_integer_exits_3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _require_real_tools()
    monkeypatch.delenv(SETTINGS_ENV_VAR, raising=False)
    from conftest import DEFAULT_CUES, make_media, write_srt

    kit = tmp_path / 'kit'
    kit.mkdir()
    video = make_media(kit / 'lecture.mp4')
    srt = write_srt(kit / 'lecture.srt', DEFAULT_CUES)
    huge_digits = '1' * 5000
    segments = [{'id': 's1', 'source_cues': [10**5000], 'translation': 'X.'}]
    del huge_digits, segments
    translation_text = (
        '{"schema_version": 1,'
        ' "source": {"video_sha256": "'
        + hashlib.sha256(video.read_bytes()).hexdigest()
        + '",'
        ' "srt_sha256": "'
        + hashlib.sha256(srt.read_bytes()).hexdigest()
        + '"},'
        ' "source_language": "en", "target_language": "ru",'
        ' "segments": [{"id": "s1", "source_cues": ['
        + '1' * 5000
        + '], "translation": "X."}]}'
    )
    translation = kit / 'huge.json'
    translation.write_text(translation_text, encoding='utf-8')
    argv = _prepare_argv(
        video, srt, translation, tmp_path / 'job', tmp_path / 'out.mp4'
    )
    assert main(argv) == 3
    captured = capsys.readouterr()
    assert 'invalid input' in captured.err
    assert 'Traceback' not in captured.out + captured.err


def test_prepare_accepts_integral_float_cues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _require_real_tools()
    monkeypatch.delenv(SETTINGS_ENV_VAR, raising=False)
    from conftest import DEFAULT_CUES, make_media, write_srt, write_translation

    kit = tmp_path / 'kit'
    kit.mkdir()
    video = make_media(kit / 'lecture.mp4')
    srt = write_srt(kit / 'lecture.srt', DEFAULT_CUES)
    segments = [
        {'id': 's001', 'source_cues': [1.0], 'translation': 'Первая.'},
        {'id': 's002', 'source_cues': [2.0], 'translation': 'Вторая.'},
    ]
    translation = write_translation(kit / 'floats.json', video, srt, segments)
    argv = _prepare_argv(
        video, srt, translation, tmp_path / 'job', tmp_path / 'out.mp4'
    )
    assert main(argv) == 0
    capsys.readouterr()
    payload = _status_json(tmp_path / 'job', capsys)
    assert payload['segment_count'] == 2


def test_prepare_repo_local_job_exits_3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _require_real_tools()
    monkeypatch.delenv(SETTINGS_ENV_VAR, raising=False)
    from conftest import (
        DEFAULT_CUES,
        default_segments,
        make_media,
        write_srt,
        write_translation,
    )
    from lecture_localizer.jobs import _application_source_root

    root = _application_source_root()
    assert root is not None
    kit = tmp_path / 'kit'
    kit.mkdir()
    video = make_media(kit / 'lecture.mp4')
    srt = write_srt(kit / 'lecture.srt', DEFAULT_CUES)
    translation = write_translation(
        kit / 'translation.json', video, srt, default_segments()
    )
    nested_job = root / 'src' / 'lecture_localizer' / 'cli-probe-job'
    argv = _prepare_argv(
        video, srt, translation, nested_job, tmp_path / 'out.mp4'
    )
    assert main(argv) == 3
    assert 'outside the application source' in capsys.readouterr().err
    assert not nested_job.exists()


def test_prepare_corrupt_cache_exits_3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _require_real_tools()
    monkeypatch.delenv(SETTINGS_ENV_VAR, raising=False)
    from conftest import (
        DEFAULT_CUES,
        default_segments,
        make_media,
        plant_complete_cache,
        write_srt,
        write_translation,
    )

    kit = tmp_path / 'kit'
    kit.mkdir()
    video = make_media(kit / 'lecture.mp4')
    srt = write_srt(kit / 'lecture.srt', DEFAULT_CUES)
    translation = write_translation(
        kit / 'translation.json', video, srt, default_segments()
    )
    job = tmp_path / 'job'
    argv = _prepare_argv(video, srt, translation, job, tmp_path / 'out.mp4')
    assert main(argv) == 0
    capsys.readouterr()
    plant_complete_cache(job)
    manifest = json.loads((job / 'manifest.json').read_text(encoding='utf-8'))
    raw_records = [
        record
        for record in manifest['artifacts']
        if record['kind'] == 'raw_response'
    ]
    target = job / raw_records[0]['path']
    target.write_bytes(b'corrupted after planting')
    assert main(argv) == 3
    captured = capsys.readouterr()
    assert 'artifact integrity check failed' in captured.err
    assert 'Traceback' not in captured.out + captured.err

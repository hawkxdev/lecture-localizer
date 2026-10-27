"""Tests for job preparation, persistence, profile and reuse."""

from __future__ import annotations

import json
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_xai_client import (
    FakeResponse,
    FakeTransport,
    build_wav_bytes,
)

from conftest import (
    DEFAULT_CUES,
    NoNetwork,
    default_segments,
    fake_ffprobe,
    make_media,
    plant_artifact_record,
    plant_complete_cache,
    probe_payload,
    rebind_translation,
    require_real_media,
    sha256,
    write_srt,
    write_translation,
)
from lecture_localizer.contracts import ContractError
from lecture_localizer.jobs import (
    PaidExecutionDenied,
    PrepareResult,
    SpendingLimits,
    execute_speech_operations,
    prepare_job,
    preview_speech_operations,
    read_job_status,
    retry_uncertain_operation,
)
from lecture_localizer.media import MediaToolError
from lecture_localizer.settings import Settings
from lecture_localizer.storage import (
    DestinationExistsError,
    JobLock,
    LockUnavailableError,
    StorageError,
    atomic_write_json,
    sha256_bytes,
)
from lecture_localizer.xai_client import SpeechRequestError

KEY_MATERIAL = 'reflecting-secret-key-value'


def make_kit(tmp_path: Path, name: str = 'src') -> dict[str, Path]:
    """Create a synthetic video, SRT and bound translation triple."""
    require_real_media()
    source = tmp_path / name
    source.mkdir()
    video = make_media(source / 'lecture.mp4')
    srt = write_srt(source / 'lecture.srt', DEFAULT_CUES)
    translation = write_translation(
        source / 'translation.json', video, srt, default_segments()
    )
    return {
        'video': video,
        'srt': srt,
        'translation': translation,
        'job': tmp_path / 'job',
        'output': tmp_path / 'out.mp4',
    }


def run_prepare(
    kit: dict[str, Path],
    *,
    translation: Path | None = None,
    job: Path | None = None,
    output: Path | None = None,
    source_audio: int | None = None,
    settings: Settings | None = None,
) -> PrepareResult:
    """Run one prepare call with the kit paths."""
    return prepare_job(
        video_path=kit['video'],
        srt_path=kit['srt'],
        translation_path=translation or kit['translation'],
        job_dir=job or kit['job'],
        output_path=output or kit['output'],
        source_audio=source_audio,
        settings=settings or Settings(),
    )


def plant_synthetic_artifact(
    job_dir: Path,
    kind: str,
    segment: str | None,
    signature: str,
    data: bytes = b'synthetic cached bytes',
) -> str:
    """Write a synthetic cached artifact and record it in the manifest."""
    manifest_path = job_dir / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    relative = f'artifacts/{kind}-{segment or "job"}.bin'
    target = job_dir / relative
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(data)
    manifest['artifacts'].append(
        {
            'kind': kind,
            'segment': segment,
            'signature': signature,
            'path': relative,
            'sha256': sha256_bytes(data),
            'created_at': '2026-10-26T12:00:00+00:00',
        }
    )
    atomic_write_json(manifest_path, manifest)
    return relative


def segment_signature(
    manifest: dict[str, Any], segment: str, stage: str
) -> str:
    """Return one stored per segment stage signature."""
    signature: str = next(
        s[f'{stage}_signature']
        for s in manifest['segments']
        if s['id'] == segment
    )
    return signature


def plant_all_synthesis_artifacts(job_dir: Path) -> None:
    """Plant raw response artifacts for every segment of a manifest."""
    manifest = json.loads(
        (job_dir / 'manifest.json').read_text(encoding='utf-8')
    )
    for segment in manifest['segments']:
        plant_synthetic_artifact(
            job_dir,
            'raw_response',
            segment['id'],
            segment['synthesis_signature'],
        )


def test_media_facts_reject_multiple_video_streams(tmp_path: Path) -> None:
    from lecture_localizer.jobs import _media_facts

    payload = probe_payload()
    payload['streams'].append(dict(payload['streams'][0], index=9))
    with pytest.raises(MediaToolError, match='exactly one video stream'):
        _media_facts(payload['streams'], payload['format'])


def test_media_facts_reject_garbage_output(tmp_path: Path) -> None:
    from lecture_localizer.jobs import _media_facts

    with pytest.raises(MediaToolError, match='unusable ffprobe'):
        _media_facts(['not', 'a', 'dict'], {'format_name': 'mp4'})


def test_inspect_media_reports_unusable_tool(tmp_path: Path) -> None:
    settings = Settings(ffprobe_path=str(tmp_path / 'absent-ffprobe'))
    with pytest.raises(MediaToolError, match='not executable'):
        from lecture_localizer.jobs import inspect_media

        inspect_media(tmp_path / 'v.mp4', settings)


def test_inspect_media_uses_fake_probe_payload(tmp_path: Path) -> None:
    from lecture_localizer.jobs import inspect_media

    fake = fake_ffprobe(tmp_path, probe_payload())
    video = tmp_path / 'v.mp4'
    video.write_bytes(b'media')
    settings = Settings(ffprobe_path=str(fake['ffprobe']))
    facts = inspect_media(video, settings)
    assert facts.container_format == 'mov,mp4,m4a,3gp,3g2,mj2'
    assert facts.video.codec == 'h264'
    assert facts.audio_streams[0].codec == 'aac'


def _profile_settings(tmp_path: Path, payload: dict[str, Any]) -> Settings:
    """Settings pointing ffprobe at one canned payload script."""
    fake = fake_ffprobe(tmp_path, payload)
    return Settings(ffprobe_path=str(fake['ffprobe']))


def test_unsupported_containers_are_rejected(tmp_path: Path) -> None:
    from lecture_localizer.jobs import _validate_video, inspect_media

    payload = probe_payload(container='webm')
    video = tmp_path / 'v.webm'
    video.write_bytes(b'media')
    facts = inspect_media(video, _profile_settings(tmp_path, payload))
    with pytest.raises(ContractError, match='unsupported container'):
        _validate_video(facts)


def test_unsupported_video_codec_and_pixels(tmp_path: Path) -> None:
    from lecture_localizer.jobs import _validate_video, inspect_media

    video = tmp_path / 'v.mp4'
    video.write_bytes(b'media')
    for mutation, message in (
        ({'codec_name': 'mpeg4'}, 'unsupported video codec'),
        ({'pix_fmt': 'yuv420p10le'}, 'unsupported pixel format'),
        ({'color_transfer': 'smpte2084'}, 'HDR color transfer'),
    ):
        payload = probe_payload()
        payload['streams'][0].update(mutation)
        facts = inspect_media(video, _profile_settings(tmp_path, payload))
        with pytest.raises(ContractError, match=message):
            _validate_video(facts)


def _select_from(
    tmp_path: Path, payload: dict[str, Any]
) -> Callable[[int | None], tuple[Any, str, str]]:
    """Build a selection probe bound to one canned probe payload."""
    from lecture_localizer.jobs import _select_audio, inspect_media

    video = tmp_path / 'v.mp4'
    video.write_bytes(b'media')
    facts = inspect_media(video, _profile_settings(tmp_path, payload))

    def select(
        source_audio: int | None,
    ) -> tuple[Any, str, str]:
        """Run selection once for the prepared probe facts."""
        selected, mode, disposition = _select_audio(facts, source_audio)
        return selected, mode, disposition

    return select


def test_missing_audio_is_reported(tmp_path: Path) -> None:
    select = _select_from(tmp_path, probe_payload(audio=[]))
    with pytest.raises(ContractError, match='no audio track'):
        select(None)


def test_multiple_audio_requires_explicit_selection(tmp_path: Path) -> None:
    second = dict(probe_payload()['streams'][1], index=2)
    select = _select_from(
        tmp_path, probe_payload(audio=[probe_payload()['streams'][1], second])
    )
    with pytest.raises(ContractError, match='--source-audio INDEX'):
        select(None)
    with pytest.raises(ContractError, match='does not select an audio'):
        select(7)


def test_explicit_audio_selection_is_recorded(tmp_path: Path) -> None:
    second = dict(probe_payload()['streams'][1], index=2)
    select = _select_from(
        tmp_path, probe_payload(audio=[probe_payload()['streams'][1], second])
    )
    _, mode, disposition = select(2)
    assert mode == 'explicit'
    assert disposition == 'copy'


@pytest.mark.parametrize(
    'codec_args,codec,expected_disposition',
    [
        pytest.param(('-c:a', 'pcm_s16le'), 'pcm_s16le', 'convert', id='pcm'),
        pytest.param(('-c:a', 'libmp3lame'), 'mp3', 'convert', id='mp3'),
        pytest.param(('-c:a', 'libopus'), 'opus', 'convert', id='opus'),
    ],
)
def test_convert_dispositions_for_supported_codecs(
    tmp_path: Path,
    codec_args: tuple[str, ...],
    codec: str,
    expected_disposition: str,
) -> None:
    require_real_media()
    source = tmp_path / f'conv-{codec}'
    media = make_media(
        source / 'lecture.mkv',
        audio_args=codec_args,
        extension='mkv',
    )
    result = run_prepare(
        {
            'video': media,
            'srt': write_srt(source / 'lecture.srt', DEFAULT_CUES),
            'translation': write_translation(
                source / 'translation.json',
                media,
                source / 'lecture.srt',
                default_segments(),
            ),
            'job': source / 'job',
            'output': source / 'out.mp4',
        }
    )
    manifest = result.manifest
    assert manifest['media']['audio_stream']['codec'] == codec
    assert manifest['media']['audio_disposition'] == expected_disposition


def test_he_aac_profile_is_rejected(tmp_path: Path) -> None:
    audio = dict(probe_payload()['streams'][1], profile='HE-AAC')
    select = _select_from(tmp_path, probe_payload(audio=[audio]))
    with pytest.raises(ContractError, match='AAC profile'):
        select(None)


def test_six_channel_audio_is_rejected(tmp_path: Path) -> None:
    audio = dict(probe_payload()['streams'][1], channels=6)
    select = _select_from(tmp_path, probe_payload(audio=[audio]))
    with pytest.raises(ContractError, match='channel count'):
        select(None)


def test_prepare_creates_inspectable_job(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    result = run_prepare(kit)
    assert result.created is True
    manifest = result.manifest
    assert manifest['schema_version'] == 1
    assert manifest['revision'] == 1
    assert manifest['binding']['video_sha256'] == sha256(kit['video'])
    assert manifest['binding']['srt_sha256'] == sha256(kit['srt'])
    assert manifest['media']['video_stream']['codec'] == 'h264'
    assert manifest['media']['video_stream']['pix_fmt'] == 'yuv420p'
    assert manifest['media']['audio_stream']['profile'] == 'LC'
    assert manifest['media']['audio_selection']['mode'] == 'single'
    assert manifest['media']['audio_disposition'] == 'copy'
    assert manifest['segments'][0]['window'] == {
        'start_ms': 0,
        'end_ms': 1000,
    }
    assert manifest['segments'][1]['window'] == {
        'start_ms': 1000,
        'end_ms': 2000,
    }
    assert manifest['stages']['synthesis']['state'] == 'pending'
    assert manifest['blocked'] == []
    snapshots = manifest['snapshots']
    for record in snapshots.values():
        stored = kit['job'] / record['path']
        assert stored.is_file()
        assert sha256(stored) == record['sha256']
    narration = json.loads(
        (kit['job'] / snapshots['narration']['path']).read_text(
            encoding='utf-8'
        )
    )
    assert narration['segments'][0]['text'] == 'Первая реплика для речи.'
    assert narration['segments'][1]['text'] == 'Вторая реплика.'
    cues = json.loads(
        (kit['job'] / snapshots['source_cues']['path']).read_text(
            encoding='utf-8'
        )
    )
    assert cues['cues'][0] == {
        'position': 1,
        'start_ms': 0,
        'end_ms': 1000,
        'text': 'First spoken line.',
    }
    translation_raw = (
        kit['job'] / snapshots['translation']['path']
    ).read_bytes()
    assert translation_raw == kit['translation'].read_bytes()


def test_prepare_is_idempotent_without_new_revision(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    first = run_prepare(kit)
    before = (kit['job'] / 'manifest.json').read_bytes()
    second = run_prepare(kit)
    assert second.unchanged is True
    assert second.manifest['revision'] == first.manifest['revision']
    assert (kit['job'] / 'manifest.json').read_bytes() == before


def test_narration_change_invalidates_affected_synthesis(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    changed = json.loads(kit['translation'].read_text(encoding='utf-8'))
    changed['segments'][0]['narration'] = 'Совершенно другой текст речи.'
    path = tmp_path / 'changed-narration.json'
    path.write_text(
        json.dumps(changed, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    result = run_prepare(
        kit, translation=rebind_translation(path, kit['video'], kit['srt'])
    )
    invalidated = result.invalidated
    assert invalidated['synthesis'] == ['s001']
    assert invalidated['fitting'] == ['s001']
    assert invalidated['subtitle'] is False
    assert invalidated['container'] is True
    manifest = result.manifest
    assert manifest['revision'] == 2
    assert {v['revision'] for v in manifest['snapshots'].values()} == {2}


def test_translation_only_change_preserves_speech(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    plant_all_synthesis_artifacts(kit['job'])
    changed = json.loads(kit['translation'].read_text(encoding='utf-8'))
    changed['segments'][0]['translation'] = 'Полностью новый перевод первой.'
    path = tmp_path / 'changed-translation.json'
    path.write_text(
        json.dumps(changed, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    result = run_prepare(
        kit, translation=rebind_translation(path, kit['video'], kit['srt'])
    )
    invalidated = result.invalidated
    assert invalidated['synthesis'] == []
    assert invalidated['fitting'] == []
    assert invalidated['subtitle'] is True
    assert invalidated['container'] is True
    reuse = result.reuse
    assert reuse['synthesis']['preserved'] == ['s001', 's002']
    assert reuse['synthesis']['invalidated'] == {}


def test_fitting_change_reuses_raw_speech(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    plant_all_synthesis_artifacts(kit['job'])
    strict = Settings(fitting_max_speedup=1.15)
    result = run_prepare(kit, settings=strict)
    invalidated = result.invalidated
    assert invalidated['synthesis'] == []
    assert invalidated['fitting'] == ['s001', 's002']
    assert invalidated['subtitle'] is False
    assert invalidated['container'] is True
    assert result.manifest['policy']['fitting_max_speedup'] == 1.15


def test_defaults_change_reuses_prepared_tracks(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    flipped = Settings(startup_original_default=True)
    result = run_prepare(kit, settings=flipped)
    invalidated = result.invalidated
    assert invalidated['synthesis'] == []
    assert invalidated['fitting'] == []
    assert invalidated['subtitle'] is False
    assert invalidated['container'] is True
    assert result.manifest['policy']['startup_original_default'] is True


def test_video_change_requires_rebinding_and_preserves_source(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    original_bytes = kit['video'].read_bytes()
    original_video = kit['video']
    mutated = tmp_path / 'mutated.mp4'
    mutated.write_bytes(original_bytes + b'x')
    kit['video'] = mutated
    with pytest.raises(ContractError, match='source binding mismatch'):
        run_prepare(kit, output=tmp_path / 'fresh-output.mp4')
    assert original_video.read_bytes() == original_bytes
    status = read_job_status(kit['job'])
    assert status['binding']['video_sha256'] == sha256(original_video)


def test_output_collision_preserves_existing_bytes(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    existing = tmp_path / 'taken.mp4'
    existing.write_bytes(b'do not touch')
    with pytest.raises(DestinationExistsError, match='existing output'):
        run_prepare(kit, output=existing)
    assert existing.read_bytes() == b'do not touch'


def test_alias_between_sources_and_output_is_rejected(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    with pytest.raises(StorageError, match='same file'):
        run_prepare(kit, output=kit['video'])


def test_paths_inside_job_directory_are_rejected(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    inside_output = kit['job'] / 'out.mp4'
    with pytest.raises(StorageError, match='outside the job directory'):
        run_prepare(kit, output=inside_output)
    inside_video = kit['job'] / 'video.mp4'
    inside_video.parent.mkdir(parents=True, exist_ok=True)
    inside_video.write_bytes(kit['video'].read_bytes())
    with pytest.raises(StorageError, match='outside the job directory'):
        run_prepare(
            kit,
            job=kit['job'].parent,
            output=tmp_path / 'outside.mp4',
        )


def test_nonempty_job_dir_without_manifest_is_refused(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    job = kit['job']
    job.mkdir(parents=True)
    (job / 'user-file.txt').write_text('mine', encoding='utf-8')
    with pytest.raises(DestinationExistsError, match='unrelated files'):
        run_prepare(kit)


def test_lock_contention_prevents_concurrent_writers(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    holder = JobLock.acquire(kit['job'])
    try:
        with pytest.raises(LockUnavailableError, match='running process'):
            run_prepare(kit)
        assert (kit['job'] / 'lock').is_file()
    finally:
        holder.release()
    result = run_prepare(kit)
    assert result.created is True


def test_stale_lock_remains_visible(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    lock_path = kit['job'] / 'lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps(
            {'pid': 999_999_999, 'created_at': '2026-10-26T00:00:00+00:00'}
        ),
        encoding='utf-8',
    )
    with pytest.raises(LockUnavailableError, match='uncertain stale lock'):
        run_prepare(kit)
    assert json.loads(lock_path.read_text(encoding='utf-8'))['pid'] == (
        999_999_999
    )


def test_interruption_between_snapshots_and_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kit = make_kit(tmp_path)

    def interrupted(payload_path: object, data: object) -> None:
        """Simulate user interruption during the manifest write."""
        raise KeyboardInterrupt

    monkeypatch.setattr(
        'lecture_localizer.jobs.atomic_write_json', interrupted
    )
    with pytest.raises(KeyboardInterrupt):
        run_prepare(kit)
    assert not (kit['job'] / 'lock').exists()
    assert not (kit['job'] / 'manifest.json').exists()
    orphans = sorted(p.name for p in (kit['job'] / 'snapshots').iterdir())
    assert orphans == [
        'narration-r001.json',
        'source_cues-r001.json',
        'translation-r001.json',
    ]
    monkeypatch.undo()
    result = run_prepare(kit)
    assert result.created is True
    manifest = result.manifest
    assert {v['revision'] for v in manifest['snapshots'].values()} == {2}


def test_disk_failure_during_manifest_update_is_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    changed = json.loads(kit['translation'].read_text(encoding='utf-8'))
    changed['segments'][0]['translation'] = 'Другой перевод.'
    path = tmp_path / 'changed.json'
    path.write_text(
        json.dumps(changed, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    translation = rebind_translation(path, kit['video'], kit['srt'])

    def no_space(payload_path: object, data: object) -> None:
        """Simulate a full disk during the manifest write."""
        raise OSError(28, 'No space left on device')

    monkeypatch.setattr('lecture_localizer.jobs.atomic_write_json', no_space)
    with pytest.raises(OSError, match='No space left'):
        run_prepare(kit, translation=translation)
    assert not (kit['job'] / 'lock').exists()
    stored = json.loads(
        (kit['job'] / 'manifest.json').read_text(encoding='utf-8')
    )
    assert stored['revision'] == 1
    monkeypatch.undo()
    result = run_prepare(kit, translation=translation)
    assert result.manifest['revision'] == 2


def test_verified_artifact_makes_stage_complete_and_reusable(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    plant_all_synthesis_artifacts(kit['job'])
    adopted = run_prepare(kit)
    assert adopted.unchanged is False
    assert adopted.manifest['stages']['synthesis']['state'] == 'complete'
    assert adopted.reuse['synthesis']['preserved'] == ['s001', 's002']
    assert adopted.manifest['blocked'] == []
    repeat = run_prepare(kit)
    assert repeat.unchanged is True
    status = read_job_status(kit['job'])
    assert status['stages']['synthesis']['state'] == 'complete'
    assert status['blocked'] == []


def test_corrupt_artifact_fails_prepare_and_keeps_status_blocked(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    plant_all_synthesis_artifacts(kit['job'])
    corrupt_target = kit['job'] / 'artifacts' / 'raw_response-s002.bin'
    corrupt_target.write_bytes(b'tampered bytes')
    with pytest.raises(ContractError, match='artifact integrity check'):
        run_prepare(kit)
    status = read_job_status(kit['job'])
    assert status['stages']['synthesis']['state'] == 'blocked'
    assert status['reuse']['synthesis']['preserved'] == ['s001']
    assert status['reuse']['synthesis']['invalidated']['s002'] == (
        'artifact_corrupt'
    )
    assert any(
        'raw_response artifact for s002' in b for b in status['blocked']
    )


def test_missing_artifact_file_is_reported(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    manifest_path = kit['job'] / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    segment = manifest['segments'][0]
    manifest['artifacts'].append(
        {
            'kind': 'raw_response',
            'segment': segment['id'],
            'signature': segment['synthesis_signature'],
            'path': 'artifacts/raw_response-s001.bin',
            'sha256': sha256_bytes(b'absent'),
            'created_at': '2026-10-26T12:00:00+00:00',
        }
    )
    atomic_write_json(manifest_path, manifest)
    status = read_job_status(kit['job'])
    assert status['stages']['synthesis']['state'] == 'blocked'
    assert 's001' in status['reuse']['synthesis']['invalidated']


def test_corrupt_snapshot_blocks_prepare_but_status_stays_readable(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    manifest = json.loads(
        (kit['job'] / 'manifest.json').read_text(encoding='utf-8')
    )
    narration_path = kit['job'] / manifest['snapshots']['narration']['path']
    narration_path.write_bytes(b'corrupted')
    with pytest.raises(ContractError, match='snapshot integrity'):
        run_prepare(kit)
    status = read_job_status(kit['job'])
    assert any('narration snapshot' in b for b in status['blocked'])


def test_unknown_manifest_version_is_not_migrated(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    manifest_path = kit['job'] / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['schema_version'] = 2
    atomic_write_json(manifest_path, manifest)
    with pytest.raises(ContractError, match='unsupported job manifest'):
        run_prepare(kit)
    with pytest.raises(ContractError, match='unsupported job manifest'):
        read_job_status(kit['job'])


def test_multi_audio_selection_end_to_end(tmp_path: Path) -> None:
    kit = make_kit(tmp_path, name='multi')
    video = make_media(
        kit['video'].with_name('dual.mp4'), extra_audio_inputs=1
    )
    kit['video'] = video
    write_translation(
        kit['translation'], video, kit['srt'], default_segments()
    )
    with pytest.raises(ContractError, match='--source-audio INDEX'):
        run_prepare(kit)
    result = run_prepare(kit, source_audio=2)
    assert result.manifest['media']['audio_selection'] == {
        'mode': 'explicit',
        'index': 2,
    }
    with pytest.raises(ContractError, match='does not select'):
        run_prepare(
            kit,
            job=tmp_path / 'job-b',
            output=tmp_path / 'out-b.mp4',
            source_audio=5,
        )


def test_unsupported_video_codec_end_to_end(tmp_path: Path) -> None:
    kit = make_kit(tmp_path, name='mpeg4kit')
    video = make_media(kit['video'].with_name('old.mp4'), video_codec='mpeg4')
    translation = write_translation(
        tmp_path / 'mpeg4-translation.json',
        video,
        kit['srt'],
        default_segments(),
    )
    with pytest.raises(ContractError, match='unsupported video codec'):
        prepare_job(
            video_path=video,
            srt_path=kit['srt'],
            translation_path=translation,
            job_dir=tmp_path / 'job-mp4',
            output_path=tmp_path / 'out-mp4.mp4',
            source_audio=None,
            settings=Settings(),
        )


def test_prepare_and_status_make_zero_network_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kit = make_kit(tmp_path)
    monkeypatch.setattr(socket, 'socket', NoNetwork())
    monkeypatch.setattr(socket, 'create_connection', NoNetwork())
    monkeypatch.setenv('XAI_API_KEY', 'sk-sentinel-not-real')
    result = run_prepare(kit)
    assert result.created is True
    status = read_job_status(kit['job'])
    assert status['command'] == 'status'
    assert 'sk-sentinel-not-real' not in json.dumps(status)


def test_rebind_srt_accepts_updated_translation(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    first = run_prepare(kit)
    old_snapshots = {
        kind: record['path']
        for kind, record in first.manifest['snapshots'].items()
    }
    changed_srt = write_srt(
        tmp_path / 'changed.srt',
        [
            (0, 900, 'First spoken line.'),
            (900, 2000, 'Second spoken line.'),
        ],
    )
    kit['srt'] = changed_srt
    rebind_translation(kit['translation'], kit['video'], changed_srt)
    result = run_prepare(kit)
    assert result.unchanged is False
    manifest = result.manifest
    assert manifest['revision'] == 2
    assert manifest['binding']['srt_sha256'] == sha256(changed_srt)
    entry = manifest['history'][-1]
    assert entry.get('rebind') is True
    assert entry['invalidated']['subtitle'] is True
    assert entry['invalidated']['fitting'] == ['s001', 's002']
    assert entry['invalidated']['synthesis'] == []
    assert entry['invalidated']['container'] is True
    assert manifest['segments'][0]['window'] == {'start_ms': 0, 'end_ms': 900}
    new_names = {record['path'] for record in manifest['snapshots'].values()}
    assert new_names.isdisjoint(set(old_snapshots.values()))
    for relative in old_snapshots.values():
        assert (kit['job'] / relative).is_file()


def test_rebind_video_accepts_updated_translation(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    replanted = make_media(tmp_path / 'reencoded' / 'lecture.mp4', seconds=2.2)
    kit['video'] = replanted
    rebind_translation(kit['translation'], replanted, kit['srt'])
    result = run_prepare(kit)
    manifest = result.manifest
    entry = manifest['history'][-1]
    assert entry.get('rebind') is True
    assert entry['invalidated']['container'] is True
    assert entry['invalidated']['subtitle'] is False
    assert entry['invalidated']['synthesis'] == []
    assert entry['invalidated']['fitting'] == []
    assert manifest['media']['video_stream']['codec'] == 'h264'


def test_container_signature_tracks_original_stream_bytes(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    first_signature = read_job_status(kit['job'])
    other = make_media(tmp_path / 'other-audio' / 'lecture.mp4', seconds=2.2)
    kit['video'] = other
    rebind_translation(kit['translation'], other, kit['srt'])
    result = run_prepare(kit)
    identity_before = first_signature['media']['audio_stream']
    identity_after = result.manifest['media']['audio_stream']
    assert identity_before['codec'] == identity_after['codec']
    assert identity_before['channels'] == identity_after['channels']
    assert identity_before['profile'] == identity_after['profile']
    original_media = tmp_path / 'src' / 'lecture.mp4'
    assert other.read_bytes() != original_media.read_bytes()
    entry = result.manifest['history'][-1]
    assert entry.get('rebind') is True
    assert entry['invalidated']['container'] is True


def test_stale_translation_without_rebind_is_rejected(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    other = make_media(tmp_path / 'stale-video' / 'lecture.mp4', seconds=2.2)
    with pytest.raises(ContractError, match='source binding mismatch'):
        prepare_job(
            video_path=other,
            srt_path=kit['srt'],
            translation_path=kit['translation'],
            job_dir=tmp_path / 'stale-job',
            output_path=tmp_path / 'stale-out.mp4',
            source_audio=None,
            settings=Settings(),
        )


def test_changed_translation_invalidates_cached_subtitle_and_container(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    plant_complete_cache(kit['job'])
    changed = json.loads(kit['translation'].read_text(encoding='utf-8'))
    changed['segments'][0]['translation'] = 'Обновлённый перевод.'
    path = tmp_path / 'changed-translation.json'
    path.write_text(
        json.dumps(changed, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    result = run_prepare(
        kit, translation=rebind_translation(path, kit['video'], kit['srt'])
    )
    reuse = result.reuse
    assert reuse['subtitle']['invalidated'] == 'signature_changed'
    assert reuse['subtitle']['preserved'] is False
    assert reuse['container']['invalidated'] == 'signature_changed'
    assert reuse['container']['preserved'] is False
    assert reuse['synthesis']['preserved'] == ['s001', 's002']
    status = read_job_status(kit['job'])
    assert status['stages']['subtitle']['state'] == 'pending'
    assert status['stages']['container']['state'] == 'pending'


def test_partial_cache_reports_synthesis_pending(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    manifest = json.loads(
        (kit['job'] / 'manifest.json').read_text(encoding='utf-8')
    )
    segment = manifest['segments'][0]
    plant_artifact_record(
        kit['job'],
        'raw_response',
        segment['id'],
        segment['synthesis_signature'],
    )
    status = read_job_status(kit['job'])
    assert status['stages']['synthesis']['state'] == 'pending'
    assert status['stages']['fitting']['state'] == 'pending'
    reuse = status['reuse']
    assert reuse['synthesis']['preserved'] == ['s001']
    assert reuse['synthesis']['invalidated'] == {'s002': 'initial_revision'}
    assert reuse['synthesis']['pending'] == []


def test_historical_records_do_not_block_current_completion(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    plant_complete_cache(kit['job'])
    changed = json.loads(kit['translation'].read_text(encoding='utf-8'))
    changed['segments'][0]['narration'] = 'Новый текст речи первой.'
    path = tmp_path / 'changed-narration.json'
    path.write_text(
        json.dumps(changed, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    run_prepare(
        kit, translation=rebind_translation(path, kit['video'], kit['srt'])
    )
    plant_complete_cache(kit['job'])
    status = read_job_status(kit['job'])
    assert status['stages']['synthesis']['state'] == 'complete'
    assert status['reuse']['synthesis']['preserved'] == ['s001', 's002']


def test_missing_raw_under_fitted_work_blocks(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    manifest = json.loads(
        (kit['job'] / 'manifest.json').read_text(encoding='utf-8')
    )
    segment = manifest['segments'][0]
    plant_artifact_record(
        kit['job'],
        'fitted_audio',
        segment['id'],
        segment['fitting_signature'],
    )
    status = read_job_status(kit['job'])
    assert status['stages']['synthesis']['state'] == 'pending'
    assert status['stages']['fitting']['state'] == 'blocked'
    assert any(
        'required synthesis artifact for s001' in b for b in status['blocked']
    )
    assert status['reuse']['fitting']['invalidated']['s001'] == (
        'missing_provenance'
    )


def test_defaults_only_change_invalidates_only_container(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    plant_complete_cache(kit['job'])
    flipped = Settings(startup_narration_default=False)
    result = run_prepare(kit, settings=flipped)
    reuse = result.reuse
    assert reuse['container']['invalidated'] == 'signature_changed'
    assert reuse['subtitle']['preserved'] is True
    assert reuse['synthesis']['preserved'] == ['s001', 's002']
    assert reuse['fitting']['preserved'] == ['s001', 's002']


def test_invalid_utf8_srt_raises_contract_error(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    kit['srt'].write_bytes(b'\xff\xfe broken')
    rebind_translation(kit['translation'], kit['video'], kit['srt'])
    with pytest.raises(ContractError, match='not valid UTF-8'):
        run_prepare(kit)


def test_repo_local_job_is_rejected_before_writes(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    from lecture_localizer.jobs import _application_source_root

    root = _application_source_root()
    assert root is not None
    nested = root / 'src' / 'lecture_localizer' / 'probe-job-correction'
    with pytest.raises(ContractError, match='outside the application source'):
        run_prepare(kit, job=nested)
    assert not nested.exists()


def test_relative_paths_persist_absolute_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kit = make_kit(tmp_path, name='relsrc')
    work_one = tmp_path / 'work-one'
    work_two = tmp_path / 'work-two'
    work_one.mkdir()
    work_two.mkdir()
    monkeypatch.chdir(work_one)
    first = prepare_job(
        video_path=kit['video'],
        srt_path=kit['srt'],
        translation_path=kit['translation'],
        job_dir=kit['job'],
        output_path=Path('out.mp4'),
        source_audio=None,
        settings=Settings(),
    )
    recorded_one = first.manifest['output_path']
    assert Path(recorded_one).is_absolute()
    assert Path(recorded_one) == (work_one / 'out.mp4')
    monkeypatch.chdir(work_two)
    second = prepare_job(
        video_path=kit['video'],
        srt_path=kit['srt'],
        translation_path=kit['translation'],
        job_dir=kit['job'],
        output_path=Path('out.mp4'),
        source_audio=None,
        settings=Settings(),
    )
    assert second.unchanged is False
    assert second.manifest['output_path'] == str(work_two / 'out.mp4')
    assert second.manifest['output_path'] != recorded_one
    monkeypatch.chdir(work_one)
    third = prepare_job(
        video_path=kit['video'],
        srt_path=kit['srt'],
        translation_path=kit['translation'],
        job_dir=kit['job'],
        output_path=Path('out.mp4'),
        source_audio=None,
        settings=Settings(),
    )
    assert third.manifest['output_path'] == recorded_one


def test_return_to_retained_signature_reuses_history(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    original = json.loads(kit['translation'].read_text(encoding='utf-8'))
    plant_complete_cache(kit['job'])
    manifest_path = kit['job'] / 'manifest.json'
    planted = json.loads(manifest_path.read_text(encoding='utf-8'))
    a_records = {
        (record['kind'], record['segment']): record
        for record in planted['artifacts']
    }
    a_bytes = {
        key: (kit['job'] / record['path']).read_bytes()
        for key, record in a_records.items()
    }
    variant = json.loads(json.dumps(original))
    variant['segments'][0]['narration'] = 'Вариант речи для версии Б.'
    variant_path = tmp_path / 'variant.json'
    variant_path.write_text(
        json.dumps(variant, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    variant_translation = rebind_translation(
        variant_path, kit['video'], kit['srt']
    )
    run_prepare(kit, translation=variant_translation)
    plant_complete_cache(kit['job'], data=b'different synthetic B response')
    original_path = tmp_path / 'original-restored.json'
    original_path.write_text(
        json.dumps(original, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    restored_translation = rebind_translation(
        original_path, kit['video'], kit['srt']
    )
    run_prepare(kit, translation=restored_translation)
    status = read_job_status(kit['job'])
    assert status['stages']['synthesis']['state'] == 'complete'
    assert status['stages']['fitting']['state'] == 'complete'
    assert status['stages']['subtitle']['state'] == 'complete'
    assert status['stages']['container']['state'] == 'complete'
    assert status['reuse']['synthesis']['preserved'] == ['s001', 's002']
    assert status['reuse']['fitting']['preserved'] == ['s001', 's002']
    assert status['reuse']['synthesis']['invalidated'] == {}
    current = json.loads(manifest_path.read_text(encoding='utf-8'))
    for key, record in a_records.items():
        assert (kit['job'] / record['path']).read_bytes() == a_bytes[key]
    later_records = current['artifacts'][len(planted['artifacts']) :]
    assert later_records
    for record in later_records:
        key = (record['kind'], record['segment'])
        assert (kit['job'] / record['path']).read_bytes() != a_bytes[key]
    paths = [record['path'] for record in current['artifacts']]
    assert len(paths) == len(set(paths))
    signatures = {record['signature'] for record in current['artifacts']}
    first_sigs = {record['signature'] for record in planted['artifacts']}
    assert first_sigs <= signatures


def test_genuinely_absent_signature_is_not_reused(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    plant_complete_cache(kit['job'])
    changed = json.loads(kit['translation'].read_text(encoding='utf-8'))
    changed['segments'][0]['narration'] = 'Совершенно новая речь.'
    path = tmp_path / 'changed.json'
    path.write_text(
        json.dumps(changed, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    result = run_prepare(
        kit, translation=rebind_translation(path, kit['video'], kit['srt'])
    )
    reuse = result.reuse
    assert 's001' not in reuse['synthesis']['preserved']
    assert reuse['synthesis']['invalidated']['s001'] == 'signature_changed'


def test_missing_raw_provenance_keeps_container_not_preserved(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    plant_complete_cache(kit['job'])
    manifest_path = kit['job'] / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['artifacts'] = [
        record
        for record in manifest['artifacts']
        if not (
            record['kind'] == 'raw_response' and record['segment'] == 's001'
        )
    ]
    atomic_write_json(manifest_path, manifest)
    status = read_job_status(kit['job'])
    assert status['stages']['fitting']['state'] == 'blocked'
    assert status['reuse']['fitting']['invalidated']['s001'] == (
        'missing_provenance'
    )
    assert status['reuse']['container']['preserved'] is False


def test_missing_fitted_record_keeps_container_pending(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    plant_complete_cache(kit['job'])
    manifest_path = kit['job'] / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['artifacts'] = [
        record
        for record in manifest['artifacts']
        if not (
            record['kind'] == 'fitted_audio' and record['segment'] == 's001'
        )
    ]
    atomic_write_json(manifest_path, manifest)
    status = read_job_status(kit['job'])
    assert status['stages']['fitting']['state'] == 'pending'
    assert status['stages']['container']['state'] == 'blocked'
    assert status['reuse']['container']['preserved'] is False
    assert any(
        'upstream stages are not complete' in b for b in status['blocked']
    )


def test_installed_wheel_permits_unrelated_host_projects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from lecture_localizer import jobs as jobs_module
    from lecture_localizer.cli import main

    host = tmp_path / "host dir with 'quote' and #hash"
    package = (
        host
        / '.venv'
        / 'lib'
        / 'python3.12'
        / 'site-packages'
        / 'lecture_localizer'
    )
    package.mkdir(parents=True)
    (host / 'pyproject.toml').write_text(
        '[project]\nname = "some-other-project"\n', encoding='utf-8'
    )
    monkeypatch.setattr(jobs_module, '__file__', str(package / 'jobs.py'))
    kit = make_kit(tmp_path, name='hostkit')
    user_job = host / 'localization-data'
    argv = [
        'prepare',
        '--video',
        str(kit['video']),
        '--source-srt',
        str(kit['srt']),
        '--translation',
        str(kit['translation']),
        '--job',
        str(user_job),
        '--output',
        str(tmp_path / 'host-out.mp4'),
    ]
    assert main(argv) == 0
    assert 'revision 1 (created)' in capsys.readouterr().out
    assert user_job.is_dir()


# --- Guarded speech operations and the durable journal (issue 11) ---

TEST_LIMITS = SpendingLimits(
    max_requests=10,
    max_text_chars=100000,
    max_cost_estimate=10.0,
    price_per_request=0.01,
    price_basis='synthetic test price list',
)


def trap_reader(name: str) -> str | None:
    """Fail the test when a key is read at the wrong moment."""
    raise AssertionError(f'key read attempted for {name}')


def recording_key_reader(calls: list[str]) -> Callable[[str], str | None]:
    """Return a reader that records every key lookup."""

    def reader(name: str) -> str | None:
        calls.append(name)
        return 'test-secret-key-value'

    return reader


def success_transport() -> FakeTransport:
    """Return a fake transport answering one small valid WAV."""
    return FakeTransport(FakeResponse(chunks=(build_wav_bytes(samples=4),)))


class InspectingTransport:
    """Transport that reads the journal at request time."""

    def __init__(
        self,
        job_dir: Path,
        observed: list[dict[str, Any]],
    ) -> None:
        """Bind the job directory and the observation sink."""
        self.job_dir = job_dir
        self.observed = observed
        self.requests: list[dict[str, Any]] = []

    def __call__(
        self,
        *,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout: httpx.Timeout,
    ) -> FakeResponse:
        """Record the durable journal state seen during the request."""
        self.requests.append(payload)
        state = load_journal(self.job_dir)
        self.observed.append(state['operations'][-1])
        return FakeResponse(chunks=(build_wav_bytes(samples=4),))


def timeout_transport() -> FakeTransport:
    """Return a fake transport raising one inactivity timeout."""
    return FakeTransport(httpx.ReadTimeout('peer went quiet'))


def load_journal(job_dir: Path) -> dict[str, Any]:
    """Parse the operations journal of one job."""
    data: dict[str, Any] = json.loads(
        (job_dir / 'operations.json').read_text(encoding='utf-8')
    )
    return data


def test_prepare_and_status_without_journal(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    assert not (kit['job'] / 'operations.json').exists()
    status = read_job_status(kit['job'])
    assert status['operations']['state'] == 'absent'
    assert status['operations']['operations'] == 0
    assert run_prepare(kit).unchanged


def test_prepare_blocks_malformed_journal(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    (kit['job'] / 'operations.json').write_text(
        json.dumps(
            {
                'schema_version': 2,
                'updated_at': '2026-10-27T10:00:00+00:00',
                'operations': [],
            }
        ),
        encoding='utf-8',
    )
    with pytest.raises(ContractError, match='operations journal'):
        run_prepare(kit)
    (kit['job'] / 'operations.json').write_text('{not json', encoding='utf-8')
    with pytest.raises(ContractError, match='operations journal'):
        run_prepare(kit)
    status = read_job_status(kit['job'])
    assert status['operations']['state'] == 'malformed'
    assert any('operations journal' in line for line in status['blocked'])


def test_prepare_blocks_unknown_journal_fields(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    (kit['job'] / 'operations.json').write_text(
        json.dumps(
            {
                'schema_version': 1,
                'updated_at': '2026-10-27T10:00:00+00:00',
                'operations': [],
                'extra_field': 'unexpected',
            }
        ),
        encoding='utf-8',
    )
    with pytest.raises(ContractError, match='operations journal'):
        run_prepare(kit)


def test_prepare_never_resets_an_uncertain_journal(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    execute_speech_operations(
        kit['job'],
        allow_paid=True,
        limits=TEST_LIMITS,
        transport=timeout_transport(),
        key_reader=lambda name: 'test-secret-key-value',
    )
    journal_bytes = (kit['job'] / 'operations.json').read_bytes()
    segments = [
        {
            'id': 's001',
            'source_cues': [1],
            'translation': 'Первая реплика.',
            'narration': 'Совершенно другая озвучка.',
        },
        *default_segments()[1:],
    ]
    changed = write_translation(
        tmp_path / 'changed.json', kit['video'], kit['srt'], segments
    )
    run_prepare(kit, translation=changed)
    assert (kit['job'] / 'operations.json').read_bytes() == journal_bytes
    status = read_job_status(kit['job'])
    assert status['operations']['by_state'].get('uncertain') == 2
    assert len(status['operations']['unresolved']) == 2


def test_complete_cache_reuse_makes_zero_requests(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    plant_all_synthesis_artifacts(kit['job'])
    transport = FakeTransport(AssertionError('transport must never be called'))
    result = execute_speech_operations(
        kit['job'],
        allow_paid=False,
        limits=None,
        transport=transport,
        key_reader=trap_reader,
    )
    assert result.request_count == 0
    assert result.reused == ('s001', 's002')
    assert transport.requests == []


def test_missing_permission_stops_before_key(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    transport = FakeTransport(AssertionError('transport must never be called'))
    with pytest.raises(PaidExecutionDenied) as raised:
        execute_speech_operations(
            kit['job'],
            allow_paid=False,
            limits=TEST_LIMITS,
            transport=transport,
            key_reader=trap_reader,
        )
    assert raised.value.kind == 'permission'
    assert transport.requests == []
    assert not (kit['job'] / 'operations.json').exists()


def test_missing_limits_stop_before_key(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    with pytest.raises(PaidExecutionDenied) as raised:
        execute_speech_operations(
            kit['job'],
            allow_paid=True,
            limits=None,
            transport=success_transport(),
            key_reader=trap_reader,
        )
    assert raised.value.kind == 'limits'


def test_missing_key_stops_before_http(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    transport = FakeTransport(AssertionError('transport must never be called'))
    with pytest.raises(PaidExecutionDenied) as raised:
        execute_speech_operations(
            kit['job'],
            allow_paid=True,
            limits=TEST_LIMITS,
            transport=transport,
            key_reader=lambda name: None,
        )
    assert raised.value.kind == 'key'
    assert transport.requests == []


def test_request_count_limit_stops_before_http(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    limits = SpendingLimits(
        max_requests=1,
        max_text_chars=100000,
        max_cost_estimate=10.0,
        price_per_request=0.01,
        price_basis='synthetic test price list',
    )
    transport = FakeTransport(AssertionError('transport must never be called'))
    with pytest.raises(PaidExecutionDenied) as raised:
        execute_speech_operations(
            kit['job'],
            allow_paid=True,
            limits=limits,
            transport=transport,
            key_reader=trap_reader,
        )
    assert raised.value.kind == 'limits_exceeded'
    assert transport.requests == []


def test_text_volume_limit_stops_before_http(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    limits = SpendingLimits(
        max_requests=10,
        max_text_chars=1,
        max_cost_estimate=10.0,
        price_per_request=0.01,
        price_basis='synthetic test price list',
    )
    with pytest.raises(PaidExecutionDenied, match='text characters'):
        execute_speech_operations(
            kit['job'],
            allow_paid=True,
            limits=limits,
            transport=success_transport(),
            key_reader=trap_reader,
        )


def test_window_beyond_duration_stops_before_http(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    manifest_path = kit['job'] / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['segments'][0]['window']['end_ms'] = 999999
    atomic_write_json(manifest_path, manifest)
    transport = FakeTransport(AssertionError('transport must never be called'))
    with pytest.raises(ContractError, match='beyond the recorded video'):
        execute_speech_operations(
            kit['job'],
            allow_paid=True,
            limits=TEST_LIMITS,
            transport=transport,
            key_reader=trap_reader,
        )
    assert transport.requests == []


def test_reservation_and_start_are_durable_before_http(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    observed: list[dict[str, Any]] = []
    transport = InspectingTransport(kit['job'], observed)
    result = execute_speech_operations(
        kit['job'],
        allow_paid=True,
        limits=TEST_LIMITS,
        transport=transport,
        key_reader=recording_key_reader([]),
    )
    assert result.completed == ('s001', 's002')
    assert len(transport.requests) == 2
    assert len(observed) == 2
    for operation in observed:
        assert operation['state'] == 'started'
        attempt = operation['attempts'][-1]
        assert attempt['verdict'] is None
        assert attempt['started_at']
        assert operation['reserved_at']


def test_success_records_receipt_manifest_and_journal(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    calls: list[str] = []
    result = execute_speech_operations(
        kit['job'],
        allow_paid=True,
        limits=TEST_LIMITS,
        transport=success_transport(),
        key_reader=recording_key_reader(calls),
    )
    assert result.request_count == 2
    journal = load_journal(kit['job'])
    assert len(journal['operations']) == 2
    operation = journal['operations'][0]
    assert operation['state'] == 'completed'
    attempt = operation['attempts'][0]
    assert attempt['verdict'] == 'completed'
    assert attempt['response']['status'] == 200
    assert attempt['response']['content_type'] == 'audio/wav'
    assert attempt['response']['wav']['sample_rate_hz'] == 48000
    artifact_path = kit['job'] / attempt['artifact']['path']
    assert artifact_path.is_file()
    manifest = json.loads(
        (kit['job'] / 'manifest.json').read_text(encoding='utf-8')
    )
    assert manifest['stages']['synthesis']['state'] == 'complete'
    raw_records = [
        a for a in manifest['artifacts'] if a['kind'] == 'raw_response'
    ]
    assert len(raw_records) == 2
    journal_text = (kit['job'] / 'operations.json').read_text(encoding='utf-8')
    assert 'test-secret-key-value' not in journal_text
    assert calls == ['XAI_API_KEY']


def test_http_error_is_definitive_with_evidence(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    transport = FakeTransport(
        FakeResponse(
            chunks=(b'{"error": "bad request"}',),
            status_code=400,
            headers={'content-type': 'application/json'},
        )
    )
    result = execute_speech_operations(
        kit['job'],
        allow_paid=True,
        limits=TEST_LIMITS,
        transport=transport,
        key_reader=lambda name: 'test-secret-key-value',
    )
    assert set(result.failed) == {'s001', 's002'}
    journal = load_journal(kit['job'])
    operation = journal['operations'][0]
    assert operation['state'] == 'failed'
    attempt = operation['attempts'][0]
    assert attempt['failure']['kind'] == 'http_status'
    evidence = attempt['evidence']
    assert evidence is not None
    assert (kit['job'] / evidence['path']).read_bytes() == (
        b'{"error": "bad request"}'
    )


def test_uncertain_blocks_new_planning_until_retry(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    first = execute_speech_operations(
        kit['job'],
        allow_paid=True,
        limits=TEST_LIMITS,
        transport=timeout_transport(),
        key_reader=lambda name: 'test-secret-key-value',
    )
    assert set(first.uncertain) == {'s001', 's002'}
    second = execute_speech_operations(
        kit['job'],
        allow_paid=True,
        limits=TEST_LIMITS,
        transport=success_transport(),
        key_reader=lambda name: 'test-secret-key-value',
    )
    assert second.request_count == 0
    assert len(second.blocked_unresolved) == 2
    journal = load_journal(kit['job'])
    assert len(journal['operations']) == 2
    operation_id = journal['operations'][0]['operation_id']
    receipt_before = json.dumps(journal['operations'][0]['attempts'][0])
    third = retry_uncertain_operation(
        kit['job'],
        operation_id,
        allow_paid=True,
        limits=TEST_LIMITS,
        transport=success_transport(),
        key_reader=lambda name: 'test-secret-key-value',
    )
    assert third.completed == ('s001',)
    journal = load_journal(kit['job'])
    retried = journal['operations'][0]
    assert retried['state'] == 'completed'
    assert len(retried['attempts']) == 2
    assert retried['attempts'][0]['verdict'] == 'uncertain'
    assert json.dumps(retried['attempts'][0]) == receipt_before
    assert retried['attempts'][1]['linked_retry'] is True
    assert retried['attempts'][1]['verdict'] == 'completed'
    status = read_job_status(kit['job'])
    assert status['operations']['by_state'].get('uncertain') == 1
    assert status['operations']['by_state'].get('completed') == 1


def test_retry_refuses_changed_narration(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    execute_speech_operations(
        kit['job'],
        allow_paid=True,
        limits=TEST_LIMITS,
        transport=timeout_transport(),
        key_reader=lambda name: 'test-secret-key-value',
    )
    journal = load_journal(kit['job'])
    operation_id = journal['operations'][0]['operation_id']
    segments = [
        {
            'id': 's001',
            'source_cues': [1],
            'translation': 'Первая реплика.',
            'narration': 'Изменённый текст озвучки.',
        },
        *default_segments()[1:],
    ]
    changed = write_translation(
        tmp_path / 'changed.json', kit['video'], kit['srt'], segments
    )
    run_prepare(kit, translation=changed)
    with pytest.raises(ContractError, match='signature'):
        retry_uncertain_operation(
            kit['job'],
            operation_id,
            allow_paid=True,
            limits=TEST_LIMITS,
            transport=success_transport(),
            key_reader=lambda name: 'test-secret-key-value',
        )


def test_retry_refuses_unknown_operation(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    with pytest.raises(ContractError, match='no operation'):
        retry_uncertain_operation(
            kit['job'],
            'f' * 32,
            allow_paid=True,
            limits=TEST_LIMITS,
            transport=success_transport(),
            key_reader=trap_reader,
        )


def test_retry_without_permission_keeps_uncertainty(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    execute_speech_operations(
        kit['job'],
        allow_paid=True,
        limits=TEST_LIMITS,
        transport=timeout_transport(),
        key_reader=lambda name: 'test-secret-key-value',
    )
    journal = load_journal(kit['job'])
    operation_id = journal['operations'][0]['operation_id']
    with pytest.raises(PaidExecutionDenied, match='permission'):
        retry_uncertain_operation(
            kit['job'],
            operation_id,
            allow_paid=False,
            limits=TEST_LIMITS,
            transport=success_transport(),
            key_reader=trap_reader,
        )
    journal_after = load_journal(kit['job'])
    assert journal_after['operations'][0]['state'] == 'uncertain'
    assert len(journal_after['operations'][0]['attempts']) == 1


def test_storage_failure_is_uncertain(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    artifacts_dir = kit['job'] / 'artifacts'
    artifacts_dir.mkdir()
    artifacts_dir.chmod(0o500)
    try:
        result = execute_speech_operations(
            kit['job'],
            allow_paid=True,
            limits=TEST_LIMITS,
            transport=success_transport(),
            key_reader=lambda name: 'test-secret-key-value',
        )
    finally:
        artifacts_dir.chmod(0o755)
    assert set(result.uncertain) == {'s001', 's002'}
    journal = load_journal(kit['job'])
    assert (
        journal['operations'][0]['attempts'][0]['failure']['kind']
        == 'storage_error'
    )


def test_cancellation_journals_uncertainty_before_exit(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    transport = FakeTransport(
        FakeResponse(
            chunks=(b'partial',),
            fail_after=KeyboardInterrupt(),
        )
    )
    with pytest.raises(KeyboardInterrupt):
        execute_speech_operations(
            kit['job'],
            allow_paid=True,
            limits=TEST_LIMITS,
            transport=transport,
            key_reader=lambda name: 'test-secret-key-value',
        )
    journal = load_journal(kit['job'])
    operation = journal['operations'][0]
    assert operation['state'] == 'uncertain'
    assert operation['attempts'][0]['failure']['kind'] == 'cancelled'


def test_damaged_raw_provenance_stops_execution(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    plant_all_synthesis_artifacts(kit['job'])
    manifest = json.loads(
        (kit['job'] / 'manifest.json').read_text(encoding='utf-8')
    )
    corrupt_path = kit['job'] / manifest['artifacts'][0]['path']
    corrupt_path.write_bytes(b'damaged bytes')
    transport = FakeTransport(AssertionError('transport must never be called'))
    with pytest.raises(ContractError, match='integrity'):
        execute_speech_operations(
            kit['job'],
            allow_paid=True,
            limits=TEST_LIMITS,
            transport=transport,
            key_reader=trap_reader,
        )
    assert transport.requests == []


def test_preview_reports_planned_work_and_volume(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    manifest = json.loads(
        (kit['job'] / 'manifest.json').read_text(encoding='utf-8')
    )
    first = next(s for s in manifest['segments'] if s['id'] == 's001')
    plant_synthetic_artifact(
        kit['job'],
        'raw_response',
        's001',
        first['synthesis_signature'],
    )
    preview = preview_speech_operations(kit['job'])
    assert preview['planned'] == ['s002']
    assert preview['planned_count'] == 1
    assert preview['planned_text_chars'] == len('Вторая реплика.')
    assert preview['reused'] == ['s001']


# --- First correction round controls (R1 R3 R5 R6) ---


def test_reflecting_error_message_never_persists_the_key(
    tmp_path: Path,
) -> None:
    transport = FakeTransport(
        httpx.ReadTimeout(
            f'connection dropped while reading for {KEY_MATERIAL}'
        )
    )
    result = execute_speech_operations(
        _prepare_simple_job(tmp_path),
        allow_paid=True,
        limits=TEST_LIMITS,
        transport=transport,
        key_reader=lambda name: KEY_MATERIAL,
    )
    assert set(result.uncertain) == {'s001', 's002'}
    journal_text = (tmp_path / 'job' / 'operations.json').read_text(
        encoding='utf-8'
    )
    assert KEY_MATERIAL not in journal_text
    assert '[redacted]' in journal_text


def test_oversized_narration_preflight_refuses_before_key_and_journal(
    tmp_path: Path,
) -> None:
    source = tmp_path / 'src'
    source.mkdir()
    require_real_media()
    video = make_media(source / 'lecture.mp4')
    srt = write_srt(source / 'lecture.srt', DEFAULT_CUES)
    segments = [
        {
            'id': 's001',
            'source_cues': [1],
            'translation': 'Первая реплика.',
            'narration': 'я' * 60001,
        },
        {
            'id': 's002',
            'source_cues': [2],
            'translation': 'Вторая реплика.',
        },
    ]
    translation = write_translation(
        source / 'translation.json', video, srt, segments
    )
    job = tmp_path / 'job'
    prepare_job(
        video_path=video,
        srt_path=srt,
        translation_path=translation,
        job_dir=job,
        output_path=tmp_path / 'out.mp4',
        source_audio=None,
        settings=Settings(),
    )
    transport = FakeTransport(AssertionError('transport must never be called'))
    with pytest.raises(SpeechRequestError):
        execute_speech_operations(
            job,
            allow_paid=True,
            limits=TEST_LIMITS,
            transport=transport,
            key_reader=trap_reader,
        )
    assert transport.requests == []
    assert not (job / 'operations.json').exists()
    with pytest.raises(SpeechRequestError):
        execute_speech_operations(
            job,
            allow_paid=True,
            limits=TEST_LIMITS,
            transport=transport,
            key_reader=trap_reader,
        )
    assert not (job / 'operations.json').exists()


def test_unicode_and_separator_ids_keep_the_job_readable(
    tmp_path: Path,
) -> None:
    source = tmp_path / 'src'
    source.mkdir()
    require_real_media()
    video = make_media(source / 'lecture.mp4')
    srt = write_srt(source / 'lecture.srt', DEFAULT_CUES)
    segments = [
        {
            'id': 'часть1',  # noqa: RUF001
            'source_cues': [1],
            'translation': 'Первая реплика.',
        },
        {
            'id': 'part 2/slash',
            'source_cues': [2],
            'translation': 'Вторая реплика.',
        },
    ]
    translation = write_translation(
        source / 'translation.json', video, srt, segments
    )
    job = tmp_path / 'job'
    prepare_job(
        video_path=video,
        srt_path=srt,
        translation_path=translation,
        job_dir=job,
        output_path=tmp_path / 'out.mp4',
        source_audio=None,
        settings=Settings(),
    )
    result = execute_speech_operations(
        job,
        allow_paid=True,
        limits=TEST_LIMITS,
        transport=success_transport(),
        key_reader=lambda name: KEY_MATERIAL,
    )
    assert result.completed == ('часть1', 'part 2/slash')  # noqa: RUF001
    status = read_job_status(job)
    assert status['stages']['synthesis']['state'] == 'complete'
    assert status['operations']['by_state'].get('completed') == 2
    assert run_prepare(
        {
            'video': video,
            'srt': srt,
            'translation': translation,
            'job': job,
            'output': tmp_path / 'out.mp4',
        }
    ).unchanged


def test_retry_refuses_damaged_posted_text_provenance(
    tmp_path: Path,
) -> None:
    job = _prepare_simple_job(tmp_path)
    first = execute_speech_operations(
        job,
        allow_paid=True,
        limits=TEST_LIMITS,
        transport=timeout_transport(),
        key_reader=lambda name: KEY_MATERIAL,
    )
    assert len(first.uncertain) == 2
    journal = load_journal(job)
    operation_id = journal['operations'][0]['operation_id']
    journal['operations'][0]['posted_text_sha256'] = '0' * 64
    atomic_write_json(job / 'operations.json', journal)
    damaged_bytes = (job / 'operations.json').read_bytes()
    transport = FakeTransport(AssertionError('transport must never be called'))
    with pytest.raises(ContractError, match='provenance is damaged'):
        retry_uncertain_operation(
            job,
            operation_id,
            allow_paid=True,
            limits=TEST_LIMITS,
            transport=transport,
            key_reader=trap_reader,
        )
    assert transport.requests == []
    assert (job / 'operations.json').read_bytes() == damaged_bytes


def _prepare_simple_job(tmp_path: Path) -> Path:
    """Prepare a two segment job from the default synthetic kit."""
    source = tmp_path / 'src'
    source.mkdir()
    require_real_media()
    video = make_media(source / 'lecture.mp4')
    srt = write_srt(source / 'lecture.srt', DEFAULT_CUES)
    translation = write_translation(
        source / 'translation.json', video, srt, default_segments()
    )
    job = tmp_path / 'job'
    prepare_job(
        video_path=video,
        srt_path=srt,
        translation_path=translation,
        job_dir=job,
        output_path=tmp_path / 'out.mp4',
        source_audio=None,
        settings=Settings(),
    )
    return job


# --- Second review residual controls (R1 R6) ---


def test_reflected_content_type_never_persists_the_key(
    tmp_path: Path,
) -> None:
    transport = FakeTransport(
        FakeResponse(
            chunks=(b'service error',),
            status_code=500,
            headers={'content-type': f'text/plain; note={KEY_MATERIAL}'},
        )
    )
    job = _prepare_simple_job(tmp_path)
    result = execute_speech_operations(
        job,
        allow_paid=True,
        limits=TEST_LIMITS,
        transport=transport,
        key_reader=lambda name: KEY_MATERIAL,
    )
    assert set(result.failed) == {'s001', 's002'}
    journal_text = (job / 'operations.json').read_text(encoding='utf-8')
    assert KEY_MATERIAL not in journal_text
    assert '[redacted]' in journal_text


def test_retry_after_sibling_narration_change_completes(
    tmp_path: Path,
) -> None:
    job = _prepare_simple_job(tmp_path)
    first = execute_speech_operations(
        job,
        allow_paid=True,
        limits=TEST_LIMITS,
        transport=timeout_transport(),
        key_reader=lambda name: KEY_MATERIAL,
    )
    assert len(first.uncertain) == 2
    journal = load_journal(job)
    operation_id = journal['operations'][0]['operation_id']
    segments = [
        {
            'id': 's001',
            'source_cues': [1],
            'translation': 'Первая реплика.',
            'narration': 'Первая реплика для речи.',
        },
        {
            'id': 's002',
            'source_cues': [2],
            'translation': 'Вторая реплика.',
            'narration': 'Совершенно новая вторая озвучка.',
        },
    ]
    changed = write_translation(
        tmp_path / 'changed.json',
        tmp_path / 'src' / 'lecture.mp4',
        tmp_path / 'src' / 'lecture.srt',
        segments,
    )
    prepare_job(
        video_path=tmp_path / 'src' / 'lecture.mp4',
        srt_path=tmp_path / 'src' / 'lecture.srt',
        translation_path=changed,
        job_dir=job,
        output_path=tmp_path / 'out.mp4',
        source_audio=None,
        settings=Settings(),
    )
    transport = success_transport()
    result = retry_uncertain_operation(
        job,
        operation_id,
        allow_paid=True,
        limits=TEST_LIMITS,
        transport=transport,
        key_reader=lambda name: KEY_MATERIAL,
    )
    assert result.completed == ('s001',)
    assert len(transport.requests) == 1
    journal = load_journal(job)
    retried = next(
        op
        for op in journal['operations']
        if op['operation_id'] == operation_id
    )
    assert retried['state'] == 'completed'
    assert retried['attempts'][0]['verdict'] == 'uncertain'
    assert retried['attempts'][1]['verdict'] == 'completed'


def test_retry_blocks_when_recorded_snapshot_is_damaged(
    tmp_path: Path,
) -> None:
    job = _prepare_simple_job(tmp_path)
    first = execute_speech_operations(
        job,
        allow_paid=True,
        limits=TEST_LIMITS,
        transport=timeout_transport(),
        key_reader=lambda name: KEY_MATERIAL,
    )
    assert len(first.uncertain) == 2
    journal = load_journal(job)
    operation = journal['operations'][0]
    operation_id = operation['operation_id']
    segments = [
        {
            'id': 's001',
            'source_cues': [1],
            'translation': 'Первая реплика.',
            'narration': 'Первая реплика для речи.',
        },
        {
            'id': 's002',
            'source_cues': [2],
            'translation': 'Вторая реплика.',
            'narration': 'Совершенно новая вторая озвучка.',
        },
    ]
    changed = write_translation(
        tmp_path / 'changed.json',
        tmp_path / 'src' / 'lecture.mp4',
        tmp_path / 'src' / 'lecture.srt',
        segments,
    )
    prepare_job(
        video_path=tmp_path / 'src' / 'lecture.mp4',
        srt_path=tmp_path / 'src' / 'lecture.srt',
        translation_path=changed,
        job_dir=job,
        output_path=tmp_path / 'out.mp4',
        source_audio=None,
        settings=Settings(),
    )
    snapshot_path = job / operation['posted_text_snapshot']['path']
    snapshot_path.write_bytes(b'damaged snapshot bytes')
    transport = FakeTransport(AssertionError('transport must never be called'))
    with pytest.raises(ContractError, match='provenance is damaged'):
        retry_uncertain_operation(
            job,
            operation_id,
            allow_paid=True,
            limits=TEST_LIMITS,
            transport=transport,
            key_reader=trap_reader,
        )
    assert transport.requests == []


# --- Same head R7 monetary ceiling controls ---


def test_zero_cost_ceiling_refuses_subepsilon_estimates(
    tmp_path: Path,
) -> None:
    job = _prepare_simple_job(tmp_path)
    limits = SpendingLimits(
        max_requests=10,
        max_text_chars=100000,
        max_cost_estimate=0.0,
        price_per_request=1e-12,
        price_basis='monetary probe basis',
    )
    transport = FakeTransport(AssertionError('transport must never be called'))
    with pytest.raises(PaidExecutionDenied, match='estimated cost'):
        execute_speech_operations(
            job,
            allow_paid=True,
            limits=limits,
            transport=transport,
            key_reader=trap_reader,
        )
    assert transport.requests == []
    assert not (job / 'operations.json').exists()


def test_exact_cost_ceiling_permits_planned_requests(
    tmp_path: Path,
) -> None:
    job = _prepare_simple_job(tmp_path)
    limits = SpendingLimits(
        max_requests=10,
        max_text_chars=100000,
        max_cost_estimate=0.02,
        price_per_request=0.01,
        price_basis='monetary probe basis',
    )
    result = execute_speech_operations(
        job,
        allow_paid=True,
        limits=limits,
        transport=success_transport(),
        key_reader=lambda name: KEY_MATERIAL,
    )
    assert result.completed == ('s001', 's002')

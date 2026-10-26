"""Tests for job preparation, persistence, profile and reuse."""

from __future__ import annotations

import json
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from conftest import (
    DEFAULT_CUES,
    NoNetwork,
    default_segments,
    fake_ffprobe,
    make_media,
    probe_payload,
    rebind_translation,
    require_real_media,
    sha256,
    write_srt,
    write_translation,
)
from lecture_localizer.contracts import ContractError
from lecture_localizer.jobs import (
    PrepareResult,
    prepare_job,
    read_job_status,
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
    from lecture_localizer.jobs import _select_audio, inspect_media

    video = tmp_path / 'v.mp4'
    video.write_bytes(b'media')
    facts = inspect_media(video, _profile_settings(tmp_path, payload))

    def select(
        source_audio: int | None,
    ) -> tuple[Any, str, str]:
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


def test_corrupt_artifact_cannot_pass_as_reusable(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    run_prepare(kit)
    plant_all_synthesis_artifacts(kit['job'])
    corrupt_target = kit['job'] / 'artifacts' / 'raw_response-s002.bin'
    corrupt_target.write_bytes(b'tampered bytes')
    status = read_job_status(kit['job'])
    assert status['stages']['synthesis']['state'] == 'blocked'
    assert status['reuse']['synthesis']['preserved'] == ['s001']
    assert status['reuse']['synthesis']['invalidated']['s002'] == (
        'artifact_corrupt'
    )
    assert any(
        'raw_response artifact for s002' in b for b in status['blocked']
    )
    result = run_prepare(kit)
    assert any(
        'raw_response artifact for s002' in b
        for b in result.manifest['blocked']
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

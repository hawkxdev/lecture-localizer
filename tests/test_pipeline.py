"""Integration tests for the guarded run and offline assembly pipeline."""

from __future__ import annotations

import json
import shutil
import socket
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_xai_client import FakeResponse, build_wav_bytes

from conftest import (
    DEFAULT_CUES,
    NoNetwork,
    default_segments,
    make_media,
    rebind_translation,
    require_real_media,
    sha256,
    write_srt,
    write_translation,
)
from lecture_localizer.cli import main
from lecture_localizer.contracts import ContractError
from lecture_localizer.jobs import (
    PaidExecutionDenied,
    PipelineFailure,
    assemble_job_pipeline,
    prepare_job,
    read_job_status,
    run_job_pipeline,
)
from lecture_localizer.settings import Settings, spending_limits_from_file
from lecture_localizer.storage import (
    JobLock,
    LockUnavailableError,
)
from lecture_localizer.xai_client import TransportUncertainFailure

KEY_MATERIAL = 'pipeline-fake-key-value'
LIMITS_PAYLOAD: dict[str, Any] = {
    'max_requests': 10,
    'max_text_chars': 100000,
    'max_cost_estimate': 100.0,
    'price_per_request': 0.01,
    'price_basis': 'invented synthetic test basis',
}


class CountingTransport:
    """Fake provider returning one small valid WAV per request."""

    def __init__(self, samples: int = 2400) -> None:
        """Store the scripted body size and start empty counters."""
        self.samples = samples
        self.requests: list[dict[str, Any]] = []

    def __call__(
        self,
        *,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout: httpx.Timeout,
    ) -> FakeResponse:
        """Record one request and return the scripted response."""
        self.requests.append({'payload': payload, 'headers': dict(headers)})
        return FakeResponse(chunks=(build_wav_bytes(samples=self.samples),))


class CountingKeys:
    """Key reader that counts every credential access."""

    def __init__(self, value: str | None = KEY_MATERIAL) -> None:
        """Store the returned value and start empty counters."""
        self.value = value
        self.reads = 0

    def __call__(self, name: str) -> str | None:
        """Count one read and return the stored value."""
        self.reads += 1
        return self.value


def limits() -> Any:
    """Return fresh synthetic spending limits."""
    from lecture_localizer.jobs import SpendingLimits

    return SpendingLimits(**LIMITS_PAYLOAD)


def write_limits_file(path: Path) -> Path:
    """Write the synthetic limits payload as one JSON file."""
    path.write_text(
        json.dumps(LIMITS_PAYLOAD, indent=2) + '\n', encoding='utf-8'
    )
    return path


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


def prepare(kit: dict[str, Path], **kwargs: Any) -> None:
    """Prepare the kit job with default settings."""
    prepare_job(
        video_path=kit['video'],
        srt_path=kit['srt'],
        translation_path=kwargs.get('translation', kit['translation']),
        job_dir=kit['job'],
        output_path=kwargs.get('output', kit['output']),
        source_audio=None,
        settings=kwargs.get('settings') or Settings(),
    )


def run_full(
    kit: dict[str, Path],
    tmp_path: Path,
    *,
    transport: Any | None = None,
    keys: CountingKeys | None = None,
    **kwargs: Any,
) -> Any:
    """Run the full pipeline over the kit job with counting fakes."""
    return run_job_pipeline(
        kit['job'],
        allow_paid=kwargs.get('allow_paid', True),
        limits=kwargs.get('limits', limits()),
        transport=kwargs.get('transport', transport or CountingTransport()),
        key_reader=kwargs.get('key_reader', keys or CountingKeys()),
        settings=kwargs.get('settings'),
        retry_uncertain=kwargs.get('retry_uncertain'),
    )


def artifact_records(job: Path, kind: str) -> list[dict[str, Any]]:
    """Return current manifest records of one artifact kind."""
    manifest = json.loads((job / 'manifest.json').read_text(encoding='utf-8'))
    return [r for r in manifest['artifacts'] if r['kind'] == kind]


def artifact_digests(job: Path) -> dict[tuple[str, str | None, str], str]:
    """Map current artifact records to their recorded digests."""
    manifest = json.loads((job / 'manifest.json').read_text(encoding='utf-8'))
    return {
        (r['kind'], r['segment'], r['signature']): r['sha256']
        for r in manifest['artifacts']
    }


def report_paths(job: Path) -> list[Path]:
    """List durable run report files of one job."""
    evidence = job / 'evidence'
    if not evidence.is_dir():
        return []
    return sorted(evidence.glob('run-*.json'))


def test_run_synthesizes_and_publishes_verified_output(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    prepare(kit)
    transport = CountingTransport()
    keys = CountingKeys()
    result = run_full(kit, tmp_path, transport=transport, keys=keys)
    assert len(transport.requests) == 2
    assert keys.reads == 1
    assert result.publication['state'] == 'published'
    assert Path(result.publication['path']).is_file()
    assert result.verification is not None
    assert result.verification['ok'] is True
    for kind in (
        'raw_response',
        'normalized_audio',
        'fitted_audio',
        'timeline_audio',
        'narration_audio',
        'subtitles',
        'container_track',
    ):
        assert artifact_records(kit['job'], kind), kind
    status = read_job_status(kit['job'])
    assert status['publication']['state'] == 'published'
    assert status['assembly']['complete'] is True
    assert status['blocked'] == []
    reports = report_paths(kit['job'])
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding='utf-8'))
    assert report['command'] == 'run'
    assert report['verification']['ok'] is True
    assert report['publication']['state'] == 'published'
    assert report['text_outputs']['full_translation']['sha256']
    assert report['text_outputs']['narration_text']['sha256']
    assert report['pending_human_checks']


def test_run_cache_hit_makes_zero_calls_and_zero_key_reads(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    prepare(kit)
    run_full(kit, tmp_path)
    digests = artifact_digests(kit['job'])
    manifest_before = json.loads(
        (kit['job'] / 'manifest.json').read_text(encoding='utf-8')
    )
    transport = CountingTransport()
    keys = CountingKeys()
    run_full(kit, tmp_path, transport=transport, keys=keys)
    assert transport.requests == []
    assert keys.reads == 0
    assert artifact_digests(kit['job']) == digests
    manifest_after = json.loads(
        (kit['job'] / 'manifest.json').read_text(encoding='utf-8')
    )
    assert manifest_after['revision'] == manifest_before['revision']
    assert len(manifest_after['history']) == len(manifest_before['history'])


def test_run_without_permission_denied_before_any_access(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    prepare(kit)
    transport = CountingTransport()
    keys = CountingKeys()
    with pytest.raises(PaidExecutionDenied) as excinfo:
        run_job_pipeline(
            kit['job'],
            allow_paid=False,
            limits=limits(),
            transport=transport,
            key_reader=keys,
        )
    assert excinfo.value.kind == 'permission'
    assert transport.requests == []
    assert keys.reads == 0


def test_run_without_limits_denied_before_key(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    prepare(kit)
    keys = CountingKeys()
    with pytest.raises(PaidExecutionDenied) as excinfo:
        run_job_pipeline(
            kit['job'],
            allow_paid=True,
            limits=None,
            transport=CountingTransport(),
            key_reader=keys,
        )
    assert excinfo.value.kind == 'limits'
    assert keys.reads == 0


def test_run_without_key_denied_after_zero_requests(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    prepare(kit)
    transport = CountingTransport()
    keys = CountingKeys(value=None)
    with pytest.raises(PaidExecutionDenied) as excinfo:
        run_full(kit, tmp_path, transport=transport, keys=keys)
    assert excinfo.value.kind == 'key'
    assert transport.requests == []


def test_uncertain_http_blocks_then_named_retry_completes(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    prepare(kit)

    class UncertainTransport(CountingTransport):
        def __call__(self, **kwargs: Any) -> FakeResponse:
            raise TransportUncertainFailure(
                'partial_response',
                'the response ended before a complete body arrived',
            )

    with pytest.raises(PipelineFailure) as excinfo:
        run_job_pipeline(
            kit['job'],
            allow_paid=True,
            limits=limits(),
            transport=UncertainTransport(),
            key_reader=CountingKeys(),
        )
    assert excinfo.value.exit_code == 5
    status = read_job_status(kit['job'])
    unresolved = status['operations']['unresolved']
    assert len(unresolved) == 2
    assert status['publication']['state'] == 'pending'
    assert any('retry' in item.lower() for item in status['blocked'])
    with pytest.raises(PipelineFailure) as first_retry:
        run_full(kit, tmp_path, retry_uncertain=unresolved[0])
    assert first_retry.value.exit_code == 5
    retried = run_full(kit, tmp_path, retry_uncertain=unresolved[1])
    assert retried.speech is not None
    assert retried.speech.completed == ('s002',)
    assert retried.publication['state'] == 'published'
    journal = json.loads((kit['job'] / 'operations.json').read_text('utf-8'))
    states = [op['state'] for op in journal['operations']]
    assert 'uncertain' not in states


def test_definitive_http_failure_is_non_success(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    prepare(kit)

    class FailingTransport(CountingTransport):
        def __call__(self, **kwargs: Any) -> FakeResponse:
            self.requests.append({})
            return FakeResponse(
                chunks=(b'{"error": "denied"}',),
                status_code=500,
                headers={'content-type': 'application/json'},
            )

    with pytest.raises(PipelineFailure) as excinfo:
        run_full(kit, tmp_path, transport=FailingTransport())
    assert excinfo.value.exit_code == 5
    assert artifact_records(kit['job'], 'raw_response') == []
    assert artifact_records(kit['job'], 'container_track') == []
    assert not kit['output'].exists()


def test_fit_refusal_preserves_job_and_fails(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    prepare(kit)
    oversized = CountingTransport(samples=60000)
    with pytest.raises(PipelineFailure) as excinfo:
        run_full(kit, tmp_path, transport=oversized)
    assert excinfo.value.exit_code == 6
    assert artifact_records(kit['job'], 'raw_response')
    assert artifact_records(kit['job'], 'timeline_audio') == []
    assert artifact_records(kit['job'], 'container_track') == []
    assert not kit['output'].exists()
    report = json.loads(report_paths(kit['job'])[-1].read_text('utf-8'))
    assert report['failure']['kind'] == 'timing'
    report_text = json.dumps(report, ensure_ascii=False)
    assert '1.2' in report_text or 'required' in report_text


def test_corrupt_staging_fails_verification_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kit = make_kit(tmp_path)
    prepare(kit)
    import lecture_localizer.mux as mux_module

    original_build = mux_module.build_candidate

    def corrupting_build(request: Any, settings: Settings) -> Any:
        result = original_build(request, settings)
        data = request.destination.read_bytes()
        request.destination.write_bytes(data[: int(len(data) * 0.8)])
        return result

    monkeypatch.setattr(
        'lecture_localizer.mux.build_candidate', corrupting_build
    )
    with pytest.raises(PipelineFailure) as excinfo:
        run_full(kit, tmp_path)
    assert excinfo.value.exit_code == 6
    assert artifact_records(kit['job'], 'container_track') == []
    assert not kit['output'].exists()
    leftovers = [p for p in kit['output'].parent.iterdir() if p.is_file()]
    assert kit['output'] not in leftovers or kit['output'].exists() is False


def test_destination_collision_preserves_foreign_file(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    prepare(kit)
    kit['output'].write_bytes(b'foreign preexisting output bytes')
    with pytest.raises(PipelineFailure) as excinfo:
        run_full(kit, tmp_path)
    assert excinfo.value.exit_code == 3
    assert kit['output'].read_bytes() == b'foreign preexisting output bytes'
    assert artifact_records(kit['job'], 'raw_response')


def test_identical_foreign_file_without_provenance_is_collision(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    prepare(kit)
    run_full(kit, tmp_path)
    identical_bytes = kit['output'].read_bytes()
    manifest_path = kit['job'] / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    kept = [r for r in manifest['artifacts'] if r['kind'] != 'container_track']
    removed = [
        r for r in manifest['artifacts'] if r['kind'] == 'container_track'
    ]
    assert removed
    for record in removed:
        (kit['job'] / record['path']).unlink()
    manifest['artifacts'] = kept
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
        + '\n',
        encoding='utf-8',
    )
    receipt = kit['job'] / 'evidence' / 'publication.json'
    if receipt.exists():
        receipt.unlink()
    with pytest.raises(PipelineFailure) as excinfo:
        run_full(kit, tmp_path)
    assert excinfo.value.exit_code == 3
    assert kit['output'].read_bytes() == identical_bytes


def test_foreign_identical_file_during_interrupted_link_is_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kit = make_kit(tmp_path)
    prepare(kit)
    from lecture_localizer import jobs as jobs_module

    real_receipt = jobs_module._write_publication_receipt

    def crashing_receipt(job_dir: Path, payload: dict[str, Any]) -> None:
        real_receipt(job_dir, payload)
        if payload.get('state') == 'linking':
            raise RuntimeError('simulated crash before linking output')

    monkeypatch.setattr(
        jobs_module, '_write_publication_receipt', crashing_receipt
    )
    with pytest.raises(RuntimeError, match='simulated crash'):
        run_full(kit, tmp_path)
    records = artifact_records(kit['job'], 'container_track')
    assert len(records) == 1
    assert not kit['output'].exists()
    foreign = tmp_path / 'foreign-identical.mp4'
    shutil.copyfile(kit['job'] / records[0]['path'], foreign)
    shutil.copyfile(foreign, kit['output'])
    assert not foreign.samefile(kit['output'])
    monkeypatch.setattr(
        jobs_module, '_write_publication_receipt', real_receipt
    )
    with pytest.raises(PipelineFailure) as excinfo:
        run_full(kit, tmp_path)
    assert excinfo.value.exit_code == 3
    assert kit['output'].read_bytes() == foreign.read_bytes()
    assert len(artifact_records(kit['job'], 'container_track')) == 1


def test_interrupted_own_publication_republishes_from_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kit = make_kit(tmp_path)
    prepare(kit)
    from lecture_localizer import jobs as jobs_module

    real_receipt = jobs_module._write_publication_receipt

    def crashing_receipt(job_dir: Path, payload: dict[str, Any]) -> None:
        real_receipt(job_dir, payload)
        if payload.get('state') == 'linking':
            raise RuntimeError('simulated crash after registration')

    monkeypatch.setattr(
        jobs_module, '_write_publication_receipt', crashing_receipt
    )
    with pytest.raises(RuntimeError, match='simulated crash'):
        run_full(kit, tmp_path)
    records = artifact_records(kit['job'], 'container_track')
    assert len(records) == 1
    candidate_digest = records[0]['sha256']
    assert not kit['output'].exists()
    monkeypatch.setattr(
        jobs_module, '_write_publication_receipt', real_receipt
    )
    keys = CountingKeys()
    result = run_full(kit, tmp_path, keys=keys)
    assert keys.reads == 0
    assert result.publication['state'] == 'published'
    assert sha256(kit['output']) == candidate_digest
    assert len(artifact_records(kit['job'], 'container_track')) == 1


def test_untouched_output_with_receipt_is_idempotent_published(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    prepare(kit)
    run_full(kit, tmp_path)
    published_digest = sha256(kit['output'])
    keys = CountingKeys()
    result = run_full(kit, tmp_path, keys=keys)
    assert keys.reads == 0
    assert result.publication['state'] == 'published'
    assert sha256(kit['output']) == published_digest
    assert len(artifact_records(kit['job'], 'container_track')) == 1


def test_unequal_output_with_provenance_is_collision(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    prepare(kit)
    run_full(kit, tmp_path)
    kit['output'].write_bytes(b'foreign replacement bytes')
    with pytest.raises(PipelineFailure) as excinfo:
        run_full(kit, tmp_path)
    assert excinfo.value.exit_code == 3
    assert kit['output'].read_bytes() == b'foreign replacement bytes'


def test_assemble_republishes_offline_with_zero_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kit = make_kit(tmp_path)
    prepare(kit)
    run_full(kit, tmp_path)
    published_digest = sha256(kit['output'])
    kit['output'].unlink()
    monkeypatch.setattr(socket, 'socket', NoNetwork())
    result = assemble_job_pipeline(kit['job'])
    assert result.publication['state'] == 'published'
    assert sha256(kit['output']) == published_digest


def test_assemble_second_fresh_output_is_rejected_when_occupied(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    prepare(kit)
    run_full(kit, tmp_path)
    extra = tmp_path / 'extra.mp4'
    extra.write_bytes(b'occupied')
    with pytest.raises(PipelineFailure) as excinfo:
        assemble_job_pipeline(kit['job'], extra_output=extra)
    assert excinfo.value.exit_code == 3
    assert extra.read_bytes() == b'occupied'


def test_assemble_without_prepared_tracks_is_blocked(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    prepare(kit)
    with pytest.raises(PipelineFailure) as excinfo:
        assemble_job_pipeline(kit['job'])
    assert excinfo.value.exit_code == 3
    assert not kit['output'].exists()


def test_defaults_only_change_reuses_prepared_tracks(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    prepare(kit)
    run_full(kit, tmp_path)
    published_digest = sha256(kit['output'])
    digests = artifact_digests(kit['job'])
    changed = Settings(startup_original_default=True)
    second_output = tmp_path / 'out-v2.mp4'
    prepare(kit, settings=changed, output=second_output)
    transport = CountingTransport()
    keys = CountingKeys()
    result = run_full(
        kit, tmp_path, transport=transport, keys=keys, settings=changed
    )
    assert transport.requests == []
    assert keys.reads == 0
    assert result.publication['state'] == 'published'
    assert Path(result.publication['path']) == second_output
    assert sha256(kit['output']) == published_digest
    after = artifact_digests(kit['job'])
    for key in digests:
        if key[0] in (
            'raw_response',
            'normalized_audio',
            'fitted_audio',
            'timeline_audio',
            'narration_audio',
            'subtitles',
        ):
            assert after[key] == digests[key], key
    narration_records = artifact_records(kit['job'], 'narration_audio')
    assert len(narration_records) == 1
    assert len(artifact_records(kit['job'], 'container_track')) == 2


def test_translation_only_change_keeps_speech(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    prepare(kit)
    run_full(kit, tmp_path)
    digests = artifact_digests(kit['job'])
    payload = json.loads(kit['translation'].read_text(encoding='utf-8'))
    payload['segments'][0]['translation'] = (
        'Изменённый полный перевод первой реплики.'
    )
    path = tmp_path / 'translation-v2.json'
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    prepare(
        kit,
        translation=rebind_translation(path, kit['video'], kit['srt']),
        output=tmp_path / 'out-v2.mp4',
    )
    transport = CountingTransport()
    keys = CountingKeys()
    result = run_full(kit, tmp_path, transport=transport, keys=keys)
    assert transport.requests == []
    assert keys.reads == 0
    assert result.publication['state'] == 'published'
    after = artifact_digests(kit['job'])
    s001_key = next(
        k for k in digests if k[0] == 'raw_response' and k[1] == 's001'
    )
    assert after[s001_key] == digests[s001_key]
    subtitle_records = artifact_records(kit['job'], 'subtitles')
    assert len(subtitle_records) == 2


def test_narration_change_resynthesizes_only_affected(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    prepare(kit)
    run_full(kit, tmp_path)
    digests = artifact_digests(kit['job'])
    payload = json.loads(kit['translation'].read_text(encoding='utf-8'))
    payload['segments'][0]['narration'] = 'Совершенно новая формулировка речи.'
    path = tmp_path / 'translation-v3.json'
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    second_output = tmp_path / 'out-v2.mp4'
    prepare(
        kit,
        translation=rebind_translation(path, kit['video'], kit['srt']),
        output=second_output,
    )
    transport = CountingTransport()
    keys = CountingKeys()
    result = run_full(kit, tmp_path, transport=transport, keys=keys)
    assert len(transport.requests) == 1
    assert transport.requests[0]['payload']['text'] == (
        'Совершенно новая формулировка речи.'
    )
    assert keys.reads == 1
    assert result.publication['state'] == 'published'
    old_s002_raw = next(
        v
        for k, v in digests.items()
        if k[0] == 'raw_response' and k[1] == 's002'
    )
    current_s002_raw = [
        v
        for k, v in artifact_digests(kit['job']).items()
        if k[0] == 'raw_response' and k[1] == 's002'
    ]
    assert old_s002_raw in current_s002_raw
    assert len(artifact_records(kit['job'], 'raw_response')) == 3


def test_fitting_policy_change_refits_without_http(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    prepare(kit)
    run_full(kit, tmp_path)
    changed = Settings(fitting_max_speedup=1.05)
    prepare(kit, settings=changed, output=tmp_path / 'out-v2.mp4')
    transport = CountingTransport()
    keys = CountingKeys()
    result = run_full(
        kit, tmp_path, transport=transport, keys=keys, settings=changed
    )
    assert transport.requests == []
    assert keys.reads == 0
    assert result.publication['state'] == 'published'
    assert len(artifact_records(kit['job'], 'fitted_audio')) == 4


def test_changed_video_and_srt_with_rebound_translation(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    prepare(kit)
    run_full(kit, tmp_path)
    new_video = make_media(
        tmp_path / 'src' / 'lecture-v2.mp4',
        seconds=2.2,
        use_cache=False,
    )
    new_srt = write_srt(
        tmp_path / 'src' / 'lecture-v2.srt',
        [(200, 1200, 'First spoken line.'), (1200, 2200, 'Second spoken.')],
    )
    payload = json.loads(kit['translation'].read_text(encoding='utf-8'))
    path = tmp_path / 'translation-rebound.json'
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    kit['video'] = new_video
    kit['srt'] = new_srt
    second_output = tmp_path / 'out-v2.mp4'
    prepare(
        kit,
        translation=rebind_translation(path, new_video, new_srt),
        output=second_output,
    )
    transport = CountingTransport()
    keys = CountingKeys()
    result = run_full(kit, tmp_path, transport=transport, keys=keys)
    assert transport.requests == []
    assert keys.reads == 0
    assert result.publication['state'] == 'published'
    assert Path(result.publication['path']) == second_output


def test_concurrent_writer_holds_the_lock(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    prepare(kit)
    with (
        JobLock.acquire(kit['job']),
        pytest.raises(LockUnavailableError),
    ):
        run_full(kit, tmp_path)


def test_partial_timeline_write_is_detected(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    prepare(kit)
    run_full(kit, tmp_path)
    record = artifact_records(kit['job'], 'timeline_audio')[0]
    target = kit['job'] / record['path']
    target.write_bytes(target.read_bytes()[:-16])
    with pytest.raises(ContractError):
        run_full(kit, tmp_path)


def test_processed_audio_never_substitutes_missing_raw(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    prepare(kit)
    run_full(kit, tmp_path)
    raw_records = [
        r
        for r in artifact_records(kit['job'], 'raw_response')
        if r['segment'] == 's001'
    ]
    (kit['job'] / raw_records[0]['path']).unlink()
    with pytest.raises(ContractError):
        run_full(kit, tmp_path)


def test_cli_exit_table(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    prepare(kit)
    limits_path = write_limits_file(tmp_path / 'limits.json')
    run_full(kit, tmp_path)
    code = main(
        [
            'run',
            '--job',
            str(kit['job']),
            '--allow-paid',
            '--limits',
            str(limits_path),
        ]
    )
    assert code == 0
    code = main(['status', '--job', str(kit['job'])])
    assert code == 0
    code = main(['assemble', '--job', str(kit['job'])])
    assert code == 0
    with pytest.raises(SystemExit) as usage:
        main(['run', '--job', str(kit['job']), 'unexpected'])
    assert usage.value.code == 2
    kit2 = make_kit(tmp_path, name='src2')
    kit2['job'] = tmp_path / 'job2'
    kit2['output'] = tmp_path / 'out2.mp4'
    prepare(kit2)
    code = main(['run', '--job', str(kit2['job'])])
    assert code == 4
    code = main(
        [
            'run',
            '--job',
            str(kit2['job']),
            '--allow-paid',
            '--limits',
            str(limits_path),
        ]
    )
    assert code == 4
    code = main(['status', '--job', str(tmp_path / 'missing-job')])
    assert code == 3


def test_cli_cancellation_maps_to_130(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kit = make_kit(tmp_path)
    prepare(kit)
    limits_path = write_limits_file(tmp_path / 'limits.json')

    class InterruptingTransport(CountingTransport):
        def __call__(self, **kwargs: Any) -> FakeResponse:
            raise KeyboardInterrupt

    monkeypatch.setattr(
        'lecture_localizer.cli.run_job_pipeline',
        lambda *a, **kw: _run_with_interrupt(kit, kw),
    )
    code = main(
        [
            'run',
            '--job',
            str(kit['job']),
            '--allow-paid',
            '--limits',
            str(limits_path),
        ]
    )
    assert code == 130
    journal = json.loads((kit['job'] / 'operations.json').read_text('utf-8'))
    states = [op['state'] for op in journal['operations']]
    assert 'uncertain' in states
    assert not kit['output'].exists()


def _run_with_interrupt(kit: dict[str, Path], kwargs: dict[str, Any]) -> Any:
    """Run the real pipeline with an interrupting transport."""
    run_job_pipeline(
        kit['job'],
        allow_paid=True,
        limits=limits(),
        transport=InterruptingTransportStatic(),
        key_reader=CountingKeys(),
    )
    raise AssertionError('pipeline should have been interrupted')


class InterruptingTransportStatic(CountingTransport):
    def __call__(self, **kwargs: Any) -> FakeResponse:
        raise KeyboardInterrupt


def test_limits_file_loader_validates(tmp_path: Path) -> None:
    path = write_limits_file(tmp_path / 'limits.json')
    loaded = spending_limits_from_file(path)
    assert loaded.max_requests == 10
    assert loaded.price_basis == LIMITS_PAYLOAD['price_basis']
    bad = tmp_path / 'bad.json'
    bad.write_text('{"max_requests": 1}\n', encoding='utf-8')
    from lecture_localizer.settings import SettingsError

    with pytest.raises(SettingsError):
        spending_limits_from_file(bad)


def test_source_change_or_loss_stops_before_any_request(
    tmp_path: Path,
) -> None:
    for mode in ('video_changed', 'srt_deleted'):
        kit = make_kit(tmp_path, name=f'src-{mode}')
        kit['job'] = tmp_path / f'job-{mode}'
        kit['output'] = tmp_path / f'out-{mode}.mp4'
        prepare(kit)
        if mode == 'video_changed':
            with kit['video'].open('ab') as stream:
                stream.write(b'source-change-after-prepare')
        else:
            kit['srt'].unlink()
        transport = CountingTransport()
        keys = CountingKeys()
        with pytest.raises(ContractError):
            run_full(kit, tmp_path, transport=transport, keys=keys)
        assert transport.requests == [], mode
        assert keys.reads == 0, mode


def test_in_place_output_edit_is_collision_with_stale_receipt(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    prepare(kit)
    run_full(kit, tmp_path)
    with kit['output'].open('r+b') as stream:
        stream.seek(-1, 2)
        previous = stream.read(1)
        stream.seek(-1, 2)
        stream.write(bytes([previous[0] ^ 1]))
    identity_before = (
        kit['output'].stat().st_dev,
        kit['output'].stat().st_ino,
        kit['output'].stat().st_size,
    )
    modified = kit['output'].read_bytes()
    with pytest.raises(PipelineFailure) as excinfo:
        run_full(kit, tmp_path)
    assert excinfo.value.exit_code == 3
    assert kit['output'].read_bytes() == modified
    identity_after = (
        kit['output'].stat().st_dev,
        kit['output'].stat().st_ino,
        kit['output'].stat().st_size,
    )
    assert identity_before == identity_after
    status = read_job_status(kit['job'])
    assert status['publication']['state'] == 'collision'


def test_rebound_foreign_hardlink_at_new_destination_is_collision(
    tmp_path: Path,
) -> None:
    import os

    kit = make_kit(tmp_path)
    prepare(kit)
    run_full(kit, tmp_path)
    old_output = kit['output']
    old_bytes = old_output.read_bytes()
    payload = json.loads(kit['translation'].read_text(encoding='utf-8'))
    payload['segments'][0]['translation'] = (
        'Изменённый полный перевод для повторной публикации.'
    )
    path = tmp_path / 'translation-rebound2.json'
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    new_output = tmp_path / 'out-v2.mp4'
    prepare(
        kit,
        translation=rebind_translation(path, kit['video'], kit['srt']),
        output=new_output,
    )
    os.link(old_output, new_output)
    with pytest.raises(PipelineFailure) as excinfo:
        run_full(kit, tmp_path)
    assert excinfo.value.exit_code == 3
    assert new_output.read_bytes() == old_bytes


def test_converted_pcm_44100_stereo_publishes(tmp_path: Path) -> None:
    import subprocess

    require_real_media()
    source_dir = tmp_path / 'src-conv'
    source_dir.mkdir()
    converted = source_dir / 'converted.mov'
    command = [
        'ffmpeg',
        '-hide_banner',
        '-loglevel',
        'error',
        '-nostdin',
        '-f',
        'lavfi',
        '-i',
        'color=c=black:s=128x96:r=10:d=2.0',
        '-f',
        'lavfi',
        '-i',
        'sine=frequency=440:duration=2.0',
        '-c:v',
        'libx264',
        '-pix_fmt',
        'yuv420p',
        '-c:a',
        'pcm_s16le',
        '-ar',
        '44100',
        '-ac',
        '2',
        '-y',
        str(converted),
    ]
    subprocess.run(
        command, capture_output=True, text=True, timeout=120, check=True
    )
    srt = write_srt(source_dir / 'lecture.srt', DEFAULT_CUES)
    translation = write_translation(
        source_dir / 'translation.json',
        converted,
        srt,
        default_segments(),
    )
    kit = {
        'video': converted,
        'srt': srt,
        'translation': translation,
        'job': tmp_path / 'job-conv',
        'output': tmp_path / 'out-conv.mp4',
    }
    prepare(kit)
    transport = CountingTransport()
    keys = CountingKeys()
    result = run_full(kit, tmp_path, transport=transport, keys=keys)
    assert len(transport.requests) == 2
    assert keys.reads == 1
    assert result.publication['state'] == 'published'
    assert result.verification is not None
    assert result.verification['ok'] is True


def test_status_readable_for_unknown_duration(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    prepare(kit)
    manifest_path = kit['job'] / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['media']['duration_seconds'] = None
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
        + '\n',
        encoding='utf-8',
    )
    status = read_job_status(kit['job'])
    assert any('duration' in str(item).lower() for item in status['blocked'])
    assert status['assembly']['complete'] is False
    transport = CountingTransport()
    keys = CountingKeys()
    with pytest.raises(ContractError):
        run_full(kit, tmp_path, transport=transport, keys=keys)
    assert transport.requests == []
    assert keys.reads == 0


def test_corrupt_registered_candidate_reports_blocked_publication(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    prepare(kit)
    run_full(kit, tmp_path)
    record = artifact_records(kit['job'], 'container_track')[0]
    target = kit['job'] / record['path']
    target.write_bytes(target.read_bytes()[:-64])
    status = read_job_status(kit['job'])
    assert status['publication']['candidate'] == 'corrupt'
    assert status['publication']['state'] != 'published'
    assert any('candidate' in str(item).lower() for item in status['blocked'])


def test_unknown_journal_version_blocks_cached_run(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    prepare(kit)
    run_full(kit, tmp_path)
    journal = kit['job'] / 'operations.json'
    payload = json.loads(journal.read_text(encoding='utf-8'))
    payload['schema_version'] = 999
    journal.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    transport = CountingTransport()
    keys = CountingKeys()
    with pytest.raises(ContractError):
        run_full(kit, tmp_path, transport=transport, keys=keys)
    assert transport.requests == []
    assert keys.reads == 0
    status = read_job_status(kit['job'])
    assert any('journal' in str(item).lower() for item in status['blocked'])


def test_interrupted_final_journal_write_reconciles_without_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kit = make_kit(tmp_path)
    prepare(kit)
    import lecture_localizer.jobs as jobs_module

    real_write = jobs_module._write_journal

    def failing_write(path: Path, journal: dict[str, Any], now: str) -> None:
        last_state = (
            journal['operations'][-1]['state'] if journal['operations'] else ''
        )
        if last_state == 'completed':
            raise OSError('simulated journal interruption')
        real_write(path, journal, now)

    monkeypatch.setattr(jobs_module, '_write_journal', failing_write)
    with pytest.raises(OSError, match='simulated journal'):
        run_full(kit, tmp_path)
    manifest_records = artifact_records(kit['job'], 'raw_response')
    assert len(manifest_records) == 1
    monkeypatch.setattr(jobs_module, '_write_journal', real_write)
    journal_path = kit['job'] / 'operations.json'
    payload = json.loads(journal_path.read_text(encoding='utf-8'))
    states = [op['state'] for op in payload['operations']]
    assert states == ['started']
    transport = CountingTransport()
    keys = CountingKeys()
    result = run_full(kit, tmp_path, transport=transport, keys=keys)
    assert len(transport.requests) == 1
    assert keys.reads == 1
    assert result.publication['state'] == 'published'
    payload = json.loads(journal_path.read_text(encoding='utf-8'))
    states = [op['state'] for op in payload['operations']]
    assert states == ['completed', 'completed']
    reconciled = payload['operations'][0]['attempts'][-1]
    assert reconciled['verdict'] == 'completed'
    assert reconciled['response'] is None
    reconciled_evidence = reconciled['evidence']
    registered = artifact_records(kit['job'], 'raw_response')[0]
    assert reconciled_evidence['sha256'] == registered['sha256']
    assert reconciled_evidence['path'] == registered['path']


def test_latest_report_reflects_actual_last_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uuid as uuid_module

    kit = make_kit(tmp_path)
    prepare(kit)
    run_full(kit, tmp_path)

    controlled = iter(
        [
            uuid_module.UUID('ffffffff-ffff-4fff-afff-ffffffffffff'),
            uuid_module.UUID('00000000-0000-4000-a000-000000000000'),
        ]
    )
    real_uuid4 = uuid_module.uuid4

    def controlled_uuid4() -> Any:
        """Return scripted UUIDs, then fall back to real ones."""
        try:
            return next(controlled)
        except StopIteration:
            return real_uuid4()

    monkeypatch.setattr(uuid_module, 'uuid4', controlled_uuid4)
    first = run_full(kit, tmp_path)
    second = run_full(kit, tmp_path)
    status = read_job_status(kit['job'])
    assert status['latest_report'] == Path(second.report_path).name
    assert status['latest_report'] != Path(first.report_path).name
    first_created = json.loads(
        (kit['job'] / first.report_path).read_text(encoding='utf-8')
    )['created_at']
    second_created = json.loads(
        (kit['job'] / second.report_path).read_text(encoding='utf-8')
    )['created_at']
    assert second_created >= first_created


def test_corrupt_bound_source_stops_before_any_request(
    tmp_path: Path,
) -> None:
    import json as json_module
    import subprocess

    require_real_media()
    from lecture_localizer.media import scrubbed_environment

    kit = make_kit(tmp_path, name='src-decode')
    kit['job'] = tmp_path / 'job-decode'
    kit['output'] = tmp_path / 'out-decode.mp4'
    probe = subprocess.run(
        [
            'ffprobe',
            '-v',
            'error',
            '-select_streams',
            'v:0',
            '-show_packets',
            '-show_entries',
            'packet=pos,size',
            '-of',
            'json',
            str(kit['video']),
        ],
        capture_output=True,
        text=True,
        check=True,
        env=scrubbed_environment(),
    )
    packets = json_module.loads(probe.stdout)['packets']
    victim = packets[len(packets) // 2]
    with kit['video'].open('r+b') as stream:
        stream.seek(int(victim['pos']))
        stream.write(b'\x00' * int(victim['size']))
    payload = json.loads(kit['translation'].read_text(encoding='utf-8'))
    payload['source']['video_sha256'] = sha256(kit['video'])
    kit['translation'].write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    prepare(kit)
    decode = subprocess.run(
        [
            'ffmpeg',
            '-hide_banner',
            '-loglevel',
            'error',
            '-nostdin',
            '-xerror',
            '-i',
            str(kit['video']),
            '-f',
            'null',
            '-',
        ],
        capture_output=True,
        env=scrubbed_environment(),
    )
    assert decode.returncode != 0
    transport = CountingTransport()
    keys = CountingKeys()
    with pytest.raises(ContractError):
        run_full(kit, tmp_path, transport=transport, keys=keys)
    assert transport.requests == []
    assert keys.reads == 0


def test_renamed_occupied_destination_with_stale_candidate_is_collision(
    tmp_path: Path,
) -> None:
    import os

    kit = make_kit(tmp_path)
    prepare(kit)
    run_full(kit, tmp_path)
    occupied_bytes = kit['output'].read_bytes()
    renamed = tmp_path / 'renamed-result.mp4'
    kit['output'].rename(renamed)
    payload = json.loads(kit['translation'].read_text(encoding='utf-8'))
    payload['segments'][0]['translation'] = (
        'Изменённый перевод для проверки занятия назначения.'
    )
    path = tmp_path / 'translation-rebind3.json'
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    prepare(
        kit,
        translation=rebind_translation(path, kit['video'], kit['srt']),
        output=kit['output'],
    )
    os.link(renamed, kit['output'])
    transport = CountingTransport()
    keys = CountingKeys()
    with pytest.raises(PipelineFailure) as excinfo:
        run_full(kit, tmp_path, transport=transport, keys=keys)
    assert excinfo.value.exit_code == 3
    assert kit['output'].read_bytes() == occupied_bytes
    status = read_job_status(kit['job'])
    assert status['publication']['state'] == 'collision'


def test_assemble_requested_extra_output_is_created(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    prepare(kit)
    run_full(kit, tmp_path)
    extra = tmp_path / 'requested-extra.mp4'
    result = assemble_job_pipeline(kit['job'], extra_output=extra)
    assert result.publication['state'] == 'published'
    assert extra.is_file()
    candidate = artifact_records(kit['job'], 'container_track')[0]
    assert sha256(extra) == candidate['sha256']


def test_reconciliation_requires_current_signature(tmp_path: Path) -> None:
    kit = make_kit(tmp_path, name='src-own')
    prepare(kit)
    run_full(kit, tmp_path)
    payload = json.loads(kit['translation'].read_text(encoding='utf-8'))
    payload['segments'][0]['narration'] = (
        'Формулировка версии Б и неопределённый запрос.'
    )
    path = tmp_path / 'translation-own-b.json'
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    prepare(
        kit,
        translation=rebind_translation(path, kit['video'], kit['srt']),
        output=tmp_path / 'out-own-b.mp4',
    )

    class UncertainTransport(CountingTransport):
        def __call__(self, **kwargs: Any) -> FakeResponse:
            raise TransportUncertainFailure(
                'partial_response',
                'the response ended before a complete body arrived',
            )

    with pytest.raises(PipelineFailure):
        run_full(kit, tmp_path, transport=UncertainTransport())
    payload = json.loads(kit['translation'].read_text(encoding='utf-8'))
    del payload['segments'][0]['narration']
    path = tmp_path / 'translation-own-a.json'
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    prepare(
        kit,
        translation=rebind_translation(path, kit['video'], kit['srt']),
        output=tmp_path / 'out-own-a.mp4',
    )
    journal = json.loads(
        (kit['job'] / 'operations.json').read_text(encoding='utf-8')
    )
    uncertain_ops = [
        op
        for op in journal['operations']
        if op['state'] in ('started', 'uncertain', 'reserved')
    ]
    assert len(uncertain_ops) == 1
    transport = CountingTransport()
    keys = CountingKeys()
    with pytest.raises(PipelineFailure) as excinfo:
        run_full(kit, tmp_path, transport=transport, keys=keys)
    assert excinfo.value.exit_code == 5
    assert transport.requests == []
    journal = json.loads(
        (kit['job'] / 'operations.json').read_text(encoding='utf-8')
    )
    uncertain_ops = [
        op
        for op in journal['operations']
        if op['state'] in ('started', 'uncertain', 'reserved')
    ]
    assert len(uncertain_ops) == 1
    attempts = uncertain_ops[0]['attempts']
    assert attempts[-1]['verdict'] == 'uncertain'


def test_reconciliation_never_falsifies_known_uncertain_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kit = make_kit(tmp_path, name='src-att')
    prepare(kit)
    import lecture_localizer.jobs as jobs_module

    real_write = jobs_module._write_journal

    def failing_write(path: Path, journal: dict[str, Any], now: str) -> None:
        last_state = (
            journal['operations'][-1]['state'] if journal['operations'] else ''
        )
        if last_state == 'completed':
            raise OSError('simulated journal interruption')
        real_write(path, journal, now)

    monkeypatch.setattr(jobs_module, '_write_journal', failing_write)
    with pytest.raises(OSError, match='simulated journal'):
        run_full(kit, tmp_path)
    monkeypatch.setattr(jobs_module, '_write_journal', real_write)
    journal_path = kit['job'] / 'operations.json'
    payload = json.loads(journal_path.read_text(encoding='utf-8'))
    operation_id = payload['operations'][0]['operation_id']

    class UncertainTransport(CountingTransport):
        def __call__(self, **kwargs: Any) -> FakeResponse:
            raise TransportUncertainFailure(
                'inactivity_timeout',
                'the response read phase exceeded its inactivity limit',
            )

    from lecture_localizer.jobs import retry_uncertain_operation

    retried = retry_uncertain_operation(
        kit['job'],
        operation_id,
        allow_paid=True,
        limits=limits(),
        transport=UncertainTransport(),
        key_reader=CountingKeys(),
    )
    assert list(retried.uncertain) == ['s001']
    transport = CountingTransport()
    keys = CountingKeys()
    with pytest.raises(PipelineFailure) as run_failure:
        run_full(kit, tmp_path, transport=transport, keys=keys)
    assert run_failure.value.exit_code == 5
    assert transport.requests == []
    assert keys.reads == 0
    payload = json.loads(journal_path.read_text(encoding='utf-8'))
    operation = payload['operations'][0]
    assert operation['state'] == 'uncertain'
    attempts = operation['attempts']
    assert attempts[0]['verdict'] == 'completed'
    assert attempts[0]['response'] is None
    assert attempts[1]['verdict'] == 'uncertain'
    registered = artifact_records(kit['job'], 'raw_response')
    assert len(registered) == 1
    assert attempts[0]['evidence']['sha256'] == registered[0]['sha256']


def test_malformed_latest_report_keeps_status_readable(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    prepare(kit)
    run_full(kit, tmp_path)
    first_status = read_job_status(kit['job'])
    latest = first_status['latest_report']
    assert latest is not None
    report_file = kit['job'] / 'evidence' / latest
    damaged = report_file.read_text(encoding='utf-8')
    report_file.write_text('[]\n', encoding='utf-8')
    status = read_job_status(kit['job'])
    assert status['command'] == 'status'
    assert report_file.read_text(encoding='utf-8') == '[]\n'
    assert status['latest_report'] != latest or status['latest_report'] is None
    report_file.write_text(damaged, encoding='utf-8')


def test_decode_timeout_is_tooling_failure(tmp_path: Path) -> None:
    kit = make_kit(tmp_path, name='src-timeout')
    kit['job'] = tmp_path / 'job-timeout'
    kit['output'] = tmp_path / 'out-timeout.mp4'
    prepare(kit)
    transport = CountingTransport()
    keys = CountingKeys()
    with pytest.raises(PipelineFailure) as excinfo:
        run_full(
            kit,
            tmp_path,
            transport=transport,
            keys=keys,
            settings=Settings(subprocess_timeout_seconds=0.000001),
        )
    assert excinfo.value.exit_code == 7
    assert 'timed out' in excinfo.value.detail.lower()
    assert transport.requests == []
    assert keys.reads == 0

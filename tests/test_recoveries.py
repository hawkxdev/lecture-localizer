"""Offline recovery contracts for retained failed HTTP responses."""

from __future__ import annotations

import copy
import hashlib
import json
import socket
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest
from test_pipeline import (
    CountingKeys,
    CountingTransport,
    make_kit,
    prepare,
    run_full,
)
from test_xai_client import FakeResponse, build_wav_bytes, streaming_wav_bytes

from conftest import NoNetwork, plant_artifact_record
from lecture_localizer import jobs, recoveries, xai_client
from lecture_localizer.cli import main
from lecture_localizer.contracts import ContractError
from lecture_localizer.jobs import (
    PipelineFailure,
    assemble_job_pipeline,
    read_job_status,
    recover_response,
    run_job_pipeline,
)
from lecture_localizer.storage import (
    JobLock,
    LockUnavailableError,
    atomic_write_json,
)


class StreamingTransport:
    """Return the observed envelope with synthetic PCM samples."""

    def __init__(self, streamed: bool = True) -> None:
        """Start with no requests."""
        self.calls = 0
        self.streamed = streamed

    def __call__(self, **kwargs: Any) -> FakeResponse:
        """Return one completed synthetic response."""
        self.calls += 1
        body = (
            streaming_wav_bytes(samples=2400)
            if self.streamed
            else build_wav_bytes(samples=2400)
        )
        return FakeResponse(chunks=(body,))


def failed_job(
    tmp_path: Path,
    *,
    streamed: bool = True,
) -> tuple[dict[str, Path], StreamingTransport]:
    """Record a genuine validation failure using the older strict policy."""
    kit = make_kit(tmp_path)
    prepare(kit)
    transport = StreamingTransport(streamed=streamed)

    def older_validation(path: Path) -> xai_client.WavFacts:
        """Simulate a superseded refusal for finite response fixtures."""
        if streamed:
            return xai_client.parse_wav_header(path)
        raise xai_client.InvalidAudioResponse(
            'older validation refused the response',
            xai_client.FailureEvidence(None, None, 0, '0' * 64, ''),
        )

    with (
        patch.object(xai_client, 'parse_completed_wav', older_validation),
        pytest.raises(PipelineFailure, match='invalid_audio'),
    ):
        run_full(kit, tmp_path, transport=transport)
    assert transport.calls == 2
    return kit, transport


def journal(job: Path) -> dict[str, Any]:
    """Read the exact operation journal."""
    return cast(
        dict[str, Any], json.loads((job / 'operations.json').read_text())
    )


def write_journal(job: Path, payload: dict[str, Any]) -> None:
    """Store one deliberately altered test journal."""
    (job / 'operations.json').write_text(json.dumps(payload))


def recover_first(kit: dict[str, Path]) -> dict[str, Any]:
    """Revalidate the first recorded operation explicitly."""
    operation = journal(kit['job'])['operations'][0]
    return recover_response(
        kit['job'], operation_id=operation['operation_id'], attempt_number=1
    )


def forbid_key(*args: Any, **kwargs: Any) -> None:
    """Reject any unexpected provider credential access."""
    raise AssertionError('recovery read a speech key')


def test_recovery_and_offline_assembly_preserve_failed_http_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    kit, transport = failed_job(tmp_path)
    original = (kit['job'] / 'operations.json').read_bytes()
    bodies = {
        p: p.read_bytes() for p in (kit['job'] / 'evidence').glob('*.bin')
    }
    monkeypatch.setattr(jobs, '_read_speech_key', forbid_key)
    monkeypatch.setattr(socket, 'create_connection', NoNetwork())
    receipts = []
    for operation in journal(kit['job'])['operations']:
        assert (
            main(
                [
                    'recover',
                    '--job',
                    str(kit['job']),
                    '--operation',
                    operation['operation_id'],
                    '--attempt',
                    '1',
                ]
            )
            == 0
        )
        receipts.append(json.loads(capsys.readouterr().out)['receipt'])
    assert transport.calls == 2
    assert (kit['job'] / 'operations.json').read_bytes() == original
    for path, body in bodies.items():
        assert path.read_bytes() == body
    assert len(receipts) == 2
    assert receipts[0]['validated_at']
    assert receipts[0]['source']['sha256'] == receipts[0]['artifact']['sha256']
    assert (
        receipts[0]['canonical']['sha256'] != receipts[0]['source']['sha256']
    )
    result = assemble_job_pipeline(kit['job'])
    assert result.verification is not None and result.verification['ok']
    assert kit['output'].is_file()
    assert (kit['job'] / 'operations.json').read_bytes() == original
    report = json.loads((kit['job'] / result.report_path).read_text())
    assert len(report['local_recoveries']['receipts']) == 2
    assert report['speech']['request_count'] == 0
    assert read_job_status(kit['job'])['operations']['by_state'] == {
        'failed': 2
    }


def test_live_streaming_flow_retains_raw_and_uses_canonical_wav(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    prepare(kit)
    transport = StreamingTransport()
    keys = CountingKeys()
    result = run_full(kit, tmp_path, transport=transport, keys=keys)
    assert result.verification is not None and result.verification['ok']
    assert transport.calls == 2 and keys.reads == 1
    for operation in journal(kit['job'])['operations']:
        attempt = operation['attempts'][0]
        raw = kit['job'] / attempt['artifact']['path']
        assert raw.read_bytes() == streaming_wav_bytes(samples=2400)
        with pytest.raises(xai_client.InvalidAudioResponse):
            xai_client.parse_wav_header(raw)
        canonical = recoveries.canonical_path(
            kit['job'], attempt['artifact']['sha256']
        )
        assert xai_client.parse_wav_header(canonical).data_bytes == 4800


def test_recovery_is_idempotent_without_journal_or_receipt_rewrites(
    tmp_path: Path,
) -> None:
    kit, _ = failed_job(tmp_path)
    receipt = recover_first(kit)
    before = {p: p.read_bytes() for p in kit['job'].rglob('*') if p.is_file()}
    assert recover_first(kit) == receipt
    assert {
        p: p.read_bytes() for p in kit['job'].rglob('*') if p.is_file()
    } == before


@pytest.mark.parametrize(
    'case',
    [
        'status',
        'type',
        'response_hash',
        'response_size',
        'evidence_hash',
        'body',
        'posted_hash',
        'posted_chars',
        'request',
        'attempt_request',
        'signature',
        'failure_kind',
        'uncertain_verdict',
        'uncertain_state',
        'later_attempt',
        'later_operation',
        'symlink',
        'source_binding',
    ],
)
def test_recovery_rejects_unproven_or_stale_responses(
    tmp_path: Path,
    case: str,
) -> None:
    kit, _ = failed_job(tmp_path)
    data = journal(kit['job'])
    operation = data['operations'][0]
    attempt = operation['attempts'][0]
    evidence = kit['job'] / attempt['evidence']['path']
    if case == 'status':
        attempt['response']['status'] = 500
    elif case == 'type':
        attempt['response']['content_type'] = 'application/json'
    elif case == 'response_hash':
        attempt['response']['sha256'] = '0' * 64
    elif case == 'response_size':
        attempt['response']['bytes'] += 2
    elif case == 'evidence_hash':
        attempt['evidence']['sha256'] = '0' * 64
    elif case == 'body':
        evidence.write_bytes(evidence.read_bytes()[:-2])
    elif case == 'posted_hash':
        operation['posted_text_sha256'] = '0' * 64
    elif case == 'posted_chars':
        operation['posted_text_chars'] += 1
    elif case == 'request':
        operation['request_signature'] = '0' * 64
    elif case == 'attempt_request':
        attempt['request_signature'] = '0' * 64
    elif case == 'signature':
        operation['synthesis_signature'] = '0' * 64
    elif case == 'failure_kind':
        attempt['failure']['kind'] = 'connect_failure'
    elif case == 'uncertain_verdict':
        attempt['verdict'] = 'uncertain'
    elif case == 'uncertain_state':
        operation['state'] = 'uncertain'
    elif case == 'later_attempt':
        later = copy.deepcopy(attempt)
        later['attempt'] = 2
        operation['attempts'].append(later)
    elif case == 'later_operation':
        later = copy.deepcopy(operation)
        later['operation_id'] = 'a' * 32
        later['state'] = 'uncertain'
        data['operations'].append(later)
    elif case == 'symlink':
        other = tmp_path / 'external.bin'
        evidence.rename(other)
        evidence.symlink_to(other)
    elif case == 'source_binding':
        kit['srt'].write_text('changed source')
    write_journal(kit['job'], data)
    before = (kit['job'] / 'operations.json').read_bytes()
    with pytest.raises(ContractError):
        recover_first(kit)
    assert (kit['job'] / 'operations.json').read_bytes() == before
    assert not list((kit['job'] / 'evidence/recoveries').glob('*.json'))
    assert not list((kit['job'] / 'artifacts').glob('recovered-*'))


def test_recovery_obeys_the_single_writer_lock(tmp_path: Path) -> None:
    kit, _ = failed_job(tmp_path)
    with JobLock.acquire(kit['job']), pytest.raises(LockUnavailableError):
        recover_first(kit)


def test_receipt_precedes_manifest_and_recovers_after_checkpoint_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kit, _ = failed_job(tmp_path)
    manifest = kit['job'] / 'manifest.json'
    original = manifest.read_bytes()
    original_writer = atomic_write_json

    def fail_manifest(path: Path, payload: Any) -> None:
        """Refuse only the final manifest checkpoint."""
        if path == manifest:
            raise OSError('checkpoint failure')
        original_writer(path, payload)

    with monkeypatch.context() as scoped:
        scoped.setattr(jobs, 'atomic_write_json', fail_manifest)
        with pytest.raises(OSError, match='checkpoint failure'):
            recover_first(kit)
    assert manifest.read_bytes() == original
    receipts = list((kit['job'] / 'evidence/recoveries').glob('*.json'))
    assert len(receipts) == 1
    receipt_bytes = receipts[0].read_bytes()
    recovered = recover_first(kit)
    assert receipts[0].read_bytes() == receipt_bytes
    assert len(json.loads(manifest.read_text())['artifacts']) == 1
    assert recovered['artifact']['path']


@pytest.mark.parametrize(
    'case',
    [
        'missing',
        'pcm',
        'identity',
        'version',
        'manifest',
        'extra',
        'receipt_symlink',
        'duplicate',
    ],
)
def test_tampered_recovery_blocks_status_and_assembly(
    tmp_path: Path,
    case: str,
) -> None:
    kit, _ = failed_job(tmp_path)
    receipt = recover_first(kit)
    path = next((kit['job'] / 'evidence/recoveries').glob('*.json'))
    if case == 'missing':
        path.unlink()
    elif case == 'pcm':
        canonical = kit['job'] / receipt['canonical']['path']
        body = bytearray(canonical.read_bytes())
        body[-1] ^= 1
        canonical.write_bytes(body)
        receipt['canonical']['sha256'] = hashlib.sha256(body).hexdigest()
        path.write_text(json.dumps(receipt))
    elif case == 'identity':
        receipt['posted_text_sha256'] = '0' * 64
        path.write_text(json.dumps(receipt))
    elif case == 'version':
        receipt['schema_version'] = 2
        path.write_text(json.dumps(receipt))
    elif case == 'extra':
        receipt['unexpected'] = True
        path.write_text(json.dumps(receipt))
    elif case == 'receipt_symlink':
        outside = tmp_path / 'external-receipt.json'
        path.rename(outside)
        path.symlink_to(outside)
    elif case == 'duplicate':
        (path.parent / 'duplicate.json').write_text(path.read_text())
    elif case == 'manifest':
        manifest_path = kit['job'] / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['artifacts'][0]['signature'] = '0' * 64
        manifest_path.write_text(json.dumps(manifest))
    status = read_job_status(kit['job'])
    assert status['local_recoveries']['state'] == 'invalid'
    assert any('recovery' in str(x) for x in status['blocked'])
    with pytest.raises(ContractError):
        assemble_job_pipeline(kit['job'])
    with pytest.raises(ContractError):
        prepare(kit)


def test_recovered_old_bytes_do_not_resolve_a_later_unknown_attempt(
    tmp_path: Path,
) -> None:
    kit, _ = failed_job(tmp_path)
    recover_first(kit)
    data = journal(kit['job'])
    later = copy.deepcopy(data['operations'][0])
    later['operation_id'] = 'b' * 32
    later['state'] = 'started'
    attempt = later['attempts'][0]
    attempt.update(
        verdict=None,
        finished_at=None,
        response=None,
        evidence=None,
        failure=None,
        artifact=None,
    )
    data['operations'].append(later)
    write_journal(kit['job'], data)
    with pytest.raises(PipelineFailure, match='unresolved paid operations'):
        run_job_pipeline(kit['job'], key_reader=forbid_key)
    saved = journal(kit['job'])['operations'][-1]
    assert saved['state'] == 'started'
    assert saved['attempts'][0]['verdict'] is None


def test_recovery_refuses_an_evidence_directory_alias(tmp_path: Path) -> None:
    kit, _ = failed_job(tmp_path)
    evidence = kit['job'] / 'evidence'
    outside = tmp_path / 'external-evidence'
    evidence.rename(outside)
    evidence.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ContractError, match='symlink'):
        recover_first(kit)
    assert not (outside / 'canonical-wav').exists()
    assert not (outside / 'recoveries').exists()


def test_streaming_cache_without_http_provenance_blocks_before_paid_work(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    prepare(kit)
    manifest = json.loads((kit['job'] / 'manifest.json').read_text())
    segment = manifest['segments'][0]
    plant_artifact_record(
        kit['job'],
        'raw_response',
        segment['id'],
        segment['synthesis_signature'],
        streaming_wav_bytes(samples=2400),
    )
    transport = CountingTransport()
    keys = CountingKeys()
    with pytest.raises(ContractError, match='complete HTTP provenance'):
        run_full(kit, tmp_path, transport=transport, keys=keys)
    assert transport.requests == []
    assert keys.reads == 0
    status = read_job_status(kit['job'])
    assert status['local_recoveries']['state'] == 'invalid'
    assert any('complete HTTP provenance' in str(x) for x in status['blocked'])
    with pytest.raises(ContractError, match='complete HTTP provenance'):
        prepare(kit)


def test_completed_streaming_cache_requires_matching_response_metadata(
    tmp_path: Path,
) -> None:
    kit = make_kit(tmp_path)
    prepare(kit)
    run_full(kit, tmp_path, transport=StreamingTransport())
    data = journal(kit['job'])
    data['operations'][0]['attempts'][0]['response']['status'] = 500
    write_journal(kit['job'], data)
    with pytest.raises(ContractError, match='complete HTTP provenance'):
        run_job_pipeline(kit['job'], key_reader=forbid_key)


def test_missing_finite_receipt_blocks_recovered_cache(tmp_path: Path) -> None:
    kit, _ = failed_job(tmp_path, streamed=False)
    receipt = recover_first(kit)
    assert receipt['canonical']['sha256'] == receipt['source']['sha256']
    next((kit['job'] / 'evidence/recoveries').glob('*.json')).unlink()
    assert (
        read_job_status(kit['job'])['local_recoveries']['state'] == 'invalid'
    )
    with pytest.raises(ContractError, match='matching provenance'):
        assemble_job_pipeline(kit['job'])

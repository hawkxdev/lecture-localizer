"""Immutable local validation of retained speech responses."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, cast

from lecture_localizer import xai_client
from lecture_localizer.contracts import (
    ContractError,
    load_schema,
    load_strict_json_file,
    validate_instance,
)
from lecture_localizer.storage import (
    atomic_write_json,
    copy_new_file,
    move_new_file,
    sha256_file,
)

RECEIPT_DIRECTORY = 'evidence/recoveries'
CANONICAL_DIRECTORY = 'evidence/canonical-wav'
RECOVERY_SCHEMA = load_schema('recovery-v1.schema.json')
PROVENANCE_KEYS = (
    'segment_id',
    'synthesis_signature',
    'request_signature',
    'posted_text_sha256',
)


def attempt_signature(attempt: dict[str, Any]) -> str:
    """Bind every field of the unchanged historical attempt."""
    body = json.dumps(
        attempt, ensure_ascii=False, sort_keys=True, separators=(',', ':')
    ).encode()
    return hashlib.sha256(body).hexdigest()


def _token(operation_id: str, attempt: int) -> str:
    """Identify one local receipt without using unsafe path text."""
    return hashlib.sha256(f'{operation_id}:{attempt}'.encode()).hexdigest()


def _owned_path(job_dir: Path, relative: str) -> Path:
    """Require a path without aliases inside the owning job."""
    if Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise ContractError('recovery path escapes its job')
    target = job_dir.resolve()
    for component in Path(relative).parts:
        target = target / component
        if target.is_symlink():
            raise ContractError('recovery paths cannot contain symlinks')
    if not target.resolve().is_relative_to(job_dir.resolve()):
        raise ContractError('recovery path escapes its job')
    return target


def _path(job_dir: Path, relative: str) -> Path:
    """Require a retained regular file inside the owning job."""
    target = _owned_path(job_dir, relative)
    if not target.is_file():
        raise ContractError(f'recovery file is missing: {relative}')
    return target


def canonical_path(job_dir: Path, source_sha256: str) -> Path:
    """Locate the owned canonical copy of a retained response."""
    return _owned_path(job_dir, f'{CANONICAL_DIRECTORY}/{source_sha256}.wav')


def _operation_attempt(
    journal: dict[str, Any], operation_id: str, attempt_number: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Select one recorded operation and its exact attempt."""
    for operation in journal['operations']:
        if operation['operation_id'] != operation_id:
            continue
        attempts = [
            a for a in operation['attempts'] if a['attempt'] == attempt_number
        ]
        if len(attempts) != 1:
            raise ContractError('recovery needs one exact recorded attempt')
        return operation, attempts[0]
    raise ContractError('recovery operation was not found')


def _completed_source(attempt: dict[str, Any]) -> dict[str, Any]:
    """Require completed HTTP evidence of an audio validation failure."""
    failure = attempt['failure']
    response = attempt['response']
    source = attempt['evidence']
    if (
        attempt['verdict'] != 'failed'
        or failure is None
        or failure['kind'] != 'invalid_audio'
        or response is None
        or response['status'] != 200
        or response['content_type'].split(';')[0].strip().lower()
        != 'audio/wav'
        or source is None
        or response['sha256'] != source['sha256']
        or response['bytes'] != source['bytes']
        or not attempt['finished_at']
    ):
        raise ContractError('recovery requires a complete failed WAV response')
    return cast(dict[str, Any], source)


def _verify_file(job_dir: Path, record: dict[str, Any]) -> Path:
    """Verify the full bytes and size of one retained record."""
    target = _path(job_dir, record['path'])
    if (
        target.stat().st_size != record['bytes']
        or sha256_file(target) != record['sha256']
    ):
        raise ContractError('recovery file integrity mismatch')
    return target


def _verify_receipt(
    job_dir: Path, receipt: dict[str, Any], journal: dict[str, Any]
) -> None:
    """Verify provenance and complete canonical derivation read only."""
    validate_instance(receipt, RECOVERY_SCHEMA, 'recovery receipt')
    operation, attempt = _operation_attempt(
        journal, receipt['operation_id'], receipt['attempt']
    )
    source = _completed_source(attempt)
    token = _token(receipt['operation_id'], receipt['attempt'])
    if (
        any(receipt[key] != operation[key] for key in PROVENANCE_KEYS)
        or attempt['request_signature'] != receipt['request_signature']
        or receipt['attempt_sha256'] != attempt_signature(attempt)
        or receipt['source'] != source
        or receipt['artifact']['sha256'] != source['sha256']
        or receipt['artifact']['bytes'] != source['bytes']
        or receipt['artifact']['path'] != f'artifacts/recovered-{token}.wav'
        or receipt['canonical']['path']
        != f'{CANONICAL_DIRECTORY}/{source["sha256"]}.wav'
    ):
        raise ContractError('recovery receipt provenance mismatch')
    original = _verify_file(job_dir, source)
    _verify_file(job_dir, receipt['artifact'])
    canonical = _verify_file(job_dir, receipt['canonical'])
    try:
        xai_client.validate_canonical_wav(
            original, canonical, source_sha256=source['sha256']
        )
    except xai_client.InvalidAudioResponse as error:
        raise ContractError(
            f'recovery canonical WAV is invalid: {error}'
        ) from error


def read_receipts(
    job_dir: Path, journal: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Read and validate all immutable local recovery receipts."""
    directory = _owned_path(job_dir, RECEIPT_DIRECTORY)
    if not directory.exists():
        return []
    if not directory.is_dir() or journal is None:
        raise ContractError(
            'recovery receipts require their operation journal'
        )
    receipts = []
    for path in sorted(directory.glob('*.json')):
        if path.is_symlink():
            raise ContractError('recovery receipt cannot be a symlink')
        receipt = load_strict_json_file(path)
        validate_instance(receipt, RECOVERY_SCHEMA, 'recovery receipt')
        if (
            path.name
            != f'{_token(receipt["operation_id"], receipt["attempt"])}.json'
        ):
            raise ContractError(
                'recovery receipt name disagrees with identity'
            )
        _verify_receipt(job_dir, receipt, journal)
        receipts.append(receipt)
    return receipts


def validate_manifest_links(
    manifest: dict[str, Any], receipts: list[dict[str, Any]]
) -> None:
    """Bind every recovered cache record to its local validation."""
    for record in manifest['artifacts']:
        if not record['path'].startswith('artifacts/recovered-'):
            continue
        matches = [
            r for r in receipts if r['artifact']['path'] == record['path']
        ]
        if (
            len(matches) != 1
            or record['kind'] != 'raw_response'
            or record['sha256'] != matches[0]['source']['sha256']
            or record['segment'] != matches[0]['segment_id']
            or record['signature'] != matches[0]['synthesis_signature']
        ):
            raise ContractError('recovered cache lacks matching provenance')


def recover(
    job_dir: Path,
    manifest: dict[str, Any],
    journal: dict[str, Any],
    *,
    operation_id: str,
    attempt_number: int,
    posted_text_sha256: str,
    request_signature: str,
    validated_at: str,
) -> dict[str, Any]:
    """Validate retained bytes and publish a local recovery receipt."""
    operation, attempt = _operation_attempt(
        journal, operation_id, attempt_number
    )
    segment = next(
        (
            s
            for s in manifest['segments']
            if s['id'] == operation['segment_id']
        ),
        None,
    )
    if (
        segment is None
        or operation['state'] != 'failed'
        or operation['attempts'][-1] != attempt
        or operation['synthesis_signature'] != segment['synthesis_signature']
        or operation['posted_text_sha256'] != posted_text_sha256
        or operation['request_signature'] != request_signature
        or attempt['request_signature'] != request_signature
        or [
            o
            for o in journal['operations']
            if o['segment_id'] == operation['segment_id']
        ][-1]
        != operation
    ):
        raise ContractError('recovery needs the latest failed current request')
    source = _completed_source(attempt)
    original = _verify_file(job_dir, source)
    token = _token(operation_id, attempt_number)
    for receipt in read_receipts(job_dir, journal):
        if (
            receipt['operation_id'] == operation_id
            and receipt['attempt'] == attempt_number
        ):
            return receipt
    canonical_relative = f'{CANONICAL_DIRECTORY}/{source["sha256"]}.wav'
    canonical = canonical_path(job_dir, source['sha256'])
    try:
        xai_client.canonicalize_completed_wav(
            original, canonical, source_sha256=source['sha256']
        )
    except xai_client.InvalidAudioResponse as error:
        raise ContractError(
            f'retained WAV cannot be recovered: {error}'
        ) from error
    artifact_relative = f'artifacts/recovered-{token}.wav'
    artifact = _owned_path(job_dir, artifact_relative)
    if artifact.exists() or artifact.is_symlink():
        _verify_file(job_dir, {**source, 'path': artifact_relative})
    else:
        copy_new_file(original, artifact)
    receipt = {
        'schema_version': 1,
        'operation_id': operation_id,
        'attempt': attempt_number,
        **{key: operation[key] for key in PROVENANCE_KEYS},
        'attempt_sha256': attempt_signature(attempt),
        'source': source,
        'artifact': {**source, 'path': artifact_relative},
        'canonical': {
            'path': canonical_relative,
            'sha256': sha256_file(canonical),
            'bytes': canonical.stat().st_size,
        },
        'validated_at': validated_at,
    }
    _verify_receipt(job_dir, receipt, journal)
    directory = _owned_path(job_dir, RECEIPT_DIRECTORY)
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f'.{uuid.uuid4().hex}.partial'
    try:
        atomic_write_json(temporary, receipt)
        move_new_file(temporary, directory / f'{token}.json')
    finally:
        temporary.unlink(missing_ok=True)
    return receipt

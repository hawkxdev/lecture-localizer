"""Tests for durable writes, immutable artifacts, locks and guards."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from lecture_localizer.storage import (
    DestinationExistsError,
    IntegrityRecord,
    JobLock,
    LockUnavailableError,
    StorageError,
    atomic_write_bytes,
    atomic_write_json,
    create_new_file,
    ensure_distinct_files,
    ensure_outside,
    sha256_bytes,
    sha256_file,
    utc_now_iso,
    verify_integrity,
)


def test_sha256_file_matches_bytes(tmp_path: Path) -> None:
    path = tmp_path / 'data.bin'
    path.write_bytes(b'payload')
    assert sha256_file(path) == sha256_bytes(b'payload')


def test_atomic_write_json_roundtrip_and_replace(tmp_path: Path) -> None:
    path = tmp_path / 'state.json'
    atomic_write_json(path, {'ключ': 'значение', 'n': 1})
    assert json.loads(path.read_text(encoding='utf-8')) == {
        'ключ': 'значение',
        'n': 1,
    }
    atomic_write_json(path, {'n': 2})
    assert json.loads(path.read_text(encoding='utf-8')) == {'n': 2}
    assert list(tmp_path.iterdir()) == [path]


def test_atomic_write_failure_preserves_previous_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / 'state.json'
    atomic_write_json(path, {'n': 1})
    original = path.read_bytes()

    def fail_replace(src: object, dst: object) -> None:
        """Simulate a full disk at the final replace step."""
        raise OSError(28, 'No space left on device')

    monkeypatch.setattr(os, 'replace', fail_replace)
    with pytest.raises(OSError, match='No space left'):
        atomic_write_json(path, {'n': 2})
    assert path.read_bytes() == original
    leftovers = [p for p in tmp_path.iterdir() if p.name != 'state.json']
    assert leftovers == []


def test_atomic_write_bytes_durability_call(tmp_path: Path) -> None:
    nested = tmp_path / 'nested'
    nested.mkdir()
    path = nested / 'blob.bin'
    atomic_write_bytes(path, b'abc')
    assert path.read_bytes() == b'abc'


def test_create_new_file_refuses_existing(tmp_path: Path) -> None:
    path = tmp_path / 'immutable.bin'
    create_new_file(path, b'first')
    with pytest.raises(DestinationExistsError, match='immutable'):
        create_new_file(path, b'second')
    assert path.read_bytes() == b'first'


def test_verify_integrity_reports_missing_and_corrupt(
    tmp_path: Path,
) -> None:
    good = tmp_path / 'good.bin'
    corrupt = tmp_path / 'corrupt.bin'
    good.write_bytes(b'good')
    corrupt.write_bytes(b'corrupted')
    records = (
        IntegrityRecord('good', 'good.bin', sha256_bytes(b'good')),
        IntegrityRecord('corrupt', 'corrupt.bin', sha256_bytes(b'expected')),
        IntegrityRecord('absent', 'absent.bin', sha256_bytes(b'x')),
    )
    failures = verify_integrity(tmp_path, records)
    assert [record.label for record in failures] == ['corrupt', 'absent']


def test_job_lock_create_release_and_content(tmp_path: Path) -> None:
    lock = JobLock.acquire(tmp_path)
    try:
        assert (tmp_path / 'lock').is_file()
        payload = json.loads((tmp_path / 'lock').read_text(encoding='utf-8'))
        assert payload['pid'] == os.getpid()
        assert payload['created_at'].startswith(utc_now_iso()[:10])
    finally:
        lock.release()
    assert not (tmp_path / 'lock').exists()


def test_job_lock_blocks_second_writer(tmp_path: Path) -> None:
    holder = JobLock.acquire(tmp_path)
    try:
        with pytest.raises(LockUnavailableError, match='running process'):
            JobLock.acquire(tmp_path)
        assert (tmp_path / 'lock').is_file()
    finally:
        holder.release()


def test_stale_lock_stays_visible_and_blocks(tmp_path: Path) -> None:
    dead_pid = 999_999_999
    lock_path = tmp_path / 'lock'
    lock_path.write_text(
        json.dumps({'pid': dead_pid, 'created_at': utc_now_iso()}),
        encoding='utf-8',
    )
    with pytest.raises(LockUnavailableError, match='uncertain stale lock'):
        JobLock.acquire(tmp_path)
    payload = json.loads(lock_path.read_text(encoding='utf-8'))
    assert payload['pid'] == dead_pid


def test_unreadable_lock_is_reported_as_uncertain(tmp_path: Path) -> None:
    lock_path = tmp_path / 'lock'
    lock_path.write_bytes(b'\x00not json')
    with pytest.raises(
        LockUnavailableError, match='uncertain lock with unreadable'
    ):
        JobLock.acquire(tmp_path)
    assert lock_path.read_bytes() == b'\x00not json'


def test_job_lock_releases_on_exception(tmp_path: Path) -> None:
    with (
        pytest.raises(RuntimeError, match='boom'),
        JobLock.acquire(tmp_path),
    ):
        raise RuntimeError('boom')
    assert not (tmp_path / 'lock').exists()


def test_ensure_distinct_files_rejects_same_path(tmp_path: Path) -> None:
    video = tmp_path / 'v.mp4'
    video.write_bytes(b'v')
    with pytest.raises(StorageError, match='same file'):
        ensure_distinct_files({'video': video, 'output': video})


def test_ensure_distinct_files_rejects_hardlinks(tmp_path: Path) -> None:
    first = tmp_path / 'a.bin'
    second = tmp_path / 'b.bin'
    first.write_bytes(b'data')
    os.link(first, second)
    with pytest.raises(StorageError, match='same file'):
        ensure_distinct_files({'first': first, 'second': second})


def test_ensure_distinct_files_accepts_distinct(tmp_path: Path) -> None:
    first = tmp_path / 'a.bin'
    second = tmp_path / 'b.bin'
    first.write_bytes(b'a')
    second.write_bytes(b'b')
    ensure_distinct_files({'first': first, 'second': second})


def test_ensure_outside_rejects_job_internal_paths(tmp_path: Path) -> None:
    job = tmp_path / 'job'
    job.mkdir()
    inside = job / 'snapshots' / 'x.json'
    inside.parent.mkdir()
    inside.write_bytes(b'{}')
    with pytest.raises(StorageError, match='outside the job directory'):
        ensure_outside(job, {'video': inside})
    ensure_outside(job, {'video': tmp_path / 'v.mp4'})


def test_ensure_outside_rejects_job_dir_itself(tmp_path: Path) -> None:
    job = tmp_path / 'job'
    job.mkdir()
    with pytest.raises(StorageError, match='outside the job directory'):
        ensure_outside(job, {'output': job})


def test_release_reports_replacement_and_keeps_foreign_lock(
    tmp_path: Path,
) -> None:
    first = JobLock.acquire(tmp_path)
    replaced = json.dumps(
        {
            'pid': os.getpid(),
            'created_at': utc_now_iso(),
            'token': 'external-replacement-token',
        }
    )
    (tmp_path / 'lock').write_text(replaced, encoding='utf-8')
    with pytest.raises(LockUnavailableError, match='was replaced'):
        first.release()
    payload = json.loads((tmp_path / 'lock').read_text(encoding='utf-8'))
    assert payload['token'] == 'external-replacement-token'
    assert payload['pid'] == os.getpid()


@pytest.mark.parametrize(
    'replacement',
    [
        pytest.param(b'{partial json', id='malformed json'),
        pytest.param(b'[]', id='non object json'),
        pytest.param(b'\xff\xfe invalid utf8', id='invalid utf8'),
    ],
)
def test_unreadable_replacement_lock_survives_release(
    tmp_path: Path, replacement: bytes
) -> None:
    lock = JobLock.acquire(tmp_path)
    lock.path.write_bytes(replacement)
    with pytest.raises(LockUnavailableError, match='stays in place'):
        lock.release()
    assert lock.path.read_bytes() == replacement


def test_release_of_absent_lock_is_idempotent(tmp_path: Path) -> None:
    lock = JobLock.acquire(tmp_path)
    lock.path.unlink()
    lock.release()
    assert not (tmp_path / 'lock').exists()

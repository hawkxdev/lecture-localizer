"""Filesystem durability: atomic writes, immutable artifacts, locking."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class StorageError(RuntimeError):
    """A durable file operation failed."""

    def __init__(self, message: str, kind: str = 'storage') -> None:
        """Record the failure message and its stable kind."""
        super().__init__(message)
        self.kind = kind


class DestinationExistsError(StorageError):
    """A destination that must stay untouched already exists."""


class LockUnavailableError(StorageError):
    """Another writer holds the job or an uncertain lock remains."""


class ArtifactIntegrityError(StorageError):
    """Cached bytes do not match their recorded integrity."""


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(UTC).isoformat()


def sha256_bytes(data: bytes) -> str:
    """Return the hex SHA 256 digest of bytes."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    """Return the hex SHA 256 digest of one file."""
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def fsync_directory(directory: Path) -> None:
    """Flush a directory entry so a replace survives a crash."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    """Write bytes through a temporary file and one atomic replace."""
    target = Path(path)
    directory = target.parent
    fd, temp_name = None, ''
    try:
        fd, temp_name = _mkstemp_in(directory, target.name)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    except BaseException:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)
        raise
    fsync_directory(directory)


def _mkstemp_in(directory: Path, name: str) -> tuple[int, str]:
    """Create a unique temporary file beside the target."""
    return tempfile.mkstemp(
        dir=directory, prefix=f'.{name}.tmp-', suffix='.partial'
    )


def atomic_write_json(path: str | Path, payload: Any) -> None:
    """Serialize a payload as UTF 8 JSON and write it atomically."""
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    atomic_write_bytes(path, (text + '\n').encode('utf-8'))


def create_new_file(path: str | Path, data: bytes) -> None:
    """Create one file that must not exist yet, durably."""
    target = Path(path)
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError as error:
        raise DestinationExistsError(
            f'refusing to overwrite an existing immutable file: {target}'
        ) from error
    with os.fdopen(fd, 'wb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    fsync_directory(target.parent)


def move_new_file(source: str | Path, target: str | Path) -> None:
    """Move one fully written file to a fresh immutable destination.

    Publication is a hard link creation, which never clobbers any
    existing destination name, including dangling symlinks and names
    created between the check and the link, and the source file is
    retained untouched when the destination already exists. The
    destination directory entry is flushed so the artifact survives a
    crash.
    """
    source_path = Path(source)
    target_path = Path(target)
    try:
        os.link(source_path, target_path)
    except FileExistsError as error:
        raise DestinationExistsError(
            f'refusing to overwrite an existing destination: {target_path}'
        ) from error
    os.unlink(source_path)
    fsync_directory(target_path.parent)


_COPY_CHUNK_BYTES = 1 << 20


def copy_new_file(source: str | Path, target: str | Path) -> None:
    """Copy one file to a fresh destination without clobbering it.

    The bytes are streamed through a temporary file on the destination
    filesystem, so cross filesystem registration of a large candidate
    never loads the whole file into memory. The destination name is
    claimed by a no clobber link only after the copy is fully written
    and flushed, so an existing destination, including a dangling
    symlink, stays untouched and the temporary file is removed.
    """
    source_path = Path(source)
    target_path = Path(target)
    fd, temp_name = _mkstemp_in(target_path.parent, target_path.name)
    linked = False
    try:
        with (
            open(source_path, 'rb') as reader,
            os.fdopen(fd, 'wb') as writer,
        ):
            shutil.copyfileobj(reader, writer, length=_COPY_CHUNK_BYTES)
            writer.flush()
            os.fsync(writer.fileno())
        try:
            os.link(temp_name, target_path)
        except FileExistsError as error:
            raise DestinationExistsError(
                f'refusing to overwrite an existing destination: {target_path}'
            ) from error
        linked = True
    finally:
        if not linked:
            os.unlink(temp_name)
    os.unlink(temp_name)
    fsync_directory(target_path.parent)


@dataclass(frozen=True)
class IntegrityRecord:
    """One expected file with its recorded digest."""

    label: str
    relative_path: str
    sha256: str


def verify_integrity(
    base: Path, records: tuple[IntegrityRecord, ...]
) -> tuple[IntegrityRecord, ...]:
    """Return records whose cached bytes are missing or corrupted."""
    failures: list[IntegrityRecord] = []
    for record in records:
        target = base / record.relative_path
        try:
            actual = sha256_file(target)
        except OSError:
            failures.append(record)
            continue
        if actual != record.sha256:
            failures.append(record)
    return tuple(failures)


def _read_lock(path: Path) -> dict[str, Any] | None:
    """Parse an existing lock file, returning None when unreadable."""
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _pid_is_alive(pid: int) -> bool:
    """Return True when a process id exists right now."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _describe_lock(path: Path) -> str:
    """Explain an existing lock without removing it."""
    payload = _read_lock(path)
    if payload is None:
        return (
            f'an uncertain lock with unreadable content remains at {path};'
            ' inspect it and remove the lock file manually after checking'
            ' that no writer is active'
        )
    pid = payload.get('pid')
    started = payload.get('created_at')
    if isinstance(pid, int) and _pid_is_alive(pid):
        return (
            f'the job is locked by running process {pid} since {started!r};'
            ' wait for that writer to finish'
        )
    return (
        f'an uncertain stale lock from process {pid!r} since {started!r}'
        f' remains at {path}; its writer is not running anymore; inspect'
        ' the job and remove the lock file manually'
    )


@dataclass
class JobLock:
    """Exclusive one writer lock for a job directory.

    Every acquisition stores a unique instance token in the lock file;
    release removes the file only when it still carries this instance
    token, so an externally replaced lock is reported instead of
    deleting the replacement that belongs to another writer.
    """

    path: Path
    token: str

    @classmethod
    def acquire(cls, job_dir: Path) -> JobLock:
        """Create the lock file atomically or explain the blocker."""
        job_dir.mkdir(parents=True, exist_ok=True)
        path = job_dir / 'lock'
        token = secrets.token_hex(16)
        payload = json.dumps(
            {
                'pid': os.getpid(),
                'created_at': utc_now_iso(),
                'token': token,
            }
        )
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError as error:
            raise LockUnavailableError(
                _describe_lock(path), kind='lock'
            ) from error
        with os.fdopen(fd, 'wb') as handle:
            handle.write(payload.encode('utf-8'))
            handle.flush()
            os.fsync(handle.fileno())
        return cls(path=path, token=token)

    def release(self) -> None:
        """Remove the lock only when this instance still owns it.

        Ownership is established positively from readable content that
        carries this instance token. An unreadable, malformed or foreign
        replacement stays in place and is reported; actual absence is
        idempotent.
        """
        if not self.path.exists():
            return
        try:
            raw = self.path.read_bytes()
        except OSError as error:
            raise LockUnavailableError(
                f'the lock at {self.path} cannot be read to confirm'
                f' ownership: {error}',
                kind='lock',
            ) from error
        try:
            payload = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, ValueError) as error:
            raise LockUnavailableError(
                f'the lock at {self.path} was replaced with unreadable'
                ' content; it stays in place and must be inspected'
                ' manually',
                kind='lock',
            ) from error
        if not isinstance(payload, dict) or payload.get('token') != self.token:
            raise LockUnavailableError(
                f'the lock at {self.path} was replaced by another writer'
                ' after this instance acquired it; the replacement stays'
                ' in place and must be inspected manually',
                kind='lock',
            )
        self.path.unlink(missing_ok=True)

    def __enter__(self) -> JobLock:
        """Return self for use as a context manager."""
        return self

    def __exit__(self, *_exc: object) -> None:
        """Release the lock on any exit path."""
        self.release()


def ensure_distinct_files(paths: dict[str, Path]) -> None:
    """Reject any two entries that point to one existing file."""
    resolved: dict[str, Path] = {}
    for name, path in paths.items():
        resolved_path = path.resolve()
        for other_name, other in resolved.items():
            if resolved_path == other or (
                resolved_path.is_file()
                and other.is_file()
                and resolved_path.samefile(other)
            ):
                raise StorageError(
                    f'{name} and {other_name} point to the same file:'
                    f' {resolved_path}',
                    kind='alias',
                )
        resolved[name] = resolved_path


def ensure_outside(job_dir: Path, paths: dict[str, Path]) -> None:
    """Reject paths located inside the managed job directory."""
    base = job_dir.resolve()
    for name, path in paths.items():
        resolved = path.resolve()
        if resolved == base or base in resolved.parents:
            raise StorageError(
                f'{name} must stay outside the job directory {base}',
                kind='alias',
            )

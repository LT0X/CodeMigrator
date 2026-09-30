"""Immutable, host-local content addressed objects for private runtime state."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import re
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID


class CasIntegrityError(RuntimeError):
    """A stored object is missing or no longer matches its digest and size."""


@dataclass(frozen=True, slots=True)
class CasObject:
    digest: str
    size: int

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", self.digest):
            raise ValueError("CAS digest must be SHA-256")
        if type(self.size) is not int or self.size < 0:
            raise ValueError("CAS size must be non-negative")


@dataclass(frozen=True, slots=True)
class CheckpointIndex:
    graph_family: str
    owner_kind: str
    owner_id: UUID
    thread_id: str
    namespace: str
    checkpoint_id: str
    parent_checkpoint_id: str | None
    object: CasObject

    @property
    def reference_key(self) -> str:
        return f"checkpoint:{self.thread_id}:{self.namespace}:{self.checkpoint_id}"


@dataclass(frozen=True, slots=True)
class PendingWriteIndex:
    graph_family: str
    owner_kind: str
    owner_id: UUID
    thread_id: str
    namespace: str
    checkpoint_id: str
    task_id: str
    write_index: int
    object: CasObject

    @property
    def reference_key(self) -> str:
        return (
            f"write:{self.thread_id}:{self.namespace}:"
            f"{self.checkpoint_id}:{self.task_id}:{self.write_index}"
        )


class FileHostCAS:
    """Append-only CAS; publication uses a same-directory hard link."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, digest: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("CAS digest must be SHA-256")
        return self.root / digest[:2] / digest

    def _ensure_shard(self, shard: Path) -> None:
        lock_dir = self.root / ".locks"
        lock_dir.mkdir(exist_ok=True)
        descriptor = os.open(lock_dir / f"shard-{shard.name}.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                shard.mkdir()
            except FileExistsError:
                if not shard.is_dir():
                    raise
            else:
                root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    try:
                        os.fsync(root_fd)
                    except OSError:
                        shard.rmdir()
                        raise
                finally:
                    os.close(root_fd)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def put(self, body: bytes) -> CasObject:
        if not isinstance(body, bytes):
            raise TypeError("CAS body must be bytes")
        ref = CasObject(hashlib.sha256(body).hexdigest(), len(body))
        target = self.path_for(ref.digest)
        self._ensure_shard(target.parent)
        if target.exists():
            self.read(ref)
            return ref
        descriptor, temporary = tempfile.mkstemp(suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                self.read(ref)
            else:
                directory_fd = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return ref

    def read(self, ref: CasObject) -> bytes:
        try:
            descriptor = os.open(self.path_for(ref.digest), os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                body = stream.read()
        except (FileNotFoundError, OSError) as exc:
            raise CasIntegrityError("CAS object unavailable") from exc
        if len(body) != ref.size or hashlib.sha256(body).hexdigest() != ref.digest:
            raise CasIntegrityError("CAS object integrity mismatch")
        return body

    def delete(self, digest: str) -> None:
        self.path_for(digest).unlink(missing_ok=True)

    def iter_objects(self) -> tuple[Path, ...]:
        return tuple(
            path
            for path in self.root.glob("[0-9a-f][0-9a-f]/*")
            if path.is_file() and re.fullmatch(r"[0-9a-f]{64}", path.name)
        )

    @asynccontextmanager
    async def lock(self, digest: str) -> AsyncIterator[None]:
        self.path_for(digest)  # Validate before using the digest as a filename.
        lock_dir = self.root / ".locks"
        lock_dir.mkdir(exist_ok=True)
        descriptor = os.open(lock_dir / f"{digest}.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            await asyncio.to_thread(fcntl.flock, descriptor, fcntl.LOCK_EX)
            yield
        finally:
            await asyncio.to_thread(fcntl.flock, descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


class CasReferenceStore(Protocol):
    async def add_cas_reference(
        self, object_ref: CasObject, owner_kind: str, owner_id: UUID, reference_key: str
    ) -> None: ...

    async def get_cas_reference(
        self, owner_kind: str, owner_id: UUID, reference_key: str
    ) -> CasObject | None: ...

    async def release_cas_reference(
        self, owner_kind: str, owner_id: UUID, reference_key: str
    ) -> CasObject | None: ...

    async def referenced_digests(self) -> frozenset[str]: ...


class CasLedger:
    """Publish verified CAS objects and release them after the last owner ref."""

    def __init__(self, cas: FileHostCAS, store: CasReferenceStore) -> None:
        self.cas = cas
        self.store = store

    async def put(
        self, body: bytes, owner_kind: str, owner_id: UUID, reference_key: str
    ) -> CasObject:
        digest = hashlib.sha256(body).hexdigest()
        async with self.cas.lock(digest):
            object_ref = await asyncio.to_thread(self.cas.put, body)
            await self.store.add_cas_reference(object_ref, owner_kind, owner_id, reference_key)
            return object_ref

    async def release(self, owner_kind: str, owner_id: UUID, reference_key: str) -> bool:
        current = await self.store.get_cas_reference(owner_kind, owner_id, reference_key)
        if current is None:
            return False
        async with self.cas.lock(current.digest):
            last = await self.store.release_cas_reference(owner_kind, owner_id, reference_key)
            if last is None:
                return False
            await asyncio.to_thread(self.cas.delete, last.digest)
            return True

    async def collect_orphans(self, *, min_age_seconds: float) -> int:
        if min_age_seconds < 0:
            raise ValueError("orphan grace period must be non-negative")
        removed = 0
        for path in self.cas.iter_objects():
            async with self.cas.lock(path.name):
                if path.exists() and time.time() - path.stat().st_mtime >= min_age_seconds:
                    if path.name not in await self.store.referenced_digests():
                        await asyncio.to_thread(self.cas.delete, path.name)
                        removed += 1
        return removed


__all__ = [
    "CasIntegrityError",
    "CasLedger",
    "CasObject",
    "CheckpointIndex",
    "FileHostCAS",
    "PendingWriteIndex",
]

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import uuid4

import pytest

from codemigrator.runtime.cas import CasIntegrityError, CasLedger, FileHostCAS
from codemigrator.runtime.store import InMemoryRuntimeStore, StoreCommitError


def test_file_cas_is_content_addressed_and_survives_reopen(tmp_path: Path):
    cas = FileHostCAS(tmp_path)
    body = b"private checkpoint body\x00"
    ref = cas.put(body)
    assert ref.digest == hashlib.sha256(body).hexdigest()
    assert ref.size == len(body)
    assert cas.put(body) == ref
    assert FileHostCAS(tmp_path).read(ref) == body
    assert len(list(tmp_path.rglob(ref.digest))) == 1


def test_new_shard_fsyncs_cas_root_before_object_publication(tmp_path: Path, monkeypatch):
    cas = FileHostCAS(tmp_path)
    first_body = b"new shard"
    prefix = hashlib.sha256(first_body).hexdigest()[:2]
    existing_shard_body = next(
        candidate
        for index in range(4096)
        if (candidate := f"another object {index}".encode())
        and hashlib.sha256(candidate).hexdigest().startswith(prefix)
    )
    actions: list[tuple[str, Path]] = []
    real_fsync, real_link = os.fsync, os.link

    def observed_fsync(descriptor: int) -> None:
        actions.append(("fsync", Path(os.readlink(f"/proc/self/fd/{descriptor}"))))
        real_fsync(descriptor)

    def observed_link(source: str, target: str) -> None:
        actions.append(("link", Path(target)))
        real_link(source, target)

    monkeypatch.setattr(os, "fsync", observed_fsync)
    monkeypatch.setattr(os, "link", observed_link)
    cas.put(first_body)
    assert ("fsync", tmp_path) in actions
    assert actions.index(("fsync", tmp_path)) < next(
        index for index, action in enumerate(actions) if action[0] == "link"
    )
    actions.clear()
    cas.put(existing_shard_body)
    assert ("fsync", tmp_path) not in actions


def test_file_cas_rejects_tampered_body_before_decode(tmp_path: Path):
    cas = FileHostCAS(tmp_path)
    ref = cas.put(b"trusted")
    cas.path_for(ref.digest).write_bytes(b"corrupt")
    with pytest.raises(CasIntegrityError):
        cas.read(ref)
    with pytest.raises(CasIntegrityError):
        cas.put(b"trusted")


def test_file_cas_write_failure_does_not_publish_partial_object(tmp_path: Path, monkeypatch):
    cas = FileHostCAS(tmp_path)
    real_link = os.link

    def fail_link(source, target):
        raise OSError("injected CAS publication failure")

    monkeypatch.setattr(os, "link", fail_link)
    with pytest.raises(OSError, match="injected"):
        cas.put(b"uncommitted")
    monkeypatch.setattr(os, "link", real_link)
    assert list(tmp_path.rglob("*.tmp")) == []
    assert cas.put(b"uncommitted").size == len(b"uncommitted")


def test_file_cas_rejects_invalid_digest_path(tmp_path: Path):
    cas = FileHostCAS(tmp_path)
    with pytest.raises(ValueError):
        cas.path_for("../escape")


@pytest.mark.asyncio
async def test_multi_owner_reference_is_idempotent_and_last_release_deletes(tmp_path: Path):
    cas = FileHostCAS(tmp_path)
    ledger = CasLedger(cas, InMemoryRuntimeStore())
    first_owner, second_owner = uuid4(), uuid4()
    first = await ledger.put(b"shared private body", "draft", first_owner, "graph:1")
    assert await ledger.put(b"shared private body", "draft", first_owner, "graph:1") == first
    assert await ledger.put(b"shared private body", "run", second_owner, "agent:1") == first
    assert not await ledger.release("draft", first_owner, "graph:1")
    assert cas.read(first) == b"shared private body"
    assert await ledger.release("run", second_owner, "agent:1")
    assert not cas.path_for(first.digest).exists()
    assert not await ledger.release("run", second_owner, "agent:1")


@pytest.mark.asyncio
async def test_failed_reference_publish_leaves_collectible_orphan(tmp_path: Path):
    cas = FileHostCAS(tmp_path)
    store = InMemoryRuntimeStore()
    ledger = CasLedger(cas, store)
    store.fail_next_commit()
    with pytest.raises(StoreCommitError, match="injected"):
        await ledger.put(b"unpublished", "run", uuid4(), "checkpoint:1")
    assert list(cas.iter_objects())
    assert await ledger.collect_orphans(min_age_seconds=0) == 1
    assert list(cas.iter_objects()) == []

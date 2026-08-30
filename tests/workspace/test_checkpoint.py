from __future__ import annotations

import uuid
from pathlib import Path

from codemigrator.core import StableErrorCode, WriteScope, WriteScopeOut
from codemigrator.workspace import (
    CheckpointReceipt,
    CheckpointRejection,
    CheckpointService,
    DirectoryDiffProvider,
    InMemoryCandidateRefStore,
    WorkspaceManager,
)


def _scope() -> WriteScope:
    return WriteScope(out=WriteScopeOut(write_paths=["src/a.py"], create_roots=["generated"]))


def test_shell_scope_violation_does_not_create_commit_or_advance_ref(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "managed")
    handle = manager.provision(uuid.uuid4(), uuid.uuid4(), 0, "base")
    manager.start_iteration(handle)
    root = manager.root(handle)
    (Path(handle.path) / "src").mkdir()
    root.write_atomic("src/a.py", b"ok")
    root.write_atomic("outside.txt", b"bad")
    git = InMemoryCandidateRefStore(expected_oid="base")
    service = CheckpointService(manager=manager, git=git)

    result = service.checkpoint(
        handle, _scope(), DirectoryDiffProvider(root.path, base_files={}), "base"
    )

    assert isinstance(result, CheckpointRejection)
    assert result.code is StableErrorCode.WRITE_SCOPE_VIOLATION
    assert result.out_of_scope_paths == ("outside.txt",)
    assert git.create_calls == 0
    assert git.ref == "base"


def test_allowed_changes_checkpoint_once_and_repeat_is_idempotent(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "managed")
    handle = manager.provision(uuid.uuid4(), uuid.uuid4(), 0, "base")
    manager.start_iteration(handle)
    root = manager.root(handle)
    (Path(handle.path) / "src").mkdir()
    root.write_atomic("src/a.py", b"ok")
    git = InMemoryCandidateRefStore(expected_oid="base")
    service = CheckpointService(manager=manager, git=git)
    diff = DirectoryDiffProvider(root.path, base_files={})

    first = service.checkpoint(handle, _scope(), diff, "base")
    second = service.checkpoint(handle, _scope(), diff, "base")

    assert isinstance(first, CheckpointReceipt)
    assert second == first
    assert git.create_calls == 1
    assert first.manifest.scope_check_passed


def test_create_root_only_allows_new_files_and_excludes_build_cache(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "managed")
    handle = manager.provision(uuid.uuid4(), uuid.uuid4(), 0, "base")
    manager.start_iteration(handle)
    root = manager.root(handle)
    (Path(handle.path) / "generated").mkdir()
    (Path(handle.path) / "generated/.venv").mkdir()
    root.write_atomic("generated/new.py", b"new")
    root.write_atomic("generated/.venv/cache", b"cache")
    diff = DirectoryDiffProvider(root.path, base_files={})
    git = InMemoryCandidateRefStore(expected_oid="base")
    service = CheckpointService(manager=manager, git=git)

    result = service.checkpoint(
        handle,
        WriteScope(out=WriteScopeOut(write_paths=[], create_roots=["generated"])),
        diff,
        "base",
        build_excludes=("generated/.venv",),
    )

    assert isinstance(result, CheckpointReceipt)
    assert result.manifest.file_count == 1


def test_ref_movement_is_not_force_overwritten(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "managed")
    handle = manager.provision(uuid.uuid4(), uuid.uuid4(), 0, "base")
    manager.start_iteration(handle)
    (Path(handle.path) / "src").mkdir()
    manager.root(handle).write_atomic("src/a.py", b"ok")
    git = InMemoryCandidateRefStore(expected_oid="someone-else")
    service = CheckpointService(manager=manager, git=git)

    result = service.checkpoint(
        handle, _scope(), DirectoryDiffProvider(handle.path, base_files={}), "base"
    )

    assert isinstance(result, CheckpointRejection)
    assert result.code is StableErrorCode.CANDIDATE_REF_CONFLICT
    assert git.force_updates == 0


def test_ref_conflict_retries_existing_commit_without_creating_another(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "managed")
    handle = manager.provision(uuid.uuid4(), uuid.uuid4(), 0, "base")
    manager.start_iteration(handle)
    (Path(handle.path) / "src").mkdir()
    manager.root(handle).write_atomic("src/a.py", b"ok")
    git = InMemoryCandidateRefStore(expected_oid="someone-else")
    service = CheckpointService(manager=manager, git=git)
    diff = DirectoryDiffProvider(handle.path, base_files={})

    first = service.checkpoint(handle, _scope(), diff, "base")
    assert isinstance(first, CheckpointRejection)
    assert first.commit_created
    assert git.create_calls == 1

    git.ref = "base"
    second = service.checkpoint(handle, _scope(), diff, "base")

    assert isinstance(second, CheckpointReceipt)
    assert git.create_calls == 1


def test_checkpoint_marks_structured_ledger_bypass_as_infrastructure_failure(
    tmp_path: Path,
) -> None:
    manager = WorkspaceManager(tmp_path / "managed")
    handle = manager.provision(uuid.uuid4(), uuid.uuid4(), 0, "base")
    manager.start_iteration(handle)
    manager.record_write(handle, "WriteFile", "generated/existing.py", 3, "OVERWRITTEN")
    events = []
    service = CheckpointService(
        manager=manager,
        git=InMemoryCandidateRefStore(expected_oid="base"),
        audit_sink=events.append,
    )

    result = service.checkpoint(
        handle,
        WriteScope(out=WriteScopeOut(write_paths=[], create_roots=["generated"])),
        DirectoryDiffProvider(handle.path, base_files={}),
        "base",
    )

    assert isinstance(result, CheckpointRejection)
    assert result.infrastructure_failure
    assert events[0].point == "checkpoint.pre"
    assert events[0].outcome == "REJECTED"

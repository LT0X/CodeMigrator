from __future__ import annotations

import uuid
from pathlib import Path

from codemigrator.core import StableErrorCode, WriteScope, WriteScopeOut
from codemigrator.workspace import (
    CandidateRefConflict,
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
    assert result.generation_consumed
    assert manager.root(handle).path.exists()
    assert manager._handles[handle.path].state.value == "FROZEN"


class _ConflictOnceAtExpected(InMemoryCandidateRefStore):
    def __init__(self) -> None:
        super().__init__(expected_oid="base")
        self._conflicted = False

    def advance_candidate_ref(self, handle, expected_oid, new_oid) -> None:
        if not self._conflicted:
            self._conflicted = True
            raise CandidateRefConflict("simulated commit/ref recovery window")
        super().advance_candidate_ref(handle, expected_oid, new_oid)


def test_ref_conflict_retries_existing_commit_without_creating_another(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "managed")
    handle = manager.provision(uuid.uuid4(), uuid.uuid4(), 0, "base")
    manager.start_iteration(handle)
    (Path(handle.path) / "src").mkdir()
    manager.root(handle).write_atomic("src/a.py", b"ok")
    git = _ConflictOnceAtExpected()
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


def test_pending_commit_is_not_reused_after_workspace_content_changes(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "managed")
    run_id, slice_id = uuid.uuid4(), uuid.uuid4()
    handle = manager.provision(run_id, slice_id, 0, "base")
    manager.start_iteration(handle)
    (Path(handle.path) / "src").mkdir()
    root = manager.root(handle)
    root.write_atomic("src/a.py", b"one")
    git = _ConflictOnceAtExpected()
    service = CheckpointService(manager=manager, git=git)
    scope = WriteScope(out=WriteScopeOut(write_paths=[], create_roots=["src"]))
    diff = DirectoryDiffProvider(handle.path, base_files={})

    first = service.checkpoint(handle, scope, diff, "base")
    assert isinstance(first, CheckpointRejection)
    root.write_atomic("src/b.py", b"two")
    second = service.checkpoint(handle, scope, diff, "base")

    assert isinstance(second, CheckpointReceipt)
    assert second.manifest.file_count == 2
    assert git.create_calls == 2


def test_symlink_scope_requires_both_link_and_effective_target_in_scope(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "managed")
    handle = manager.provision(uuid.uuid4(), uuid.uuid4(), 0, "base")
    manager.start_iteration(handle)
    root = manager.root(handle)
    (Path(handle.path) / "evil").mkdir()
    root.write_atomic("src.py", b"source")
    (Path(handle.path) / "evil" / "link").symlink_to("../src.py")
    service = CheckpointService(manager=manager, git=InMemoryCandidateRefStore(expected_oid="base"))

    result = service.checkpoint(
        handle,
        WriteScope(out=WriteScopeOut(write_paths=["src.py"], create_roots=[])),
        DirectoryDiffProvider(handle.path, base_files={}),
        "base",
    )

    assert isinstance(result, CheckpointRejection)
    assert result.code is StableErrorCode.WRITE_SCOPE_VIOLATION
    assert "evil/link" in result.out_of_scope_paths


def test_checkpoint_intent_and_receipt_survive_service_restart(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    run_id, slice_id = uuid.uuid4(), uuid.uuid4()
    manager = WorkspaceManager(managed)
    handle = manager.provision(run_id, slice_id, 0, "base")
    manager.start_iteration(handle)
    (Path(handle.path) / "src").mkdir()
    manager.root(handle).write_atomic("src/a.py", b"one")
    git = _ConflictOnceAtExpected()
    diff = DirectoryDiffProvider(handle.path, base_files={})
    scope = WriteScope(out=WriteScopeOut(write_paths=["src/a.py"], create_roots=[]))

    first = CheckpointService(manager=manager, git=git).checkpoint(handle, scope, diff, "base")
    assert isinstance(first, CheckpointRejection)
    assert git.create_calls == 1

    restarted_manager = WorkspaceManager(managed)
    restarted = restarted_manager.recover(run_id, slice_id, 0, "base")
    second = CheckpointService(manager=restarted_manager, git=git).checkpoint(
        restarted,
        scope,
        DirectoryDiffProvider(restarted.path, base_files={}),
        "base",
    )
    assert isinstance(second, CheckpointReceipt)
    assert git.create_calls == 1

    third_manager = WorkspaceManager(managed)
    third = third_manager.recover(run_id, slice_id, 0, "base")
    cached = CheckpointService(manager=third_manager, git=git).checkpoint(
        third,
        scope,
        DirectoryDiffProvider(third.path, base_files={}),
        "base",
    )
    assert cached == second


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

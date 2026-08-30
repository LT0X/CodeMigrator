from __future__ import annotations

import uuid
from pathlib import Path

from codemigrator.workspace import (
    InMemorySandboxVolume,
    WorkspaceManager,
)


def test_each_generation_has_an_independent_workspace_and_volume(tmp_path: Path) -> None:
    volume = InMemorySandboxVolume()
    manager = WorkspaceManager(tmp_path / "managed", volume=volume)
    run_id, slice_id = uuid.uuid4(), uuid.uuid4()

    first = manager.provision(run_id, slice_id, 0, "verified-1")
    second = manager.provision(run_id, slice_id, 1, "verified-1", checkpoint_files={"a.py": b"a"})

    assert first.path != second.path
    assert manager.root(second).read_bytes("a.py") == b"a"
    assert volume.created == (first.path, second.path)
    manager.close(first)
    assert not Path(first.path).exists()
    assert first.path in volume.destroyed
    manager.close(first)


def test_iteration_records_structured_writes_without_content(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "managed")
    handle = manager.provision(uuid.uuid4(), uuid.uuid4(), 0, "verified-1")
    manager.start_iteration(handle)
    operation = manager.record_write(handle, "WriteFile", "src/a.py", 3, "CREATED")

    assert operation.path == "src/a.py"
    assert operation.bytes_written == 3
    assert "content" not in operation.model_dump()
    assert manager.operations(handle) == (operation,)


def test_crash_rebuild_keeps_generation_and_uses_latest_checkpoint(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "managed")
    handle = manager.provision(uuid.uuid4(), uuid.uuid4(), 0, "verified-1")
    manager.start_iteration(handle)
    (Path(handle.path) / "src").mkdir()
    manager.root(handle).write_atomic("src/a.py", b"checkpoint")
    manager.freeze(handle)

    rebuilt = manager.rebuild(handle, checkpoint_files={"src/a.py": b"checkpoint"})
    assert rebuilt.generation == handle.generation
    assert manager.root(rebuilt).read_bytes("src/a.py") == b"checkpoint"


def test_restart_recovery_restores_handle_and_operation_ledger(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    run_id, slice_id = uuid.uuid4(), uuid.uuid4()
    manager = WorkspaceManager(managed)
    handle = manager.provision(run_id, slice_id, 0, "verified-1")
    manager.start_iteration(handle)
    operation = manager.record_write(handle, "WriteFile", "src/a.py", 3, "CREATED")

    restarted = WorkspaceManager(managed)
    recovered = restarted.recover(run_id, slice_id, 0, "verified-1")

    assert recovered.state.value == "ITERATING"
    assert restarted.operations(recovered) == (operation,)

"""Candidate workspace and long-lived sandbox-volume lifecycle."""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from codemigrator.core import CandidateGeneration, RunId, SliceId, validate_candidate_generation
from codemigrator.core._base import CoreModel

from .models import WorkspaceFileOperation, WorkspaceHandle, WorkspaceState
from .paths import SecureRoot, validate_relative_path


class WorkspaceStateError(RuntimeError):
    """A lifecycle operation was attempted in the wrong state."""


class SandboxVolumePort(Protocol):
    def create(self, path: str) -> None: ...

    def quiesce(self, path: str) -> None: ...

    def destroy(self, path: str) -> None: ...


class WorkspaceStateRecord(CoreModel):
    """Durable lifecycle facts kept outside the sandbox-visible workspace."""

    handle: WorkspaceHandle
    operations: tuple[WorkspaceFileOperation, ...] = ()


class WorkspaceStateStore(Protocol):
    def load(self, workspace_path: str) -> WorkspaceStateRecord | None: ...

    def save(self, record: WorkspaceStateRecord) -> None: ...

    def delete(self, workspace_path: str) -> None: ...


class JsonWorkspaceStateStore:
    """Small atomic file store used by the deterministic lifecycle adapter."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, workspace_path: str) -> Path:
        digest = hashlib.sha256(workspace_path.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.json"

    def load(self, workspace_path: str) -> WorkspaceStateRecord | None:
        path = self._path(workspace_path)
        try:
            return WorkspaceStateRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise WorkspaceStateError("workspace state is corrupted") from exc

    def save(self, record: WorkspaceStateRecord) -> None:
        destination = self._path(record.handle.path)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(record.model_dump_json(), encoding="utf-8")
            temporary.replace(destination)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def delete(self, workspace_path: str) -> None:
        try:
            self._path(workspace_path).unlink()
        except FileNotFoundError:
            pass


class InMemorySandboxVolume:
    """A deterministic volume adapter used by lifecycle and recovery tests."""

    def __init__(self) -> None:
        self._created: list[str] = []
        self._destroyed: list[str] = []
        self._quiesced: list[str] = []

    @property
    def created(self) -> tuple[str, ...]:
        return tuple(self._created)

    @property
    def destroyed(self) -> tuple[str, ...]:
        return tuple(self._destroyed)

    @property
    def quiesced(self) -> tuple[str, ...]:
        return tuple(self._quiesced)

    def create(self, path: str) -> None:
        self._created.append(path)

    def quiesce(self, path: str) -> None:
        self._quiesced.append(path)

    def destroy(self, path: str) -> None:
        self._destroyed.append(path)


class WorkspaceManager:
    """Own one filesystem root and one volume for every Slice generation."""

    def __init__(
        self,
        managed_root: Path | str,
        *,
        volume: SandboxVolumePort | None = None,
        state_store: WorkspaceStateStore | None = None,
    ) -> None:
        self.managed_root = Path(managed_root)
        self.managed_root.mkdir(parents=True, exist_ok=True)
        self.volume = volume or InMemorySandboxVolume()
        self.state_store = state_store or JsonWorkspaceStateStore(self.managed_root / ".state")
        self._roots: dict[str, SecureRoot] = {}
        self._handles: dict[str, WorkspaceHandle] = {}
        self._operations: dict[str, list[WorkspaceFileOperation]] = {}

    def provision(
        self,
        run_id: uuid.UUID,
        slice_id: uuid.UUID,
        generation: int,
        base_verified_oid: str,
        checkpoint_files: Mapping[str, bytes] | None = None,
    ) -> WorkspaceHandle:
        generation = validate_candidate_generation(generation)
        path = self.managed_root / str(run_id) / str(slice_id) / str(generation)
        if path.exists():
            raise WorkspaceStateError("workspace already exists for this generation")
        path.mkdir(parents=True)
        for relative, content in (checkpoint_files or {}).items():
            safe = validate_relative_path(relative)
            target = path / safe
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(bytes(content))
        root = SecureRoot("workspace", path)
        handle = WorkspaceHandle(
            run_id=RunId(run_id),
            slice_id=SliceId(slice_id),
            generation=CandidateGeneration(generation),
            path=str(path),
            state=WorkspaceState.Provisioned,
            base_verified_oid=base_verified_oid,
        )
        self._roots[handle.path] = root
        self._handles[handle.path] = handle
        self._operations[handle.path] = []
        try:
            self.volume.create(handle.path)
            self._persist(handle)
        except BaseException:
            try:
                self.volume.destroy(handle.path)
            except BaseException:
                pass
            root.close()
            shutil.rmtree(path)
            del self._roots[handle.path], self._handles[handle.path], self._operations[handle.path]
            self.state_store.delete(handle.path)
            raise
        return handle

    def recover(
        self,
        run_id: uuid.UUID,
        slice_id: uuid.UUID,
        generation: int,
        base_verified_oid: str,
    ) -> WorkspaceHandle:
        """Reattach a surviving workspace and its durable ledger after a restart."""

        generation = validate_candidate_generation(generation)
        path = self.managed_root / str(run_id) / str(slice_id) / str(generation)
        if str(path) in self._handles:
            return self._handles[str(path)]
        if not path.is_dir():
            raise WorkspaceStateError("workspace does not exist for recovery")
        record = self.state_store.load(str(path))
        if record is None:
            handle = WorkspaceHandle(
                run_id=RunId(run_id),
                slice_id=SliceId(slice_id),
                generation=CandidateGeneration(generation),
                path=str(path),
                state=WorkspaceState.Iterating,
                base_verified_oid=base_verified_oid,
            )
            operations: list[WorkspaceFileOperation] = []
        else:
            handle = record.handle
            if (
                handle.path != str(path)
                or handle.run_id != RunId(run_id)
                or handle.slice_id != SliceId(slice_id)
                or handle.generation != generation
                or handle.base_verified_oid != base_verified_oid
            ):
                raise WorkspaceStateError("workspace recovery identity does not match")
            handle = handle.model_copy(update={"state": WorkspaceState.Iterating})
            operations = list(record.operations)
        root = SecureRoot("workspace", path)
        self._roots[handle.path] = root
        self._handles[handle.path] = handle
        self._operations[handle.path] = operations
        self._persist(handle)
        return handle

    def start_iteration(self, handle: WorkspaceHandle) -> WorkspaceHandle:
        self._require(handle, WorkspaceState.Provisioned)
        return self._set_state(handle, WorkspaceState.Iterating)

    def begin_checkpoint(self, handle: WorkspaceHandle) -> WorkspaceHandle:
        self._require(handle, WorkspaceState.Iterating)
        return self._set_state(handle, WorkspaceState.Checkpointing)

    def abort_checkpoint(self, handle: WorkspaceHandle) -> WorkspaceHandle:
        self._require(handle, WorkspaceState.Checkpointing)
        return self._set_state(handle, WorkspaceState.Iterating)

    def freeze(self, handle: WorkspaceHandle) -> WorkspaceHandle:
        self._require(handle, (WorkspaceState.Iterating, WorkspaceState.Checkpointing))
        return self._set_state(handle, WorkspaceState.Frozen)

    def root(self, handle: WorkspaceHandle) -> SecureRoot:
        try:
            return self._roots[handle.path]
        except KeyError as exc:
            raise WorkspaceStateError("workspace is not managed") from exc

    def operations(self, handle: WorkspaceHandle) -> tuple[WorkspaceFileOperation, ...]:
        self._require_known(handle)
        return tuple(self._operations[handle.path])

    def record_write(
        self,
        handle: WorkspaceHandle,
        tool: str,
        path: str,
        bytes_written: int,
        disposition: str,
    ) -> WorkspaceFileOperation:
        self._require(handle, WorkspaceState.Iterating)
        if tool not in {"WriteFile", "EditFile"}:
            raise ValueError("only structured write tools belong in the file ledger")
        operation = WorkspaceFileOperation(
            run_id=handle.run_id,
            slice_id=handle.slice_id,
            generation=handle.generation,
            tool=tool,  # type: ignore[arg-type]
            path=validate_relative_path(path),
            bytes_written=bytes_written,
            disposition=disposition,  # type: ignore[arg-type]
        )
        self._operations[handle.path].append(operation)
        self._persist(handle)
        return operation

    def record_operation(self, operation: WorkspaceFileOperation) -> None:
        """Accept a successful gateway operation into this workspace's ledger."""

        matches = [
            handle
            for handle in self._handles.values()
            if handle.run_id == operation.run_id
            and handle.slice_id == operation.slice_id
            and handle.generation == operation.generation
        ]
        if len(matches) != 1:
            raise WorkspaceStateError("operation does not identify one managed workspace")
        handle = matches[0]
        self._require(handle, WorkspaceState.Iterating)
        self._operations[handle.path].append(operation)
        self._persist(handle)

    def rebuild(
        self, handle: WorkspaceHandle, *, checkpoint_files: Mapping[str, bytes] | None = None
    ) -> WorkspaceHandle:
        self._require_known(handle)
        run_id, slice_id, generation, base_oid = (
            handle.run_id,
            handle.slice_id,
            handle.generation,
            handle.base_verified_oid,
        )
        self.close(handle)
        return self.provision(run_id, slice_id, generation, base_oid, checkpoint_files)

    def close(self, handle: WorkspaceHandle) -> None:
        if handle.path not in self._handles:
            if self.state_store.load(handle.path) is None and not Path(handle.path).exists():
                return
            raise WorkspaceStateError("workspace is not managed")
        self.volume.quiesce(handle.path)
        root = self._roots[handle.path]
        root.close()
        self.volume.destroy(handle.path)
        if Path(handle.path).exists():
            shutil.rmtree(handle.path, ignore_errors=False)
        self.state_store.delete(handle.path)
        del self._handles[handle.path], self._operations[handle.path]
        del self._roots[handle.path]

    def _require_known(self, handle: WorkspaceHandle) -> None:
        if handle.path not in self._handles:
            raise WorkspaceStateError("workspace is not managed")

    def _require(
        self, handle: WorkspaceHandle, expected: WorkspaceState | tuple[WorkspaceState, ...]
    ) -> None:
        self._require_known(handle)
        allowed = expected if isinstance(expected, tuple) else (expected,)
        if self._handles[handle.path].state not in allowed:
            raise WorkspaceStateError(
                f"workspace state {self._handles[handle.path].state.value} is not allowed"
            )

    def _set_state(self, handle: WorkspaceHandle, state: WorkspaceState) -> WorkspaceHandle:
        updated = self._handles[handle.path].model_copy(update={"state": state})
        self._persist(updated)
        self._handles[handle.path] = updated
        return updated

    def _persist(self, handle: WorkspaceHandle) -> None:
        self.state_store.save(
            WorkspaceStateRecord(
                handle=handle,
                operations=tuple(self._operations.get(handle.path, ())),
            )
        )


__all__ = [
    "InMemorySandboxVolume",
    "JsonWorkspaceStateStore",
    "SandboxVolumePort",
    "WorkspaceStateRecord",
    "WorkspaceStateStore",
    "WorkspaceManager",
    "WorkspaceStateError",
]

"""Candidate workspace and long-lived sandbox-volume lifecycle."""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from codemigrator.core import CandidateGeneration, RunId, SliceId, validate_candidate_generation

from .models import WorkspaceFileOperation, WorkspaceHandle, WorkspaceState
from .paths import SecureRoot, validate_relative_path


class WorkspaceStateError(RuntimeError):
    """A lifecycle operation was attempted in the wrong state."""


class SandboxVolumePort(Protocol):
    def create(self, path: str) -> None: ...

    def quiesce(self, path: str) -> None: ...

    def destroy(self, path: str) -> None: ...


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
        self, managed_root: Path | str, *, volume: SandboxVolumePort | None = None
    ) -> None:
        self.managed_root = Path(managed_root)
        self.managed_root.mkdir(parents=True, exist_ok=True)
        self.volume = volume or InMemorySandboxVolume()
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
        except BaseException:
            root.close()
            shutil.rmtree(path)
            del self._roots[handle.path], self._handles[handle.path], self._operations[handle.path]
            raise
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
        self._require_known(handle)
        self.volume.quiesce(handle.path)
        root = self._roots.pop(handle.path)
        root.close()
        self.volume.destroy(handle.path)
        shutil.rmtree(handle.path, ignore_errors=False)
        del self._handles[handle.path], self._operations[handle.path]

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
        self._handles[handle.path] = updated
        return updated


__all__ = [
    "InMemorySandboxVolume",
    "SandboxVolumePort",
    "WorkspaceManager",
    "WorkspaceStateError",
]

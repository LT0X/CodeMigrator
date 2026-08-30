"""Checkpoint scope validation and candidate-ref handoff ports."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

from pydantic import ConfigDict, Field

from codemigrator.core import (
    CandidateGeneration,
    GitOid,
    RunId,
    SliceCandidate,
    SliceId,
    StableErrorCode,
    WriteScope,
    canonical_json_bytes,
)
from codemigrator.core._base import CoreModel

from .lifecycle import WorkspaceManager, WorkspaceStateError
from .models import AuditEvent, WorkspaceFileOperation, WorkspaceHandle
from .paths import sha256_bytes, validate_relative_path


class WorkspaceFileFact(CoreModel):
    model_config = ConfigDict(frozen=True)

    path: str
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    symlink_target: str | None = None


class WorkspaceChange(CoreModel):
    model_config = ConfigDict(frozen=True)

    path: str
    kind: Literal["ADDED", "MODIFIED", "DELETED", "SYMLINK"]
    size: int = Field(default=0, ge=0)
    sha256: str = Field(default="0" * 64, pattern=r"^[0-9a-f]{64}$")
    symlink_target: str | None = None
    is_new: bool = False


class WorkspaceDiff(CoreModel):
    model_config = ConfigDict(frozen=True)

    changes: tuple[WorkspaceChange, ...]
    files: tuple[WorkspaceFileFact, ...]


class WorkspaceDiffPort(Protocol):
    def diff(self, base_oid: str, workspace_root: str) -> WorkspaceDiff: ...


class DirectoryDiffProvider:
    """Host-side diff provider over a workspace with an immutable base map."""

    def __init__(self, workspace_root: Path | str, *, base_files: Mapping[str, bytes]) -> None:
        self.workspace_root = Path(workspace_root)
        self.base_files = {
            validate_relative_path(path): bytes(value) for path, value in base_files.items()
        }

    def diff(self, base_oid: str, workspace_root: str) -> WorkspaceDiff:
        root = Path(workspace_root)
        current: dict[str, WorkspaceFileFact] = {}
        for filesystem_path in sorted(
            root.rglob("*"), key=lambda item: str(item.relative_to(root)).encode("utf-8")
        ):
            relative = str(filesystem_path.relative_to(root)).replace(os.sep, "/")
            if relative == ".git" or relative.startswith(".git/"):
                continue
            if filesystem_path.is_symlink():
                target = os.readlink(filesystem_path)
                encoded = target.encode("utf-8")
                current[relative] = WorkspaceFileFact(
                    path=relative,
                    size=len(encoded),
                    sha256=sha256_bytes(encoded),
                    symlink_target=target,
                )
            elif filesystem_path.is_file():
                data = filesystem_path.read_bytes()
                current[relative] = WorkspaceFileFact(
                    path=relative,
                    size=len(data),
                    sha256=sha256_bytes(data),
                )
        changes: list[WorkspaceChange] = []
        for path, fact in current.items():
            old = self.base_files.get(path)
            if fact.symlink_target is not None:
                changes.append(
                    WorkspaceChange(
                        path=path,
                        kind="SYMLINK",
                        size=fact.size,
                        sha256=fact.sha256,
                        symlink_target=fact.symlink_target,
                        is_new=path not in self.base_files,
                    )
                )
            elif old is None:
                changes.append(
                    WorkspaceChange(path=path, kind="ADDED", size=fact.size, sha256=fact.sha256)
                )
            elif sha256_bytes(old) != fact.sha256:
                changes.append(
                    WorkspaceChange(path=path, kind="MODIFIED", size=fact.size, sha256=fact.sha256)
                )
        for path, old in self.base_files.items():
            if path not in current:
                changes.append(
                    WorkspaceChange(path=path, kind="DELETED", size=0, sha256=sha256_bytes(old))
                )
        return WorkspaceDiff(
            changes=tuple(sorted(changes, key=lambda item: item.path.encode("utf-8"))),
            files=tuple(
                current[path] for path in sorted(current, key=lambda item: item.encode("utf-8"))
            ),
        )


class CandidateRefConflict(RuntimeError):
    """The candidate ref moved and may not be force-updated."""


class CandidateRefPort(Protocol):
    def create_checkpoint(
        self,
        handle: WorkspaceHandle,
        manifest: CheckpointManifest,
        files: Sequence[WorkspaceFileFact],
    ) -> GitOid: ...

    def advance_candidate_ref(
        self, handle: WorkspaceHandle, expected_oid: GitOid, new_oid: GitOid
    ) -> None: ...

    def current_candidate_oid(self, handle: WorkspaceHandle) -> GitOid: ...


class InMemoryCandidateRefStore:
    def __init__(self, *, expected_oid: str) -> None:
        self.ref = expected_oid
        self.create_calls = 0
        self.force_updates = 0
        self._counter = 0

    def create_checkpoint(
        self,
        handle: WorkspaceHandle,
        manifest: CheckpointManifest,
        files: Sequence[WorkspaceFileFact],
    ) -> GitOid:
        self.create_calls += 1
        self._counter += 1
        return GitOid(f"candidate-{self._counter}")

    def current_candidate_oid(self, handle: WorkspaceHandle) -> GitOid:
        return GitOid(self.ref)

    def advance_candidate_ref(
        self, handle: WorkspaceHandle, expected_oid: GitOid, new_oid: GitOid
    ) -> None:
        if self.ref == str(new_oid):
            return
        if self.ref != str(expected_oid):
            raise CandidateRefConflict("candidate ref moved")
        self.ref = str(new_oid)


class CheckpointManifest(CoreModel):
    model_config = ConfigDict(frozen=True)

    slice_candidate: SliceCandidate
    file_count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    file_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_check_passed: bool


class CheckpointReceipt(CoreModel):
    model_config = ConfigDict(frozen=True)

    run_id: RunId
    slice_id: SliceId
    generation: int = Field(ge=0, le=2)
    expected_candidate_oid: str
    new_candidate_oid: str
    manifest: CheckpointManifest
    idempotency_key: str = Field(pattern=r"^[0-9a-f]{64}$")


class CheckpointIntent(CoreModel):
    """Durable commit intent used to finish a checkpoint after a restart."""

    workspace_path: str
    expected_candidate_oid: str
    manifest: CheckpointManifest
    idempotency_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    new_candidate_oid: str | None = None


class CheckpointStore(Protocol):
    def get_receipt(self, key: str) -> CheckpointReceipt | None: ...

    def get_intent(self, key: str) -> CheckpointIntent | None: ...

    def save_intent(self, key: str, intent: CheckpointIntent) -> None: ...

    def save_receipt(self, key: str, receipt: CheckpointReceipt) -> None: ...

    def delete_intent(self, key: str) -> None: ...


class JsonCheckpointStore:
    """Atomic JSON adapter; production composition may replace it with PostgreSQL."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str, suffix: str) -> Path:
        return self.root / f"{key}.{suffix}.json"

    def _read(self, path: Path, model: type[CoreModel]) -> CoreModel | None:
        try:
            return model.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError("checkpoint state is corrupted") from exc

    def _write(self, path: Path, value: CoreModel) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(value.model_dump_json(), encoding="utf-8")
            temporary.replace(path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def get_receipt(self, key: str) -> CheckpointReceipt | None:
        value = self._read(self._path(key, "receipt"), CheckpointReceipt)
        return value if isinstance(value, CheckpointReceipt) else None

    def get_intent(self, key: str) -> CheckpointIntent | None:
        value = self._read(self._path(key, "intent"), CheckpointIntent)
        return value if isinstance(value, CheckpointIntent) else None

    def save_intent(self, key: str, intent: CheckpointIntent) -> None:
        self._write(self._path(key, "intent"), intent)

    def save_receipt(self, key: str, receipt: CheckpointReceipt) -> None:
        self._write(self._path(key, "receipt"), receipt)

    def delete_intent(self, key: str) -> None:
        try:
            self._path(key, "intent").unlink()
        except FileNotFoundError:
            pass


class CheckpointRejection(CoreModel):
    model_config = ConfigDict(frozen=True)

    accepted: Literal[False] = False
    code: StableErrorCode
    message: str
    out_of_scope_paths: tuple[str, ...] = ()
    commit_created: bool = False
    ref_advanced: bool = False
    generation_consumed: bool = False
    infrastructure_failure: bool = False


class CheckpointService:
    def __init__(
        self,
        *,
        manager: WorkspaceManager,
        git: CandidateRefPort,
        audit_sink: Callable[[AuditEvent], None] | None = None,
        store: CheckpointStore | None = None,
    ) -> None:
        self.manager = manager
        self.git = git
        self.audit_sink = audit_sink
        self.store = store or JsonCheckpointStore(manager.managed_root / ".checkpoint-state")
        self._receipts: dict[tuple[str, str, str], CheckpointReceipt] = {}
        self._pending: dict[tuple[str, str, str], tuple[GitOid, CheckpointManifest]] = {}

    def checkpoint(
        self,
        handle: WorkspaceHandle,
        scope: WriteScope,
        diff_provider: WorkspaceDiffPort,
        expected_candidate_oid: str,
        *,
        build_excludes: Sequence[str] = (),
    ) -> CheckpointReceipt | CheckpointRejection:
        key = (handle.path, expected_candidate_oid, handle.base_verified_oid)
        store_key = self._store_key(handle, expected_candidate_oid)
        cached = self._receipts.get(key) or self.store.get_receipt(store_key)
        if cached is not None:
            self._receipts[key] = cached
            return cached
        pending = self._pending.get(key)
        if pending is None:
            intent = self.store.get_intent(store_key)
            if intent is not None and intent.new_candidate_oid is not None:
                pending = (GitOid(intent.new_candidate_oid), intent.manifest)
                self._pending[key] = pending
        commit_created = pending is not None
        ref_advanced = False
        audit_emitted = False
        try:
            current = self.manager.begin_checkpoint(handle)
        except WorkspaceStateError:
            return CheckpointRejection(
                code=StableErrorCode.CHECKPOINT_WRITE_FAILED,
                message="workspace is not ready for checkpoint",
                infrastructure_failure=True,
            )
        try:
            self.manager.volume.quiesce(current.path)
            diff = diff_provider.diff(current.base_verified_oid, current.path)
            excluded = tuple(validate_relative_path(item) for item in build_excludes)
            changes = tuple(
                change for change in diff.changes if not self._excluded(change.path, excluded)
            )
            operations = self.manager.operations(current)
            structured_violation = tuple(
                operation.path
                for operation in operations
                if not self._operation_allowed(operation, scope)
            )
            violations = tuple(
                sorted(
                    {
                        path
                        for change in changes
                        if not self._change_allowed(change, scope)
                        for path in self._violation_paths(change)
                    },
                    key=lambda item: item.encode("utf-8"),
                )
            )
            files = tuple(
                file for file in diff.files if not self._excluded(file.path, excluded)
            )
            self._emit_checkpoint(
                current,
                passed=not violations and not structured_violation,
                changed_paths=tuple(
                    sorted(
                        set(violations).union(structured_violation),
                        key=lambda item: item.encode("utf-8"),
                    )
                ),
                file_count=len(files),
                total_bytes=sum(file.size for file in files),
            )
            audit_emitted = True
            if structured_violation:
                self.manager.abort_checkpoint(current)
                self.manager.close(current)
                return CheckpointRejection(
                    code=StableErrorCode.CHECKPOINT_WRITE_FAILED,
                    message="structured write ledger crossed its frozen scope",
                    infrastructure_failure=True,
                )
            if violations:
                self.manager.abort_checkpoint(current)
                return CheckpointRejection(
                    code=StableErrorCode.WRITE_SCOPE_VIOLATION,
                    message="workspace diff is outside the frozen write scope",
                    out_of_scope_paths=violations,
                )
            file_map = {
                file.path: {
                    "sha256": file.sha256,
                    "size": file.size,
                    "symlink_target": file.symlink_target,
                }
                for file in files
            }
            file_set_digest = hashlib.sha256(canonical_json_bytes(file_map)).hexdigest()
            manifest = CheckpointManifest(
                slice_candidate=SliceCandidate(
                    run_id=current.run_id,
                    slice_id=current.slice_id,
                    generation=CandidateGeneration(current.generation),
                    base_verified_oid=GitOid(current.base_verified_oid),
                    candidate_commit_oid=GitOid(expected_candidate_oid),
                ),
                file_count=len(files),
                total_bytes=sum(file.size for file in files),
                file_set_digest=file_set_digest,
                scope_check_passed=True,
            )
            if pending is None or pending[1].file_set_digest != manifest.file_set_digest:
                self._pending.pop(key, None)
                self.store.delete_intent(store_key)
                idempotency_key = self._idempotency_key(current, expected_candidate_oid, manifest)
                self.store.save_intent(
                    store_key,
                    CheckpointIntent(
                        workspace_path=current.path,
                        expected_candidate_oid=expected_candidate_oid,
                        manifest=manifest,
                        idempotency_key=idempotency_key,
                    ),
                )
                new_oid = self.git.create_checkpoint(current, manifest, files)
                commit_created = True
                pending = (new_oid, manifest)
                self._pending[key] = pending
                self.store.save_intent(
                    store_key,
                    CheckpointIntent(
                        workspace_path=current.path,
                        expected_candidate_oid=expected_candidate_oid,
                        manifest=manifest,
                        idempotency_key=idempotency_key,
                        new_candidate_oid=str(new_oid),
                    ),
                )
            else:
                new_oid = pending[0]
                idempotency_key = self._idempotency_key(current, expected_candidate_oid, manifest)
            try:
                self.git.advance_candidate_ref(current, GitOid(expected_candidate_oid), new_oid)
            except CandidateRefConflict:
                actual = self._current_candidate_oid(current)
                if actual == str(new_oid):
                    ref_advanced = True
                elif actual == expected_candidate_oid:
                    self.manager.abort_checkpoint(current)
                    return CheckpointRejection(
                        code=StableErrorCode.CANDIDATE_REF_CONFLICT,
                        message="candidate ref was not advanced; retry is safe",
                        commit_created=commit_created,
                    )
                else:
                    self._pending.pop(key, None)
                    self.store.delete_intent(store_key)
                    self.manager.freeze(current)
                    return CheckpointRejection(
                        code=StableErrorCode.CANDIDATE_REF_CONFLICT,
                        message="candidate ref forked; generation is frozen",
                        commit_created=commit_created,
                        generation_consumed=True,
                        infrastructure_failure=True,
                    )
            ref_advanced = True
            receipt = CheckpointReceipt(
                run_id=current.run_id,
                slice_id=current.slice_id,
                generation=current.generation,
                expected_candidate_oid=expected_candidate_oid,
                new_candidate_oid=str(new_oid),
                manifest=manifest,
                idempotency_key=idempotency_key,
            )
            self.manager.freeze(current)
            self.store.save_receipt(store_key, receipt)
            self.store.delete_intent(store_key)
            self._receipts[key] = receipt
            self._pending.pop(key, None)
            return receipt
        except Exception:  # noqa: BLE001 - ports are converted to a stable infrastructure result
            if not audit_emitted:
                self._emit_checkpoint(current, passed=False)
            try:
                self.manager.abort_checkpoint(current)
            except WorkspaceStateError:
                pass
            return CheckpointRejection(
                code=StableErrorCode.CHECKPOINT_WRITE_FAILED,
                message="checkpoint failed without exposing host details",
                commit_created=commit_created,
                ref_advanced=ref_advanced,
                infrastructure_failure=True,
            )

    @staticmethod
    def _store_key(handle: WorkspaceHandle, expected_candidate_oid: str) -> str:
        return hashlib.sha256(
            canonical_json_bytes(
                {
                    "run_id": str(handle.run_id),
                    "slice_id": str(handle.slice_id),
                    "generation": handle.generation,
                    "base_verified_oid": handle.base_verified_oid,
                    "expected_candidate_oid": expected_candidate_oid,
                }
            )
        ).hexdigest()

    @staticmethod
    def _idempotency_key(
        handle: WorkspaceHandle, expected_candidate_oid: str, manifest: CheckpointManifest
    ) -> str:
        return hashlib.sha256(
            canonical_json_bytes(
                {
                    "run_id": str(handle.run_id),
                    "slice_id": str(handle.slice_id),
                    "generation": handle.generation,
                    "expected_candidate_oid": expected_candidate_oid,
                    "file_set_digest": manifest.file_set_digest,
                }
            )
        ).hexdigest()

    @staticmethod
    def _excluded(path: str, excludes: Sequence[str]) -> bool:
        return any(path == item or path.startswith(item.rstrip("/") + "/") for item in excludes)

    @classmethod
    def _change_allowed(cls, change: WorkspaceChange, scope: WriteScope) -> bool:
        effective = cls._effective_path(change)
        if change.kind == "SYMLINK":
            return cls._scope_contains(
                change.path, scope, is_new=change.is_new
            ) and cls._scope_contains(effective, scope, is_new=change.is_new)
        if change.kind in {"MODIFIED", "DELETED"}:
            return effective in {str(item) for item in scope.out.write_paths}
        return cls._scope_contains(effective, scope, is_new=True)

    @classmethod
    def _violation_paths(cls, change: WorkspaceChange) -> tuple[str, ...]:
        if change.kind == "SYMLINK":
            return (change.path, cls._effective_path(change))
        return (cls._effective_path(change),)

    def _current_candidate_oid(self, handle: WorkspaceHandle) -> str | None:
        try:
            return str(self.git.current_candidate_oid(handle))
        except (AttributeError, OSError, RuntimeError, ValueError):
            return None

    @classmethod
    def _operation_allowed(cls, operation: WorkspaceFileOperation, scope: WriteScope) -> bool:
        if operation.disposition == "OVERWRITTEN":
            return operation.path in {str(item) for item in scope.out.write_paths}
        return cls._scope_contains(operation.path, scope, is_new=True)

    @staticmethod
    def _scope_contains(path: str, scope: WriteScope, *, is_new: bool) -> bool:
        if path in {str(item) for item in scope.out.write_paths}:
            return True
        if not is_new:
            return False
        return any(
            path == str(root).rstrip("/") or path.startswith(str(root).rstrip("/") + "/")
            for root in scope.out.create_roots
        )

    @staticmethod
    def _effective_path(change: WorkspaceChange) -> str:
        if change.symlink_target is None:
            return change.path
        target = PurePosixPath(change.symlink_target)
        if target.is_absolute():
            return "/"
        combined = PurePosixPath(change.path).parent / target
        parts: list[str] = []
        for part in combined.parts:
            if part in {"", "."}:
                continue
            if part == "..":
                if not parts:
                    return "/"
                parts.pop()
            else:
                parts.append(part)
        return "/".join(parts)

    def _emit_checkpoint(
        self,
        handle: WorkspaceHandle,
        *,
        passed: bool,
        changed_paths: tuple[str, ...] = (),
        file_count: int | None = None,
        total_bytes: int | None = None,
    ) -> None:
        if self.audit_sink is not None:
            self.audit_sink(
                AuditEvent(
                    point="checkpoint.pre",
                    run_id=handle.run_id,
                    slice_id=handle.slice_id,
                    generation=handle.generation,
                    phase=None,
                    tool=None,
                    outcome="PASSED" if passed else "REJECTED",
                    file_count=file_count,
                    total_bytes=total_bytes,
                    scope_check_passed=passed,
                    changed_paths=changed_paths,
                )
            )


__all__ = [
    "CandidateRefConflict",
    "CandidateRefPort",
    "CheckpointManifest",
    "CheckpointReceipt",
    "CheckpointRejection",
    "CheckpointService",
    "DirectoryDiffProvider",
    "InMemoryCandidateRefStore",
    "WorkspaceChange",
    "WorkspaceDiff",
    "WorkspaceDiffPort",
    "WorkspaceFileFact",
]

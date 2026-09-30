"""Event-triggered recovery and cursor checkpoint integrity."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from codemigrator.core import (
    GitOid,
    Phase,
    SessionKind,
    SliceGenerationRef,
    WriteScope,
    canonical_json_bytes,
)
from codemigrator.workspace import (
    CheckpointReceipt,
    WorkspaceHandle,
    WorkspaceManager,
    checkpoint_receipt_digest,
)

from .agent_runs import AgentRun, AgentRunId
from .contracts import RuntimeEvent
from .loop_contracts import SessionState


class RecoveryTrigger(str, Enum):
    Startup = "startup"
    Interruption = "interruption"
    IntentGap = "intent_gap"


class CandidateCheckpointVerifier(Protocol):
    def is_committed_receipt(self, receipt: CheckpointReceipt) -> bool: ...


class CandidateSnapshotPort(Protocol):
    def files_at(self, candidate_oid: str) -> Mapping[str, bytes]: ...


class AgentCheckpointReader(Protocol):
    async def verify_checkpoint(self, thread_id: str, digest: str) -> bool: ...


class AgentRunRecoveryStore(Protocol):
    async def create_or_get_agent_run(self, record: AgentRun) -> AgentRun: ...


@dataclass(frozen=True, slots=True)
class WriteSessionRecovery:
    agent_run: AgentRun
    workspace: WorkspaceHandle
    write_scope: WriteScope


class AgentRunRecoveryCoordinator:
    """Recover read-only threads or rebuild write sessions from M-08 facts."""

    _READ_ONLY_KINDS = frozenset(
        {
            SessionKind.AnalyzeAuxiliary,
            SessionKind.PlanAuxiliary,
            SessionKind.ExploreCoordinator,
            SessionKind.ExecuteSupervisor,
        }
    )
    _WRITE_KINDS = frozenset(
        {
            SessionKind.Contract,
            SessionKind.Implementation,
            SessionKind.TestTranslation,
            SessionKind.TestGeneration,
            SessionKind.RepairSession,
        }
    )

    def __init__(
        self,
        *,
        checkpoint_reader: AgentCheckpointReader | None = None,
        candidate_checkpoints: CandidateCheckpointVerifier | None = None,
        candidate_source: CandidateSnapshotPort | None = None,
        workspace_manager: WorkspaceManager | None = None,
        agent_runs: AgentRunRecoveryStore | None = None,
    ) -> None:
        self.checkpoint_reader = checkpoint_reader
        self.candidate_checkpoints = candidate_checkpoints
        self.candidate_source = candidate_source
        self.workspace_manager = workspace_manager
        self.agent_runs = agent_runs

    async def resume_readonly(self, stored: AgentRun, expected: AgentRun) -> AgentRun:
        if (
            stored.agent_run_id != expected.agent_run_id
            or stored.owner_key != expected.owner_key
            or not stored.same_frozen_identity(expected)
            or stored.session_kind not in self._READ_ONLY_KINDS
            or stored.state in {SessionState.Closed, SessionState.Failed, SessionState.Invalidated}
            or stored.checkpoint_sha256 is None
            or self.checkpoint_reader is None
            or not await self.checkpoint_reader.verify_checkpoint(
                stored.thread_id, stored.checkpoint_sha256
            )
        ):
            raise ValueError("read-only AgentRun checkpoint failed recovery validation")
        return stored

    async def restart_write_session(
        self,
        previous: AgentRun,
        receipt: CheckpointReceipt,
        workspace: WorkspaceHandle,
        *,
        write_scope: WriteScope,
        context_sha256: str,
        agent_run_id: AgentRunId,
        thread_id: str,
    ) -> WriteSessionRecovery:
        if (
            previous.owner_kind != "run"
            or previous.phase is not Phase.Execute
            or previous.session_kind not in self._WRITE_KINDS
            or previous.slice_ref is None
            or previous.owner_id != workspace.run_id
            or previous.slice_ref.slice_id != workspace.slice_id
            or previous.slice_ref.generation != workspace.generation
            or previous.write_scope_sha256 != write_scope_digest(write_scope)
            or receipt.run_id != workspace.run_id
            or receipt.slice_id != workspace.slice_id
            or receipt.generation != workspace.generation
            or receipt.expected_candidate_oid != str(previous.slice_ref.baseline_candidate_oid)
            or receipt.manifest.slice_candidate.run_id != workspace.run_id
            or receipt.manifest.slice_candidate.slice_id != workspace.slice_id
            or receipt.manifest.slice_candidate.generation != workspace.generation
            or str(receipt.manifest.slice_candidate.candidate_commit_oid)
            != receipt.expected_candidate_oid
            or not receipt.manifest.scope_check_passed
            or (
                previous.candidate_checkpoint_sha256 is not None
                and previous.candidate_checkpoint_sha256 != checkpoint_receipt_digest(receipt)
            )
            or self.candidate_checkpoints is None
            or not self.candidate_checkpoints.is_committed_receipt(receipt)
            or self.candidate_source is None
            or self.workspace_manager is None
            or self.agent_runs is None
        ):
            raise ValueError("write-session candidate checkpoint failed recovery validation")
        if agent_run_id == previous.agent_run_id or thread_id == previous.thread_id:
            raise ValueError("write-session restart requires a fresh AgentRun and thread")
        files = self.candidate_source.files_at(receipt.new_candidate_oid)
        rebuilt = self.workspace_manager.rebuild_from_candidate(
            workspace,
            candidate_oid=receipt.new_candidate_oid,
            checkpoint_files=files,
        )
        assert previous.slice_ref is not None
        restarted = AgentRun(
            agent_run_id=agent_run_id,
            owner_kind=previous.owner_kind,
            owner_id=previous.owner_id,
            logical_task_key=_restart_task_key(previous),
            phase=previous.phase,
            session_kind=previous.session_kind,
            thread_id=thread_id,
            model_binding_sha256=previous.model_binding_sha256,
            context_sha256=context_sha256,
            toolset_sha256=previous.toolset_sha256,
            template_sha256=previous.template_sha256,
            slice_ref=SliceGenerationRef(
                slice_id=previous.slice_ref.slice_id,
                generation=previous.slice_ref.generation,
                baseline_candidate_oid=GitOid(receipt.new_candidate_oid),
            ),
            write_scope_sha256=previous.write_scope_sha256,
            restarted_from=previous.agent_run_id,
        )
        persisted = await self.agent_runs.create_or_get_agent_run(restarted)
        if (
            persisted.restarted_from != previous.agent_run_id
            or persisted.thread_id == previous.thread_id
            or persisted.checkpoint_sha256 is not None
        ):
            raise ValueError("write-session restart identity was not persisted safely")
        return WriteSessionRecovery(persisted, rebuilt, write_scope)


def write_scope_digest(scope: WriteScope) -> str:
    return hashlib.sha256(
        canonical_json_bytes(scope.model_dump(mode="json", by_alias=True))
    ).hexdigest()


def candidate_checkpoint_digest(receipt: CheckpointReceipt) -> str:
    return checkpoint_receipt_digest(receipt)


def _restart_task_key(previous: AgentRun) -> str:
    suffix = f":restart:{previous.agent_run_id}"
    return f"{previous.logical_task_key[: 256 - len(suffix)]}{suffix}"


def has_committed_owner_receipt(events: Sequence[RuntimeEvent], receipt_key: str) -> bool:
    """Treat only an append-only owner event as recovery evidence for a graph cursor."""

    if not receipt_key:
        return False
    return any(event.data.get("receipt_key") == receipt_key for event in events)


@dataclass(frozen=True, slots=True)
class ActorCheckpoint:
    cursor: str
    receipt_refs: tuple[str, ...]
    candidate_index: int
    checksum: str

    @classmethod
    def create(
        cls, *, cursor: str, receipt_refs: tuple[str, ...], candidate_index: int
    ) -> ActorCheckpoint:
        checksum = _checksum(cursor, receipt_refs, candidate_index)
        return cls(cursor, receipt_refs, candidate_index, checksum)


@dataclass(frozen=True, slots=True)
class CheckpointPolicy:
    task_interval: int = 10
    time_interval_seconds: float = 60.0

    def __post_init__(self) -> None:
        if type(self.task_interval) is not int or self.task_interval < 1:
            raise ValueError("task_interval must be positive")
        if self.time_interval_seconds <= 0:
            raise ValueError("time_interval_seconds must be positive")

    def due(self, *, completed_tasks: int, elapsed_seconds: float) -> bool:
        if type(completed_tasks) is not int or completed_tasks < 0:
            raise ValueError("completed_tasks must be a non-negative integer")
        if elapsed_seconds < 0:
            raise ValueError("elapsed_seconds must be non-negative")
        return (
            completed_tasks > 0 and completed_tasks % self.task_interval == 0
        ) or elapsed_seconds >= self.time_interval_seconds


@dataclass(frozen=True, slots=True)
class CheckpointRestore:
    checkpoint: ActorCheckpoint | None
    rebuild: bool
    completion_evidence: bool
    reason: str


def _checksum(cursor: str, receipt_refs: tuple[str, ...], candidate_index: int) -> str:
    payload = {
        "cursor": cursor,
        "receipt_refs": list(receipt_refs),
        "candidate_index": candidate_index,
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def restore_checkpoint(checkpoint: ActorCheckpoint) -> CheckpointRestore:
    """Accept only an intact cursor; corruption requests a fact-based rebuild."""

    expected = _checksum(checkpoint.cursor, checkpoint.receipt_refs, checkpoint.candidate_index)
    if expected != checkpoint.checksum:
        return CheckpointRestore(None, True, False, "CHECKPOINT_CORRUPT")
    return CheckpointRestore(checkpoint, False, False, "CHECKPOINT_ACCEPTED")


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    trigger: RecoveryTrigger
    events: tuple[str, ...]
    report_halted: bool = False


class RecoveryCoordinator:
    """Build recovery actions only when an explicit event asks for them."""

    periodic_poll = False

    def trigger(
        self,
        trigger: RecoveryTrigger,
        *,
        active_dispatch_ids: tuple[str, ...] = (),
        missing_intent_ids: tuple[str, ...] = (),
        checkpoint_corrupt: bool = False,
        ref_drift: bool = False,
    ) -> RecoveryPlan:
        events = ["recovery.actor_rebuilt"]
        events.extend(f"dispatch.interrupted:{item}" for item in active_dispatch_ids)
        events.extend(("git.refs.reconciled", "receipts.repaired"))
        events.extend(f"integration.intent.retry:{item}" for item in missing_intent_ids)
        if checkpoint_corrupt:
            events.append("checkpoint.discarded")
        if ref_drift:
            events.append("recovery.ref_drift")
        events.append("recovery.completed")
        return RecoveryPlan(trigger, tuple(events), report_halted=ref_drift)


__all__ = [
    "AgentRunRecoveryCoordinator",
    "AgentRunRecoveryStore",
    "CandidateCheckpointVerifier",
    "CandidateSnapshotPort",
    "ActorCheckpoint",
    "CheckpointRestore",
    "CheckpointPolicy",
    "RecoveryCoordinator",
    "RecoveryPlan",
    "RecoveryTrigger",
    "WriteSessionRecovery",
    "candidate_checkpoint_digest",
    "has_committed_owner_receipt",
    "restore_checkpoint",
    "write_scope_digest",
]

"""Private identities and metadata for one dispatched Agent session.

Only digests and opaque references belong in this ledger. Session bodies live
behind separately managed CAS references.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import NewType
from uuid import UUID

from codemigrator.core import Phase, SessionKind, SliceGenerationRef

from .loop_contracts import SessionExit, SessionState

AgentRunId = NewType("AgentRunId", UUID)

_TERMINAL_EXITS = {
    SessionState.Closed: {SessionExit.Completed, SessionExit.SegmentStopped},
    SessionState.Failed: {SessionExit.Failed, SessionExit.BudgetExhausted},
    SessionState.Invalidated: {SessionExit.Invalidated},
}


@dataclass(frozen=True, slots=True)
class AgentRun:
    agent_run_id: AgentRunId
    owner_kind: str
    owner_id: UUID
    logical_task_key: str
    phase: Phase
    session_kind: SessionKind
    thread_id: str
    model_binding_sha256: str
    context_sha256: str
    toolset_sha256: str
    template_sha256: str
    slice_ref: SliceGenerationRef | None = None
    write_scope_sha256: str | None = None
    checkpoint_sha256: str | None = None
    candidate_checkpoint_sha256: str | None = None
    result_sha256: str | None = None
    usage_sha256: str | None = None
    retry_of: AgentRunId | None = None
    continuation_of: AgentRunId | None = None
    restarted_from: AgentRunId | None = None
    state: SessionState = SessionState.Created
    exit: SessionExit | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.agent_run_id, UUID) or not isinstance(self.owner_id, UUID):
            raise ValueError("AgentRun identities must be UUIDs")
        if self.owner_kind not in {"run", "draft"}:
            raise ValueError("AgentRun owner kind must be run or draft")
        if not isinstance(self.phase, Phase) or not isinstance(self.session_kind, SessionKind):
            raise ValueError("AgentRun phase and session kind must use core enums")
        if self.slice_ref is not None and not isinstance(self.slice_ref, SliceGenerationRef):
            raise ValueError("AgentRun slice ref must use the core typed reference")
        for name in ("retry_of", "continuation_of", "restarted_from"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, UUID):
                raise ValueError(f"AgentRun {name} must be a UUID reference")
        if not isinstance(self.state, SessionState) or (
            self.exit is not None and not isinstance(self.exit, SessionExit)
        ):
            raise ValueError("AgentRun state and exit must use session enums")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", self.logical_task_key):
            raise ValueError("AgentRun task key must be an opaque token")
        try:
            UUID(self.thread_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("AgentRun thread must be a UUID") from exc
        for name in (
            "model_binding_sha256",
            "context_sha256",
            "toolset_sha256",
            "template_sha256",
            "write_scope_sha256",
            "checkpoint_sha256",
            "candidate_checkpoint_sha256",
            "result_sha256",
            "usage_sha256",
        ):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, str)
                or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
            ):
                raise ValueError(f"AgentRun {name} digest must be SHA-256")
        expected = _TERMINAL_EXITS.get(self.state)
        if (
            expected is None
            and self.exit is not None
            or expected is not None
            and self.exit not in expected
        ):
            raise ValueError("AgentRun state and exit are inconsistent")
        if (
            sum(
                value is not None
                for value in (self.retry_of, self.continuation_of, self.restarted_from)
            )
            > 1
        ):
            raise ValueError("AgentRun lineage is ambiguous")
        if self.agent_run_id in (self.retry_of, self.continuation_of, self.restarted_from):
            raise ValueError("AgentRun cannot descend from itself")

    @property
    def owner_key(self) -> tuple[str, UUID, str]:
        return self.owner_kind, self.owner_id, self.logical_task_key

    @property
    def is_terminal(self) -> bool:
        return self.state in _TERMINAL_EXITS

    def same_creation_identity(self, other: AgentRun) -> bool:
        """A replay may supply a fresh ID but cannot change frozen bindings."""
        frozen = (
            "owner_kind",
            "owner_id",
            "logical_task_key",
            "phase",
            "session_kind",
            "slice_ref",
            "model_binding_sha256",
            "context_sha256",
            "toolset_sha256",
            "template_sha256",
            "write_scope_sha256",
            "retry_of",
            "continuation_of",
            "restarted_from",
        )
        return all(getattr(self, name) == getattr(other, name) for name in frozen) and (
            self.agent_run_id != other.agent_run_id or self.thread_id == other.thread_id
        )

    def same_frozen_identity(self, other: AgentRun) -> bool:
        return (
            self.agent_run_id == other.agent_run_id
            and self.thread_id == other.thread_id
            and self.same_creation_identity(other)
        )


@dataclass(frozen=True, slots=True)
class AgentRunReceipt:
    receipt_id: UUID
    agent_run_id: AgentRunId
    category: str

    def __post_init__(self) -> None:
        if not isinstance(self.receipt_id, UUID) or not isinstance(self.agent_run_id, UUID):
            raise ValueError("AgentRun receipt identities must be UUIDs")
        if not re.fullmatch(r"[a-z][a-z0-9._-]{0,63}", self.category):
            raise ValueError("AgentRun receipt category must be an opaque token")


__all__ = ["AgentRun", "AgentRunId", "AgentRunReceipt"]

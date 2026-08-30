"""Immutable contracts shared by the Agent Loop and its adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from codemigrator.core import (
    ContextPack,
    GitOid,
    Phase,
    RunId,
    RunStatus,
    SessionKind,
    SliceGenerationRef,
    SliceId,
)

from .binding import LockedModelBinding
from .context import ContextEnvelope


class SessionState(str, Enum):
    Created = "CREATED"
    Running = "RUNNING"
    CheckpointPending = "CHECKPOINT_PENDING"
    Closed = "CLOSED"
    Invalidated = "INVALIDATED"
    Failed = "FAILED"


class SessionExit(str, Enum):
    Completed = "COMPLETED"
    Failed = "FAILED"
    SegmentStopped = "SEGMENT_STOPPED"
    Invalidated = "INVALIDATED"


@dataclass(frozen=True, slots=True)
class SessionIdentity:
    run_id: RunId
    phase: Phase
    session_kind: SessionKind
    slice_ref: SliceGenerationRef | None

    @property
    def slice_id(self) -> SliceId | None:
        return self.slice_ref.slice_id if self.slice_ref is not None else None

    @property
    def generation(self) -> int | None:
        return self.slice_ref.generation if self.slice_ref is not None else None

    @property
    def candidate_oid(self) -> GitOid | None:
        return self.slice_ref.baseline_candidate_oid if self.slice_ref is not None else None


@dataclass(frozen=True, slots=True)
class SessionSpec:
    identity: SessionIdentity
    run_status: RunStatus
    binding: LockedModelBinding
    context_pack: ContextPack
    context: ContextEnvelope = field(default_factory=ContextEnvelope)
    template: str = "session"
    state: SessionState = SessionState.Created


__all__ = [
    "SessionExit",
    "SessionIdentity",
    "SessionSpec",
    "SessionState",
]

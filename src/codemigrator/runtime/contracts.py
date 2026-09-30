"""Typed runtime messages and immutable control-plane facts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TypeAlias
from uuid import UUID

from codemigrator.core import (
    ActiveDispatch,
    Advice,
    CreateRun,
    FailureReason,
    RunId,
    RunStatus,
    SliceId,
    validate_candidate_generation,
)
from codemigrator.workspace import CheckpointReceipt

from .agent_runs import AgentRun, AgentRunId, AgentRunReceipt
from .budget import BudgetUsage


@dataclass(frozen=True, slots=True)
class CreateRunCommand:
    run_id: RunId
    create_run: CreateRun


@dataclass(frozen=True, slots=True)
class RunCreatedReceipt:
    """Durable acknowledgement derived from the committed ``run.created`` event."""

    run_id: RunId
    receipt_key: str
    event_sequence: int
    state_version: int

    def __post_init__(self) -> None:
        if self.event_sequence != 1 or self.state_version != 1:
            raise ValueError("RunCreated receipt must identify the initial Run commit")
        if self.receipt_key != f"run.created:{self.run_id}":
            raise ValueError("RunCreated receipt key does not match its Run")


@dataclass(frozen=True, slots=True)
class DraftOwnerReceipt:
    """Durable acknowledgement for one immutable Draft-owned fact."""

    draft_id: UUID
    receipt_key: str
    category: str
    fact_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.draft_id, UUID):
            raise ValueError("Draft receipt owner must be a UUID")
        if not self.receipt_key or len(self.receipt_key) > 256:
            raise ValueError("Draft receipt key must be non-empty and bounded")
        if not self.category or len(self.category) > 64:
            raise ValueError("Draft receipt category must be non-empty and bounded")
        if len(self.fact_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.fact_sha256
        ):
            raise ValueError("Draft receipt fact digest must be SHA-256")


@dataclass(frozen=True, slots=True)
class ActorPhaseReceipt:
    run_id: RunId
    receipt_key: str
    event_sequence: int

    def __post_init__(self) -> None:
        if not self.receipt_key or self.event_sequence < 1:
            raise ValueError("actor phase receipt is invalid")


@dataclass(frozen=True, slots=True)
class ExecuteRoundResult:
    receipt: ActorPhaseReceipt
    complete: bool


@dataclass(frozen=True, slots=True)
class CandidateCheckpointClaim:
    agent_run_id: AgentRunId
    receipt: CheckpointReceipt | None


@dataclass(frozen=True, slots=True)
class ExecutionRoundDecision:
    complete: bool
    dispatch_count: int
    completed_write_agent_run_ids: tuple[AgentRunId, ...] = ()
    candidate_claims: tuple[CandidateCheckpointClaim, ...] = ()

    def __post_init__(self) -> None:
        if type(self.dispatch_count) is not int or self.dispatch_count < 0:
            raise ValueError("execution dispatch count must be non-negative")


@dataclass(frozen=True, slots=True)
class CandidateCheckpointFact:
    agent_run_id: AgentRunId
    slice_id: SliceId
    generation: int
    expected_candidate_oid: str
    candidate_oid: str
    receipt_sha256: str

    def __post_init__(self) -> None:
        validate_candidate_generation(self.generation)
        if not self.expected_candidate_oid or not self.candidate_oid:
            raise ValueError("candidate checkpoint OIDs must be non-empty")
        if not _is_sha256(self.receipt_sha256):
            raise ValueError("candidate checkpoint receipt digest is invalid")


@dataclass(frozen=True, slots=True)
class VerificationSummary:
    passed: bool
    result_sha256: str

    def __post_init__(self) -> None:
        if type(self.passed) is not bool or not _is_sha256(self.result_sha256):
            raise ValueError("verification summary is invalid")


@dataclass(frozen=True, slots=True)
class ReportSummary:
    result_sha256: str
    status: RunStatus

    def __post_init__(self) -> None:
        if not _is_sha256(self.result_sha256):
            raise ValueError("report summary digest is invalid")
        if self.status not in {RunStatus.Completed, RunStatus.PartiallyCompleted}:
            raise ValueError("report status must be terminal and successful")


@dataclass(frozen=True, slots=True)
class WorkflowCommandMessage:
    kind: str
    run_id: RunId
    logical_key: str
    payload: object
    response: asyncio.Future[object]


@dataclass(frozen=True, slots=True)
class ExecutionRoundFinishedMessage:
    logical_key: str
    decision: ExecutionRoundDecision | None = None
    error: Exception | None = None


@dataclass(frozen=True, slots=True)
class CancelCommand:
    expected_version: int


@dataclass(frozen=True, slots=True)
class SessionInputCommand:
    kind: str
    payload: dict[str, object]


ApiCommandPayload: TypeAlias = CreateRunCommand | CancelCommand | SessionInputCommand


@dataclass(frozen=True, slots=True)
class ApiCommand:
    command: ApiCommandPayload


@dataclass(frozen=True, slots=True)
class ExecutionReceiptMessage:
    run_id: RunId
    dispatch: ActiveDispatch
    result_status: str | None = None
    started: bool = False


@dataclass(frozen=True, slots=True)
class BudgetEventMessage:
    input_tokens: int
    output_tokens: int
    cost_micros: int


@dataclass(frozen=True, slots=True)
class RecoveryCommandMessage:
    trigger: str
    active_dispatch_ids: tuple[str, ...] = ()
    missing_intent_ids: tuple[str, ...] = ()
    checkpoint_corrupt: bool = False
    ref_drift: bool = False


@dataclass(frozen=True, slots=True)
class AdviceMessage:
    advice: Advice


RuntimeMessage: TypeAlias = (
    ApiCommand
    | ExecutionReceiptMessage
    | BudgetEventMessage
    | RecoveryCommandMessage
    | AdviceMessage
    | WorkflowCommandMessage
    | ExecutionRoundFinishedMessage
)


@dataclass(frozen=True, slots=True)
class EventSpec:
    event_type: str
    data: dict[str, object] = field(default_factory=dict)


def agent_run_lifecycle_spec(
    record: AgentRun,
    receipt: AgentRunReceipt | None = None,
) -> EventSpec:
    """Build an internal low-sensitivity event for an owner ledger transaction."""
    if receipt is None:
        if record.is_terminal:
            raise ValueError("terminal AgentRun event requires owner receipt")
        data: dict[str, object] = {
            "receipt_key": f"agent_run.started:{record.agent_run_id}",
            "agent_run_id": str(record.agent_run_id),
            "phase": record.phase.value,
            "session_kind": record.session_kind.value,
        }
        if record.slice_ref is not None:
            data["slice_id"] = str(record.slice_ref.slice_id)
            data["generation"] = record.slice_ref.generation
        return EventSpec("agent_run.started", data)
    if not record.is_terminal or receipt.agent_run_id != record.agent_run_id:
        raise ValueError("terminal event requires matching AgentRun receipt")
    data = {
        "receipt_key": f"agent_run.terminal:{record.agent_run_id}",
        "agent_run_id": str(record.agent_run_id),
        "phase": record.phase.value,
        "session_kind": record.session_kind.value,
        "exit": record.exit.value if record.exit is not None else None,
        "receipt_category": receipt.category,
    }
    if record.slice_ref is not None:
        data["slice_id"] = str(record.slice_ref.slice_id)
        data["generation"] = record.slice_ref.generation
    return EventSpec("agent_run.terminal", data)


@dataclass(frozen=True, slots=True)
class RunState:
    run_id: RunId
    status: RunStatus = RunStatus.Created
    version: int = 0
    cancel_requested: bool = False
    failure_reason: FailureReason | None = None
    new_calls_enabled: bool = True
    budget_usage: BudgetUsage = field(default_factory=BudgetUsage)
    budget_warning_emitted: bool = False
    active_dispatches: tuple[ActiveDispatch, ...] = ()
    continuation_counts: tuple[tuple[int, int], ...] = ()
    terminal_slice_failures: tuple[str, ...] = ()
    adopted_advice_ids: tuple[str, ...] = ()
    pending_advice_ids: tuple[str, ...] = ()
    reporting_halted: bool = False
    create_request: CreateRun | None = None
    frozen_plan_sha256: str | None = None
    candidate_checkpoints: tuple[CandidateCheckpointFact, ...] = ()


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    sequence: int
    event_type: str
    data: dict[str, object]


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    state: RunState
    events: tuple[RuntimeEvent, ...]


def _is_sha256(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = [
    "DraftOwnerReceipt",
    "AdviceMessage",
    "ApiCommand",
    "ApiCommandPayload",
    "ActorPhaseReceipt",
    "BudgetEventMessage",
    "CancelCommand",
    "CandidateCheckpointClaim",
    "CandidateCheckpointFact",
    "CreateRunCommand",
    "EventSpec",
    "ExecutionRoundFinishedMessage",
    "ExecuteRoundResult",
    "ExecutionRoundDecision",
    "ExecutionReceiptMessage",
    "RecoveryCommandMessage",
    "RunState",
    "RuntimeEvent",
    "RuntimeMessage",
    "RuntimeSnapshot",
    "RunCreatedReceipt",
    "ReportSummary",
    "SessionInputCommand",
    "VerificationSummary",
    "WorkflowCommandMessage",
    "agent_run_lifecycle_spec",
]

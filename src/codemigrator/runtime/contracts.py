"""Typed runtime messages and immutable control-plane facts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypeAlias

from codemigrator.core import (
    ActiveDispatch,
    Advice,
    CreateRun,
    FailureReason,
    RunId,
    RunStatus,
)

from .agent_runs import AgentRun, AgentRunReceipt
from .budget import BudgetUsage


@dataclass(frozen=True, slots=True)
class CreateRunCommand:
    run_id: RunId
    create_run: CreateRun


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
        return EventSpec(
            "agent_run.started",
            {
                "agent_run_id": str(record.agent_run_id),
                "phase": record.phase.value,
                "session_kind": record.session_kind.value,
            },
        )
    if not record.is_terminal or receipt.agent_run_id != record.agent_run_id:
        raise ValueError("terminal event requires matching AgentRun receipt")
    return EventSpec(
        "agent_run.terminal",
        {
            "agent_run_id": str(record.agent_run_id),
            "phase": record.phase.value,
            "session_kind": record.session_kind.value,
            "exit": record.exit.value if record.exit is not None else None,
            "receipt_category": receipt.category,
        },
    )


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


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    sequence: int
    event_type: str
    data: dict[str, object]


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    state: RunState
    events: tuple[RuntimeEvent, ...]


__all__ = [
    "AdviceMessage",
    "ApiCommand",
    "ApiCommandPayload",
    "BudgetEventMessage",
    "CancelCommand",
    "CreateRunCommand",
    "EventSpec",
    "ExecutionReceiptMessage",
    "RecoveryCommandMessage",
    "RunState",
    "RuntimeEvent",
    "RuntimeMessage",
    "RuntimeSnapshot",
    "SessionInputCommand",
    "agent_run_lifecycle_spec",
]

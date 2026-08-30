"""Runtime composition root and deterministic Run orchestration primitives."""

import signal

from .actor import ActorRegistry, CheckpointWriter, RunActor
from .advice import (
    AdviceDisposition,
    AdviceValidationContext,
    AdviceValidationResult,
    advice_proposal_hash,
    evaluate_advice,
)
from .app import AdvisoryLockPort, AppLifecycle, AppState, InMemoryAdvisoryLock
from .budget import BudgetEvaluation, BudgetLimits, BudgetUsage, evaluate_budget
from .contracts import (
    AdviceMessage,
    ApiCommand,
    ApiCommandPayload,
    BudgetEventMessage,
    CancelCommand,
    CreateRunCommand,
    EventSpec,
    ExecutionReceiptMessage,
    RecoveryCommandMessage,
    RunState,
    RuntimeEvent,
    RuntimeMessage,
    RuntimeSnapshot,
    SessionInputCommand,
)
from .integration import (
    IntegrationCoordinator,
    IntegrationItem,
    IntegrationStart,
    RepairRetryBudget,
)
from .recovery import (
    ActorCheckpoint,
    CheckpointRestore,
    RecoveryCoordinator,
    RecoveryPlan,
    RecoveryTrigger,
    restore_checkpoint,
)
from .report import build_report
from .scheduler import FairScheduler, ReadySlice, ResourcePool
from .store import InMemoryRuntimeStore, RuntimeStore, StoreCommitError
from .supervisor import SupervisorAdviceKind, SupervisorTrigger, supervisor_triggers


def main() -> None:
    """Keep the process alive for the deployment-provided composition root."""

    signal.pause()


__all__ = [
    "ActorRegistry",
    "ActorCheckpoint",
    "AdviceMessage",
    "AdviceDisposition",
    "AdviceValidationContext",
    "AdviceValidationResult",
    "ApiCommand",
    "ApiCommandPayload",
    "AdvisoryLockPort",
    "AppLifecycle",
    "AppState",
    "BudgetEvaluation",
    "BudgetEventMessage",
    "BudgetLimits",
    "BudgetUsage",
    "CancelCommand",
    "CheckpointRestore",
    "CheckpointWriter",
    "CreateRunCommand",
    "EventSpec",
    "ExecutionReceiptMessage",
    "FairScheduler",
    "InMemoryAdvisoryLock",
    "InMemoryRuntimeStore",
    "IntegrationCoordinator",
    "IntegrationItem",
    "IntegrationStart",
    "ReadySlice",
    "RecoveryCoordinator",
    "RecoveryCommandMessage",
    "RecoveryPlan",
    "RecoveryTrigger",
    "ResourcePool",
    "RepairRetryBudget",
    "RunActor",
    "RunState",
    "RuntimeEvent",
    "RuntimeMessage",
    "RuntimeSnapshot",
    "RuntimeStore",
    "SessionInputCommand",
    "StoreCommitError",
    "SupervisorAdviceKind",
    "SupervisorTrigger",
    "advice_proposal_hash",
    "build_report",
    "evaluate_advice",
    "evaluate_budget",
    "main",
    "restore_checkpoint",
    "supervisor_triggers",
]

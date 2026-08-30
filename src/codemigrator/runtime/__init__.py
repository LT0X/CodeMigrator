"""Runtime composition root and deterministic Run orchestration primitives."""

from .actor import (
    ActorRegistry,
    ArchivePort,
    CancellationPort,
    CheckpointWriter,
    ContinuationPort,
    RunActor,
)
from .advice import (
    AdviceDisposition,
    AdviceValidationContext,
    AdviceValidationResult,
    advice_proposal_hash,
    evaluate_advice,
)
from .app import (
    AdvisoryLockPort,
    AppLifecycle,
    AppState,
    AsyncAdvisoryLockPort,
    AsyncAppLifecycle,
    InMemoryAdvisoryLock,
    PostgreSQLAdvisoryLock,
    RuntimeApplication,
    run_from_environment,
)
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
    CheckpointPolicy,
    CheckpointRestore,
    RecoveryCoordinator,
    RecoveryPlan,
    RecoveryTrigger,
    restore_checkpoint,
)
from .report import build_report
from .scheduler import FairScheduler, ReadySlice, ResourcePool
from .store import InMemoryRuntimeStore, PostgreSQLRuntimeStore, RuntimeStore, StoreCommitError
from .supervisor import SupervisorAdviceKind, SupervisorTrigger, supervisor_triggers


def main() -> None:
    """Start the application using deployment-provided configuration."""

    exit_code = run_from_environment()
    if exit_code:
        raise SystemExit(exit_code)


__all__ = [
    "ActorRegistry",
    "ActorCheckpoint",
    "ArchivePort",
    "AdviceMessage",
    "AdviceDisposition",
    "AdviceValidationContext",
    "AdviceValidationResult",
    "AsyncAdvisoryLockPort",
    "AsyncAppLifecycle",
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
    "CheckpointPolicy",
    "CheckpointWriter",
    "CancellationPort",
    "CreateRunCommand",
    "ContinuationPort",
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
    "RuntimeApplication",
    "RuntimeStore",
    "PostgreSQLRuntimeStore",
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
    "PostgreSQLAdvisoryLock",
    "run_from_environment",
    "supervisor_triggers",
]

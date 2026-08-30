"""Shared enumerations for the CodeMigrator contracts."""

from enum import Enum


class MigrationSessionStatus(str, Enum):
    Drafting = "Drafting"
    ReadyToConfirm = "ReadyToConfirm"
    AttachedToRun = "AttachedToRun"
    Closed = "Closed"


class InteractionStatus(str, Enum):
    Ready = "Ready"
    PausingForInput = "PausingForInput"
    WaitingForUser = "WaitingForUser"
    ApplyingCorrection = "ApplyingCorrection"


class CorrectionIntentStatus(str, Enum):
    Received = "Received"
    NeedsClarification = "NeedsClarification"
    NeedsConfirmation = "NeedsConfirmation"
    Accepted = "Accepted"
    Applied = "Applied"
    DeferredToFollowUp = "DeferredToFollowUp"
    Rejected = "Rejected"


class SliceKind(str, Enum):
    Contract = "CONTRACT"
    Implementation = "IMPLEMENTATION"
    TestTranslation = "TEST_TRANSLATION"
    TestGeneration = "TEST_GENERATION"


class ArtifactKind(str, Enum):
    GeneratedCode = "GENERATED_CODE"
    DeclarativeConfig = "DECLARATIVE_CONFIG"
    ResourceFile = "RESOURCE_FILE"


class DossierBudgetTier(str, Enum):
    Shallow = "Shallow"
    Deep = "Deep"


class SliceAttemptStatus(str, Enum):
    Ready = "READY"
    Running = "RUNNING"
    LocalVerifying = "LOCAL_VERIFYING"
    LocallyVerified = "LOCALLY_VERIFIED"
    IntegrationQueued = "INTEGRATION_QUEUED"
    Integrating = "INTEGRATING"
    Regenerating = "REGENERATING"
    Integrated = "INTEGRATED"
    TerminalFailed = "TERMINAL_FAILED"
    Cancelled = "CANCELLED"


class PlanEdgeKind(str, Enum):
    Requires = "REQUIRES"
    OrderedBefore = "ORDERED_BEFORE"


class RunStatus(str, Enum):
    Created = "CREATED"
    Planning = "PLANNING"
    Executing = "EXECUTING"
    Verifying = "VERIFYING"
    Reporting = "REPORTING"
    Completed = "COMPLETED"
    PartiallyCompleted = "PARTIALLY_COMPLETED"
    Failed = "FAILED"
    Cancelled = "CANCELLED"


class FailureReason(str, Enum):
    AnalysisFailed = "ANALYSIS_FAILED"
    DossierInconsistent = "DOSSIER_INCONSISTENT"
    PlanFailed = "PLAN_FAILED"
    ExecutionFailed = "EXECUTION_FAILED"
    VerificationTerminal = "VERIFICATION_TERMINAL"
    ReportGenerationFailed = "REPORT_GENERATION_FAILED"
    BudgetExhausted = "BUDGET_EXHAUSTED"
    ResourceExhausted = "RESOURCE_EXHAUSTED"
    OutputLimitExceeded = "OUTPUT_LIMIT_EXCEEDED"
    SliceRegenerationExhausted = "SLICE_REGENERATION_EXHAUSTED"
    NondeterministicVerification = "NONDETERMINISTIC_VERIFICATION"


class DeliveryChannelStatus(str, Enum):
    Pending = "PENDING"
    Generating = "GENERATING"
    Ready = "READY"
    DeliveryFailed = "DELIVERY_FAILED"


class ModelProfile(str, Enum):
    Reasoning = "REASONING"
    Code = "CODE"


class Phase(str, Enum):
    Plan = "PLAN"
    Execute = "EXECUTE"
    Verify = "VERIFY"
    Report = "REPORT"


class ResidentRole(str, Enum):
    ExploreCoordinator = "EXPLORE_COORDINATOR"
    ExecuteSupervisor = "EXECUTE_SUPERVISOR"


class AdviceKind(str, Enum):
    ExploreReassignment = "EXPLORE_REASSIGNMENT"
    RepairDecision = "REPAIR_DECISION"
    RouteSuggestion = "ROUTE_SUGGESTION"
    PlanRevision = "PLAN_REVISION"
    AskUser = "ASK_USER"


class ModuleBoundaryStrategy(str, Enum):
    ManifestPerModule = "MANIFEST_PER_MODULE"
    SingleManifestDirectoryConvention = "SINGLE_MANIFEST_DIRECTORY_CONVENTION"
    DirectoryConvention = "DIRECTORY_CONVENTION"


class CheckAction(str, Enum):
    Scaffold = "SCAFFOLD"
    Compile = "COMPILE"
    Test = "TEST"
    Lint = "LINT"
    TypeCheck = "TYPE_CHECK"


class DiagnosticSeverity(str, Enum):
    Error = "Error"
    Warning = "Warning"


class CheckStatus(str, Enum):
    Passed = "PASSED"
    Failed = "FAILED"
    TimedOut = "TIMED_OUT"
    OutputLimitExceeded = "OUTPUT_LIMIT_EXCEEDED"
    InfrastructureError = "INFRASTRUCTURE_ERROR"


class SessionKind(str, Enum):
    AnalyzeAuxiliary = "ANALYZE_AUXILIARY"
    PlanAuxiliary = "PLAN_AUXILIARY"
    Contract = "CONTRACT"
    Implementation = "IMPLEMENTATION"
    TestTranslation = "TEST_TRANSLATION"
    TestGeneration = "TEST_GENERATION"
    ExploreCoordinator = "EXPLORE_COORDINATOR"
    ExecuteSupervisor = "EXECUTE_SUPERVISOR"
    RepairSession = "REPAIR_SESSION"


class AttributionReliability(str, Enum):
    Reliable = "Reliable"
    Uncertain = "Uncertain"
    Dynamic = "Dynamic"

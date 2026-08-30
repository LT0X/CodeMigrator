"""Deterministic orchestration contracts for global repair sessions."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Protocol
from uuid import UUID

from pydantic import ConfigDict

from codemigrator.core import (
    Advice,
    AdviceKind,
    GlobalRepairSession,
    RepairDecision,
    RepairDecisionId,
    RepairEvidence,
    RepoRelativePath,
    RunId,
    SessionKind,
    WriteScope,
    WriteScopeOut,
    normalize_repo_relative_paths,
)

from .contracts import EventSpec
from .integration import IntegrationCoordinator, IntegrationItem

_TERMINAL_STATUSES = frozenset({"INTEGRATED", "TERMINAL", "TERMINAL_FAILED"})
_ACTIVE_STATUSES = frozenset(
    {"RUNNING", "REGENERATING", "CHECKPOINT_PENDING", "CHECKPOINTPENDING"}
)
_CAS_URI = re.compile(r"cas://[0-9a-fA-F]{64}\Z")


class FrozenWriteScopeOut(WriteScopeOut):
    """Immutable runtime projection of the core scope lists."""

    model_config = ConfigDict(frozen=True)
    write_paths: tuple[RepoRelativePath, ...]  # type: ignore[assignment]
    create_roots: tuple[RepoRelativePath, ...]  # type: ignore[assignment]


class FrozenWriteScope(WriteScope):
    model_config = ConfigDict(frozen=True)
    out: FrozenWriteScopeOut


class FrozenGlobalRepairSession(GlobalRepairSession):
    model_config = ConfigDict(frozen=True)
    joint_write_scope: FrozenWriteScope


def _non_empty_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value


def _identity_text(value: object, name: str) -> str:
    if isinstance(value, UUID):
        return str(value)
    return _non_empty_text(value, name)


def _text_tuple(value: object, name: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{name} must be a sequence of strings")
    result = tuple(value)
    if not allow_empty and not result:
        raise ValueError(f"{name} must not be empty")
    if any(not isinstance(item, str) or not item.strip() for item in result):
        raise ValueError(f"{name} must contain non-empty strings")
    return result


def _status(value: object, name: str) -> str:
    return _non_empty_text(value, name).upper().replace("-", "_")


def _scope_paths(scope: WriteScope) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return tuple(scope.out.write_paths), tuple(scope.out.create_roots)


def _freeze_scope(scope: WriteScope) -> FrozenWriteScope:
    paths, roots = _scope_paths(scope)
    return FrozenWriteScope(
        out=FrozenWriteScopeOut(
            write_paths=tuple(RepoRelativePath(path) for path in paths),
            create_roots=tuple(RepoRelativePath(root) for root in roots),
        )
    )


def _path_in_root(path: str, root: str) -> bool:
    return path == root or path.startswith(f"{root}/")


def _scopes_overlap(
    left_paths: Sequence[str],
    left_roots: Sequence[str],
    right_paths: Sequence[str],
    right_roots: Sequence[str],
) -> bool:
    left_path_set = set(left_paths)
    right_path_set = set(right_paths)
    if left_path_set.intersection(right_path_set):
        return True
    if any(_path_in_root(path, root) for path in left_path_set for root in right_roots):
        return True
    if any(_path_in_root(path, root) for path in right_path_set for root in left_roots):
        return True
    return any(
        _path_in_root(left_root, right_root) or _path_in_root(right_root, left_root)
        for left_root in left_roots
        for right_root in right_roots
    )


@dataclass(frozen=True, slots=True)
class RepairSlice:
    """A terminal Slice fact and its frozen write scope."""

    slice_id: str | UUID
    status: str
    write_scope: WriteScope

    def __post_init__(self) -> None:
        object.__setattr__(self, "slice_id", _identity_text(self.slice_id, "slice_id"))
        object.__setattr__(self, "status", _status(self.status, "status"))
        if not isinstance(self.write_scope, WriteScope):
            raise TypeError("write_scope must be the core WriteScope model")


@dataclass(frozen=True, slots=True)
class ActiveWriter:
    """A currently in-flight Slice writer visible to dispatch admission."""

    slice_id: str | UUID
    status: str
    write_scope: WriteScope

    def __post_init__(self) -> None:
        object.__setattr__(self, "slice_id", _identity_text(self.slice_id, "slice_id"))
        normalized = _status(self.status, "status")
        if normalized not in _ACTIVE_STATUSES:
            raise ValueError("active writer status is not active")
        object.__setattr__(self, "status", normalized)
        if not isinstance(self.write_scope, WriteScope):
            raise TypeError("write_scope must be the core WriteScope model")


@dataclass(frozen=True, slots=True)
class JointRepairAdmission:
    admitted: bool
    reason: Literal[
        "ADMITTED",
        "EMPTY_REPAIR_SET",
        "REPAIR_SET_NOT_TERMINAL",
        "IN_FLIGHT_SCOPE_CONFLICT",
    ]
    session: GlobalRepairSession | None = None
    blocking_slice_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.admitted != (self.reason == "ADMITTED"):
            raise ValueError("admission flag and reason disagree")
        if self.admitted and self.session is None:
            raise ValueError("admitted repair requires a session")
        if not self.admitted and self.session is not None:
            raise ValueError("blocked repair must not expose a session")
        object.__setattr__(
            self,
            "blocking_slice_ids",
            _text_tuple(self.blocking_slice_ids, "blocking_slice_ids"),
        )


def evaluate_joint_repair_dispatch(
    *,
    run_id: RunId,
    repair_decision_id: RepairDecisionId,
    repair_set: Sequence[RepairSlice],
    active_writers: Sequence[ActiveWriter],
) -> JointRepairAdmission:
    """Freeze a joint scope only when the dispatch-time safety table passes."""

    if not repair_set:
        return JointRepairAdmission(False, "EMPTY_REPAIR_SET")
    if len({item.slice_id for item in repair_set}) != len(repair_set):
        raise ValueError("repair set must contain unique Slice identities")
    nonterminal = tuple(
        str(item.slice_id) for item in repair_set if item.status not in _TERMINAL_STATUSES
    )
    if nonterminal:
        return JointRepairAdmission(
            False, "REPAIR_SET_NOT_TERMINAL", blocking_slice_ids=nonterminal
        )

    write_paths: list[str] = []
    create_roots: list[str] = []
    for item in repair_set:
        paths, roots = _scope_paths(item.write_scope)
        write_paths.extend(paths)
        create_roots.extend(roots)
    joint_scope = _freeze_scope(
        WriteScope(
            out=WriteScopeOut(
                write_paths=[RepoRelativePath(path) for path in write_paths],
                create_roots=[RepoRelativePath(path) for path in create_roots],
            )
        )
    )
    joint_paths, joint_roots = _scope_paths(joint_scope)
    blockers = tuple(
        str(writer.slice_id)
        for writer in active_writers
        if _scopes_overlap(joint_paths, joint_roots, *_scope_paths(writer.write_scope))
    )
    if blockers:
        return JointRepairAdmission(
            False,
            "IN_FLIGHT_SCOPE_CONFLICT",
            blocking_slice_ids=tuple(dict.fromkeys(blockers)),
        )
    session = FrozenGlobalRepairSession(
        repair_decision_id=repair_decision_id,
        run_id=run_id,
        joint_write_scope=joint_scope,
    )
    return JointRepairAdmission(True, "ADMITTED", session=session)


@dataclass(frozen=True, slots=True)
class RepairFailureFacts:
    """Failure identity and diagnostic summary; full bodies remain external."""

    failed_test_refs: tuple[str, ...]
    diagnostic_summary: Mapping[str, object]
    cas_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "failed_test_refs",
            _text_tuple(self.failed_test_refs, "failed_test_refs", allow_empty=False),
        )
        if not isinstance(self.diagnostic_summary, Mapping):
            raise TypeError("diagnostic_summary must be a mapping")
        if not self.diagnostic_summary:
            raise ValueError("diagnostic_summary must not be empty")
        object.__setattr__(
            self, "diagnostic_summary", _freeze_json(self.diagnostic_summary)
        )
        refs = _text_tuple(self.cas_refs, "cas_refs")
        if any(_CAS_URI.fullmatch(ref) is None for ref in refs):
            raise ValueError("cas_refs must contain cas:// SHA-256 URIs")
        object.__setattr__(self, "cas_refs", refs)


@dataclass(frozen=True, slots=True)
class RepairNavigationIndex:
    paths: tuple[str, ...]
    positions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        paths = normalize_repo_relative_paths(self.paths)
        if not paths:
            raise ValueError("paths must not be empty")
        path_set = set(paths)
        positions = _text_tuple(self.positions, "positions")
        for position in positions:
            path, separator, line = position.rpartition(":")
            if not separator or path not in path_set or not line.isdigit() or int(line) < 1:
                raise ValueError("positions must use '<indexed path>:<positive line>'")
        object.__setattr__(self, "paths", tuple(paths))
        object.__setattr__(self, "positions", positions)


@dataclass(frozen=True, slots=True)
class RepairHistoryEntry:
    decision_id: str
    result: str

    def __post_init__(self) -> None:
        _non_empty_text(self.decision_id, "decision_id")
        _non_empty_text(self.result, "result")


@dataclass(frozen=True, slots=True)
class RepairConstraints:
    write_scope: WriteScope
    verification_requirements: tuple[str, ...]
    impact_preview_required: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.write_scope, WriteScope):
            raise TypeError("write_scope must be the core WriteScope model")
        object.__setattr__(
            self,
            "verification_requirements",
            _text_tuple(
                self.verification_requirements,
                "verification_requirements",
                allow_empty=False,
            ),
        )
        if type(self.impact_preview_required) is not bool:
            raise TypeError("impact_preview_required must be a boolean")


@dataclass(frozen=True, slots=True)
class RepairBrief:
    """The complete five-section handoff for a RepairSession."""

    attribution: RepairEvidence
    failure_facts: RepairFailureFacts
    scope_index: RepairNavigationIndex
    repair_history: tuple[RepairHistoryEntry, ...]
    constraints: RepairConstraints

    def __post_init__(self) -> None:
        if not isinstance(self.attribution, RepairEvidence):
            raise TypeError("attribution must use core RepairEvidence")
        if not isinstance(self.failure_facts, RepairFailureFacts):
            raise TypeError("failure_facts must use RepairFailureFacts")
        if not isinstance(self.scope_index, RepairNavigationIndex):
            raise TypeError("scope_index must use RepairNavigationIndex")
        history = tuple(self.repair_history)
        if any(not isinstance(item, RepairHistoryEntry) for item in history):
            raise TypeError("repair_history must contain RepairHistoryEntry values")
        if len({item.decision_id for item in history}) != len(history):
            raise ValueError("repair_history must not contain duplicate decisions")
        if len(set(self.attribution.candidate_slice_set)) != len(
            self.attribution.candidate_slice_set
        ):
            raise ValueError("attribution candidate Slice identities must be unique")
        object.__setattr__(self, "repair_history", history)
        if not isinstance(self.constraints, RepairConstraints):
            raise TypeError("constraints must use RepairConstraints")


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _json_size(value: Mapping[str, object]) -> int:
    try:
        return len(
            json.dumps(
                _jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("repair fact is not JSON serializable") from exc


def assemble_repair_brief(
    *,
    evidence: RepairEvidence,
    failure_facts: RepairFailureFacts,
    scope_index: RepairNavigationIndex,
    repair_history: Sequence[RepairHistoryEntry],
    constraints: RepairConstraints,
    max_inline_bytes: int | None = None,
    run_id: str | UUID | None = None,
    cas_ref_validator: Callable[[str, str], bool] | None = None,
) -> RepairBrief:
    """Assemble all required facts; oversized summaries require a CAS pointer."""

    if max_inline_bytes is not None and (
        type(max_inline_bytes) is not int or max_inline_bytes < 1
    ):
        raise ValueError("max_inline_bytes must be a positive integer")
    if failure_facts.cas_refs and cas_ref_validator is not None:
        if run_id is None:
            raise ValueError("CAS ownership validation requires the current run identity")
        run_identity = _identity_text(run_id, "run_id")
        if any(not cas_ref_validator(ref, run_identity) for ref in failure_facts.cas_refs):
            raise ValueError("CAS reference is not owned by the current run")
    diagnostic_summary = failure_facts.diagnostic_summary
    if max_inline_bytes is not None and _json_size(diagnostic_summary) > max_inline_bytes:
        if not failure_facts.cas_refs:
            raise ValueError("oversized failure facts require a controlled cas reference")
        diagnostic_summary = {
            "externalized": True,
            "cas_refs": list(failure_facts.cas_refs),
        }
        failure_facts = RepairFailureFacts(
            failed_test_refs=failure_facts.failed_test_refs,
            diagnostic_summary=diagnostic_summary,
            cas_refs=failure_facts.cas_refs,
        )
    return RepairBrief(
        attribution=evidence,
        failure_facts=failure_facts,
        scope_index=scope_index,
        repair_history=tuple(repair_history),
        constraints=constraints,
    )


@dataclass(frozen=True, slots=True)
class SituationalSnapshot:
    """Harness-derived decision facts, deliberately not a context payload."""

    slice_states: Mapping[str, str]
    verified_oid: str
    active_dispatches: tuple[str, ...]
    budget_ratio: float
    prior_repair_history: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.slice_states, Mapping):
            raise TypeError("slice_states must be a mapping")
        if any(not isinstance(key, str) or not key for key in self.slice_states):
            raise ValueError("slice state keys must be non-empty strings")
        if any(not isinstance(value, str) or not value for value in self.slice_states.values()):
            raise ValueError("slice state values must be non-empty strings")
        object.__setattr__(self, "slice_states", MappingProxyType(dict(self.slice_states)))
        object.__setattr__(
            self, "verified_oid", _non_empty_text(self.verified_oid, "verified_oid")
        )
        object.__setattr__(
            self,
            "active_dispatches",
            _text_tuple(self.active_dispatches, "active_dispatches"),
        )
        if isinstance(self.budget_ratio, bool) or not 0 <= self.budget_ratio <= 1:
            raise ValueError("budget_ratio must be between 0 and 1")
        object.__setattr__(
            self,
            "prior_repair_history",
            _text_tuple(self.prior_repair_history, "prior_repair_history"),
        )


@dataclass(frozen=True, slots=True)
class RepairSessionIdentity:
    run_id: RunId
    repair_decision_id: RepairDecisionId

    @property
    def session_kind(self) -> SessionKind:
        return SessionKind.RepairSession

    @property
    def generation(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class RepairReadScope:
    source_snapshot_oid: str
    contract_refs: tuple[str, ...]
    domain_workspace_refs: tuple[str, ...]
    verified_head_oid: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_snapshot_oid",
            _non_empty_text(self.source_snapshot_oid, "source_snapshot_oid"),
        )
        object.__setattr__(
            self,
            "contract_refs",
            _text_tuple(self.contract_refs, "contract_refs", allow_empty=False),
        )
        object.__setattr__(
            self,
            "domain_workspace_refs",
            _text_tuple(self.domain_workspace_refs, "domain_workspace_refs", allow_empty=False),
        )
        object.__setattr__(
            self,
            "verified_head_oid",
            _non_empty_text(self.verified_head_oid, "verified_head_oid"),
        )


@dataclass(frozen=True, slots=True)
class RepairSessionDispatch:
    identity: RepairSessionIdentity
    read_scope: RepairReadScope
    write_scope: WriteScope
    brief: RepairBrief
    impact_preview_required: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.identity, RepairSessionIdentity):
            raise TypeError("identity must use RepairSessionIdentity")
        if not isinstance(self.read_scope, RepairReadScope):
            raise TypeError("read_scope must use RepairReadScope")
        if not isinstance(self.write_scope, WriteScope):
            raise TypeError("write_scope must use core WriteScope")
        if not isinstance(self.brief, RepairBrief):
            raise TypeError("brief must use RepairBrief")
        if _scope_paths(self.write_scope) != _scope_paths(self.brief.constraints.write_scope):
            raise ValueError("repair dispatch scope must match the brief constraints")
        if self.impact_preview_required:
            raise ValueError("attribution-driven repair sessions do not require ImpactPreview")


def build_repair_session_dispatch(
    *,
    session: GlobalRepairSession,
    read_scope: RepairReadScope,
    brief: RepairBrief,
) -> RepairSessionDispatch:
    """Instantiate a RepairSession without consuming a Slice generation."""

    if not isinstance(session, GlobalRepairSession):
        raise TypeError("session must use GlobalRepairSession")
    frozen_scope = _freeze_scope(session.joint_write_scope)
    return RepairSessionDispatch(
        identity=RepairSessionIdentity(session.run_id, session.repair_decision_id),
        read_scope=read_scope,
        write_scope=frozen_scope,
        brief=brief,
    )


@dataclass(frozen=True, slots=True)
class RepairAttemptAdmission:
    accepted: bool
    reason: Literal["ADMITTED", "DUPLICATE_EVIDENCE", "REPAIR_RETRY_EXHAUSTED"]
    attempt: int | None = None

    def __post_init__(self) -> None:
        if self.accepted != (self.reason == "ADMITTED"):
            raise ValueError("attempt admission flag and reason disagree")
        if self.accepted != (self.attempt is not None):
            raise ValueError("accepted attempts require a number")


class RepairAttemptGate:
    """Count repair attempts independently by ``(run_id, decision_id)``."""

    def __init__(self, limit: int = 3) -> None:
        if type(limit) is not int or limit < 1:
            raise ValueError("repair attempt limit must be a positive integer")
        self.limit = limit
        self._attempts: dict[tuple[str, str], int] = {}
        self._evidence: dict[tuple[str, str], set[str]] = {}

    def try_start(
        self, run_id: str | UUID, repair_decision_id: str | UUID, evidence_key: str
    ) -> RepairAttemptAdmission:
        key = (
            _identity_text(run_id, "run_id"),
            _identity_text(repair_decision_id, "repair_decision_id"),
        )
        evidence = _non_empty_text(evidence_key, "evidence_key")
        seen = self._evidence.setdefault(key, set())
        if evidence in seen:
            return RepairAttemptAdmission(False, "DUPLICATE_EVIDENCE")
        if self._attempts.get(key, 0) >= self.limit:
            return RepairAttemptAdmission(False, "REPAIR_RETRY_EXHAUSTED")
        seen.add(evidence)
        attempt = self._attempts.get(key, 0) + 1
        self._attempts[key] = attempt
        return RepairAttemptAdmission(True, "ADMITTED", attempt)

    def attempts(self, run_id: str | UUID, repair_decision_id: str | UUID) -> int:
        return self._attempts.get(
            (
                _identity_text(run_id, "run_id"),
                _identity_text(repair_decision_id, "repair_decision_id"),
            ),
            0,
        )


@dataclass(frozen=True, slots=True)
class RepairLineage:
    run_id: str
    original_slice_id: str
    repair_decision_id: str
    relation: Literal["superseded-by-repair"]
    original_generation: int

    @classmethod
    def supersede(
        cls,
        *,
        run_id: str | UUID,
        original_slice_id: str | UUID,
        repair_decision_id: str | UUID,
        generation: int,
    ) -> RepairLineage:
        if type(generation) is not int or generation not in (0, 1, 2):
            raise ValueError("original generation must be one of 0, 1, or 2")
        return cls(
            _identity_text(run_id, "run_id"),
            _identity_text(original_slice_id, "original_slice_id"),
            _identity_text(repair_decision_id, "repair_decision_id"),
            "superseded-by-repair",
            generation,
        )


class RepairSessionRunner(Protocol):
    """Execute a dispatched RepairSession through Agent Loop and ToolGateway.

    The adapter owns checkpoint creation and local verification.  It returns
    only their verified result and a candidate OID; it never advances the
    verified ref or writes the integration queue itself.
    """

    async def run(self, dispatch: RepairSessionDispatch) -> RepairExecutionResult: ...


class RepairEventSink(Protocol):
    """Persist repair lifecycle summaries in the runtime event stream."""

    async def append(self, event: EventSpec) -> None: ...


class RepairBudgetPort(Protocol):
    """Connect RepairSession admission to the actor-owned budget ledger."""

    async def admit(self, identity: RepairSessionIdentity) -> bool: ...


@dataclass(frozen=True, slots=True)
class RepairExecutionResult:
    candidate_commit_oid: str
    checkpoint_passed: bool
    local_verification_passed: bool

    def __post_init__(self) -> None:
        _non_empty_text(self.candidate_commit_oid, "candidate_commit_oid")
        if type(self.checkpoint_passed) is not bool:
            raise TypeError("checkpoint_passed must be a boolean")
        if type(self.local_verification_passed) is not bool:
            raise TypeError("local_verification_passed must be a boolean")


@dataclass(frozen=True, slots=True)
class RepairDispatchRequest:
    """The control-plane facts required to dispatch one adopted decision."""

    decision: RepairDecision
    repair_slices: tuple[RepairSlice, ...]
    active_writers: tuple[ActiveWriter, ...]
    evidence: RepairEvidence
    failure_facts: RepairFailureFacts
    scope_index: RepairNavigationIndex
    repair_history: tuple[RepairHistoryEntry, ...]
    source_snapshot_oid: str
    contract_refs: tuple[str, ...]
    domain_workspace_refs: tuple[str, ...]
    verified_head_oid: str
    verification_requirements: tuple[str, ...]
    evidence_key: str
    original_slice_id: str | UUID
    original_generation: int
    cas_ref_validator: Callable[[str, str], bool] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.decision, RepairDecision):
            raise TypeError("decision must use the core RepairDecision model")
        if any(not isinstance(item, RepairSlice) for item in self.repair_slices):
            raise TypeError("repair_slices must contain RepairSlice values")
        if any(not isinstance(item, ActiveWriter) for item in self.active_writers):
            raise TypeError("active_writers must contain ActiveWriter values")
        if not self.repair_slices:
            raise ValueError("repair_slices must not be empty")
        object.__setattr__(self, "repair_slices", tuple(self.repair_slices))
        object.__setattr__(self, "active_writers", tuple(self.active_writers))
        object.__setattr__(self, "repair_history", tuple(self.repair_history))
        decision_slice_ids = tuple(str(slice_id) for slice_id in self.decision.repair_set)
        supplied_slice_ids = tuple(str(item.slice_id) for item in self.repair_slices)
        if len(set(decision_slice_ids)) != len(decision_slice_ids):
            raise ValueError("decision repair_set must contain unique Slice identities")
        if len(set(supplied_slice_ids)) != len(supplied_slice_ids):
            raise ValueError("repair_slices must contain unique Slice identities")
        if set(decision_slice_ids) != set(supplied_slice_ids):
            raise ValueError("decision repair_set and repair_slices must identify the same Slices")
        object.__setattr__(
            self,
            "source_snapshot_oid",
            _non_empty_text(self.source_snapshot_oid, "source_snapshot_oid"),
        )
        object.__setattr__(
            self,
            "contract_refs",
            _text_tuple(self.contract_refs, "contract_refs", allow_empty=False),
        )
        object.__setattr__(
            self,
            "domain_workspace_refs",
            _text_tuple(self.domain_workspace_refs, "domain_workspace_refs", allow_empty=False),
        )
        object.__setattr__(
            self,
            "verified_head_oid",
            _non_empty_text(self.verified_head_oid, "verified_head_oid"),
        )
        object.__setattr__(
            self,
            "verification_requirements",
            _text_tuple(
                self.verification_requirements,
                "verification_requirements",
                allow_empty=False,
            ),
        )
        object.__setattr__(self, "evidence_key", _non_empty_text(self.evidence_key, "evidence_key"))
        object.__setattr__(
            self,
            "original_slice_id",
            _identity_text(self.original_slice_id, "original_slice_id"),
        )
        if type(self.original_generation) is not int or self.original_generation not in (0, 1, 2):
            raise ValueError("original generation must be one of 0, 1, or 2")
        if self.cas_ref_validator is not None and not callable(self.cas_ref_validator):
            raise TypeError("cas_ref_validator must be callable")
        if self.failure_facts.cas_refs and self.cas_ref_validator is None:
            raise ValueError("CAS references require a current-run ownership validator")


@dataclass(frozen=True, slots=True)
class RepairDispatchResult:
    accepted: bool
    reason: str
    session: RepairSessionDispatch | None = None
    item: IntegrationItem | None = None
    attempt: int | None = None
    lineage: RepairLineage | None = None

    def __post_init__(self) -> None:
        _non_empty_text(self.reason, "reason")
        if self.accepted and self.session is None:
            raise ValueError("accepted repair dispatch requires a session")
        if self.accepted and self.item is None:
            raise ValueError("accepted repair dispatch requires an integration item")
        if self.accepted and self.attempt is None:
            raise ValueError("accepted repair dispatch requires an attempt")


@dataclass(frozen=True, slots=True)
class RepairIntegrationResult:
    integrated: bool
    item: IntegrationItem | None
    lineage: RepairLineage | None = None


class GlobalRepairOrchestrator:
    """Close the adopted-decision → RepairSession → FIFO integration chain."""

    def __init__(
        self,
        coordinator: IntegrationCoordinator,
        runner: RepairSessionRunner,
        *,
        event_sink: RepairEventSink | None = None,
        budget: RepairBudgetPort | None = None,
        attempt_gate: RepairAttemptGate | None = None,
        request_factory: Callable[[Advice], RepairDispatchRequest] | None = None,
    ) -> None:
        self.coordinator = coordinator
        self.runner = runner
        self.event_sink = event_sink
        self.budget = budget
        self.attempt_gate = attempt_gate or RepairAttemptGate()
        self.request_factory = request_factory
        self.last_result: RepairDispatchResult | None = None

    async def dispatch_adopted(self, advice: Advice) -> None:
        """Receive an actor-adopted RepairDecision and run its materialized request."""

        if advice.kind is not AdviceKind.RepairDecision:
            raise ValueError("only adopted RepairDecision advice may enter repair orchestration")
        if self.request_factory is None:
            raise RuntimeError("repair request materialization is not configured")
        self.last_result = await self.dispatch(self.request_factory(advice))

    async def _emit(self, event_type: str, data: dict[str, object]) -> None:
        if self.event_sink is not None:
            await self.event_sink.append(EventSpec(event_type, data))

    async def dispatch(self, request: RepairDispatchRequest) -> RepairDispatchResult:
        """Admit, execute, and enqueue one adopted global repair decision."""

        run_id = str(request.decision.run_id)
        decision_id = str(request.decision.decision_id)
        expected_slices = {str(slice_id) for slice_id in request.decision.repair_set}
        supplied_slices = {str(item.slice_id) for item in request.repair_slices}
        if expected_slices != supplied_slices:
            return RepairDispatchResult(False, "REPAIR_SET_IDENTITY_MISMATCH")
        try:
            self.coordinator.register_verified_head(run_id, request.verified_head_oid)
        except ValueError:
            return RepairDispatchResult(False, "VERIFIED_HEAD_STALE")

        admission = evaluate_joint_repair_dispatch(
            run_id=request.decision.run_id,
            repair_decision_id=request.decision.decision_id,
            repair_set=request.repair_slices,
            active_writers=request.active_writers,
        )
        if not admission.admitted or admission.session is None:
            await self._emit(
                "repair.session.blocked",
                {"run_id": run_id, "repair_decision_id": decision_id, "reason": admission.reason},
            )
            return RepairDispatchResult(False, admission.reason)

        try:
            brief = assemble_repair_brief(
                evidence=request.evidence,
                failure_facts=request.failure_facts,
                scope_index=request.scope_index,
                repair_history=request.repair_history,
                constraints=RepairConstraints(
                    write_scope=admission.session.joint_write_scope,
                    verification_requirements=request.verification_requirements,
                ),
                max_inline_bytes=64 * 1024,
                run_id=request.decision.run_id,
                cas_ref_validator=request.cas_ref_validator,
            )
            dispatch = build_repair_session_dispatch(
                session=admission.session,
                read_scope=RepairReadScope(
                    source_snapshot_oid=request.source_snapshot_oid,
                    contract_refs=request.contract_refs,
                    domain_workspace_refs=request.domain_workspace_refs,
                    verified_head_oid=request.verified_head_oid,
                ),
                brief=brief,
            )
        except (TypeError, ValueError):
            await self._emit(
                "repair.session.blocked",
                {"run_id": run_id, "repair_decision_id": decision_id, "reason": "BRIEF_REJECTED"},
            )
            return RepairDispatchResult(False, "BRIEF_REJECTED")

        attempt = self.attempt_gate.try_start(run_id, decision_id, request.evidence_key)
        if not attempt.accepted:
            await self._emit(
                "repair.session.blocked",
                {"run_id": run_id, "repair_decision_id": decision_id, "reason": attempt.reason},
            )
            return RepairDispatchResult(False, attempt.reason)
        assert attempt.attempt is not None
        if self.budget is not None and not await self.budget.admit(dispatch.identity):
            await self._emit(
                "repair.session.blocked",
                {"run_id": run_id, "repair_decision_id": decision_id, "reason": "BUDGET_CLOSED"},
            )
            return RepairDispatchResult(
                False, "BUDGET_CLOSED", session=dispatch, attempt=attempt.attempt
            )

        await self._emit(
            "repair.session.started",
            {
                "run_id": run_id,
                "repair_decision_id": decision_id,
                "attempt": attempt.attempt,
                "slice_count": len(request.repair_slices),
            },
        )
        try:
            execution = await self.runner.run(dispatch)
        except Exception:
            await self._emit(
                "repair.session.failed",
                {"run_id": run_id, "repair_decision_id": decision_id, "reason": "RUNNER_ERROR"},
            )
            return RepairDispatchResult(
                False, "RUNNER_ERROR", session=dispatch, attempt=attempt.attempt
            )
        if not execution.checkpoint_passed or not execution.local_verification_passed:
            await self._emit(
                "repair.session.failed",
                {
                    "run_id": run_id,
                    "repair_decision_id": decision_id,
                    "reason": "CHECKPOINT_OR_LOCAL_VERIFY_FAILED",
                },
            )
            return RepairDispatchResult(
                False,
                "CHECKPOINT_OR_LOCAL_VERIFY_FAILED",
                session=dispatch,
                attempt=attempt.attempt,
            )

        item = IntegrationItem(
            run_id=run_id,
            slice_id=f"repair:{decision_id}",
            generation=None,
            candidate_commit_oid=execution.candidate_commit_oid,
            repair=True,
            repair_decision_id=decision_id,
            replaces_slice_id=str(request.original_slice_id),
            original_generation=request.original_generation,
        )
        if not self.coordinator.enqueue_repair(item):
            return RepairDispatchResult(
                False, "INTEGRATION_REJECTED", session=dispatch, attempt=attempt.attempt
            )
        await self._emit(
            "repair.session.integration_queued",
            {"run_id": run_id, "repair_decision_id": decision_id, "slice_id": item.slice_id},
        )
        return RepairDispatchResult(
            True,
            "ADMITTED",
            session=dispatch,
            item=item,
            attempt=attempt.attempt,
        )

    async def complete_integration(
        self, *, success: bool, new_verified_oid: str | None = None
    ) -> RepairIntegrationResult:
        """Record FIFO completion and derive supersession lineage for repairs."""

        item = self.coordinator.complete(success=success, new_verified_oid=new_verified_oid)
        if item is None:
            return RepairIntegrationResult(False, None)
        lineage = (
            RepairLineage.supersede(
                run_id=item.run_id,
                original_slice_id=item.replaces_slice_id,
                repair_decision_id=item.repair_decision_id,
                generation=item.original_generation,
            )
            if success
            and item.repair
            and item.replaces_slice_id is not None
            and item.repair_decision_id is not None
            and item.original_generation is not None
            else None
        )
        if self.event_sink is not None:
            await self._emit(
                "repair.session.integrated" if success and item.repair else "integration.completed",
                {"run_id": item.run_id, "slice_id": item.slice_id, "success": success},
            )
        return RepairIntegrationResult(success, item, lineage)


__all__ = [
    "ActiveWriter",
    "FrozenGlobalRepairSession",
    "FrozenWriteScope",
    "FrozenWriteScopeOut",
    "GlobalRepairOrchestrator",
    "JointRepairAdmission",
    "RepairAttemptAdmission",
    "RepairAttemptGate",
    "RepairBrief",
    "RepairConstraints",
    "RepairDispatchRequest",
    "RepairDispatchResult",
    "RepairFailureFacts",
    "RepairHistoryEntry",
    "RepairEventSink",
    "RepairExecutionResult",
    "RepairBudgetPort",
    "RepairIntegrationResult",
    "RepairLineage",
    "RepairNavigationIndex",
    "RepairReadScope",
    "RepairSessionDispatch",
    "RepairSessionIdentity",
    "RepairSessionRunner",
    "RepairSlice",
    "SituationalSnapshot",
    "assemble_repair_brief",
    "build_repair_session_dispatch",
    "evaluate_joint_repair_dispatch",
]

"""Repair evidence, conservation facts, retry limits, and parity projections."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from types import MappingProxyType
from typing import Literal, cast
from uuid import UUID

from codemigrator.core import (
    AttributionReliability,
    CheckAction,
    CheckStatus,
    RepairEvidence,
    SliceId,
    load_resource,
)

from .checks import VerificationLayer


def _as_slice_id(value: object) -> SliceId:
    return SliceId(cast(UUID, value))


@dataclass(frozen=True)
class VerificationPolicySnapshot:
    flaky_reruns: int
    majority_required: int
    majority_total: int
    feedback_repair_limit: int
    conservation_bandwidth: tuple[float, float]
    global_repair_attempts: int
    sha256: str
    default_timeout_secs: Mapping[str, int]


VERIFICATION_POLICY_URI = "core://verification-policy/v1"
EXPECTED_VERIFICATION_POLICY_SHA256 = (
    "fd4d792a8ae408828d8e70930f203b7ada19b7333a12a9299eb56e962081c54f"
)
DEFAULT_TIMEOUT_SECS = MappingProxyType(
    {
        CheckAction.Scaffold.value: 300,
        CheckAction.Compile.value: 300,
        CheckAction.Lint.value: 300,
        CheckAction.TypeCheck.value: 300,
        CheckAction.Test.value: 120,
    }
)


@lru_cache(maxsize=1)
def load_policy_snapshot() -> VerificationPolicySnapshot:
    document = load_resource(VERIFICATION_POLICY_URI)
    if document.sha256 != EXPECTED_VERIFICATION_POLICY_SHA256:
        raise ValueError("verification policy resource digest does not match the frozen digest")
    payload = document.payload
    majority = payload["majority"]
    if not isinstance(majority, dict):
        raise ValueError("verification policy majority must be an object")
    bandwidth = payload["conservation_bandwidth"]
    if not isinstance(bandwidth, list) or len(bandwidth) != 2:
        raise ValueError("verification policy bandwidth must contain two bounds")
    snapshot = VerificationPolicySnapshot(
        flaky_reruns=int(payload["flaky_reruns"]),
        majority_required=int(majority["required"]),
        majority_total=int(majority["total"]),
        feedback_repair_limit=int(payload["feedback_repair_limit"]),
        conservation_bandwidth=(float(bandwidth[0]), float(bandwidth[1])),
        global_repair_attempts=int(payload["global_repair_attempts"]),
        sha256=document.sha256,
        default_timeout_secs=DEFAULT_TIMEOUT_SECS,
    )
    if snapshot.flaky_reruns + 1 != snapshot.majority_total:
        raise ValueError("flaky policy total must equal initial execution plus reruns")
    if snapshot.majority_required * 2 <= snapshot.majority_total:
        raise ValueError("majority policy is not a strict majority")
    if snapshot.global_repair_attempts < 1:
        raise ValueError("global repair policy must allow an initial attempt")
    if snapshot.feedback_repair_limit < 0:
        raise ValueError("feedback repair limit cannot be negative")
    if snapshot.conservation_bandwidth[0] > snapshot.conservation_bandwidth[1]:
        raise ValueError("conservation bandwidth bounds are reversed")
    return snapshot


@dataclass(frozen=True)
class RouteDecision:
    kind: Literal["DIRECT_REGENERATION", "SUPERVISOR", "SLICE_REGENERATION_EXHAUSTED"]
    slice_id: object | None = None
    generation: int | None = None


@dataclass(frozen=True)
class FailureReduction:
    """Status-priority and layer reduction applied before attribution routing."""

    status: CheckStatus
    action: CheckAction
    layer: str
    route: Literal["DIRECT_ELIGIBLE", "SUPERVISOR", "NO_FAILURE"]
    reason: str


def reduce_failure(
    *,
    status: CheckStatus,
    action: CheckAction,
    layer: VerificationLayer | str,
    error_unknown_count: int = 0,
) -> FailureReduction:
    """Reduce a check failure before any evidence can select a repair route."""

    layer_name = layer.value if isinstance(layer, VerificationLayer) else str(layer)
    if error_unknown_count < 0:
        raise ValueError("error_unknown_count cannot be negative")
    if error_unknown_count:
        return FailureReduction(status, action, layer_name, "SUPERVISOR", "error_unknown")
    if status is CheckStatus.Passed:
        return FailureReduction(status, action, layer_name, "NO_FAILURE", "passed")
    if status in {
        CheckStatus.TimedOut,
        CheckStatus.OutputLimitExceeded,
        CheckStatus.InfrastructureError,
    }:
        return FailureReduction(status, action, layer_name, "SUPERVISOR", "resource_failure")
    if action is CheckAction.Test:
        return FailureReduction(status, action, layer_name, "SUPERVISOR", "dynamic_test_failure")
    if layer_name not in {item.value for item in VerificationLayer}:
        return FailureReduction(status, action, layer_name, "SUPERVISOR", "unknown_layer")
    return FailureReduction(status, action, layer_name, "DIRECT_ELIGIBLE", "static_failure")


def build_repair_evidence(
    candidate_slice_set: Iterable[object],
    reliability: AttributionReliability,
    *,
    strong_coupling: bool = False,
    cross_generation_recurrence: bool = False,
    conservation_signal_summary: Mapping[str, object] | None = None,
    error_unknown_count: int = 0,
    coupling_evidence_complete: bool = False,
) -> RepairEvidence:
    candidates = sorted(set(candidate_slice_set), key=lambda value: str(value))
    summary = dict(conservation_signal_summary or {})
    summary["error_unknown_count"] = error_unknown_count
    summary["coupling_evidence_complete"] = coupling_evidence_complete
    return RepairEvidence(
        candidate_slice_set=[_as_slice_id(item) for item in candidates],
        reliability=reliability,
        strong_coupling=strong_coupling,
        cross_generation_recurrence=cross_generation_recurrence,
        conservation_signal_summary=summary,
    )


def choose_failure_route(
    evidence: RepairEvidence,
    *,
    generation: int = 0,
    failure: FailureReduction | None = None,
) -> RouteDecision:
    """Apply only the reliable-domain direct-route boundary."""

    if type(generation) is not int or not 0 <= generation <= 2:
        raise ValueError("candidate generation must be between 0 and 2")
    if failure is None or failure.route != "DIRECT_ELIGIBLE":
        return RouteDecision("SUPERVISOR")
    unknown_count = evidence.conservation_signal_summary.get("error_unknown_count", 0)
    if type(unknown_count) is not int or unknown_count != 0:
        return RouteDecision("SUPERVISOR")
    if evidence.conservation_signal_summary.get("coupling_evidence_complete") is not True:
        return RouteDecision("SUPERVISOR")
    if (
        evidence.reliability is AttributionReliability.Reliable
        and len(evidence.candidate_slice_set) == 1
        and not evidence.strong_coupling
        and not evidence.cross_generation_recurrence
    ):
        if generation == 2:
            return RouteDecision(
                "SLICE_REGENERATION_EXHAUSTED",
                evidence.candidate_slice_set[0],
                generation,
            )
        return RouteDecision("DIRECT_REGENERATION", evidence.candidate_slice_set[0], generation)
    return RouteDecision("SUPERVISOR")


class GlobalRepairBudget:
    """A bounded, new-evidence-driven global repair attempt counter."""

    def __init__(self, max_attempts: int | None = None) -> None:
        limit = (
            max_attempts
            if max_attempts is not None
            else load_policy_snapshot().global_repair_attempts
        )
        if type(limit) is not int or limit < 1:
            raise ValueError("max_attempts must be a positive integer")
        self._max_attempts = limit
        self._attempts = 0
        self._evidence: set[str] = set()

    @property
    def attempts(self) -> int:
        return self._attempts

    @property
    def max_attempts(self) -> int:
        return self._max_attempts

    @property
    def exhausted(self) -> bool:
        return self._attempts >= self._max_attempts

    def record(self, evidence_key: str) -> int | None:
        if not evidence_key:
            raise ValueError("evidence_key must be non-empty")
        if self.exhausted or evidence_key in self._evidence:
            return None
        self._evidence.add(evidence_key)
        self._attempts += 1
        return self._attempts


class FeedbackRepairBudget(GlobalRepairBudget):
    """The per-generation local feedback limit from the frozen policy."""

    def __init__(self) -> None:
        super().__init__(load_policy_snapshot().feedback_repair_limit)


@dataclass(frozen=True)
class CollectionCompleteness:
    module: object
    source_tests: int
    target_tests: int
    suspicious: bool


def check_collection_completeness(
    source_tests: Mapping[object, int], target_tests: Mapping[object, int]
) -> tuple[CollectionCompleteness, ...]:
    """Surface a suspiciously smaller target collection for reporting."""

    modules = sorted(set(source_tests) | set(target_tests), key=lambda value: str(value))
    return tuple(
        CollectionCompleteness(
            module=module,
            source_tests=source_tests.get(module, 0),
            target_tests=target_tests.get(module, 0),
            suspicious=target_tests.get(module, 0) < source_tests.get(module, 0),
        )
        for module in modules
    )


@dataclass(frozen=True)
class ConfidenceAssessment:
    primary_evidence: Literal["TRANSLATED_TESTS", "GENERATED_TESTS"]
    downgraded: bool
    disclosure: str | None = None
    usable_as_primary: bool = True
    source_smoke_passed: bool | None = None


def assess_confidence(
    *,
    source_has_tests: bool,
    source_smoke_passed: bool | None = None,
    generated: bool = False,
    low_quality: bool = False,
    generated_assessment: object | None = None,
) -> ConfidenceAssessment:
    """Select the two evidence tiers without changing execution strictness."""

    if generated_assessment is not None:
        generated = bool(getattr(generated_assessment, "generated", generated))
        low_quality = bool(getattr(generated_assessment, "low_quality", low_quality))

    if source_has_tests:
        downgraded = source_smoke_passed is False
        return ConfidenceAssessment(
            "TRANSLATED_TESTS",
            downgraded,
            disclosure=("source baseline smoke verification failed" if downgraded else None),
            usable_as_primary=True,
            source_smoke_passed=source_smoke_passed,
        )
    return ConfidenceAssessment(
        "GENERATED_TESTS",
        True,
        disclosure=(
            "generated tests are below the LOW_QUALITY gate and cannot be primary evidence"
            if low_quality
            else "generated tests establish self-consistency with the Agent's source understanding"
        ),
        usable_as_primary=generated and not low_quality if generated else not low_quality,
        source_smoke_passed=source_smoke_passed,
    )


@dataclass(frozen=True)
class TerminalClassification:
    status: Literal["PARTIALLY_COMPLETED", "FAILED", "NON_TERMINAL"]
    reason: Literal["INDEPENDENT_SLICE", "VERIFICATION_TERMINAL", "REPAIR_IN_PROGRESS"]


def classify_terminal_failure(
    *, independent_slice: bool, budget: GlobalRepairBudget | None = None
) -> TerminalClassification:
    """Apply the terminal boundary only after the global repair budget is exhausted."""

    if budget is None or not budget.exhausted:
        return TerminalClassification("NON_TERMINAL", "REPAIR_IN_PROGRESS")
    if independent_slice:
        return TerminalClassification("PARTIALLY_COMPLETED", "INDEPENDENT_SLICE")
    return TerminalClassification("FAILED", "VERIFICATION_TERMINAL")


@dataclass(frozen=True)
class ModuleConservation:
    module: object
    test_ratio: float | None
    assertion_ratio: float | None
    loc_ratio: float | None
    outlier: bool
    source_test_count: int | None = None
    source_assertion_count: int | None = None
    source_loc_count: int | None = None
    target_test_count: int = 0
    target_assertion_count: int = 0
    target_loc_count: int = 0
    test_outlier: bool | None = None
    assertion_outlier: bool | None = None
    loc_outlier: bool | None = None


@dataclass(frozen=True)
class StructuralConservationFacts:
    per_module: tuple[ModuleConservation, ...]

    @property
    def has_outlier(self) -> bool:
        return any(item.outlier for item in self.per_module)

    @property
    def has_comparable_baseline(self) -> bool:
        return any(
            ratio is not None
            for item in self.per_module
            for ratio in (item.test_ratio, item.assertion_ratio, item.loc_ratio)
        )

    @property
    def has_test_or_assertion_outlier(self) -> bool:
        return any(
            item.test_outlier or item.assertion_outlier
            if item.test_outlier is not None or item.assertion_outlier is not None
            else item.outlier and (item.test_ratio is not None or item.assertion_ratio is not None)
            for item in self.per_module
        )

    @property
    def zero_baseline_modules(self) -> tuple[object, ...]:
        return tuple(
            item.module
            for item in self.per_module
            if item.source_test_count in (None, 0)
            or item.source_assertion_count in (None, 0)
            or item.source_loc_count in (None, 0)
        )


def _ratio(target: int, source: int | None) -> float | None:
    return None if source is None or source == 0 else target / source


def structural_conservation(
    source_counts: Mapping[object, tuple[int | None, int | None, int | None]],
    target_counts: Mapping[object, tuple[int, int, int]],
    *,
    bandwidth: tuple[float, float] | None = None,
) -> StructuralConservationFacts:
    """Compute stable ratios without treating conservation as pass/fail."""

    low, high = bandwidth or load_policy_snapshot().conservation_bandwidth
    modules = sorted(set(source_counts) | set(target_counts), key=lambda value: str(value))
    facts: list[ModuleConservation] = []
    for module in modules:
        source = source_counts.get(module, (0, 0, 0))
        target = target_counts.get(module, (0, 0, 0))
        ratios = tuple(
            _ratio(destination, baseline) for destination, baseline in zip(target, source)
        )
        outlier = any(ratio is not None and not low <= ratio <= high for ratio in ratios)
        outliers = tuple(
            ratio is not None and not low <= ratio <= high for ratio in ratios
        )
        facts.append(
            ModuleConservation(
                module,
                ratios[0],
                ratios[1],
                ratios[2],
                outlier,
                source[0],
                source[1],
                source[2],
                target[0],
                target[1],
                target[2],
                outliers[0],
                outliers[1],
                outliers[2],
            )
        )
    return StructuralConservationFacts(tuple(facts))


@dataclass(frozen=True)
class ConservationDecision:
    kind: Literal["TEST_TRANSLATION", "IMPLEMENTATION", "SUPERVISOR"]
    slice_id: object | None = None


def assist_ambiguous_failure(
    facts: StructuralConservationFacts | None,
    test_slice: object,
    implementation_slice: object,
) -> ConservationDecision:
    """Use conservation only as a third signal for a fuzzy failure."""

    if facts is None or not facts.per_module or not facts.has_comparable_baseline:
        return ConservationDecision("SUPERVISOR")
    if facts.has_test_or_assertion_outlier:
        return ConservationDecision("TEST_TRANSLATION", test_slice)
    return ConservationDecision("IMPLEMENTATION", implementation_slice)


@dataclass(frozen=True)
class ParityResult:
    scenario_id: str
    status: Literal["PASSED", "FAILED"]
    diff_summary: str


@dataclass(frozen=True)
class ParityEvidence:
    available: bool
    results: tuple[ParityResult, ...] = ()
    disclosure: str | None = None


def parity_compare(
    scenario_ids: Sequence[str],
    source_summaries: Mapping[str, str] | None,
    target_summaries: Mapping[str, str] | None,
    runtime_image_digest: str | None = None,
) -> ParityEvidence:
    """Compare confirmed scenario summaries; absence is explicitly disclosed."""

    if (
        not scenario_ids
        or source_summaries is None
        or target_summaries is None
        or runtime_image_digest is None
    ):
        return ParityEvidence(False, disclosure="parity_unavailable")
    results = tuple(
        ParityResult(
            scenario_id=scenario_id,
            status=(
                "PASSED"
                if scenario_id in source_summaries
                and scenario_id in target_summaries
                and source_summaries[scenario_id] == target_summaries[scenario_id]
                else "FAILED"
            ),
            diff_summary=(
                "equal"
                if scenario_id in source_summaries
                and scenario_id in target_summaries
                and source_summaries[scenario_id] == target_summaries[scenario_id]
                else "source and target summaries differ"
            ),
        )
        for scenario_id in sorted(set(scenario_ids), key=lambda value: value.encode("utf-8"))
    )
    return ParityEvidence(True, results)


BOUNDARY_DECLARATIONS = (
    "behavioral equivalence is bounded by source test coverage",
    "performance equivalence is outside the primary proof scope",
    "security equivalence is outside the primary proof scope",
    "ecosystem convention equivalence is outside the primary proof scope",
    "same-source implementation and test misunderstandings can form a collusion blind spot",
)

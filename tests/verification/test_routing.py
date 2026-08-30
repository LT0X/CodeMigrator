from __future__ import annotations

from codemigrator.core import AttributionReliability, SliceId
from codemigrator.verification.routing import (
    GlobalRepairBudget,
    ModuleConservation,
    StructuralConservationFacts,
    assess_confidence,
    assist_ambiguous_failure,
    build_repair_evidence,
    check_collection_completeness,
    choose_failure_route,
    classify_terminal_failure,
    load_policy_snapshot,
    parity_compare,
)


def test_reliable_unique_evidence_direct_routes_unless_recurrent_or_coupled() -> None:
    slice_id = SliceId("00000000-0000-0000-0000-000000000001")
    evidence = build_repair_evidence((slice_id,), AttributionReliability.Reliable)
    assert choose_failure_route(evidence).kind == "DIRECT_REGENERATION"
    assert (
        choose_failure_route(evidence.model_copy(update={"strong_coupling": True})).kind
        == "SUPERVISOR"
    )
    assert (
        choose_failure_route(evidence.model_copy(update={"cross_generation_recurrence": True})).kind
        == "SUPERVISOR"
    )


def test_global_repair_budget_allows_three_distinct_evidence_attempts() -> None:
    budget = GlobalRepairBudget(max_attempts=load_policy_snapshot().global_repair_attempts)
    assert [budget.record("evidence-" + str(i)) for i in range(3)] == [1, 2, 3]
    assert budget.record("evidence-4") is None
    assert budget.record("evidence-2") is None
    assert budget.exhausted is True


def test_conservation_is_auxiliary_and_has_three_fallback_branches() -> None:
    implementation = SliceId("00000000-0000-0000-0000-000000000001")
    tests = SliceId("00000000-0000-0000-0000-000000000002")
    outlier = StructuralConservationFacts(
        per_module=(ModuleConservation("m", 0.4, 1.0, 1.0, True),)
    )
    normal = StructuralConservationFacts(
        per_module=(ModuleConservation("m", 1.0, 1.0, 1.0, False),)
    )
    assert assist_ambiguous_failure(outlier, tests, implementation).kind == "TEST_TRANSLATION"
    assert assist_ambiguous_failure(normal, tests, implementation).kind == "IMPLEMENTATION"
    assert assist_ambiguous_failure(None, tests, implementation).kind == "SUPERVISOR"
    zero_baseline = StructuralConservationFacts(
        per_module=(ModuleConservation("m", None, None, None, False),)
    )
    assert assist_ambiguous_failure(zero_baseline, tests, implementation).kind == "SUPERVISOR"


def test_parity_is_optional_and_does_not_change_main_verdict() -> None:
    absent = parity_compare([], None, None)
    assert absent.available is False
    present = parity_compare(
        ["scenario-1"],
        {"scenario-1": "same"},
        {"scenario-1": "same"},
        runtime_image_digest="a" * 64,
    )
    assert present.available is True
    assert present.results[0].status == "PASSED"
    different = parity_compare(
        ["scenario-1"],
        {"scenario-1": "source"},
        {"scenario-1": "target"},
        runtime_image_digest="a" * 64,
    )
    assert different.results[0].status == "FAILED"


def test_collection_and_confidence_are_report_evidence_only() -> None:
    facts = check_collection_completeness({"m": 3}, {"m": 2})
    assert facts[0].suspicious is True
    assert assess_confidence(source_has_tests=True).downgraded is False
    assert assess_confidence(source_has_tests=False).downgraded is True
    assert classify_terminal_failure(independent_slice=True).status == "PARTIALLY_COMPLETED"
    assert classify_terminal_failure(independent_slice=False).status == "FAILED"

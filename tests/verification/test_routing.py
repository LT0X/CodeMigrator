from __future__ import annotations

from codemigrator.core import AttributionReliability, CheckAction, CheckStatus, SliceId
from codemigrator.verification.routing import (
    FailureReduction,
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
    reduce_failure,
    structural_conservation,
)


def test_reliable_unique_evidence_direct_routes_unless_recurrent_or_coupled() -> None:
    slice_id = SliceId("00000000-0000-0000-0000-000000000001")
    evidence = build_repair_evidence(
        (slice_id,), AttributionReliability.Reliable, coupling_evidence_complete=True
    )
    static_failure = reduce_failure(
        status=CheckStatus.Failed,
        action=CheckAction.Compile,
        layer="LOCAL",
    )
    assert choose_failure_route(evidence, failure=static_failure).kind == "DIRECT_REGENERATION"
    assert choose_failure_route(evidence).kind == "SUPERVISOR"
    assert (
        choose_failure_route(
            evidence.model_copy(update={"strong_coupling": True}), failure=static_failure
        ).kind
        == "SUPERVISOR"
    )
    assert (
        choose_failure_route(
            evidence.model_copy(update={"cross_generation_recurrence": True}),
            failure=static_failure,
        ).kind
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


def test_conservation_retains_zero_baseline_counts_and_loc_only_outlier_is_not_test_route() -> None:
    implementation = SliceId("00000000-0000-0000-0000-000000000001")
    tests = SliceId("00000000-0000-0000-0000-000000000002")
    facts = structural_conservation(
        {"m": (0, 0, 10)},
        {"m": (4, 3, 30)},
        bandwidth=(0.5, 2.0),
    )
    module = facts.per_module[0]
    assert (module.target_test_count, module.target_assertion_count, module.target_loc_count) == (
        4,
        3,
        30,
    )
    assert assist_ambiguous_failure(facts, tests, implementation).kind == "IMPLEMENTATION"


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
    assert assess_confidence(source_has_tests=True, source_smoke_passed=False).downgraded is True
    assert assess_confidence(source_has_tests=False).downgraded is True
    budget = GlobalRepairBudget(max_attempts=1)
    assert (
        classify_terminal_failure(independent_slice=True, budget=budget).status == "NON_TERMINAL"
    )
    budget.record("evidence")
    assert (
        classify_terminal_failure(independent_slice=True, budget=budget).status
        == "PARTIALLY_COMPLETED"
    )
    assert classify_terminal_failure(independent_slice=False, budget=budget).status == "FAILED"


def test_failure_reduction_applies_status_layer_and_unknown_priority_before_routing() -> None:
    static = reduce_failure(
        status=CheckStatus.Failed,
        action=CheckAction.Compile,
        layer="LOCAL",
        error_unknown_count=0,
    )
    assert isinstance(static, FailureReduction)
    assert static.route == "DIRECT_ELIGIBLE"
    assert reduce_failure(
        status=CheckStatus.TimedOut,
        action=CheckAction.Compile,
        layer="LOCAL",
    ).route == "SUPERVISOR"
    assert reduce_failure(
        status=CheckStatus.Failed,
        action=CheckAction.Test,
        layer="FINAL",
    ).route == "SUPERVISOR"
    assert reduce_failure(
        status=CheckStatus.Failed,
        action=CheckAction.Compile,
        layer="LOCAL",
        error_unknown_count=1,
    ).route == "SUPERVISOR"


def test_unknown_and_incomplete_coupling_evidence_cannot_direct_route() -> None:
    slice_id = SliceId("00000000-0000-0000-0000-000000000001")
    unknown = build_repair_evidence(
        (slice_id,),
        AttributionReliability.Reliable,
        coupling_evidence_complete=True,
        error_unknown_count=1,
    )
    assert choose_failure_route(unknown).kind == "SUPERVISOR"
    incomplete = build_repair_evidence((slice_id,), AttributionReliability.Reliable)
    assert choose_failure_route(incomplete).kind == "SUPERVISOR"


def test_low_quality_generated_tests_are_not_primary_evidence() -> None:
    assessment = assess_confidence(
        source_has_tests=False,
        generated=True,
        low_quality=True,
    )
    assert assessment.usable_as_primary is False


def test_policy_snapshot_freezes_digest_and_default_timeouts() -> None:
    snapshot = load_policy_snapshot()
    assert snapshot.default_timeout_secs[CheckAction.Compile.value] == 300
    assert snapshot.default_timeout_secs[CheckAction.Test.value] == 120
    assert len(snapshot.sha256) == 64


def test_boundary_declarations_include_same_source_collusion_blind_spot() -> None:
    from codemigrator.verification.routing import BOUNDARY_DECLARATIONS

    assert any("same-source" in declaration for declaration in BOUNDARY_DECLARATIONS)

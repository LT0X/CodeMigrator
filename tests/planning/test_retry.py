from __future__ import annotations

import pytest

from codemigrator.core import StableErrorCode
from codemigrator.planning import (
    PlanFailed,
    PlanLedger,
    PlanProposal,
    PlanRetryReducer,
    PlanValidation,
    PlanViolation,
    ProviderPhysicalFailure,
)


def test_feedback_retry_runs_initial_attempt_plus_three_retries_then_plan_failed(
    planning_inputs: object,
) -> None:
    attempts: list[tuple[object, ...]] = []
    invalid = PlanProposal(slices=(), edges=(), integration_ranks={}, planner_rationale=())

    def proposer(feedback: tuple[object, ...]) -> PlanProposal:
        attempts.append(feedback)
        return invalid

    with pytest.raises(PlanFailed) as raised:
        PlanRetryReducer(max_retries=3).run(
            proposer,
            validator=lambda proposal: PlanValidation(
                accepted=False,
                violations=(
                    PlanViolation(
                        code=StableErrorCode.PLAN_PROPOSAL_INVALID,
                        pointer="/slices",
                        message="invalid proposal",
                    ),
                ),
                checked_scope_pairs=0,
                cycle_check="PASS",
                blueprint_check="PASS",
                rank_check="PASS",
                size_check="PASS",
            ),
            inputs=planning_inputs,
            ledger=PlanLedger(),
        )

    assert getattr(raised.value, "code", None) is StableErrorCode.PLAN_PROPOSAL_INVALID
    assert len(attempts) == 4


def test_provider_physical_failure_is_not_converted_to_feedback_retry(
    planning_inputs: object,
) -> None:
    attempts = 0

    def proposer(feedback: tuple[object, ...]) -> PlanProposal:
        nonlocal attempts
        attempts += 1
        raise ProviderPhysicalFailure("provider unavailable")

    with pytest.raises(ProviderPhysicalFailure):
        PlanRetryReducer(max_retries=3).run(
            proposer,
            validator=lambda proposal: None,
            inputs=planning_inputs,
            ledger=PlanLedger(),
        )

    assert attempts == 1

"""Feedback retry reduction for the pure planning boundary."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from codemigrator.core import StableErrorCode

from .models import PlanningInputs, PlanProposal, PlanValidation, PlanViolation
from .validator import PlanLedger


class ProviderPhysicalFailure(RuntimeError):
    """A transport/provider failure owned by runtime physical retry policy."""


class PlanFailed(RuntimeError):
    """Feedback retries were exhausted without a valid plan."""

    def __init__(self, validation: PlanValidation, attempts: int) -> None:
        self.validation = validation
        self.attempts = attempts
        self.code = (
            validation.violations[0].code
            if validation.violations
            else StableErrorCode.PLAN_PROPOSAL_INVALID
        )
        super().__init__(f"planning failed after {attempts} attempts: {self.code.value}")


class PlanRetryReducer:
    """Retry only proposal/schema/guard failures and never persist rejected plans."""

    def __init__(self, *, max_retries: int = 3) -> None:
        if type(max_retries) is not int or max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer")
        self.max_retries = max_retries

    def run(
        self,
        proposer: Callable[[tuple[PlanViolation, ...]], object],
        *,
        validator: Callable[[PlanProposal], PlanValidation] | object,
        inputs: PlanningInputs,
        ledger: PlanLedger,
    ) -> Any:
        feedback: tuple[PlanViolation, ...] = ()
        retries = 0
        while True:
            try:
                raw_proposal = proposer(feedback)
                proposal = (
                    raw_proposal
                    if isinstance(raw_proposal, PlanProposal)
                    else PlanProposal.model_validate(raw_proposal)
                )
                validation = (
                    validator.validate(proposal, inputs)
                    if hasattr(validator, "validate")
                    else validator(proposal)  # type: ignore[operator]
                )
            except ValidationError as exc:
                validation = _schema_failure(str(exc))
            except (TypeError, ValueError) as exc:
                validation = _schema_failure(str(exc))

            if validation.accepted:
                return ledger.freeze(proposal, inputs)
            if retries >= self.max_retries:
                raise PlanFailed(validation, attempts=retries + 1)
            retries += 1
            feedback = validation.violations


def _schema_failure(message: str) -> PlanValidation:
    violation = PlanViolation(
        code=StableErrorCode.PLAN_PROPOSAL_INVALID,
        pointer="/",
        message=message or "proposal schema is invalid",
    )
    return PlanValidation(
        accepted=False,
        violations=(violation,),
        checked_scope_pairs=0,
        cycle_check="NOT_RUN",
        blueprint_check="NOT_RUN",
        rank_check="NOT_RUN",
        size_check="NOT_RUN",
    )


__all__ = ["PlanFailed", "PlanRetryReducer", "ProviderPhysicalFailure"]

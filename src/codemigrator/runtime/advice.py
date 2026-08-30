"""Mechanical validation and two-level adoption of judgement-layer advice."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any

from codemigrator.core import Advice, AdviceKind, canonical_json_bytes


class AdviceDisposition(str, Enum):
    AutoAdopted = "AUTO_ADOPTED"
    ConfirmationRequired = "CONFIRMATION_REQUIRED"
    Discarded = "DISCARDED"


@dataclass(frozen=True, slots=True)
class AdviceValidationContext:
    expected_subjects: frozenset[str] = frozenset()
    attribution_candidates: frozenset[uuid.UUID] = frozenset()
    joint_domain_members: frozenset[uuid.UUID] = frozenset()
    max_fanout: int = 3

    def __post_init__(self) -> None:
        if type(self.max_fanout) is not int or self.max_fanout < 1:
            raise ValueError("max_fanout must be a positive integer")


@dataclass(frozen=True, slots=True)
class AdviceValidationResult:
    disposition: AdviceDisposition
    reason: str
    proposal_hash: str


def _json_safe(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    return value


def advice_proposal_hash(advice: Advice) -> str:
    """Return SHA-256 over the canonical advice identity and payload."""

    canonical = {
        "advice_id": str(advice.advice_id),
        "kind": advice.kind.value,
        "run_id": str(advice.run_id),
        "role": advice.role.value,
        "payload": _json_safe(advice.payload),
    }
    return hashlib.sha256(canonical_json_bytes(canonical)).hexdigest()


def _uuid_set(value: object) -> frozenset[uuid.UUID] | None:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return None
    result: set[uuid.UUID] = set()
    for item in value:
        try:
            result.add(item if isinstance(item, uuid.UUID) else uuid.UUID(str(item)))
        except (TypeError, ValueError, AttributeError):
            return None
    return frozenset(result)


def _validate_explore(advice: Advice, context: AdviceValidationContext) -> str | None:
    assignments = advice.payload.get("assignments")
    if not isinstance(assignments, dict):
        return "ASSIGNMENTS_REQUIRED"
    if not context.expected_subjects:
        return "EXPECTED_SUBJECTS_REQUIRED"
    if set(assignments) != set(context.expected_subjects):
        return "ASSIGNMENT_COVERAGE_MISMATCH"
    if any(
        not isinstance(value, (str, uuid.UUID)) or not str(value)
        for value in assignments.values()
    ):
        return "ASSIGNMENT_TARGET_REQUIRED"
    fanout: dict[str, int] = {}
    for target in assignments.values():
        key = str(target)
        fanout[key] = fanout.get(key, 0) + 1
    if max(fanout.values(), default=0) > context.max_fanout:
        return "ASSIGNMENT_FANOUT_EXCEEDED"
    return None


def _validate_repair(advice: Advice, context: AdviceValidationContext) -> str | None:
    repair_set = _uuid_set(advice.payload.get("repair_set"))
    if repair_set is None:
        return "REPAIR_SET_REQUIRED"
    if not repair_set:
        return "REPAIR_SET_REQUIRED"
    if not context.attribution_candidates:
        return "ATTRIBUTION_CANDIDATES_REQUIRED"
    if not repair_set.issubset(context.attribution_candidates):
        return "REPAIR_SET_NOT_IN_CANDIDATES"
    if context.joint_domain_members and repair_set != context.joint_domain_members:
        return "JOINT_DOMAIN_MISMATCH"
    return None


def evaluate_advice(advice: Advice, context: AdviceValidationContext) -> AdviceValidationResult:
    """Classify advice without performing any state or workspace write."""

    expected_hash = advice_proposal_hash(advice)
    if not hmac.compare_digest(str(advice.proposal_hash), expected_hash):
        return AdviceValidationResult(
            AdviceDisposition.Discarded, "PROPOSAL_HASH_MISMATCH", expected_hash
        )
    if advice.kind in {AdviceKind.RouteSuggestion, AdviceKind.PlanRevision, AdviceKind.AskUser}:
        return AdviceValidationResult(
            AdviceDisposition.ConfirmationRequired, "BOUNDARY_ADVICE", expected_hash
        )
    if advice.kind is AdviceKind.ExploreReassignment:
        reason = _validate_explore(advice, context)
    else:
        reason = _validate_repair(advice, context)
    if reason is not None:
        return AdviceValidationResult(AdviceDisposition.Discarded, reason, expected_hash)
    return AdviceValidationResult(
        AdviceDisposition.AutoAdopted, "MECHANICALLY_VALID", expected_hash
    )


__all__ = [
    "AdviceDisposition",
    "AdviceValidationContext",
    "AdviceValidationResult",
    "advice_proposal_hash",
    "evaluate_advice",
]

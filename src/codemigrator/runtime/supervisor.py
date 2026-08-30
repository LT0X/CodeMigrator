"""Immutable contracts for the event-triggered Supervisor judgement layer."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from codemigrator.core import Advice, AdviceKind, RepairEvidence, canonical_json_bytes

from .advice import advice_proposal_hash
from .context import ContextEnvelope, ContextSegment
from .contracts import EventSpec


class SupervisorAdviceKind(str, Enum):
    RepairDecision = "REPAIR_DECISION"
    RouteSuggestion = "ROUTE_SUGGESTION"


_TRIGGER_REASONS = {
    SupervisorAdviceKind.RepairDecision: "AMBIGUOUS_ATTRIBUTION_CANDIDATES",
    SupervisorAdviceKind.RouteSuggestion: "SLICE_SESSION_STOPPED",
}
_ADOPTION_RESULTS = frozenset(
    {
        "AUTO_ADOPTED",
        "CONFIRMATION_REQUIRED",
        "DISCARDED",
        "DUPLICATE",
        "MECHANICAL_REDUCTION",
        "REJECTED",
        "RUN_ID_MISMATCH",
        "CONFIRMED",
    }
)


def _text_tuple(value: object, name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, tuple | list):
        raise TypeError(f"{name} must be a sequence of strings")
    result = tuple(value)
    if any(not isinstance(item, str) or not item for item in result):
        raise ValueError(f"{name} must contain non-empty strings")
    return result


def _uuid_text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a UUID string")
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{name} must be a UUID string") from exc


@dataclass(frozen=True, slots=True)
class SupervisorTrigger:
    kind: SupervisorAdviceKind
    reason: str
    trigger_event_refs: tuple[str, ...] = ()
    target_slice_id: str | None = None

    def __post_init__(self) -> None:
        try:
            kind = (
                self.kind
                if isinstance(self.kind, SupervisorAdviceKind)
                else SupervisorAdviceKind(self.kind)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("unsupported Supervisor advice kind") from exc
        object.__setattr__(self, "kind", kind)
        if self.reason != _TRIGGER_REASONS[kind]:
            raise ValueError("unsupported Supervisor trigger reason")
        object.__setattr__(
            self,
            "trigger_event_refs",
            _text_tuple(self.trigger_event_refs, "trigger_event_refs"),
        )
        if self.target_slice_id is not None:
            object.__setattr__(
                self,
                "target_slice_id",
                _uuid_text(self.target_slice_id, "target_slice_id"),
            )


@dataclass(frozen=True, slots=True)
class SupervisorProjection:
    """The four directed evidence groups admitted to one Supervisor turn."""

    repair_evidence: RepairEvidence | None
    failed_test_refs: tuple[str, ...]
    diagnostic_summary: Mapping[str, object]
    slice_states: Mapping[str, str]
    prior_repair_decision_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.repair_evidence is not None and not isinstance(
            self.repair_evidence, RepairEvidence
        ):
            raise TypeError("repair_evidence must use the core RepairEvidence model")
        object.__setattr__(
            self, "failed_test_refs", _text_tuple(self.failed_test_refs, "failed_test_refs")
        )
        object.__setattr__(
            self,
            "prior_repair_decision_refs",
            _text_tuple(self.prior_repair_decision_refs, "prior_repair_decision_refs"),
        )
        if not isinstance(self.diagnostic_summary, Mapping):
            raise TypeError("diagnostic_summary must be a mapping")
        if not isinstance(self.slice_states, Mapping):
            raise TypeError("slice_states must be a mapping")
        forbidden = {"baseline", "machine_baseline", "event_stream", "source_text", "prompt"}
        if forbidden.intersection(self.diagnostic_summary):
            raise ValueError("Supervisor projection cannot contain forbidden context groups")
        if any(not isinstance(key, str) or not key for key in self.slice_states):
            raise ValueError("slice state keys must be non-empty strings")
        if any(not isinstance(value, str) or not value for value in self.slice_states.values()):
            raise ValueError("slice state values must be non-empty strings")
        object.__setattr__(
            self, "diagnostic_summary", MappingProxyType(dict(self.diagnostic_summary))
        )
        object.__setattr__(self, "slice_states", MappingProxyType(dict(self.slice_states)))

    def to_context(self) -> ContextEnvelope:
        payload = {
            "kind": "SUPERVISOR_DIRECTED_EVIDENCE",
            "repair_evidence": (
                self.repair_evidence.model_dump(mode="json")
                if self.repair_evidence is not None
                else None
            ),
            "failed_test_refs": list(self.failed_test_refs),
            "diagnostic_summary": dict(self.diagnostic_summary),
            "slice_states": dict(self.slice_states),
            "prior_repair_decision_refs": list(self.prior_repair_decision_refs),
        }
        content = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return ContextEnvelope(targeted=(ContextSegment("targeted", content),))


class SuggestedRoute(str, Enum):
    DelegateRegen = "delegate_regen"
    GlobalRepair = "global_repair"
    TerminalFail = "terminal_fail"
    Clarify = "clarify"


@dataclass(frozen=True, slots=True)
class RouteSuggestion:
    trigger_event_refs: tuple[str, ...]
    failure_class: str
    suggested_route: SuggestedRoute
    target_slice_id: str | None
    rationale: str

    def __post_init__(self) -> None:
        refs = _text_tuple(self.trigger_event_refs, "trigger_event_refs")
        if not refs:
            raise ValueError("trigger_event_refs must not be empty")
        object.__setattr__(self, "trigger_event_refs", refs)
        if not isinstance(self.failure_class, str) or not self.failure_class.strip():
            raise ValueError("failure_class must be non-empty text")
        try:
            route = (
                self.suggested_route
                if isinstance(self.suggested_route, SuggestedRoute)
                else SuggestedRoute(self.suggested_route)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("unsupported suggested route") from exc
        object.__setattr__(self, "suggested_route", route)
        if self.target_slice_id is not None:
            object.__setattr__(
                self,
                "target_slice_id",
                _uuid_text(self.target_slice_id, "target_slice_id"),
            )
        if not isinstance(self.rationale, str) or not self.rationale.strip():
            raise ValueError("rationale must be non-empty text")

    def to_payload(self) -> dict[str, object]:
        return {
            "trigger_event_refs": list(self.trigger_event_refs),
            "failure_class": self.failure_class,
            "suggested_route": self.suggested_route.value,
            "target_slice_id": self.target_slice_id,
            "rationale": self.rationale,
        }


def _payload_summary(advice: Advice) -> dict[str, object]:
    encoded = canonical_json_bytes(advice.payload)
    return {
        "keys": sorted(str(key) for key in advice.payload),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "bytes": len(encoded),
    }


def _require_valid_hash(advice: Advice) -> None:
    expected = advice_proposal_hash(advice)
    if not hmac.compare_digest(str(advice.proposal_hash), expected):
        raise ValueError("Advice proposal_hash is not canonical")


def _tier(advice: Advice) -> str:
    return (
        "CONSTRAINT_INTERNAL" if advice.kind is AdviceKind.RepairDecision else "BOUNDARY_REFERENCE"
    )


def build_proposed_event(advice: Advice) -> EventSpec:
    """Build a redacted proposal event without storing Advice payload content."""

    _require_valid_hash(advice)
    return EventSpec(
        "advice.proposed",
        {
            "advice_id": str(advice.advice_id),
            "kind": advice.kind.value,
            "role": advice.role.value,
            "tier": _tier(advice),
            "proposal_hash": str(advice.proposal_hash),
            "payload_summary": _payload_summary(advice),
        },
    )


def build_repair_decision_event(advice: Advice) -> EventSpec:
    """Build the compact repair-decision event owned by the runtime boundary."""

    if advice.kind is not AdviceKind.RepairDecision:
        raise ValueError("repair.decision requires RepairDecision advice")
    _require_valid_hash(advice)
    repair_set = advice.payload.get("repair_set")
    domain_split = advice.payload.get("domain_split")
    decision_id = advice.payload.get("decision_id")
    if (
        not isinstance(repair_set, list)
        or not isinstance(domain_split, dict)
        or not isinstance(decision_id, str)
        or not decision_id
    ):
        raise ValueError("RepairDecision payload is incomplete")
    repair_summary = canonical_json_bytes(repair_set)
    domain_summary = canonical_json_bytes(domain_split)
    return EventSpec(
        "repair.decision",
        {
            "repair_decision_id": decision_id,
            "repair_set_summary": {
                "count": len(repair_set),
                "sha256": hashlib.sha256(repair_summary).hexdigest(),
            },
            "domain_allocation_summary": {
                "slice_count": len(domain_split),
                "sha256": hashlib.sha256(domain_summary).hexdigest(),
            },
        },
    )


def build_adopted_event(advice: Advice, adoption_result: str, impact_summary: str) -> EventSpec:
    """Build a redacted actor-owned adoption event; Supervisor never emits it."""

    if adoption_result not in _ADOPTION_RESULTS:
        raise ValueError("adoption_result must be a known status")
    if not isinstance(impact_summary, str):
        raise TypeError("impact_summary must be text")
    _require_valid_hash(advice)
    encoded = impact_summary.encode("utf-8")
    return EventSpec(
        "advice.adopted",
        {
            "advice_id": str(advice.advice_id),
            "proposal_hash": str(advice.proposal_hash),
            "adoption_result": adoption_result,
            "impact_summary": {
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "bytes": len(encoded),
            },
        },
    )


def supervisor_triggers(
    *,
    candidate_slice_ids: frozenset[str],
    session_failed_and_stopped: bool,
    dynamic_tests_all_failed: bool | None = None,
    trigger_event_refs: tuple[str, ...] = (),
    target_slice_id: str | None = None,
) -> tuple[SupervisorTrigger, ...]:
    """Return only the two mechanically defined trigger conditions."""

    if dynamic_tests_all_failed is not None and type(dynamic_tests_all_failed) is not bool:
        raise TypeError("dynamic_tests_all_failed must be a boolean when supplied")
    triggers: list[SupervisorTrigger] = []
    repair_triggered = len(candidate_slice_ids) > 1 and (
        dynamic_tests_all_failed is None or dynamic_tests_all_failed
    )
    if repair_triggered:
        triggers.append(
            SupervisorTrigger(
                SupervisorAdviceKind.RepairDecision,
                "AMBIGUOUS_ATTRIBUTION_CANDIDATES",
                trigger_event_refs=trigger_event_refs,
            )
        )
    if session_failed_and_stopped:
        triggers.append(
            SupervisorTrigger(
                SupervisorAdviceKind.RouteSuggestion,
                "SLICE_SESSION_STOPPED",
                trigger_event_refs=trigger_event_refs,
                target_slice_id=target_slice_id,
            )
        )
    return tuple(triggers)


__all__ = [
    "RouteSuggestion",
    "SuggestedRoute",
    "SupervisorAdviceKind",
    "SupervisorProjection",
    "SupervisorTrigger",
    "build_adopted_event",
    "build_proposed_event",
    "build_repair_decision_event",
    "supervisor_triggers",
]

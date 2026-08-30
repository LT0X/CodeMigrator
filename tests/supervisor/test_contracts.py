from __future__ import annotations

from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest

from codemigrator.core import AttributionReliability, RepairEvidence
from codemigrator.runtime.supervisor import (
    SupervisorAdviceKind,
    SupervisorProjection,
    SupervisorTrigger,
    supervisor_triggers,
)


def test_only_two_event_trigger_reasons_are_admitted() -> None:
    triggers = supervisor_triggers(
        candidate_slice_ids=frozenset({"a", "b"}), session_failed_and_stopped=True
    )

    assert [trigger.kind for trigger in triggers] == [
        SupervisorAdviceKind.RepairDecision,
        SupervisorAdviceKind.RouteSuggestion,
    ]
    assert {trigger.reason for trigger in triggers} == {
        "AMBIGUOUS_ATTRIBUTION_CANDIDATES",
        "SLICE_SESSION_STOPPED",
    }


def test_trigger_carries_event_refs_and_optional_target_slice() -> None:
    slice_id = str(uuid4())
    trigger = SupervisorTrigger(
        SupervisorAdviceKind.RouteSuggestion,
        "SLICE_SESSION_STOPPED",
        trigger_event_refs=("event-1",),
        target_slice_id=slice_id,
    )

    assert trigger.trigger_event_refs == ("event-1",)
    assert trigger.target_slice_id == slice_id


@pytest.mark.parametrize("reason", ["", "UNKNOWN", "SLICE_SESSION_STOPPED "])
def test_unknown_or_empty_trigger_reason_is_rejected(reason: str) -> None:
    with pytest.raises(ValueError):
        SupervisorTrigger(SupervisorAdviceKind.RouteSuggestion, reason)


def test_projection_exposes_only_four_directed_evidence_groups(make_binding=None) -> None:
    evidence = RepairEvidence(
        candidate_slice_set=[uuid4(), uuid4()],
        reliability=AttributionReliability.Uncertain,
        strong_coupling=True,
        cross_generation_recurrence=False,
        conservation_signal_summary={"candidate_count": 2},
    )
    projection = SupervisorProjection(
        repair_evidence=evidence,
        failed_test_refs=("test::one",),
        diagnostic_summary={"error_count": 1, "message": "redacted"},
        slice_states={str(uuid4()): "FAILED"},
        prior_repair_decision_refs=("repair-1",),
    )

    context = projection.to_context()
    assert len(context.stable) == 0
    assert len(context.evolving) == 0
    assert len(context.targeted) == 1
    assert "repair_evidence" in context.targeted[0].content
    assert "failed_test_refs" in context.targeted[0].content
    assert "slice_states" in context.targeted[0].content
    assert "prior_repair_decision_refs" in context.targeted[0].content
    assert "baseline" not in context.targeted[0].content
    assert "source_text" not in context.targeted[0].content


def test_trigger_and_projection_are_immutable() -> None:
    trigger = SupervisorTrigger(
        SupervisorAdviceKind.RepairDecision,
        "AMBIGUOUS_ATTRIBUTION_CANDIDATES",
        trigger_event_refs=("event-1",),
    )
    with pytest.raises(FrozenInstanceError):
        trigger.reason = "changed"  # type: ignore[misc]

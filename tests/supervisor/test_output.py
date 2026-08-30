from __future__ import annotations

import json
from uuid import uuid4

import pytest

from codemigrator.core import Advice, AdviceKind, ResidentRole, new_uuid7
from codemigrator.runtime.advice import advice_proposal_hash
from codemigrator.runtime.supervisor import (
    RouteSuggestion,
    SuggestedRoute,
    build_adopted_event,
    build_proposed_event,
    build_repair_decision_event,
)


def test_route_suggestion_has_exactly_the_four_frozen_routes() -> None:
    assert {route.value for route in SuggestedRoute} == {
        "delegate_regen",
        "global_repair",
        "terminal_fail",
        "clarify",
    }
    route = RouteSuggestion(
        trigger_event_refs=("event-1",),
        failure_class="TEST_FAILURE",
        suggested_route=SuggestedRoute.DelegateRegen,
        target_slice_id=str(uuid4()),
        rationale="retry the failed slice",
    )
    assert route.to_payload()["suggested_route"] == "delegate_regen"


def test_route_suggestion_rejects_empty_rationale_and_unknown_target() -> None:
    with pytest.raises(ValueError):
        RouteSuggestion((), "TEST_FAILURE", SuggestedRoute.Clarify, None, "why")
    with pytest.raises(ValueError):
        RouteSuggestion(("event-1",), "TEST_FAILURE", SuggestedRoute.Clarify, "bad", "why")
    with pytest.raises(ValueError):
        RouteSuggestion(("event-1",), "TEST_FAILURE", SuggestedRoute.Clarify, None, "")


def _advice(payload: dict[str, object], *, kind: AdviceKind = AdviceKind.RouteSuggestion) -> Advice:
    advice = Advice(
        advice_id=new_uuid7(),
        kind=kind,
        run_id=new_uuid7(),
        role=ResidentRole.ExecuteSupervisor,
        payload=payload,
        proposal_hash="0" * 64,
    )
    return advice.model_copy(update={"proposal_hash": advice_proposal_hash(advice)})


def test_event_helpers_are_summaries_and_do_not_store_raw_diagnostics() -> None:
    advice = _advice(
        RouteSuggestion(
            ("event-1",),
            "TEST_FAILURE",
            SuggestedRoute.Clarify,
            None,
            "secret source path and diagnostic text",
        ).to_payload()
    )

    proposed = build_proposed_event(advice)
    adopted = build_adopted_event(advice, "CONFIRMED", "secret impact details")
    assert proposed.event_type == "advice.proposed"
    assert adopted.event_type == "advice.adopted"
    assert "secret" not in json.dumps(proposed.data)
    assert "secret" not in json.dumps(adopted.data)


def test_repair_event_is_only_emitted_for_repair_advice() -> None:
    advice = _advice(
        {
            "decision_id": str(new_uuid7()),
            "repair_set": [str(uuid4())],
            "domain_split": {},
            "brief_refs": [],
        },
        kind=AdviceKind.RepairDecision,
    )
    advice = advice.model_copy(update={"proposal_hash": advice_proposal_hash(advice)})
    repair_event = build_repair_decision_event(advice)
    assert repair_event.event_type == "repair.decision"
    assert repair_event.data["repair_decision_id"]


def test_event_helper_rejects_tampered_proposal_hash() -> None:
    advice = _advice(
        RouteSuggestion(
            ("event-1",), "TEST_FAILURE", SuggestedRoute.Clarify, None, "need input"
        ).to_payload()
    )
    tampered = advice.model_copy(update={"proposal_hash": "f" * 64})
    with pytest.raises(ValueError, match="proposal_hash"):
        build_proposed_event(tampered)

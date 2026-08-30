from __future__ import annotations

from codemigrator.core import Advice, AdviceKind, ResidentRole, Sha256
from codemigrator.runtime.advice import (
    AdviceDisposition,
    AdviceValidationContext,
    advice_proposal_hash,
    evaluate_advice,
)

from .conftest import uid


def advice(run_id, kind, payload, proposal_hash: str | None = None) -> Advice:
    value = Advice(
        advice_id=uid(),
        kind=kind,
        run_id=run_id,
        role=ResidentRole.ExecuteSupervisor,
        payload=payload,
        proposal_hash=Sha256(proposal_hash or "0" * 64),
    )
    return value.model_copy(update={"proposal_hash": Sha256(advice_proposal_hash(value))})


def test_proposal_hash_is_canonical_and_covers_identity_and_payload(run_id):
    first = advice(run_id, AdviceKind.RouteSuggestion, {"path": "src/a.py"})
    second = first.model_copy(update={"payload": {"path": "src/b.py"}})
    assert advice_proposal_hash(first) != advice_proposal_hash(second)
    assert len(advice_proposal_hash(first)) == 64


def test_proposal_hash_sorts_unordered_payload_sets(run_id):
    advice_id = uid()
    first = Advice(
        advice_id=advice_id,
        kind=AdviceKind.RouteSuggestion,
        run_id=run_id,
        role=ResidentRole.ExecuteSupervisor,
        payload={"items": {"b", "a"}},
        proposal_hash=Sha256("0" * 64),
    )
    second = first.model_copy(update={"payload": {"items": set(("a", "b"))}})
    assert advice_proposal_hash(first) == advice_proposal_hash(second)


def test_boundary_advice_is_confirmation_only(run_id):
    value = advice(run_id, AdviceKind.RouteSuggestion, {"route": "supervisor"})
    result = evaluate_advice(value, AdviceValidationContext())
    assert result.disposition is AdviceDisposition.ConfirmationRequired


def test_tampered_hash_is_discarded(run_id):
    value = Advice(
        advice_id=uid(),
        kind=AdviceKind.ExploreReassignment,
        run_id=run_id,
        role=ResidentRole.ExecuteSupervisor,
        payload={"assignments": {"module-a": str(uid())}},
        proposal_hash=Sha256("f" * 64),
    )
    result = evaluate_advice(
        value,
        AdviceValidationContext(expected_subjects=frozenset({"module-a"})),
    )
    assert result.disposition is AdviceDisposition.Discarded
    assert result.reason == "PROPOSAL_HASH_MISMATCH"


def test_explore_reassignment_requires_exact_coverage_and_fanout_cap(run_id):
    payload = {"assignments": {"module-a": str(uid()), "module-b": str(uid())}}
    value = advice(run_id, AdviceKind.ExploreReassignment, payload)
    context = AdviceValidationContext(
        expected_subjects=frozenset({"module-a", "module-b"}), max_fanout=1
    )
    result = evaluate_advice(value, context)
    assert result.disposition is AdviceDisposition.AutoAdopted

    missing = advice(
        run_id,
        AdviceKind.ExploreReassignment,
        {"assignments": {"module-a": str(uid())}},
    )
    assert evaluate_advice(missing, context).disposition is AdviceDisposition.Discarded


def test_repair_decision_requires_candidate_subset_and_joint_domain(run_id):
    candidate, other = uid(), uid()
    valid = advice(
        run_id,
        AdviceKind.RepairDecision,
        {
            "repair_set": [str(candidate)],
            "domain_members": [str(candidate)],
        },
    )
    context = AdviceValidationContext(
        attribution_candidates=frozenset({candidate, other}),
        joint_domain_members=frozenset({candidate}),
    )
    assert evaluate_advice(valid, context).disposition is AdviceDisposition.AutoAdopted

    invalid = advice(run_id, AdviceKind.RepairDecision, {"repair_set": [str(other)]})
    assert evaluate_advice(invalid, context).disposition is AdviceDisposition.Discarded

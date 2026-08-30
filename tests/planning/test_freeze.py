from __future__ import annotations

import uuid

import pytest

from codemigrator.core import PlanEdgeKind, StableErrorCode
from codemigrator.planning import (
    PlanLedger,
    PlanProposal,
    PlanRejected,
)

from .test_validation import edge, proposal, slice_


def test_freeze_allocates_uuid7_ids_and_orders_by_frozen_integration_key(
    planning_inputs: object,
) -> None:
    plan = PlanProposal(
        slices=(
            slice_("A", 1, "target/a.py", root="target/a"),
            slice_("B", 2, "target/b.py", root="target/b"),
        ),
        edges=(edge("A", "B"),),
        integration_ranks={"A": 0, "B": 1},
        planner_rationale=(),
    )
    frozen = PlanLedger().freeze(plan, planning_inputs)

    assert all(
        isinstance(slice_.id, uuid.UUID) and slice_.id.version == 7
        for slice_ in frozen.slices
    )
    assert frozen.integration_order == (
        frozen.local_ref_to_id["A"],
        frozen.local_ref_to_id["B"],
    )
    assert len(frozen.plan_hash) == 64
    assert frozen.validation.accepted is True


def test_invalid_freeze_has_no_partial_ledger_write(planning_inputs: object) -> None:
    plan = proposal(
        slice_("A", 1, "target/a.py", root="target/a"),
        slice_("B", 2, "target/a.py", root="target/b"),
        edges=(edge("A", "B", kind=PlanEdgeKind.OrderedBefore),),
    )
    ledger = PlanLedger()

    with pytest.raises(PlanRejected) as raised:
        ledger.freeze(plan, planning_inputs)

    assert raised.value.code is StableErrorCode.PLAN_SCOPE_CONFLICT
    assert ledger.persisted_count == 0
    assert ledger.records == ()


def test_ledger_returns_a_copy_so_frozen_nested_facts_cannot_drift(
    planning_inputs: object,
) -> None:
    plan = proposal(
        slice_("A", 1, "target/a.py", root="target/a"),
        slice_("B", 2, "target/b.py", root="target/b"),
    )
    ledger = PlanLedger()
    frozen = ledger.freeze(plan, planning_inputs)
    frozen.slices[0].write_scope.out.write_paths.append("target/injected.py")

    assert frozen.slices[0].write_scope.out.write_paths == ["target/a.py"]
    stored = ledger.records[0]
    assert stored.slices[0].write_scope.out.write_paths == ["target/a.py"]

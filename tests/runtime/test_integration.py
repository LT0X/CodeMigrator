from __future__ import annotations

import pytest

from codemigrator.runtime.integration import (
    IntegrationCoordinator,
    IntegrationItem,
    RepairRetryBudget,
)


def item(slice_id: str, *, repair: bool = False, prospective: bool = False) -> IntegrationItem:
    return IntegrationItem("run-1", slice_id, 0, f"oid-{slice_id}", prospective, repair)


def test_integration_is_one_fifo_channel_and_requires_prospective_checks():
    coordinator = IntegrationCoordinator()
    coordinator.enqueue(item("ordinary"))
    coordinator.enqueue(item("repair", repair=True, prospective=True))
    assert coordinator.start_next("run-1", "verified-0") is None
    assert coordinator.queued[0].slice_id == "ordinary"

    coordinator.mark_prospective_passed("run-1", "ordinary", 0)
    started = coordinator.start_next("run-1", "verified-0")
    assert started is not None
    assert started.item.slice_id == "ordinary"
    coordinator.complete(success=True, new_verified_oid="verified-1")
    coordinator.mark_prospective_passed("run-1", "repair", 0)
    assert coordinator.start_next("run-1", "verified-1").item.repair is True


def test_repair_retry_budget_is_independent_and_capped_at_three():
    budget = RepairRetryBudget()
    assert [budget.record("repair-a") for _ in range(3)] == [1, 2, 3]
    assert budget.can_retry("repair-a") is False
    assert budget.can_retry("repair-b") is True
    with pytest.raises(RuntimeError):
        budget.record("repair-a")


def test_cancelled_run_cannot_enqueue_or_start_integration():
    coordinator = IntegrationCoordinator()
    coordinator.cancel_run("run-1")
    assert coordinator.enqueue(item("slice-a")) is False
    assert coordinator.start_next("run-1", "verified-0") is None


def test_supervisor_has_exactly_two_event_conditions():
    from codemigrator.runtime.supervisor import SupervisorAdviceKind, supervisor_triggers

    triggers = supervisor_triggers(
        candidate_slice_ids=frozenset({"slice-a", "slice-b"}),
        session_failed_and_stopped=True,
    )
    assert [trigger.kind for trigger in triggers] == [
        SupervisorAdviceKind.RepairDecision,
        SupervisorAdviceKind.RouteSuggestion,
    ]

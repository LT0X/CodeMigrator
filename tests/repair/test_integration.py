import pytest

from codemigrator.runtime.integration import IntegrationCoordinator, IntegrationItem


def item(
    slice_id: str,
    *,
    repair: bool = False,
    prospective: bool = False,
    generation: int | None = 0,
    decision_id: str | None = None,
) -> IntegrationItem:
    return IntegrationItem(
        "run-1", slice_id, generation, f"oid-{slice_id}", prospective, repair, decision_id
    )


def test_revoke_failed_placeholder_allows_queue_head_to_progress_and_repair_is_appended():
    coordinator = IntegrationCoordinator()
    coordinator.enqueue(item("failed", prospective=True))
    coordinator.enqueue(item("ordinary", prospective=True))
    assert coordinator.revoke_slice("run-1", "failed") is True
    assert [entry.slice_id for entry in coordinator.queued] == ["ordinary"]
    started = coordinator.start_next("run-1", "verified-1")
    assert started is not None
    coordinator.complete(success=True, new_verified_oid="verified-2")

    repair = item(
        "repair-result", repair=True, generation=None, prospective=False, decision_id="decision-1"
    )
    assert coordinator.enqueue_repair(repair) is True
    assert coordinator.start_next("run-1", "verified-2") is None
    coordinator.mark_prospective_passed("run-1", "repair-result", None)
    started = coordinator.start_next("run-1", "verified-2")
    assert started is not None
    assert started.base_verified_oid == "verified-2"
    assert started.item.repair is True


def test_active_placeholder_cannot_be_revoked_and_repair_cannot_bypass_prospective_gate():
    coordinator = IntegrationCoordinator()
    coordinator.enqueue(item("slice-a", prospective=True))
    assert coordinator.start_next("run-1", "verified-0") is not None
    with pytest.raises(RuntimeError, match="active"):
        coordinator.revoke_slice("run-1", "slice-a")
    assert coordinator.complete(success=False) is not None
    assert coordinator.revoke_slice("run-1", "slice-a") is True

    coordinator.enqueue(item("repair", repair=True, generation=None))
    assert coordinator.start_next("run-1", "verified-0") is None

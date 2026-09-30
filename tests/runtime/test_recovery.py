from __future__ import annotations

from codemigrator.runtime.contracts import RuntimeEvent
from codemigrator.runtime.recovery import (
    ActorCheckpoint,
    CheckpointPolicy,
    RecoveryCoordinator,
    RecoveryTrigger,
    has_committed_owner_receipt,
    restore_checkpoint,
)


def test_corrupt_actor_checkpoint_is_discarded_for_rebuild_not_completion():
    checkpoint = ActorCheckpoint.create(cursor="task-9", receipt_refs=("r1",), candidate_index=2)
    corrupt = checkpoint.__class__(
        cursor=checkpoint.cursor,
        receipt_refs=checkpoint.receipt_refs,
        candidate_index=99,
        checksum=checkpoint.checksum,
    )
    result = restore_checkpoint(corrupt)
    assert result.rebuild is True
    assert result.completion_evidence is False


def test_recovery_is_event_triggered_and_marks_active_dispatch_interrupted():
    coordinator = RecoveryCoordinator()
    plan = coordinator.trigger(RecoveryTrigger.Interruption, active_dispatch_ids=("d1", "d2"))
    assert plan.events == (
        "recovery.actor_rebuilt",
        "dispatch.interrupted:d1",
        "dispatch.interrupted:d2",
        "git.refs.reconciled",
        "receipts.repaired",
        "recovery.completed",
    )
    assert coordinator.periodic_poll is False


def test_checkpoint_policy_is_task_or_time_triggered_without_polling():
    policy = CheckpointPolicy()
    assert policy.due(completed_tasks=10, elapsed_seconds=0) is True
    assert policy.due(completed_tasks=1, elapsed_seconds=60) is True
    assert policy.due(completed_tasks=1, elapsed_seconds=1) is False


def test_graph_recovery_advances_only_from_a_committed_owner_event():
    committed = RuntimeEvent(
        sequence=7,
        event_type="run.plan.accepted",
        data={"receipt_key": "run.plan.accepted:opaque-run-id"},
    )
    assert has_committed_owner_receipt((committed,), "run.plan.accepted:opaque-run-id")
    assert not has_committed_owner_receipt((), "run.plan.accepted:opaque-run-id")
    assert not has_committed_owner_receipt((committed,), "run.execute.round:opaque-run-id:1")

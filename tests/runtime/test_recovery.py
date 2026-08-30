from __future__ import annotations

from codemigrator.runtime.recovery import (
    ActorCheckpoint,
    RecoveryCoordinator,
    RecoveryTrigger,
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
    assert plan.events == ("dispatch.interrupted:d1", "dispatch.interrupted:d2", "recovery.rebuilt")
    assert coordinator.periodic_poll is False

"""Event-triggered recovery and cursor checkpoint integrity."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum

from codemigrator.core import canonical_json_bytes


class RecoveryTrigger(str, Enum):
    Startup = "startup"
    Interruption = "interruption"
    IntentGap = "intent_gap"


@dataclass(frozen=True, slots=True)
class ActorCheckpoint:
    cursor: str
    receipt_refs: tuple[str, ...]
    candidate_index: int
    checksum: str

    @classmethod
    def create(
        cls, *, cursor: str, receipt_refs: tuple[str, ...], candidate_index: int
    ) -> ActorCheckpoint:
        checksum = _checksum(cursor, receipt_refs, candidate_index)
        return cls(cursor, receipt_refs, candidate_index, checksum)


@dataclass(frozen=True, slots=True)
class CheckpointPolicy:
    task_interval: int = 10
    time_interval_seconds: float = 60.0

    def __post_init__(self) -> None:
        if type(self.task_interval) is not int or self.task_interval < 1:
            raise ValueError("task_interval must be positive")
        if self.time_interval_seconds <= 0:
            raise ValueError("time_interval_seconds must be positive")

    def due(self, *, completed_tasks: int, elapsed_seconds: float) -> bool:
        if type(completed_tasks) is not int or completed_tasks < 0:
            raise ValueError("completed_tasks must be a non-negative integer")
        if elapsed_seconds < 0:
            raise ValueError("elapsed_seconds must be non-negative")
        return (
            completed_tasks > 0 and completed_tasks % self.task_interval == 0
        ) or elapsed_seconds >= self.time_interval_seconds


@dataclass(frozen=True, slots=True)
class CheckpointRestore:
    checkpoint: ActorCheckpoint | None
    rebuild: bool
    completion_evidence: bool
    reason: str


def _checksum(cursor: str, receipt_refs: tuple[str, ...], candidate_index: int) -> str:
    payload = {
        "cursor": cursor,
        "receipt_refs": list(receipt_refs),
        "candidate_index": candidate_index,
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def restore_checkpoint(checkpoint: ActorCheckpoint) -> CheckpointRestore:
    """Accept only an intact cursor; corruption requests a fact-based rebuild."""

    expected = _checksum(checkpoint.cursor, checkpoint.receipt_refs, checkpoint.candidate_index)
    if expected != checkpoint.checksum:
        return CheckpointRestore(None, True, False, "CHECKPOINT_CORRUPT")
    return CheckpointRestore(checkpoint, False, False, "CHECKPOINT_ACCEPTED")


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    trigger: RecoveryTrigger
    events: tuple[str, ...]
    report_halted: bool = False


class RecoveryCoordinator:
    """Build recovery actions only when an explicit event asks for them."""

    periodic_poll = False

    def trigger(
        self,
        trigger: RecoveryTrigger,
        *,
        active_dispatch_ids: tuple[str, ...] = (),
        missing_intent_ids: tuple[str, ...] = (),
        checkpoint_corrupt: bool = False,
        ref_drift: bool = False,
    ) -> RecoveryPlan:
        events = ["recovery.actor_rebuilt"]
        events.extend(f"dispatch.interrupted:{item}" for item in active_dispatch_ids)
        events.extend(("git.refs.reconciled", "receipts.repaired"))
        events.extend(f"integration.intent.retry:{item}" for item in missing_intent_ids)
        if checkpoint_corrupt:
            events.append("checkpoint.discarded")
        if ref_drift:
            events.append("recovery.ref_drift")
        events.append("recovery.completed")
        return RecoveryPlan(trigger, tuple(events), report_halted=ref_drift)


__all__ = [
    "ActorCheckpoint",
    "CheckpointRestore",
    "CheckpointPolicy",
    "RecoveryCoordinator",
    "RecoveryPlan",
    "RecoveryTrigger",
    "restore_checkpoint",
]

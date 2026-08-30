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


class RecoveryCoordinator:
    """Build recovery actions only when an explicit event asks for them."""

    periodic_poll = False

    def trigger(
        self, trigger: RecoveryTrigger, *, active_dispatch_ids: tuple[str, ...] = ()
    ) -> RecoveryPlan:
        events = tuple(f"dispatch.interrupted:{item}" for item in active_dispatch_ids)
        return RecoveryPlan(trigger, (*events, "recovery.rebuilt"))


__all__ = [
    "ActorCheckpoint",
    "CheckpointRestore",
    "RecoveryCoordinator",
    "RecoveryPlan",
    "RecoveryTrigger",
    "restore_checkpoint",
]

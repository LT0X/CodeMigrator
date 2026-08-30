"""FIFO integration coordination and independent repair retry accounting."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class IntegrationItem:
    run_id: str
    slice_id: str
    generation: int | None
    candidate_commit_oid: str
    prospective_checks_passed: bool = False
    repair: bool = False
    repair_decision_id: str | None = None


@dataclass(frozen=True, slots=True)
class IntegrationStart:
    item: IntegrationItem
    base_verified_oid: str


class IntegrationCoordinator:
    """Serialize ordinary and repair integration in one FIFO channel."""

    def __init__(self) -> None:
        self._queue: deque[IntegrationItem] = deque()
        self._active: IntegrationItem | None = None
        self._verified_oids: dict[str, str] = {}
        self._cancelled_runs: set[str] = set()

    def enqueue(self, item: IntegrationItem) -> bool:
        if item.run_id in self._cancelled_runs:
            return False
        self._queue.append(item)
        return True

    def cancel_run(self, run_id: str) -> None:
        self._cancelled_runs.add(run_id)
        self._queue = deque(item for item in self._queue if item.run_id != run_id)
        if self._active is not None and self._active.run_id == run_id:
            self._active = None

    def revoke_slice(self, run_id: str, slice_id: str) -> bool:
        """Revoke an unstarted failed Slice placeholder from the FIFO."""

        if self._active is not None and (
            self._active.run_id,
            self._active.slice_id,
        ) == (run_id, slice_id):
            raise RuntimeError("active integration item cannot be revoked")
        before = len(self._queue)
        self._queue = deque(
            item for item in self._queue if (item.run_id, item.slice_id) != (run_id, slice_id)
        )
        return len(self._queue) != before

    revoke_placeholder = revoke_slice

    def enqueue_repair(self, item: IntegrationItem) -> bool:
        """Append a completed RepairSession result without bypassing FIFO gates."""

        if not item.repair:
            raise ValueError("repair integration item must be marked repair")
        return self.enqueue(item)

    append_repair = enqueue_repair

    def mark_prospective_passed(
        self, run_id: str, slice_id: str, generation: int | None
    ) -> None:
        self._queue = deque(
            item
            if (item.run_id, item.slice_id, item.generation) != (run_id, slice_id, generation)
            else IntegrationItem(
                run_id=item.run_id,
                slice_id=item.slice_id,
                generation=item.generation,
                candidate_commit_oid=item.candidate_commit_oid,
                prospective_checks_passed=True,
                repair=item.repair,
                repair_decision_id=item.repair_decision_id,
            )
            for item in self._queue
        )

    def start_next(self, run_id: str, latest_verified_oid: str) -> IntegrationStart | None:
        """Start only the queue head after prospective checks have passed."""

        if self._active is not None:
            return None
        if run_id in self._cancelled_runs:
            return None
        if not self._queue or self._queue[0].run_id != run_id:
            return None
        item = self._queue[0]
        if not item.prospective_checks_passed:
            return None
        self._active = item
        self._verified_oids[run_id] = latest_verified_oid
        return IntegrationStart(item, latest_verified_oid)

    def complete(
        self, *, success: bool, new_verified_oid: str | None = None
    ) -> IntegrationItem | None:
        if self._active is None:
            return None
        item = self._active
        if success:
            if new_verified_oid is None:
                raise ValueError("successful integration requires a new verified OID")
            self._verified_oids[item.run_id] = new_verified_oid
            self._queue.popleft()
        self._active = None
        return item

    @property
    def queued(self) -> tuple[IntegrationItem, ...]:
        return tuple(self._queue)

    @property
    def active(self) -> IntegrationItem | None:
        return self._active

    def latest_verified_oid(self, run_id: str) -> str | None:
        return self._verified_oids.get(run_id)


class RepairRetryBudget:
    """Track repair retries independently from candidate generations."""

    def __init__(self, limit: int = 3) -> None:
        if type(limit) is not int or limit < 1:
            raise ValueError("retry limit must be a positive integer")
        self.limit = limit
        self._attempts: dict[str, int] = {}

    def can_retry(self, repair_key: str) -> bool:
        return self._attempts.get(repair_key, 0) < self.limit

    def record(self, repair_key: str) -> int:
        if not self.can_retry(repair_key):
            raise RuntimeError("repair retry limit exhausted")
        self._attempts[repair_key] = self._attempts.get(repair_key, 0) + 1
        return self._attempts[repair_key]


__all__ = [
    "IntegrationCoordinator",
    "IntegrationItem",
    "IntegrationStart",
    "RepairRetryBudget",
]

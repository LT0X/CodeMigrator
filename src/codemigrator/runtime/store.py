"""Runtime persistence ports and a deterministic transactional test adapter."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from codemigrator.core import RunId

from .contracts import EventSpec, RunState, RuntimeEvent, RuntimeSnapshot


class RuntimeStore(Protocol):
    async def load(self, run_id: RunId) -> RuntimeSnapshot | None:
        """Load one Run and its append-only events."""

    async def create(self, state: RunState, events: Sequence[EventSpec]) -> RuntimeSnapshot:
        """Insert a new Run atomically with its first events."""

    async def commit(self, state: RunState, events: Sequence[EventSpec]) -> RuntimeSnapshot:
        """Commit state and events in one transaction."""


class StoreCommitError(RuntimeError):
    """Raised when the persistence transaction cannot be committed."""


class InMemoryRuntimeStore:
    """A transactionally behaving store double for actor and contract tests."""

    def __init__(self) -> None:
        self._snapshots: dict[RunId, RuntimeSnapshot] = {}
        self.commit_count = 0
        self._fail_next = False

    async def load(self, run_id: RunId) -> RuntimeSnapshot | None:
        return self._snapshots.get(run_id)

    async def create(self, state: RunState, events: Sequence[EventSpec]) -> RuntimeSnapshot:
        if state.run_id in self._snapshots:
            raise StoreCommitError("run already exists")
        return await self._write(state, events)

    async def commit(self, state: RunState, events: Sequence[EventSpec]) -> RuntimeSnapshot:
        if state.run_id not in self._snapshots:
            raise StoreCommitError("run does not exist")
        return await self._write(state, events)

    async def snapshot(self, run_id: RunId) -> RuntimeSnapshot:
        snapshot = await self.load(run_id)
        if snapshot is None:
            raise KeyError(run_id)
        return snapshot

    def fail_next_commit(self) -> None:
        self._fail_next = True

    async def _write(self, state: RunState, events: Sequence[EventSpec]) -> RuntimeSnapshot:
        if self._fail_next:
            self._fail_next = False
            raise StoreCommitError("injected commit failure")
        previous = self._snapshots.get(state.run_id)
        first_sequence = len(previous.events) + 1 if previous is not None else 1
        materialized = tuple(
            RuntimeEvent(
                sequence=first_sequence + index,
                event_type=event.event_type,
                data=event.data,
            )
            for index, event in enumerate(events)
        )
        snapshot = RuntimeSnapshot(
            state=state,
            events=(*previous.events, *materialized) if previous else materialized,
        )
        self._snapshots[state.run_id] = snapshot
        self.commit_count += 1
        return snapshot

__all__ = ["InMemoryRuntimeStore", "RuntimeStore", "StoreCommitError"]

"""DAG-ready, scope-safe, cross-Run fair scheduling."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from enum import Enum


class ResourcePool(str, Enum):
    Model = "model"
    Sandbox = "sandbox"
    Adjudication = "adjudication"


@dataclass(frozen=True, slots=True)
class ReadySlice:
    run_id: str
    slice_id: str
    dependencies: frozenset[str]
    write_scope: frozenset[str]
    resource_pool: ResourcePool


class FairScheduler:
    """Select ready slices with write-scope exclusion and round-robin fairness."""

    def __init__(self) -> None:
        self._queues: dict[str, list[ReadySlice]] = defaultdict(list)
        self._completed: dict[str, set[str]] = defaultdict(set)
        self._in_flight: set[tuple[str, str]] = set()
        self._run_order: list[str] = []
        self._cursor = 0

    def submit(self, item: ReadySlice) -> None:
        if item.run_id not in self._queues:
            self._run_order.append(item.run_id)
        if item.slice_id not in {candidate.slice_id for candidate in self._queues[item.run_id]}:
            self._queues[item.run_id].append(item)

    def complete(self, run_id: str, slice_id: str) -> None:
        self._completed[run_id].add(slice_id)
        self._in_flight.discard((run_id, slice_id))

    def next(
        self,
        active_scopes: frozenset[str],
        available_pools: frozenset[ResourcePool],
    ) -> ReadySlice | None:
        if not self._run_order:
            return None
        for offset in range(len(self._run_order)):
            index = (self._cursor + offset) % len(self._run_order)
            run_id = self._run_order[index]
            completed = self._completed[run_id]
            for item in self._queues[run_id]:
                identity = (item.run_id, item.slice_id)
                if (
                    item.slice_id in completed
                    or identity in self._in_flight
                    or not item.dependencies.issubset(completed)
                ):
                    continue
                if item.resource_pool not in available_pools:
                    continue
                if item.write_scope & active_scopes:
                    continue
                self._in_flight.add(identity)
                self._cursor = (index + 1) % len(self._run_order)
                return item
        return None


__all__ = ["FairScheduler", "ReadySlice", "ResourcePool"]

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
    generation: int = 0
    ordered_before_dependencies: frozenset[str] = frozenset()
    requires_dependencies: frozenset[str] | None = None

    def __post_init__(self) -> None:
        if type(self.generation) is not int or self.generation < 0:
            raise ValueError("Slice generation must be a non-negative integer")
        dependencies = frozenset(self.dependencies)
        ordered_before = frozenset(self.ordered_before_dependencies)
        requires = (
            dependencies - ordered_before
            if self.requires_dependencies is None
            else frozenset(self.requires_dependencies)
        )
        if not ordered_before.issubset(dependencies) or not requires.issubset(dependencies):
            raise ValueError("typed Slice predecessors must be declared dependencies")
        if dependencies != ordered_before | requires:
            raise ValueError("typed Slice predecessor kinds must cover all dependencies")
        object.__setattr__(self, "dependencies", dependencies)
        object.__setattr__(self, "ordered_before_dependencies", ordered_before)
        object.__setattr__(self, "requires_dependencies", requires)


class FairScheduler:
    """Select ready slices with write-scope exclusion and round-robin fairness."""

    def __init__(self) -> None:
        self._queues: dict[str, list[ReadySlice]] = defaultdict(list)
        self._integrated: dict[str, set[str]] = defaultdict(set)
        self._terminal_failed: dict[str, set[str]] = defaultdict(set)
        self._awaiting_integration: set[tuple[str, str, int]] = set()
        self._in_flight: set[tuple[str, str, int]] = set()
        self._in_flight_scopes: dict[tuple[str, str, int], frozenset[str]] = {}
        self._run_order: list[str] = []
        self._cursor = 0

    def submit(self, item: ReadySlice) -> None:
        if item.run_id not in self._queues:
            self._run_order.append(item.run_id)
        queue = self._queues[item.run_id]
        existing_index = next(
            (index for index, candidate in enumerate(queue) if candidate.slice_id == item.slice_id),
            None,
        )
        if existing_index is None:
            queue.append(item)
            return
        existing = queue[existing_index]
        old_identity = (existing.run_id, existing.slice_id, existing.generation)
        new_identity = (item.run_id, item.slice_id, item.generation)
        if old_identity in self._in_flight and old_identity != new_identity:
            raise ValueError("cannot replace a Slice generation while it is in flight")
        if existing != item and old_identity in self._in_flight:
            raise ValueError("cannot replace in-flight Slice scheduling facts")
        if existing.generation != item.generation:
            self._integrated[item.run_id].discard(item.slice_id)
            self._terminal_failed[item.run_id].discard(item.slice_id)
            self._awaiting_integration = {
                identity
                for identity in self._awaiting_integration
                if identity[:2] != (item.run_id, item.slice_id)
            }
        queue[existing_index] = item

    def complete(self, run_id: str, slice_id: str, *, generation: int | None = None) -> None:
        """Backward-compatible alias for :meth:`mark_integrated`."""

        self.mark_integrated(run_id, slice_id, generation=generation)

    def mark_integrated(
        self, run_id: str, slice_id: str, *, generation: int | None = None
    ) -> None:
        """Record formal integration completion, making dependencies eligible."""

        current_generation = self._generation_for(run_id, slice_id, None)
        selected_generation = current_generation if generation is None else generation
        if generation is None or generation == current_generation:
            self._integrated[run_id].add(slice_id)
            self._terminal_failed[run_id].discard(slice_id)
            self._awaiting_integration = {
                identity
                for identity in self._awaiting_integration
                if identity[:2] != (run_id, slice_id)
            }
        if selected_generation is not None:
            identity = (run_id, slice_id, selected_generation)
            self._in_flight.discard(identity)
            self._in_flight_scopes.pop(identity, None)

    def release(self, run_id: str, slice_id: str, *, generation: int | None = None) -> None:
        """Release a dispatch reservation without recording a Slice outcome."""

        selected_generation = self._generation_for(run_id, slice_id, generation)
        if selected_generation is not None:
            identity = (run_id, slice_id, selected_generation)
            self._in_flight.discard(identity)
            self._in_flight_scopes.pop(identity, None)

    def await_integration(self, run_id: str, slice_id: str, *, generation: int) -> None:
        """Release candidate work without satisfying dependencies or redispatching it."""

        if type(generation) is not int or generation < 0:
            raise ValueError("candidate generation must be a non-negative integer")
        identity = (run_id, slice_id, generation)
        self._awaiting_integration.add(identity)
        self._in_flight.discard(identity)
        self._in_flight_scopes.pop(identity, None)

    def restore_committed_facts(
        self,
        run_id: str,
        *,
        integrated_slice_ids: frozenset[str],
        terminal_failure_slice_ids: frozenset[str],
    ) -> None:
        """Restore readiness exclusively from facts materialized by the Run owner."""

        self._integrated[run_id] = set(integrated_slice_ids)
        self._terminal_failed[run_id] = set(terminal_failure_slice_ids) - set(
            integrated_slice_ids
        )
        self._awaiting_integration = {
            identity for identity in self._awaiting_integration if identity[0] != run_id
        }

    def next(
        self,
        active_scopes: frozenset[str],
        available_pools: frozenset[ResourcePool],
        *,
        only_run_id: str | None = None,
    ) -> ReadySlice | None:
        if not self._run_order:
            return None
        for offset in range(len(self._run_order)):
            index = (self._cursor + offset) % len(self._run_order)
            run_id = self._run_order[index]
            if only_run_id is not None and run_id != only_run_id:
                continue
            reserved_scopes = frozenset(
                scope
                for (reserved_run_id, _slice_id, _generation), scopes
                in self._in_flight_scopes.items()
                if reserved_run_id == run_id
                for scope in scopes
            )
            unavailable_scopes = active_scopes | reserved_scopes
            integrated = self._integrated[run_id]
            terminal_failed = self._terminal_failed[run_id]
            for item in self._queues[run_id]:
                identity = (item.run_id, item.slice_id, item.generation)
                requires = item.requires_dependencies
                if requires is None:  # Normalized to a set by ReadySlice.__post_init__.
                    raise ValueError("ReadySlice has no normalized Requires predecessor set")
                if (
                    item.slice_id in integrated
                    or item.slice_id in terminal_failed
                    or identity in self._in_flight
                    or identity in self._awaiting_integration
                    or not requires.issubset(integrated)
                    or not item.ordered_before_dependencies.issubset(integrated | terminal_failed)
                ):
                    continue
                if item.resource_pool not in available_pools:
                    continue
                if _scopes_overlap(item.write_scope, unavailable_scopes):
                    continue
                self._in_flight.add(identity)
                self._in_flight_scopes[identity] = item.write_scope
                self._cursor = (index + 1) % len(self._run_order)
                return item
        return None

    def _generation_for(
        self, run_id: str, slice_id: str, generation: int | None
    ) -> int | None:
        if generation is not None:
            return generation
        return next(
            (item.generation for item in self._queues.get(run_id, ()) if item.slice_id == slice_id),
            None,
        )


def _scopes_overlap(left: frozenset[str], right: frozenset[str]) -> bool:
    """Return whether any declared paths are equal or nested by path segment."""

    return any(
        first == second
        or first.startswith(f"{second.rstrip('/')}/")
        or second.startswith(f"{first.rstrip('/')}/")
        for first in left
        for second in right
    )


__all__ = ["FairScheduler", "ReadySlice", "ResourcePool"]

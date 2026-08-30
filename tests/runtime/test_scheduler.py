from __future__ import annotations

from codemigrator.runtime.scheduler import FairScheduler, ReadySlice, ResourcePool


def test_scheduler_uses_dag_readiness_scope_exclusion_and_cross_run_rotation():
    scheduler = FairScheduler()
    scheduler.submit(
        ReadySlice("run-a", "a1", frozenset(), frozenset({"src/a.py"}), ResourcePool.Model)
    )
    scheduler.submit(
        ReadySlice("run-b", "b1", frozenset(), frozenset({"src/b.py"}), ResourcePool.Sandbox)
    )
    scheduler.submit(
        ReadySlice("run-a", "a2", frozenset({"a1"}), frozenset({"src/c.py"}), ResourcePool.Model)
    )

    first = scheduler.next(active_scopes=frozenset(), available_pools=frozenset(ResourcePool))
    second = scheduler.next(
        active_scopes=first.write_scope,
        available_pools=frozenset(ResourcePool),
    )
    assert (first.run_id, second.run_id) == ("run-a", "run-b")

    scheduler.complete("run-a", "a1")
    third = scheduler.next(active_scopes=frozenset(), available_pools=frozenset(ResourcePool))
    assert third.slice_id == "a2"


def test_scheduler_does_not_dispatch_overlapping_write_scope():
    scheduler = FairScheduler()
    item = ReadySlice("run-a", "a1", frozenset(), frozenset({"src/a.py"}), ResourcePool.Model)
    scheduler.submit(item)
    assert scheduler.next(frozenset({"src/a.py"}), frozenset(ResourcePool)) is None

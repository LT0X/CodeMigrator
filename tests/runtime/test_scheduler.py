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


def test_scheduler_treats_parent_and_child_paths_as_overlapping_scopes():
    scheduler = FairScheduler()
    scheduler.submit(
        ReadySlice(
            "run-a", "child", frozenset(), frozenset({"src/generated/a.py"}), ResourcePool.Model
        )
    )
    scheduler.submit(
        ReadySlice(
            "run-a", "sibling", frozenset(), frozenset({"src/generated2/a.py"}), ResourcePool.Model
        )
    )

    selected = scheduler.next(frozenset({"src/generated"}), frozenset(ResourcePool))

    assert selected is not None
    assert selected.slice_id == "sibling"


def test_scheduler_blocks_parent_scope_when_child_path_is_active():
    scheduler = FairScheduler()
    scheduler.submit(
        ReadySlice("run-a", "parent", frozenset(), frozenset({"src/generated"}), ResourcePool.Model)
    )

    assert scheduler.next(frozenset({"src/generated/a.py"}), frozenset(ResourcePool)) is None


def test_scheduler_reserves_selected_scope_across_concurrent_rounds():
    scheduler = FairScheduler()
    scheduler.submit(
        ReadySlice("run-a", "parent", frozenset(), frozenset({"src/generated"}), ResourcePool.Model)
    )
    scheduler.submit(
        ReadySlice(
            "run-a", "child", frozenset(), frozenset({"src/generated/a.py"}), ResourcePool.Model
        )
    )

    first = scheduler.next(frozenset(), frozenset(ResourcePool))
    while_reserved = scheduler.next(frozenset(), frozenset(ResourcePool))

    assert first is not None and first.slice_id == "parent"
    assert while_reserved is None

    scheduler.release("run-a", "parent")
    retried = scheduler.next(frozenset(), frozenset(ResourcePool))
    assert retried is not None and retried.slice_id == "parent"

    scheduler.complete("run-a", "parent")
    after_commit = scheduler.next(frozenset(), frozenset(ResourcePool))
    assert after_commit is not None and after_commit.slice_id == "child"


def test_scheduler_reopens_slice_when_a_new_generation_is_submitted():
    scheduler = FairScheduler()
    generation_one = ReadySlice(
        "run-a", "slice-a", frozenset(), frozenset({"src/a.py"}), ResourcePool.Model, 1
    )
    generation_two = ReadySlice(
        "run-a", "slice-a", frozenset(), frozenset({"src/a.py"}), ResourcePool.Model, 2
    )
    scheduler.submit(generation_one)
    selected = scheduler.next(frozenset(), frozenset(ResourcePool))
    assert selected == generation_one
    scheduler.complete("run-a", "slice-a", generation=1)

    scheduler.submit(generation_two)

    retried = scheduler.next(frozenset(), frozenset(ResourcePool))
    assert retried == generation_two


def test_scheduler_scopes_write_conflicts_to_each_run_workspace():
    scheduler = FairScheduler()
    scheduler.submit(
        ReadySlice("run-a", "a1", frozenset(), frozenset({"src/a.py"}), ResourcePool.Model)
    )
    scheduler.submit(
        ReadySlice("run-b", "b1", frozenset(), frozenset({"src/a.py"}), ResourcePool.Model)
    )

    first = scheduler.next(frozenset(), frozenset(ResourcePool))
    second = scheduler.next(frozenset(), frozenset(ResourcePool))

    assert first is not None and second is not None
    assert {first.run_id, second.run_id} == {"run-a", "run-b"}


def test_stale_generation_completion_cannot_complete_the_current_generation():
    scheduler = FairScheduler()
    generation_one = ReadySlice(
        "run-a", "slice-a", frozenset(), frozenset({"src/a.py"}), ResourcePool.Model, 1
    )
    generation_two = ReadySlice(
        "run-a", "slice-a", frozenset(), frozenset({"src/a.py"}), ResourcePool.Model, 2
    )
    scheduler.submit(generation_one)
    assert scheduler.next(frozenset(), frozenset(ResourcePool)) == generation_one
    scheduler.complete("run-a", "slice-a", generation=1)
    scheduler.submit(generation_two)
    assert scheduler.next(frozenset(), frozenset(ResourcePool)) == generation_two

    scheduler.complete("run-a", "slice-a", generation=1)
    scheduler.release("run-a", "slice-a", generation=2)

    assert scheduler.next(frozenset(), frozenset(ResourcePool)) == generation_two

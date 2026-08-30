from __future__ import annotations

import pytest

from codemigrator.core import FailureReason, RunStatus
from codemigrator.runtime.actor import RunActor
from codemigrator.runtime.contracts import (
    ApiCommand,
    CancelCommand,
    CreateRunCommand,
    SessionInputCommand,
)
from codemigrator.runtime.store import InMemoryRuntimeStore

from .conftest import create_run


async def start_actor(run_id):
    store = InMemoryRuntimeStore()
    actor = RunActor(run_id, store)
    await actor.start()
    await actor.submit(ApiCommand(CreateRunCommand(run_id=run_id, create_run=create_run())))
    await actor.join()
    return actor, store


@pytest.mark.asyncio
async def test_one_actor_serializes_mailbox_and_commits_state_with_events(run_id):
    actor, store = await start_actor(run_id)

    await actor.submit(ApiCommand(SessionInputCommand(kind="accepted", payload={})))
    await actor.submit(ApiCommand(SessionInputCommand(kind="accepted", payload={})))
    await actor.join()

    snapshot = await store.snapshot(run_id)
    assert snapshot.state.status is RunStatus.Planning
    assert snapshot.state.version == 3
    assert [event.sequence for event in snapshot.events] == [1, 2, 3]
    assert store.commit_count == 3
    await actor.stop()


@pytest.mark.asyncio
async def test_failed_commit_rolls_back_state_and_event_atomically(run_id):
    actor, store = await start_actor(run_id)
    before = await store.snapshot(run_id)
    store.fail_next_commit()

    await actor.submit(ApiCommand(SessionInputCommand(kind="accepted", payload={})))
    await actor.join()

    after = await store.snapshot(run_id)
    assert after == before
    assert actor.last_error is not None
    await actor.stop()


@pytest.mark.asyncio
async def test_stale_cancel_has_zero_writes_and_matching_cancel_is_terminal(run_id):
    actor, store = await start_actor(run_id)
    before = await store.snapshot(run_id)

    await actor.submit(ApiCommand(CancelCommand(expected_version=before.state.version - 1)))
    await actor.join()
    assert await store.snapshot(run_id) == before
    assert store.commit_count == 1

    await actor.submit(ApiCommand(CancelCommand(expected_version=before.state.version)))
    await actor.join()
    cancelled = await store.snapshot(run_id)
    assert cancelled.state.status is RunStatus.Cancelled
    assert cancelled.state.cancel_requested is True
    await actor.stop()


@pytest.mark.asyncio
async def test_cancelled_run_rejects_new_dispatch_and_continuation(run_id):
    actor, store = await start_actor(run_id)
    version = (await store.snapshot(run_id)).state.version
    await actor.submit(ApiCommand(CancelCommand(expected_version=version)))
    await actor.join()

    accepted = await actor.dispatch_started(None)
    assert accepted is False
    await actor.submit(
        ApiCommand(
            SessionInputCommand(
                kind="segment_stopped",
                payload={"generation": 0, "material_progress": True},
            )
        )
    )
    await actor.join()
    assert (await store.snapshot(run_id)).state.status is RunStatus.Cancelled
    await actor.stop()


@pytest.mark.asyncio
async def test_segment_continuation_requires_progress_and_has_independent_cap(run_id):
    actor, store = await start_actor(run_id)
    for _ in range(4):
        await actor.submit(
            ApiCommand(
                SessionInputCommand(
                    kind="segment_stopped",
                    payload={"generation": 0, "material_progress": True},
                )
            )
        )
    await actor.join()
    events = (await store.snapshot(run_id)).events
    assert [event.event_type for event in events].count("session.continuation_scheduled") == 3
    assert events[-1].event_type == "slice.terminal_failed"
    await actor.stop()


def test_failure_reason_contract_is_used_without_runtime_duplication():
    assert FailureReason.BudgetExhausted.value == "BUDGET_EXHAUSTED"

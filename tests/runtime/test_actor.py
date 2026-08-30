from __future__ import annotations

import asyncio

import pytest

from codemigrator.core import (
    Advice,
    AdviceKind,
    FailureReason,
    ResidentRole,
    RunStatus,
    Sha256,
)
from codemigrator.runtime.actor import ActorRegistry, RunActor
from codemigrator.runtime.advice import AdviceValidationContext, advice_proposal_hash
from codemigrator.runtime.contracts import (
    AdviceMessage,
    ApiCommand,
    CancelCommand,
    CreateRunCommand,
    SessionInputCommand,
)
from codemigrator.runtime.integration import IntegrationCoordinator, IntegrationItem
from codemigrator.runtime.store import InMemoryRuntimeStore

from .conftest import create_run, uid


class RecordingCancellation:
    def __init__(self):
        self.run_ids = []

    async def cancel(self, run_id):
        self.run_ids.append(run_id)


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


@pytest.mark.asyncio
async def test_actor_registry_race_returns_one_actor(run_id):
    registry = ActorRegistry(InMemoryRuntimeStore())
    actors = await asyncio.gather(
        registry.get_or_create(run_id),
        registry.get_or_create(run_id),
    )
    assert actors[0] is actors[1]
    await registry.close()


@pytest.mark.asyncio
async def test_advice_adoption_changes_projection_and_boundary_advice_waits_for_confirmation(
    run_id,
):
    store = InMemoryRuntimeStore()
    actor = RunActor(
        run_id,
        store,
        advice_context=AdviceValidationContext(expected_subjects=frozenset({"module-a"})),
    )
    await actor.start()
    await actor.create(create_run())
    auto = Advice(
        advice_id=uid(),
        kind=AdviceKind.ExploreReassignment,
        run_id=run_id,
        role=ResidentRole.ExecuteSupervisor,
        payload={"assignments": {"module-a": "slice-a"}},
        proposal_hash=Sha256("0" * 64),
    )
    auto = auto.model_copy(update={"proposal_hash": Sha256(advice_proposal_hash(auto))})
    await actor.submit(AdviceMessage(auto))
    await actor.join()
    assert str(auto.advice_id) in actor.state.adopted_advice_ids

    boundary = Advice(
        advice_id=uid(),
        kind=AdviceKind.AskUser,
        run_id=run_id,
        role=ResidentRole.ExecuteSupervisor,
        payload={"question": "confirm"},
        proposal_hash=Sha256("0" * 64),
    )
    boundary = boundary.model_copy(update={"proposal_hash": Sha256(advice_proposal_hash(boundary))})
    await actor.submit(AdviceMessage(boundary))
    await actor.join()
    assert str(boundary.advice_id) in actor.state.pending_advice_ids
    await actor.submit(
        ApiCommand(
            SessionInputCommand(
                kind="confirm_advice",
                payload={"advice_id": str(boundary.advice_id)},
            )
        )
    )
    await actor.join()
    assert str(boundary.advice_id) not in actor.state.pending_advice_ids
    assert str(boundary.advice_id) in actor.state.adopted_advice_ids
    await actor.stop()


@pytest.mark.asyncio
async def test_cancel_propagates_and_closes_integration_admission(run_id):
    cancellation = RecordingCancellation()
    integrations = IntegrationCoordinator()
    integrations.enqueue(IntegrationItem(str(run_id), "slice-a", 0, "oid"))
    store = InMemoryRuntimeStore()
    actor = RunActor(
        run_id,
        store,
        cancellation_port=cancellation,
        integration_coordinator=integrations,
    )
    await actor.start()
    await actor.create(create_run())
    version = (await store.snapshot(run_id)).state.version
    await actor.submit(ApiCommand(CancelCommand(expected_version=version)))
    await actor.join()
    assert cancellation.run_ids == [run_id]
    assert integrations.start_next(str(run_id), "verified-0") is None
    await actor.stop()

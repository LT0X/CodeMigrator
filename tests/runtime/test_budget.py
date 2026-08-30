from __future__ import annotations

import pytest

from codemigrator.core import FailureReason, RunStatus
from codemigrator.runtime.actor import RunActor
from codemigrator.runtime.budget import BudgetLimits, BudgetUsage, evaluate_budget
from codemigrator.runtime.contracts import BudgetEventMessage
from codemigrator.runtime.store import InMemoryRuntimeStore

from .conftest import create_run


class FailingCheckpoint:
    async def write(self, state):
        raise OSError("checkpoint unavailable")


class RecordingCheckpoint:
    def __init__(self):
        self.states = []

    async def write(self, state):
        self.states.append(state)


def test_budget_warning_is_thresholded_and_exhaustion_is_terminal():
    limits = BudgetLimits(input_tokens=100, output_tokens=100, cost_micros=100)
    first = evaluate_budget(BudgetUsage(), limits, input_tokens=80, output_tokens=0, cost_micros=0)
    second = evaluate_budget(
        first.usage,
        limits,
        input_tokens=20,
        output_tokens=0,
        cost_micros=0,
        warning_already_emitted=True,
    )
    assert first.warning is True
    assert second.warning is False
    assert second.exhausted is True


@pytest.mark.asyncio
async def test_budget_100_closes_new_calls_and_fails_run_in_order(run_id):
    store = InMemoryRuntimeStore()
    actor = RunActor(run_id, store, budget_limits=BudgetLimits(100, 100, 100))
    await actor.start()
    await actor.create(create_run())
    await actor.submit(
        BudgetEventMessage(input_tokens=100, output_tokens=0, cost_micros=0)
    )
    await actor.join()
    snapshot = await store.snapshot(run_id)
    assert snapshot.state.status is RunStatus.Failed
    assert snapshot.state.failure_reason is FailureReason.BudgetExhausted
    assert snapshot.state.new_calls_enabled is False
    types = [event.event_type for event in snapshot.events]
    assert types[-4:] == ["checkpoint.pre", "run.archived", "run.failed", "budget.exhausted"]
    await actor.stop()


@pytest.mark.asyncio
async def test_budget_warning_is_emitted_once(run_id):
    store = InMemoryRuntimeStore()
    actor = RunActor(run_id, store, budget_limits=BudgetLimits(100, 100, 100))
    await actor.start()
    await actor.create(create_run())
    await actor.submit(BudgetEventMessage(input_tokens=80, output_tokens=0, cost_micros=0))
    await actor.submit(BudgetEventMessage(input_tokens=1, output_tokens=0, cost_micros=0))
    await actor.join()
    events = (await store.snapshot(run_id)).events
    assert [event.event_type for event in events].count("budget.warning") == 1
    await actor.stop()


@pytest.mark.asyncio
async def test_checkpoint_failure_cannot_reopen_budget_breaker(run_id):
    store = InMemoryRuntimeStore()
    actor = RunActor(
        run_id,
        store,
        budget_limits=BudgetLimits(100, 100, 100),
        checkpoint_writer=FailingCheckpoint(),
    )
    await actor.start()
    await actor.create(create_run())
    await actor.submit(BudgetEventMessage(input_tokens=100, output_tokens=0, cost_micros=0))
    await actor.join()
    snapshot = await store.snapshot(run_id)
    assert snapshot.state.new_calls_enabled is False
    assert snapshot.state.failure_reason is FailureReason.BudgetExhausted
    assert actor.last_error is not None
    await actor.stop()


@pytest.mark.asyncio
async def test_budget_checkpoint_observes_pre_terminal_state(run_id):
    recorder = RecordingCheckpoint()
    store = InMemoryRuntimeStore()
    actor = RunActor(
        run_id,
        store,
        budget_limits=BudgetLimits(100, 100, 100),
        checkpoint_writer=recorder,
    )
    await actor.start()
    await actor.create(create_run())
    await actor.submit(BudgetEventMessage(input_tokens=100, output_tokens=0, cost_micros=0))
    await actor.join()
    assert len(recorder.states) == 1
    assert recorder.states[0].status is RunStatus.Planning
    assert recorder.states[0].new_calls_enabled is False
    await actor.stop()

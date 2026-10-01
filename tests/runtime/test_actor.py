from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from codemigrator.core import (
    Advice,
    AdviceKind,
    BranchPrefix,
    FailureReason,
    Phase,
    ResidentRole,
    RunStatus,
    SessionKind,
    Sha256,
)
from codemigrator.runtime.actor import ActorRegistry, RunActor
from codemigrator.runtime.advice import AdviceValidationContext, advice_proposal_hash
from codemigrator.runtime.agent_runs import AgentRun, AgentRunId, AgentRunReceipt
from codemigrator.runtime.cas import CasObject
from codemigrator.runtime.contracts import (
    ActorPhaseReceipt,
    AdviceMessage,
    ApiCommand,
    CancelCommand,
    CreateRunCommand,
    EventSpec,
    ExecutionRoundDecision,
    ReportSummary,
    RunState,
    SessionInputCommand,
    VerificationSummary,
)
from codemigrator.runtime.integration import IntegrationCoordinator, IntegrationItem
from codemigrator.runtime.loop_contracts import SessionExit, SessionState
from codemigrator.runtime.run_graph import PlanAgentCompletion
from codemigrator.runtime.store import InMemoryRuntimeStore, StoreCommitError

from .conftest import create_run, uid


class RecordingCancellation:
    def __init__(self):
        self.run_ids = []

    async def cancel(self, run_id):
        self.run_ids.append(run_id)


class RecordingRepairDispatch:
    def __init__(self):
        self.advices = []

    async def dispatch_adopted(self, advice):
        self.advices.append(advice)


class RecordingScheduler:
    def __init__(self) -> None:
        self.calls = []

    async def advance_one_round(self, run_id, logical_key, *, on_agent_run_started=None):
        del on_agent_run_started
        self.calls.append((run_id, logical_key))
        return ExecutionRoundDecision(
            complete=len(self.calls) == 2,
            dispatch_count=2 if len(self.calls) == 1 else 0,
        )


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
async def test_actor_registry_uses_injected_factory_when_restoring_active_run(run_id):
    store = InMemoryRuntimeStore()
    await store.create(
        RunState(run_id=run_id, status=RunStatus.Planning, version=1),
        (EventSpec("run.created", {"receipt_key": f"run.created:{run_id}"}),),
    )
    scheduler = RecordingScheduler()
    calls = []

    def actor_factory(received_run_id, received_store):
        calls.append((received_run_id, received_store))
        return RunActor(received_run_id, received_store, execution_scheduler=scheduler)

    registry = ActorRegistry(store, actor_factory=actor_factory)
    actor = await registry.get_or_create(run_id)

    assert actor is not None
    assert actor.execution_scheduler is scheduler
    assert calls == [(run_id, store)]
    assert await registry.get_or_create(run_id) is actor
    assert len(calls) == 1
    await registry.close()


@pytest.mark.asyncio
async def test_actor_registry_rejects_factory_actor_with_wrong_owner(run_id):
    store = InMemoryRuntimeStore()
    await store.create(
        RunState(run_id=run_id, status=RunStatus.Planning, version=1),
        (EventSpec("run.created", {"receipt_key": f"run.created:{run_id}"}),),
    )
    mismatched = RunActor(uuid4(), store)
    registry = ActorRegistry(store, actor_factory=lambda _run_id, _store: mismatched)

    with pytest.raises(StoreCommitError, match="RunActor factory"):
        await registry.get_or_create(run_id)

    assert registry.active_actor_count == 0
    assert mismatched._task is None


@pytest.mark.asyncio
async def test_actor_registry_rejects_factory_actor_with_wrong_store(run_id):
    store = InMemoryRuntimeStore()
    await store.create(
        RunState(run_id=run_id, status=RunStatus.Planning, version=1),
        (EventSpec("run.created", {"receipt_key": f"run.created:{run_id}"}),),
    )
    mismatched = RunActor(run_id, InMemoryRuntimeStore())
    registry = ActorRegistry(store, actor_factory=lambda _run_id, _store: mismatched)

    with pytest.raises(StoreCommitError, match="RunActor factory"):
        await registry.get_or_create(run_id)

    assert registry.active_actor_count == 0
    assert mismatched._task is None


@pytest.mark.asyncio
async def test_run_created_receipt_is_recovered_from_committed_event(run_id):
    store = InMemoryRuntimeStore()
    actor = RunActor(run_id, store)
    await actor.start()

    receipt = await actor.create(create_run())

    assert receipt is not None
    snapshot = await store.snapshot(run_id)
    assert receipt.run_id == run_id
    assert receipt.event_sequence == 1
    assert receipt.state_version == snapshot.state.version == 1
    assert snapshot.state.create_request == create_run()
    assert snapshot.events[0].data["receipt_key"] == receipt.receipt_key
    await actor.submit(ApiCommand(SessionInputCommand(kind="advance", payload={})))
    await actor.join()
    await actor.stop()

    recovered = RunActor(run_id, store)
    await recovered.start()
    commit_count = store.commit_count
    assert await recovered.create(create_run()) == receipt
    assert (
        await recovered.create(
            create_run().model_copy(update={"branch_prefix": BranchPrefix("other")})
        )
        is None
    )
    assert store.commit_count == commit_count
    await recovered.stop()


@pytest.mark.asyncio
async def test_execute_agent_run_start_is_committed_while_scheduler_is_still_running(run_id):
    class PausingScheduler:
        def __init__(self, agent_run_id):
            self.agent_run_id = agent_run_id
            self.started = asyncio.Event()
            self.resume = asyncio.Event()

        async def advance_one_round(self, owner_run_id, logical_key, *, on_agent_run_started):
            receipt = await on_agent_run_started(owner_run_id, self.agent_run_id)
            assert receipt.receipt_key == f"agent_run.started:{self.agent_run_id}"
            self.started.set()
            await self.resume.wait()
            return ExecutionRoundDecision(complete=False, dispatch_count=1)

    store = InMemoryRuntimeStore()
    await store.create(
        RunState(
            run_id=run_id,
            status=RunStatus.Executing,
            version=1,
            frozen_plan_sha256="c" * 64,
        ),
        (EventSpec("run.created", {"receipt_key": f"run.created:{run_id}"}),),
    )
    record = AgentRun(
        agent_run_id=AgentRunId(uuid4()),
        owner_kind="run",
        owner_id=run_id,
        logical_task_key="execute:slice-1:g0",
        phase=Phase.Execute,
        session_kind=SessionKind.Implementation,
        thread_id=str(uuid4()),
        model_binding_sha256="a" * 64,
        context_sha256="b" * 64,
        toolset_sha256="c" * 64,
        template_sha256="d" * 64,
    )
    await store.create_or_get_agent_run(record)
    scheduler = PausingScheduler(record.agent_run_id)
    actor = RunActor(run_id, store, execution_scheduler=scheduler)
    await actor.start()

    round_task = asyncio.create_task(actor.advance_execution_round(run_id, "execute-round-0"))
    await asyncio.wait_for(scheduler.started.wait(), timeout=1)
    snapshot = await store.snapshot(run_id)
    lifecycle = [event for event in snapshot.events if event.event_type == "agent_run.started"]
    assert len(lifecycle) == 1
    assert lifecycle[0].data["agent_run_id"] == str(record.agent_run_id)
    assert snapshot.state.status is RunStatus.Executing
    commits_after_start = store.commit_count
    assert await actor.record_agent_run_started(run_id, record.agent_run_id) == ActorPhaseReceipt(
        run_id, f"agent_run.started:{record.agent_run_id}", lifecycle[0].sequence
    )
    assert store.commit_count == commits_after_start

    scheduler.resume.set()
    await round_task
    await actor.stop()


@pytest.mark.asyncio
async def test_stop_cancels_and_awaits_execution_scheduler_before_return(run_id):
    class BlockingScheduler:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def advance_one_round(self, owner_run_id, logical_key, *, on_agent_run_started):
            del owner_run_id, logical_key, on_agent_run_started
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled.set()

    store = InMemoryRuntimeStore()
    await store.create(
        RunState(
            run_id=run_id,
            status=RunStatus.Executing,
            version=1,
            frozen_plan_sha256="c" * 64,
        ),
        (EventSpec("run.created", {"receipt_key": f"run.created:{run_id}"}),),
    )
    scheduler = BlockingScheduler()
    actor = RunActor(run_id, store, execution_scheduler=scheduler)
    await actor.start()
    round_task = asyncio.create_task(actor.advance_execution_round(run_id, "execute:blocked"))
    await asyncio.wait_for(scheduler.started.wait(), timeout=1)
    commits_before_stop = store.commit_count

    await actor.stop()

    assert scheduler.cancelled.is_set()
    with pytest.raises(StoreCommitError, match="stopped"):
        await asyncio.wait_for(round_task, timeout=1)
    assert store.commit_count == commits_before_stop
    await asyncio.sleep(0)
    assert store.commit_count == commits_before_stop


@pytest.mark.asyncio
async def test_close_admission_cancels_inflight_cancel_before_actor_close(run_id):
    class PausingCommitStore(InMemoryRuntimeStore):
        def __init__(self) -> None:
            super().__init__()
            self.cancel_commit_started = asyncio.Event()
            self.resume_cancel_commit = asyncio.Event()

        async def commit(self, state, events, *, evolution=None):
            if any(event.event_type == "run.cancelled" for event in events):
                self.cancel_commit_started.set()
                await self.resume_cancel_commit.wait()
            return await super().commit(state, events, evolution=evolution)

    store = PausingCommitStore()
    actor = RunActor(run_id, store)
    await actor.start()
    assert await actor.create(create_run()) is not None
    cancel_task = asyncio.create_task(actor.cancel(expected_version=1))
    await asyncio.wait_for(store.cancel_commit_started.wait(), timeout=1)
    commits_before_cancel = store.commit_count

    try:
        actor.close_admission()
        with pytest.raises(StoreCommitError, match="stopping"):
            await asyncio.wait_for(cancel_task, timeout=1)
        await actor.stop()
        snapshot = await store.snapshot(run_id)
        assert snapshot.state.status is RunStatus.Planning
        assert store.commit_count == commits_before_cancel
    finally:
        store.resume_cancel_commit.set()
        if not cancel_task.done():
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
        await actor.stop()


@pytest.mark.asyncio
async def test_plan_acceptance_atomically_commits_frozen_plan_agent_and_owner_receipts(run_id):
    store = InMemoryRuntimeStore()
    actor = RunActor(run_id, store)
    await actor.start()
    assert await actor.create(create_run()) is not None
    created = AgentRun(
        agent_run_id=AgentRunId(uuid4()),
        owner_kind="run",
        owner_id=run_id,
        logical_task_key=f"plan:{run_id}",
        phase=Phase.Plan,
        session_kind=SessionKind.PlanAuxiliary,
        thread_id=str(uuid4()),
        model_binding_sha256="a" * 64,
        context_sha256="b" * 64,
        toolset_sha256="c" * 64,
        template_sha256="d" * 64,
    )
    await store.create_or_get_agent_run(created)
    start_receipt = await actor.record_agent_run_started(run_id, created.agent_run_id)
    result_object = CasObject("e" * 64, 12)
    frozen_plan_payload = b"canonical frozen plan object"
    plan_object = CasObject(
        hashlib.sha256(frozen_plan_payload).hexdigest(), len(frozen_plan_payload)
    )
    frozen_plan_hash = "f" * 64
    completed = replace(
        created,
        state=SessionState.Closed,
        exit=SessionExit.Completed,
        result_sha256=result_object.digest,
    )
    completion = PlanAgentCompletion(
        record=completed,
        receipt=AgentRunReceipt(uuid4(), completed.agent_run_id, "plan.accepted"),
        result_object=result_object,
        plan_object=plan_object,
    )
    frozen_plan = SimpleNamespace(
        plan_hash=frozen_plan_hash,
        validation=SimpleNamespace(accepted=True),
        canonical_payload=lambda: frozen_plan_payload,
    )

    receipt = await actor.accept_plan(run_id, completion, frozen_plan)

    snapshot = await store.snapshot(run_id)
    assert receipt.receipt_key == f"run.plan.accepted:{run_id}"
    assert snapshot.state.status is RunStatus.Executing
    assert snapshot.state.frozen_plan_sha256 == frozen_plan_hash
    assert {event.event_type for event in snapshot.events} >= {
        "agent_run.started",
        "agent_run.terminal",
        "run.plan.accepted",
    }
    event_types = [event.event_type for event in snapshot.events]
    assert event_types.index("agent_run.started") < event_types.index("agent_run.terminal")
    assert start_receipt.receipt_key == f"agent_run.started:{created.agent_run_id}"
    assert await store.load_agent_run_receipt(completed.agent_run_id) == completion.receipt
    assert (
        await store.get_cas_reference("run", run_id, f"agent-result:{completed.agent_run_id}")
        == result_object
    )
    assert await store.get_cas_reference("run", run_id, "frozen-plan") == plan_object
    commits = store.commit_count
    assert await actor.accept_plan(run_id, completion, frozen_plan) == receipt
    assert store.commit_count == commits
    await actor.stop()


@pytest.mark.asyncio
async def test_execute_round_receipts_are_actor_idempotent_and_verify_report_are_deterministic(
    run_id,
):
    store = InMemoryRuntimeStore()
    await store.create(
        RunState(
            run_id=run_id,
            status=RunStatus.Executing,
            version=1,
            frozen_plan_sha256="c" * 64,
        ),
        (
            EventSpec(
                "run.created",
                {"receipt_key": f"run.created:{run_id}", "status": RunStatus.Executing.value},
            ),
        ),
    )
    scheduler = RecordingScheduler()
    actor = RunActor(run_id, store, execution_scheduler=scheduler)
    await actor.start()

    first = await actor.advance_execution_round(run_id, f"execute:{run_id}:0")
    replay = await actor.advance_execution_round(run_id, f"execute:{run_id}:0")
    second = await actor.advance_execution_round(run_id, f"execute:{run_id}:1")
    verification = await actor.commit_verification(
        run_id,
        VerificationSummary(passed=True, result_sha256="a" * 64),
        f"verify:{run_id}",
    )
    report = await actor.commit_report(
        run_id,
        ReportSummary(result_sha256="b" * 64, status=RunStatus.Completed),
        f"report:{run_id}",
    )

    assert first == replay
    assert first.complete is False and second.complete is True
    assert len(scheduler.calls) == 2
    assert verification.receipt_key == f"verify:{run_id}"
    assert report.receipt_key == f"report:{run_id}"
    snapshot = await store.snapshot(run_id)
    assert snapshot.state.status is RunStatus.Completed
    assert [event.event_type for event in snapshot.events].count("run.execute.round") == 2
    assert [event.event_type for event in snapshot.events].count("run.verify.completed") == 1
    assert [event.event_type for event in snapshot.events].count("run.report.completed") == 1
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
    store = InMemoryRuntimeStore()
    seed = RunActor(run_id, store)
    await seed.start()
    await seed.create(create_run())
    await seed.stop()
    registry = ActorRegistry(store)
    actors = await asyncio.gather(
        registry.get_or_create(run_id),
        registry.get_or_create(run_id),
    )
    assert actors[0] is actors[1]
    assert actors[0] is not None
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
async def test_adopted_repair_advice_is_forwarded_after_actor_commit(run_id):
    store = InMemoryRuntimeStore()
    repair_dispatch = RecordingRepairDispatch()
    slice_id = uid()
    actor = RunActor(
        run_id,
        store,
        advice_context=AdviceValidationContext(attribution_candidates=frozenset({slice_id})),
        repair_advice_port=repair_dispatch,
    )
    await actor.start()
    await actor.create(create_run())
    advice = Advice(
        advice_id=uid(),
        kind=AdviceKind.RepairDecision,
        run_id=run_id,
        role=ResidentRole.ExecuteSupervisor,
        payload={"repair_set": [str(slice_id)]},
        proposal_hash=Sha256("0" * 64),
    )
    advice = advice.model_copy(update={"proposal_hash": Sha256(advice_proposal_hash(advice))})
    await actor.submit(AdviceMessage(advice))
    await actor.join()
    assert repair_dispatch.advices == [advice]
    assert (await store.snapshot(run_id)).events[-1].event_type == "advice.adopted"
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

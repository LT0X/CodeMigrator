from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from codemigrator.core import (
    ContextPackIdentity,
    GitOid,
    MigrationSlice,
    ModelProfile,
    Phase,
    ProjectModuleId,
    RunId,
    RunStatus,
    SessionKind,
    Sha256,
    SliceCandidate,
    SliceGenerationRef,
    SliceId,
    SliceKind,
    WriteScope,
    WriteScopeOut,
    load_resource,
)
from codemigrator.runtime.actor import RunActor
from codemigrator.runtime.agent_runs import AgentRun, AgentRunId, AgentRunReceipt
from codemigrator.runtime.binding import LockedModelBinding
from codemigrator.runtime.cas import CasObject, FileHostCAS
from codemigrator.runtime.checkpointer import CasCheckpointSaver
from codemigrator.runtime.context import ContextEnvelope, ContextSegment
from codemigrator.runtime.contracts import ActorPhaseReceipt, RunState
from codemigrator.runtime.graph_composition import AgentGraphInfrastructure
from codemigrator.runtime.loop_contracts import SessionExit, SessionState
from codemigrator.runtime.memory import ContextManager, FormulaNetInputCap
from codemigrator.runtime.provider import (
    ProviderRegistry,
    ProviderRequest,
    ProviderResponse,
    TokenUsage,
)
from codemigrator.runtime.store import InMemoryRuntimeStore
from codemigrator.workspace import (
    CheckpointManifest,
    CheckpointReceipt,
    GatewayContext,
    checkpoint_receipt_digest,
)


class ExactCounter:
    def count(self, messages) -> int:
        return sum(len(message.content) for message in messages)

    def count_tool_schemas(self, tools) -> int:
        return sum(len(json.dumps(dict(tool.parameters))) for tool in tools)


class CompletedProvider:
    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        return ProviderResponse(
            content="slice work completed",
            tool_calls=(),
            finish_reason="stop",
            usage=TokenUsage(8, 3),
            model="fixed-code-model",
            provider_receipt_id="synthetic-execute-1",
        )


class UsageSink:
    async def reserve_round(self, agent_run_id, call_id: str, *, max_rounds: int):
        return 1

    async def record(self, agent_run_id, usage, receipt) -> None:
        return None


class Gateway:
    def __init__(self, context: GatewayContext, write_scope: WriteScope | None = None) -> None:
        self.context = context
        self.write_scope = write_scope
        self.calls: list[object] = []

    def dispatch(self, raw_call, *, cancellation_token=None):
        self.calls.append(raw_call)
        return {"ok": True}


class CandidateCheckpoint:
    def __init__(self, receipt: CheckpointReceipt) -> None:
        self.receipt = receipt
        self.calls = 0

    async def checkpoint(self, record, material) -> CheckpointReceipt:
        self.calls += 1
        return self.receipt


def _schedule_material(run_id: RunId, slice_id: SliceId, path: str):
    from codemigrator.runtime.execution_agent import ExecutionSessionMaterial

    scope = WriteScope(out=WriteScopeOut(write_paths=[path], create_roots=[]))
    slice_ = MigrationSlice(
        id=slice_id,
        kind=SliceKind.Implementation,
        source_modules=[ProjectModuleId("00000000-0000-7000-8000-000000000001")],
        write_scope=scope,
        required_checks=[],
        integration_rank=0,
        proposal_ref=None,
    )
    slice_ref = SliceGenerationRef(
        slice_id=slice_id,
        generation=1,
        baseline_candidate_oid=GitOid("c" * 40),
    )
    binding = LockedModelBinding(
        provider_id="openai-compatible",
        model_id="fixed-code-model",
        profile=ModelProfile.Code,
        config_revision="local-test-v1",
        context_window=64_000,
        output_cap=2_048,
    )
    identity = ContextPackIdentity(
        run_id=run_id,
        phase=Phase.Execute,
        session=SessionKind.Implementation,
        slice=slice_ref,
        spec_sha256=Sha256("1" * 64),
        model_binding_sha256=Sha256(binding.digest),
        phase_policy_sha256=Sha256(load_resource("core://phase-tool-policy/v2").sha256),
        contract_refs_sha256=Sha256("2" * 64),
        plan_revision_sha256=Sha256("3" * 64),
    )
    candidate_oid = "b" * 40
    receipt = CheckpointReceipt(
        run_id=run_id,
        slice_id=slice_id,
        generation=1,
        expected_candidate_oid="c" * 40,
        new_candidate_oid=candidate_oid,
        manifest=CheckpointManifest(
            slice_candidate=SliceCandidate(
                run_id=run_id,
                slice_id=slice_id,
                generation=1,
                base_verified_oid=GitOid("a" * 40),
                candidate_commit_oid=GitOid("c" * 40),
            ),
            file_count=1,
            total_bytes=8,
            file_set_digest="d" * 64,
            scope_check_passed=True,
        ),
        idempotency_key="e" * 64,
    )
    return ExecutionSessionMaterial(
        run_id=run_id,
        slice_=slice_,
        slice_ref=slice_ref,
        logical_task_key=f"execute:{slice_id}:g1",
        binding=binding,
        context_identity=identity,
        envelope=ContextEnvelope(),
        task=f"Work on {path}.",
        candidate_checkpoint=CandidateCheckpoint(receipt),
    )


def test_execution_round_rejects_run_and_generation_mismatches() -> None:
    from codemigrator.runtime.execution_agent import ExecutionRoundPlan, ExecutionWorkItem
    from codemigrator.runtime.scheduler import ReadySlice, ResourcePool

    run_id = RunId(uuid4())
    slice_id = SliceId(uuid4())
    material = _schedule_material(run_id, slice_id, "src/one.py")
    mismatched_generation = ReadySlice(
        str(run_id),
        str(slice_id),
        frozenset(),
        frozenset({"src/one.py"}),
        ResourcePool.Model,
        generation=2,
    )
    with pytest.raises(ValueError, match="frozen material"):
        ExecutionWorkItem(mismatched_generation, material)

    item = ExecutionWorkItem(
        ReadySlice(
            str(run_id),
            str(slice_id),
            frozenset(),
            frozenset({"src/one.py"}),
            ResourcePool.Model,
            generation=1,
        ),
        material,
    )
    with pytest.raises(ValueError, match="inconsistent Slice facts"):
        ExecutionRoundPlan(
            run_id=RunId(uuid4()),
            work_items=(item,),
            completed_slice_ids=frozenset(),
            all_slice_ids=frozenset({str(slice_id)}),
            available_pools=frozenset({ResourcePool.Model}),
        )


@pytest.mark.parametrize("gateway_scope_matches_slice", [True, False])
@pytest.mark.asyncio
async def test_persistent_execute_agent_uses_actor_start_and_terminal_receipts(
    tmp_path: Path, gateway_scope_matches_slice: bool
) -> None:
    from codemigrator.runtime.execution_agent import (
        ExecutionSessionMaterial,
        PersistentExecutionAgentSessionFactory,
    )

    run_id = RunId(uuid4())
    slice_id = SliceId(uuid4())
    generation = 1
    baseline_oid = GitOid("c" * 40)
    candidate_oid = GitOid("b" * 40)
    scope = WriteScope(out=WriteScopeOut(write_paths=["src/converted.py"], create_roots=[]))
    slice_ = MigrationSlice(
        id=slice_id,
        kind=SliceKind.Implementation,
        source_modules=[ProjectModuleId("00000000-0000-7000-8000-000000000001")],
        write_scope=scope,
        required_checks=[],
        integration_rank=0,
        proposal_ref=None,
    )
    slice_ref = SliceGenerationRef(
        slice_id=slice_id,
        generation=generation,
        baseline_candidate_oid=baseline_oid,
    )
    binding = LockedModelBinding(
        provider_id="openai-compatible",
        model_id="fixed-code-model",
        profile=ModelProfile.Code,
        config_revision="local-test-v1",
        context_window=64_000,
        output_cap=2_048,
    )
    context_identity = ContextPackIdentity(
        run_id=run_id,
        phase=Phase.Execute,
        session=SessionKind.Implementation,
        slice=slice_ref,
        spec_sha256=Sha256("1" * 64),
        model_binding_sha256=Sha256(binding.digest),
        phase_policy_sha256=Sha256(load_resource("core://phase-tool-policy/v2").sha256),
        contract_refs_sha256=Sha256("2" * 64),
        plan_revision_sha256=Sha256("3" * 64),
    )
    host_cas = FileHostCAS(tmp_path / "cas")
    store = InMemoryRuntimeStore()
    await store.create(
        RunState(
            run_id=run_id,
            status=RunStatus.Executing,
            version=1,
            frozen_plan_sha256="4" * 64,
        ),
        (),
    )
    actor = RunActor(run_id, store)
    await actor.start()
    provider = CompletedProvider()
    context_manager = ContextManager(
        token_counter=ExactCounter(),
        net_input_cap=FormulaNetInputCap(),
    )
    infrastructure = AgentGraphInfrastructure(
        provider_registry=ProviderRegistry({"openai-compatible": provider}),
        context_manager=context_manager,
        tool_gateway=object(),
        runtime_store=store,
        host_cas=host_cas,
        cas_references=store,
        usage_sink=UsageSink(),
        run_checkpointer=CasCheckpointSaver(
            host_cas,
            store,
            graph_family="run",
            owner_kind="run",
            owner_id=run_id,
        ),
        draft_graph_checkpointer=CasCheckpointSaver(
            host_cas,
            store,
            graph_family="draft",
            owner_kind="draft",
            owner_id=uuid4(),
        ),
        agent_run_checkpointer=CasCheckpointSaver(
            host_cas,
            store,
            graph_family="agent",
            owner_kind="run",
            owner_id=run_id,
        ),
    )
    checkpoint_receipt = CheckpointReceipt(
        run_id=run_id,
        slice_id=slice_id,
        generation=generation,
        expected_candidate_oid=str(baseline_oid),
        new_candidate_oid=str(candidate_oid),
        manifest=CheckpointManifest(
            slice_candidate=SliceCandidate(
                run_id=run_id,
                slice_id=slice_id,
                generation=generation,
                base_verified_oid=GitOid("a" * 40),
                candidate_commit_oid=baseline_oid,
            ),
            file_count=1,
            total_bytes=12,
            file_set_digest="5" * 64,
            scope_check_passed=True,
        ),
        idempotency_key="6" * 64,
    )
    candidate_checkpoint = CandidateCheckpoint(checkpoint_receipt)
    material = ExecutionSessionMaterial(
        run_id=run_id,
        slice_=slice_,
        slice_ref=slice_ref,
        logical_task_key=f"execute:{slice_id}:g{generation}",
        binding=binding,
        context_identity=context_identity,
        envelope=ContextEnvelope(
            targeted=(
                ContextSegment(
                    kind="targeted",
                    content="Convert the assigned source module using the frozen contract.",
                    required=True,
                    evictable=False,
                ),
            )
        ),
        task="Implement the assigned Slice and stop after the requested work is complete.",
        candidate_checkpoint=candidate_checkpoint,
    )
    gateways: list[Gateway] = []

    def gateway_factory(record, session_material):
        gateway_scope = session_material.slice.write_scope
        if not gateway_scope_matches_slice:
            gateway_scope = WriteScope(
                out=WriteScopeOut(
                    write_paths=["src/converted.py"],
                    create_roots=["src"],
                )
            )
        gateway = Gateway(
            GatewayContext(
                run_id=run_id,
                agent_run_id=record.agent_run_id,
                phase_policy_sha256=load_resource("core://phase-tool-policy/v2").sha256,
                phase=Phase.Execute,
                session_kind=SessionKind.Implementation,
                slice_id=slice_id,
                generation=generation,
            ),
            write_scope=gateway_scope,
        )
        gateways.append(gateway)
        return gateway

    factory = PersistentExecutionAgentSessionFactory(
        infrastructure=infrastructure,
        gateway_factory=gateway_factory,
    )
    try:
        if not gateway_scope_matches_slice:
            with pytest.raises(ValueError, match="write scope"):
                await factory.get_or_create(material)
            assert gateways and provider.requests == []
            return

        session = await factory.get_or_create(material)
        outcome = await session.run(
            on_agent_run_started=actor.record_agent_run_started,
            on_agent_run_terminal=actor.record_agent_run_terminal,
        )

        record = await store.load_agent_run(outcome.record.agent_run_id)
        receipt = await store.load_agent_run_receipt(outcome.record.agent_run_id)
        snapshot = await store.snapshot(run_id)
        assert record is not None
        assert record.state is SessionState.Closed
        assert record.exit is SessionExit.Completed
        assert record.candidate_checkpoint_sha256 == outcome.record.candidate_checkpoint_sha256
        assert receipt is not None and receipt.category == "session.terminal"
        assert outcome.candidate_receipt == checkpoint_receipt
        assert candidate_checkpoint.calls == 1
        assert len(provider.requests) == 1
        assert tuple(tool.name for tool in provider.requests[0].tools) == (
            "ReadFile",
            "WriteFile",
            "EditFile",
            "QuerySourceAst",
            "Shell",
            "Exec",
        )
        assert any(
            "Implement the assigned Slice" in message.content
            for message in provider.requests[0].messages
        )
        assert gateways and all(not gateway.calls for gateway in gateways)
        assert [event.event_type for event in snapshot.events] == [
            "agent_run.started",
            "agent_run.terminal",
        ]
        assert "thread_id" not in snapshot.events[-1].data
        assert "slice work completed" not in json.dumps(snapshot.events[-1].data)
    finally:
        await actor.stop()


@pytest.mark.asyncio
async def test_execution_scheduler_honors_dependencies_scopes_and_actor_receipts() -> None:
    from codemigrator.runtime.execution_agent import (
        ExecutionAgentOutcome,
        ExecutionRoundPlan,
        ExecutionWorkItem,
        PersistentExecutionScheduler,
    )
    from codemigrator.runtime.scheduler import FairScheduler, ReadySlice, ResourcePool

    run_id = RunId(uuid4())
    first_id, dependent_id, independent_id = SliceId(uuid4()), SliceId(uuid4()), SliceId(uuid4())
    first_material = _schedule_material(run_id, first_id, "src/shared.py")
    dependent_material = _schedule_material(run_id, dependent_id, "src/dependent.py")
    independent_material = _schedule_material(run_id, independent_id, "src/shared.py")
    first = ExecutionWorkItem(
        ready=ReadySlice(
            str(run_id),
            str(first_id),
            frozenset(),
            frozenset({"src/shared.py"}),
            ResourcePool.Model,
            generation=1,
        ),
        material=first_material,
    )
    dependent = ExecutionWorkItem(
        ready=ReadySlice(
            str(run_id),
            str(dependent_id),
            frozenset({str(first_id)}),
            frozenset({"src/dependent.py"}),
            ResourcePool.Model,
            generation=1,
        ),
        material=dependent_material,
    )
    independent = ExecutionWorkItem(
        ready=ReadySlice(
            str(run_id),
            str(independent_id),
            frozenset(),
            frozenset({"src/shared.py"}),
            ResourcePool.Model,
            generation=1,
        ),
        material=independent_material,
    )

    def plan(completed: frozenset[str]) -> ExecutionRoundPlan:
        return ExecutionRoundPlan(
            run_id=run_id,
            work_items=tuple(
                item
                for item in (first, dependent, independent)
                if item.ready.slice_id not in completed
            ),
            completed_slice_ids=completed,
            all_slice_ids=frozenset({str(first_id), str(dependent_id), str(independent_id)}),
            available_pools=frozenset({ResourcePool.Model}),
        )

    class Loader:
        def __init__(self) -> None:
            self.plans = [plan(frozenset()), plan(frozenset({str(first_id)}))]

        async def load(self, requested_run_id, logical_key):
            assert requested_run_id == run_id
            return self.plans.pop(0)

    class Session:
        def __init__(self, work_item) -> None:
            self.work_item = work_item
            material = work_item.material
            self.receipt = material.candidate_checkpoint.receipt
            self.created = AgentRun(
                agent_run_id=AgentRunId(uuid4()),
                owner_kind="run",
                owner_id=run_id,
                logical_task_key=material.logical_task_key,
                phase=Phase.Execute,
                session_kind=SessionKind.Implementation,
                thread_id=str(uuid4()),
                model_binding_sha256=material.binding.digest,
                context_sha256="f" * 64,
                toolset_sha256="1" * 64,
                template_sha256="2" * 64,
                slice_ref=material.slice_ref,
                write_scope_sha256="3" * 64,
            )
            self.agent_run = self.created

        async def run(self, *, on_agent_run_started, on_agent_run_terminal):
            started = await on_agent_run_started(run_id, self.created.agent_run_id)
            assert started.receipt_key == f"agent_run.started:{self.created.agent_run_id}"
            result = CasObject("4" * 64, 8)
            terminal_record = replace(
                self.created,
                state=SessionState.Closed,
                exit=SessionExit.Completed,
                result_sha256=result.digest,
                candidate_checkpoint_sha256=checkpoint_receipt_digest(self.receipt),
            )
            terminal_receipt = AgentRunReceipt(
                uuid4(), terminal_record.agent_run_id, "session.terminal"
            )
            terminal = await on_agent_run_terminal(
                run_id, terminal_record, terminal_receipt, result
            )
            assert terminal.receipt_key == f"agent_run.terminal:{terminal_record.agent_run_id}"
            return ExecutionAgentOutcome(terminal_record, self.receipt)

    class Sessions:
        def __init__(self) -> None:
            self.sessions: list[Session] = []

        async def get_or_create(self, material):
            work_item = next(
                item for item in (first, dependent, independent) if item.material is material
            )
            session = Session(work_item)
            self.sessions.append(session)
            return session

    sessions = Sessions()
    scheduler = PersistentExecutionScheduler(
        round_loader=Loader(),
        sessions=sessions,
        fair_scheduler=FairScheduler(),
        max_parallelism=3,
    )

    async def started_callback(owner_run_id, agent_run_id):
        return ActorPhaseReceipt(owner_run_id, f"agent_run.started:{agent_run_id}", 1)

    async def terminal_callback(owner_run_id, record, receipt, result_object):
        return ActorPhaseReceipt(owner_run_id, f"agent_run.terminal:{record.agent_run_id}", 2)

    first_round = await scheduler.advance_one_round(
        run_id,
        "round:1",
        on_agent_run_started=started_callback,
        on_agent_run_terminal=terminal_callback,
    )
    second_round = await scheduler.advance_one_round(
        run_id,
        "round:2",
        on_agent_run_started=started_callback,
        on_agent_run_terminal=terminal_callback,
    )

    assert first_round.dispatch_count == 1
    assert first_round.complete is False
    assert first_round.terminal_agent_run_ids == (sessions.sessions[0].created.agent_run_id,)
    assert second_round.dispatch_count == 2
    assert second_round.complete is False
    assert len(second_round.completed_write_agent_run_ids) == 2


def test_frozen_plan_dependency_projection_preserves_edge_kinds() -> None:
    from types import SimpleNamespace

    from codemigrator.core import PlanEdgeKind, SliceId
    from codemigrator.core.models.plan import PlanEdge
    from codemigrator.planning import FrozenPlan
    from codemigrator.runtime.execution_agent import project_frozen_plan_dependencies

    base_id, requires_id, ordered_id = SliceId(uuid4()), SliceId(uuid4()), SliceId(uuid4())
    frozen_plan = FrozenPlan.model_construct(
        slices=tuple(SimpleNamespace(id=item) for item in (base_id, requires_id, ordered_id)),
        edges=(
            PlanEdge(from_=base_id, to=requires_id, kind=PlanEdgeKind.Requires),
            PlanEdge(from_=base_id, to=ordered_id, kind=PlanEdgeKind.OrderedBefore),
        ),
    )

    projected = project_frozen_plan_dependencies(frozen_plan)

    assert projected[str(requires_id)].requires == frozenset({str(base_id)})
    assert projected[str(requires_id)].ordered_before == frozenset()
    assert projected[str(ordered_id)].requires == frozenset()
    assert projected[str(ordered_id)].ordered_before == frozenset({str(base_id)})


@pytest.mark.asyncio
async def test_execution_scheduler_keeps_shared_queue_work_with_its_run() -> None:
    from codemigrator.runtime.execution_agent import (
        ExecutionAgentOutcome,
        ExecutionRoundPlan,
        ExecutionWorkItem,
        PersistentExecutionScheduler,
    )
    from codemigrator.runtime.scheduler import FairScheduler, ReadySlice, ResourcePool

    first_run, second_run = RunId(uuid4()), RunId(uuid4())
    first_id, second_id = SliceId(uuid4()), SliceId(uuid4())
    first_material = _schedule_material(first_run, first_id, "src/first.py")
    second_material = _schedule_material(second_run, second_id, "src/second.py")
    work_by_run = {
        str(first_run): ExecutionWorkItem(
            ReadySlice(
                str(first_run),
                str(first_id),
                frozenset(),
                frozenset({"src/first.py"}),
                ResourcePool.Model,
                generation=1,
            ),
            first_material,
        ),
        str(second_run): ExecutionWorkItem(
            ReadySlice(
                str(second_run),
                str(second_id),
                frozenset(),
                frozenset({"src/second.py"}),
                ResourcePool.Model,
                generation=1,
            ),
            second_material,
        ),
    }

    class Loader:
        async def load(self, requested_run_id, logical_key):
            item = work_by_run[str(requested_run_id)]
            return ExecutionRoundPlan(
                run_id=requested_run_id,
                work_items=(item,),
                completed_slice_ids=frozenset(),
                all_slice_ids=frozenset({item.ready.slice_id}),
                available_pools=frozenset({ResourcePool.Model}),
            )

    class RecordingScheduler(PersistentExecutionScheduler):
        def __init__(self, fair_scheduler):
            super().__init__(
                round_loader=Loader(), sessions=object(), fair_scheduler=fair_scheduler,
                max_parallelism=2,
            )
            self.dispatched: list[str] = []

        async def _dispatch(self, item, **_callbacks):
            self.dispatched.append(item.ready.run_id)
            material = item.material
            receipt = material.candidate_checkpoint.receipt
            record = AgentRun(
                agent_run_id=AgentRunId(uuid4()),
                owner_kind="run",
                owner_id=material.run_id,
                logical_task_key=material.logical_task_key,
                phase=Phase.Execute,
                session_kind=material.context_identity.session,
                thread_id=str(uuid4()),
                model_binding_sha256=material.binding.digest,
                context_sha256="1" * 64,
                toolset_sha256="2" * 64,
                template_sha256="3" * 64,
                slice_ref=material.slice_ref,
                write_scope_sha256="4" * 64,
            )
            terminal = replace(
                record,
                state=SessionState.Closed,
                exit=SessionExit.Completed,
                result_sha256="5" * 64,
                candidate_checkpoint_sha256=checkpoint_receipt_digest(receipt),
            )
            return ExecutionAgentOutcome(terminal, receipt)

    shared = FairScheduler()
    shared.submit(work_by_run[str(first_run)].ready)
    shared.submit(work_by_run[str(second_run)].ready)
    scheduler = RecordingScheduler(shared)

    async def callback(*_args):
        raise AssertionError("stubbed dispatch does not call actor callbacks")

    first_decision = await scheduler.advance_one_round(
        first_run, "first", on_agent_run_started=callback, on_agent_run_terminal=callback
    )
    second_decision = await scheduler.advance_one_round(
        second_run, "second", on_agent_run_started=callback, on_agent_run_terminal=callback
    )

    assert first_decision.dispatch_count == second_decision.dispatch_count == 1
    assert scheduler.dispatched == [str(first_run), str(second_run)]


@pytest.mark.asyncio
async def test_execution_scheduler_releases_prior_reservations_on_plan_mismatch() -> None:
    from codemigrator.runtime.execution_agent import (
        ExecutionRoundPlan,
        ExecutionScheduleStalled,
        ExecutionWorkItem,
        PersistentExecutionScheduler,
    )
    from codemigrator.runtime.scheduler import FairScheduler, ReadySlice, ResourcePool

    run_id, slice_id = RunId(uuid4()), SliceId(uuid4())
    material = _schedule_material(run_id, slice_id, "src/expected.py")
    item = ExecutionWorkItem(
        ReadySlice(
            str(run_id),
            str(slice_id),
            frozenset(),
            frozenset({"src/expected.py"}),
            ResourcePool.Model,
            generation=1,
        ),
        material,
    )
    plan = ExecutionRoundPlan(
        run_id=run_id,
        work_items=(item,),
        completed_slice_ids=frozenset(),
        all_slice_ids=frozenset({str(slice_id)}),
        available_pools=frozenset({ResourcePool.Model}),
    )

    class Loader:
        async def load(self, requested_run_id, logical_key):
            return plan

    class InjectedMismatchScheduler(FairScheduler):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def next(self, active_scopes, available_pools, *, only_run_id=None):
            self.calls += 1
            if self.calls == 2:
                return ReadySlice(
                    str(run_id),
                    "not-in-frozen-plan",
                    frozenset(),
                    frozenset({"src/unexpected.py"}),
                    ResourcePool.Model,
                    generation=1,
                )
            return super().next(active_scopes, available_pools)

    shared = InjectedMismatchScheduler()
    scheduler = PersistentExecutionScheduler(
        round_loader=Loader(), sessions=object(), fair_scheduler=shared, max_parallelism=2
    )

    async def callback(*_args):
        raise AssertionError("selection failure occurs before dispatch")

    with pytest.raises(ExecutionScheduleStalled, match="frozen round plan"):
        await scheduler.advance_one_round(
            run_id, "mismatch", on_agent_run_started=callback, on_agent_run_terminal=callback
        )

    assert shared.next(frozenset(), frozenset({ResourcePool.Model})) == item.ready


@pytest.mark.asyncio
async def test_execution_scheduler_waits_for_parallel_dispatches_before_releasing_scopes() -> None:
    from codemigrator.runtime.execution_agent import (
        ExecutionRoundPlan,
        ExecutionWorkItem,
        PersistentExecutionScheduler,
    )
    from codemigrator.runtime.scheduler import FairScheduler, ReadySlice, ResourcePool

    run_id = RunId(uuid4())
    failing_id, active_id = SliceId(uuid4()), SliceId(uuid4())
    failing_material = _schedule_material(run_id, failing_id, "src/failing.py")
    active_material = _schedule_material(run_id, active_id, "src/active.py")
    work_items = (
        ExecutionWorkItem(
            ReadySlice(
                str(run_id),
                str(failing_id),
                frozenset(),
                frozenset({"src/failing.py"}),
                ResourcePool.Model,
                generation=1,
            ),
            failing_material,
        ),
        ExecutionWorkItem(
            ReadySlice(
                str(run_id),
                str(active_id),
                frozenset(),
                frozenset({"src/active.py"}),
                ResourcePool.Model,
                generation=1,
            ),
            active_material,
        ),
    )
    plan = ExecutionRoundPlan(
        run_id=run_id,
        work_items=work_items,
        completed_slice_ids=frozenset(),
        all_slice_ids=frozenset({str(failing_id), str(active_id)}),
        available_pools=frozenset({ResourcePool.Model}),
    )
    sibling_started = asyncio.Event()
    failure_raised = asyncio.Event()
    release_sibling = asyncio.Event()
    sibling_finished = asyncio.Event()

    class Loader:
        async def load(self, requested_run_id, logical_key):
            assert requested_run_id == run_id
            return plan

    class Sessions:
        async def get_or_create(self, material):
            if material.slice_ref.slice_id == failing_id:
                await sibling_started.wait()
                failure_raised.set()
                await asyncio.sleep(0)
                raise RuntimeError("synthetic dispatch failure")
            sibling_started.set()
            await release_sibling.wait()
            sibling_finished.set()
            raise RuntimeError("synthetic sibling failure")

    scheduler = PersistentExecutionScheduler(
        round_loader=Loader(),
        sessions=Sessions(),
        fair_scheduler=FairScheduler(),
        max_parallelism=2,
    )

    async def callback(*args):
        raise AssertionError("sessions fail before owner callbacks")

    round_task = asyncio.create_task(
        scheduler.advance_one_round(
            run_id,
            "round:failure",
            on_agent_run_started=callback,
            on_agent_run_terminal=callback,
        )
    )
    await sibling_started.wait()
    await failure_raised.wait()
    try:
        done, _pending = await asyncio.wait({round_task}, timeout=0.01)
        assert not done
    finally:
        release_sibling.set()
    with pytest.raises(RuntimeError, match="synthetic"):
        await round_task
    assert sibling_finished.is_set()


@pytest.mark.asyncio
async def test_execution_scheduler_replays_committed_terminal_before_round_receipt() -> None:
    from codemigrator.runtime.execution_agent import (
        ExecutionRecoveredTerminal,
        ExecutionRoundPlan,
        PersistentExecutionScheduler,
    )
    from codemigrator.runtime.scheduler import FairScheduler, ResourcePool

    run_id, slice_id = RunId(uuid4()), SliceId(uuid4())
    material = _schedule_material(run_id, slice_id, "src/recovered.py")
    candidate_receipt = material.candidate_checkpoint.receipt
    record = AgentRun(
        agent_run_id=AgentRunId(uuid4()),
        owner_kind="run",
        owner_id=run_id,
        logical_task_key=material.logical_task_key,
        phase=Phase.Execute,
        session_kind=SessionKind.Implementation,
        thread_id=str(uuid4()),
        model_binding_sha256=material.binding.digest,
        context_sha256="a" * 64,
        toolset_sha256="b" * 64,
        template_sha256="c" * 64,
        slice_ref=material.slice_ref,
        write_scope_sha256="d" * 64,
    )
    terminal = replace(
        record,
        state=SessionState.Closed,
        exit=SessionExit.Completed,
        result_sha256="e" * 64,
        candidate_checkpoint_sha256=checkpoint_receipt_digest(candidate_receipt),
    )
    plan = ExecutionRoundPlan(
        run_id=run_id,
        work_items=(),
        completed_slice_ids=frozenset(),
        all_slice_ids=frozenset({str(slice_id)}),
        available_pools=frozenset({ResourcePool.Model}),
        recovered_terminals=(ExecutionRecoveredTerminal(terminal, candidate_receipt),),
    )

    class Loader:
        async def load(self, requested_run_id, logical_key):
            assert requested_run_id == run_id
            return plan

    class Sessions:
        async def get_or_create(self, material):
            raise AssertionError("a committed terminal AgentRun must not be invoked again")

    scheduler = PersistentExecutionScheduler(
        round_loader=Loader(),
        sessions=Sessions(),
        fair_scheduler=FairScheduler(),
    )

    async def callback(*args):
        raise AssertionError("replay must use the existing Actor terminal receipt")

    decision = await scheduler.advance_one_round(
        run_id,
        "round:replay-terminal",
        on_agent_run_started=callback,
        on_agent_run_terminal=callback,
    )

    assert decision.complete is False
    assert decision.dispatch_count == 0
    assert decision.terminal_agent_run_ids == (terminal.agent_run_id,)
    assert decision.completed_write_agent_run_ids == (terminal.agent_run_id,)
    assert decision.candidate_claims[0].receipt == candidate_receipt


@pytest.mark.asyncio
async def test_pending_candidate_without_integration_receipt_stalls_without_redispatch() -> None:
    from codemigrator.runtime.execution_agent import (
        ExecutionRoundPlan,
        ExecutionScheduleStalled,
        PersistentExecutionScheduler,
    )
    from codemigrator.runtime.scheduler import FairScheduler, ResourcePool

    run_id, slice_id = RunId(uuid4()), SliceId(uuid4())
    plan = ExecutionRoundPlan(
        run_id=run_id,
        work_items=(),
        completed_slice_ids=frozenset(),
        all_slice_ids=frozenset({str(slice_id)}),
        available_pools=frozenset({ResourcePool.Model}),
        pending_integrations=frozenset({(str(slice_id), 0)}),
    )

    class Loader:
        async def load(self, requested_run_id, logical_key):
            assert requested_run_id == run_id
            return plan

    class Sessions:
        async def get_or_create(self, material):
            raise AssertionError("a candidate awaiting integration must not be redispatched")

    scheduler = PersistentExecutionScheduler(
        round_loader=Loader(),
        sessions=Sessions(),
        fair_scheduler=FairScheduler(),
    )

    async def callback(*args):
        raise AssertionError("a pending candidate must not call the Agent")

    with pytest.raises(ExecutionScheduleStalled):
        await scheduler.advance_one_round(
            run_id,
            "round:await-integration",
            on_agent_run_started=callback,
            on_agent_run_terminal=callback,
        )

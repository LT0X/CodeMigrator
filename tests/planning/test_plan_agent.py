from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

from codemigrator.core import (
    ArtifactRef,
    BranchPrefix,
    CreateRun,
    GitRefName,
    ModelProfile,
    Phase,
    RemoteRepository,
    RepositoryUrl,
    RunId,
    SessionKind,
    load_resource,
)
from codemigrator.core.enums import RunStatus
from codemigrator.core.models.plan import PlanProposal
from codemigrator.runtime.actor import RunActor
from codemigrator.runtime.binding import LockedModelBinding
from codemigrator.runtime.cas import FileHostCAS
from codemigrator.runtime.checkpointer import CasCheckpointSaver
from codemigrator.runtime.context import ContextEnvelope
from codemigrator.runtime.graph_composition import AgentGraphInfrastructure
from codemigrator.runtime.memory import ContextManager, FormulaNetInputCap
from codemigrator.runtime.provider import (
    ProviderRegistry,
    ProviderRequest,
    ProviderResponse,
    ProviderToolCall,
    TokenUsage,
)
from codemigrator.runtime.store import InMemoryRuntimeStore
from codemigrator.workspace import GatewayContext


class ExactCounter:
    def count(self, messages) -> int:
        return sum(len(message.content) for message in messages)

    def count_tool_schemas(self, tools) -> int:
        return sum(len(json.dumps(dict(tool.parameters))) for tool in tools)


class StructuredPlanProvider:
    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        if len(self.requests) == 1:
            proposal = {
                "slices": [],
                "edges": [],
                "integration_ranks": {},
                "planner_rationale": [],
            }
        else:
            proposal = {
                "slices": [
                    {
                        "local_ref": "A",
                        "kind": "IMPLEMENTATION",
                        "source_modules": [
                            "00000000-0000-7000-8000-000000000001"
                        ],
                        "write_paths": ["target/a.py"],
                        "create_roots": ["target/a"],
                    },
                    {
                        "local_ref": "B",
                        "kind": "IMPLEMENTATION",
                        "source_modules": [
                            "00000000-0000-7000-8000-000000000002"
                        ],
                        "write_paths": ["target/b.py"],
                        "create_roots": ["target/b"],
                    },
                ],
                "edges": [],
                "integration_ranks": {"A": 0, "B": 1},
                "planner_rationale": [],
            }
        return ProviderResponse(
            content="",
            tool_calls=(
                ProviderToolCall(
                    "PlanProposal",
                    json.dumps(proposal),
                    f"proposal-{len(self.requests)}",
                ),
            ),
            finish_reason="tool_calls",
            usage=TokenUsage(7, 5),
            model="fixed-model",
            provider_receipt_id="provider-receipt-1",
        )


class UsageSink:
    async def reserve_round(self, agent_run_id, call_id, *, max_rounds):
        return 1

    async def record(self, agent_run_id, usage, receipt) -> None:
        return None


class Gateway:
    def __init__(self, context: GatewayContext) -> None:
        self.context = context
        self.calls: list[object] = []

    def dispatch(self, raw_call, *, cancellation_token=None):
        self.calls.append(raw_call)
        return {"ok": True}


class PlanMaterialLoader:
    def __init__(self, inputs, binding) -> None:
        self.inputs = inputs
        self.binding = binding

    async def load(self, run_id):
        from codemigrator.runtime.plan_agent import PlanSessionMaterial

        return PlanSessionMaterial(self.inputs, self.binding, ContextEnvelope())


@pytest_asyncio.fixture
async def persisted_run(planning_inputs):
    run_id = RunId(uuid4())
    store = InMemoryRuntimeStore()
    actor = RunActor(run_id, store)
    await actor.start()
    await actor.create(
        CreateRun(
            source=RemoteRepository(
                repository_url=RepositoryUrl("https://github.com/example/source"),
                base_ref=GitRefName("main"),
            ),
            branch_prefix=BranchPrefix("migration"),
            frozen_artifacts=planning_inputs.frozen_artifacts,
        )
    )
    try:
        yield run_id, store, actor
    finally:
        await actor.stop()


@pytest.mark.asyncio
async def test_persistent_plan_stage_validates_with_same_persistent_agent_run(
    planning_inputs, persisted_run, tmp_path: Path
) -> None:
    from codemigrator.runtime.plan_agent import (
        PersistentPlanAgentSessionFactory,
        PersistentPlanStageFactory,
    )

    run_id, store, actor = persisted_run
    cas = FileHostCAS(tmp_path / "cas")

    provider = StructuredPlanProvider()
    registry = ProviderRegistry({"openai-compatible": provider})
    context_manager = ContextManager(
        token_counter=ExactCounter(),
        net_input_cap=FormulaNetInputCap(),
    )
    infrastructure = AgentGraphInfrastructure(
        provider_registry=registry,
        context_manager=context_manager,
        tool_gateway=object(),
        runtime_store=store,
        host_cas=cas,
        cas_references=store,
        usage_sink=UsageSink(),
        run_checkpointer=CasCheckpointSaver(
            cas,
            store,
            graph_family="run",
            owner_kind="run",
            owner_id=run_id,
        ),
        draft_graph_checkpointer=CasCheckpointSaver(
            cas,
            store,
            graph_family="draft",
            owner_kind="draft",
            owner_id=uuid4(),
        ),
        agent_run_checkpointer=CasCheckpointSaver(
            cas,
            store,
            graph_family="agent",
            owner_kind="run",
            owner_id=run_id,
        ),
    )
    binding = LockedModelBinding(
        provider_id="openai-compatible",
        model_id="fixed-model",
        profile=ModelProfile.Reasoning,
        config_revision="local-test-v1",
        context_window=64_000,
        output_cap=2_048,
    )
    gateways: list[Gateway] = []

    def gateway_factory(record):
        gateway = Gateway(
            GatewayContext(
                run_id=run_id,
                agent_run_id=record.agent_run_id,
                phase_policy_sha256=load_resource("core://phase-tool-policy/v2").sha256,
                phase=Phase.Plan,
                session_kind=SessionKind.PlanAuxiliary,
            )
        )
        gateways.append(gateway)
        return gateway

    mismatched_bundle = planning_inputs.frozen_artifacts.model_copy(
        update={
            "spec": ArtifactRef(
                sha256="f" * 64,
                size=1,
                media_type="application/json",
            )
        }
    )
    mismatched_inputs = planning_inputs.model_copy(
        update={"frozen_artifacts": mismatched_bundle}
    )
    mismatched_factory = PersistentPlanAgentSessionFactory(
        infrastructure=infrastructure,
        actor=actor,
        material_loader=PlanMaterialLoader(mismatched_inputs, binding),
        gateway_factory=gateway_factory,
    )
    with pytest.raises(ValueError, match="differs from the Run's frozen artifacts"):
        await mismatched_factory.get_or_create(run_id, f"plan:{run_id}")
    assert await store.list_agent_runs_by_owner("run", run_id) == ()

    material_loader = PlanMaterialLoader(planning_inputs, binding)
    factory = PersistentPlanAgentSessionFactory(
        infrastructure=infrastructure,
        actor=actor,
        material_loader=material_loader,
        gateway_factory=gateway_factory,
    )
    session = await factory.get_or_create(run_id, f"plan:{run_id}")

    with pytest.raises(ValueError, match="started AgentRun receipt"):
        await session.propose(())
    assert provider.requests == []
    await actor.record_agent_run_started(run_id, session.agent_run.agent_run_id)
    interrupted_proposal = await session.propose(())
    assert interrupted_proposal.slices == []
    assert len(provider.requests) == 1

    stage_factory = PersistentPlanStageFactory(
        material_loader=material_loader,
        gateway_factory=gateway_factory,
    )
    workflow = stage_factory(infrastructure, actor)
    receipt = await workflow.run(run_id)
    record = await store.load_agent_run(session.agent_run.agent_run_id)
    assert record is not None
    assert record.state.value == "CLOSED"
    assert record.exit.value == "COMPLETED"
    assert record.checkpoint_sha256 is not None
    assert receipt.receipt_key == f"run.plan.accepted:{run_id}"
    assert await actor.has_receipt(run_id, receipt.receipt_key)
    assert (await store.load(run_id)).state.status is RunStatus.Executing

    assert len(provider.requests) == 2
    assert tuple(tool.name for tool in provider.requests[0].tools) == (
        "ReadFile",
        "QuerySourceAst",
        "Exec",
        "PlanProposal",
    )
    assert "planning-test" in " ".join(
        message.content for message in provider.requests[0].messages
    )
    assert "validation_feedback" in " ".join(
        message.content for message in provider.requests[1].messages
    )
    assert all(not gateway.calls for gateway in gateways)

    recovered = await factory.get_or_create(run_id, f"plan:{run_id}")
    assert recovered.agent_run.agent_run_id == session.agent_run.agent_run_id
    assert recovered.agent_run.thread_id == session.agent_run.thread_id
    recovered_proposal = await recovered.propose(())
    assert isinstance(recovered_proposal, PlanProposal)
    assert len(recovered_proposal.slices) == 2
    assert len(provider.requests) == 2
    assert len(await store.list_agent_runs_by_owner("run", run_id)) == 1
    checkpoints = await store.list_checkpoint_indexes(session.agent_run.thread_id)
    assert len(checkpoints) >= 2
    assert record.checkpoint_sha256 == checkpoints[0].object.digest
    assert await store.get_cas_reference("run", run_id, "frozen-plan") is not None
    assert (
        await store.get_cas_reference(
            "run", run_id, f"agent-result:{session.agent_run.agent_run_id}"
        )
        is not None
    )

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from codemigrator.runtime.graph_composition import (
    AgentGraphInfrastructure,
    RuntimeGraphAssembly,
    RuntimeGraphConfigurationError,
)
from codemigrator.runtime.provider import ProviderRegistry
from codemigrator.runtime.store import InMemoryRuntimeStore


def infrastructure(tmp_path, *, provider_registry=None):
    store = InMemoryRuntimeStore()
    cas = SimpleNamespace(root=tmp_path)
    return AgentGraphInfrastructure(
        provider_registry=provider_registry or ProviderRegistry({}),
        context_manager=object(),
        tool_gateway=object(),
        runtime_store=store,
        host_cas=cas,
        cas_references=store,
        usage_sink=object(),
        run_checkpointer=InMemorySaver(),
        draft_graph_checkpointer=InMemorySaver(),
        agent_run_checkpointer=InMemorySaver(),
    )


def test_graph_infrastructure_fails_closed_when_provider_is_missing(tmp_path):
    with pytest.raises(RuntimeGraphConfigurationError, match="provider_registry"):
        AgentGraphInfrastructure(
            provider_registry=None,  # type: ignore[arg-type]
            context_manager=object(),
            tool_gateway=object(),
            runtime_store=InMemoryRuntimeStore(),
            host_cas=object(),
            cas_references=object(),
            usage_sink=object(),
            run_checkpointer=InMemorySaver(),
            draft_graph_checkpointer=InMemorySaver(),
            agent_run_checkpointer=InMemorySaver(),
        )


def test_assembly_compiles_both_graphs_with_the_injected_runtime_dependencies(tmp_path):
    infra = infrastructure(tmp_path)
    seen = []

    def plan_factory(received, actor):
        seen.append(("plan", received, actor))
        return SimpleNamespace(run=lambda run_id: None)

    def verify_factory(received, actor):
        seen.append(("verify", received, actor))
        return SimpleNamespace(run=lambda run_id: None)

    def report_factory(received, actor):
        seen.append(("report", received, actor))
        return SimpleNamespace(run=lambda run_id: None)

    def draft_runner_factory(received, owner):
        seen.append(("draft_runner", received, owner))
        return SimpleNamespace(run=lambda draft_id, logical_key, task: None)

    def create_run_factory(received, owner):
        seen.append(("create_run", received, owner))
        return SimpleNamespace(create=lambda run_id, request: None)

    assembly = RuntimeGraphAssembly(
        infra,
        plan_stage_factory=plan_factory,
        verifier_factory=verify_factory,
        reporter_factory=report_factory,
        draft_agent_runner_factory=draft_runner_factory,
        create_run_service_factory=create_run_factory,
    )
    actor = SimpleNamespace()
    run_graph = assembly.build_run_graph(actor)
    owner = SimpleNamespace(draft_id=uuid4(), freeze_receipt=None)
    draft_graph = assembly.build_draft_graph(owner)

    assert run_graph.checkpointer is infra.run_checkpointer
    assert draft_graph.checkpointer is infra.draft_graph_checkpointer
    assert draft_graph.agent_checkpointer is infra.agent_run_checkpointer
    assert draft_graph.agent_runs is infra.runtime_store
    assert all(item[1] is infra for item in seen)
    assert all(item[2] is actor for item in seen[:3])
    assert all(item[2] is owner for item in seen[3:])
    assert {item[0] for item in seen} == {
        "plan",
        "verify",
        "report",
        "draft_runner",
        "create_run",
    }


def test_run_graph_starter_is_bound_to_assembly_and_requires_durable_attestation(tmp_path):
    infra = infrastructure(tmp_path)
    assembly = RuntimeGraphAssembly(
        infra,
        plan_stage_factory=lambda _infra, _actor: object(),
        verifier_factory=lambda _infra, _actor: object(),
        reporter_factory=lambda _infra, _actor: object(),
        draft_agent_runner_factory=lambda _infra, _owner: object(),
        create_run_service_factory=lambda _infra, _owner: object(),
    )

    with pytest.raises(ValueError, match="durable checkpointer"):
        assembly.build_run_graph_starter(durable_checkpointer=False)  # type: ignore[arg-type]

    starter = assembly.build_run_graph_starter(durable_checkpointer=True)

    assert starter.receipt_idempotent is True
    assert starter._graph_factory.__self__ is assembly

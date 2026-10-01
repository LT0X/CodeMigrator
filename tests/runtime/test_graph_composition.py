from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from codemigrator.core import RunId
from codemigrator.runtime.cas import FileHostCAS
from codemigrator.runtime.checkpointer import CasCheckpointSaver
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


def durable_infrastructure(
    tmp_path,
    *,
    cas_references=None,
    run_store=None,
    draft_store=None,
    agent_store=None,
    run_cas=None,
):
    store = InMemoryRuntimeStore()
    cas = FileHostCAS(tmp_path / "durable-cas")
    owner_id = uuid4()

    def saver(graph_family, owner_kind, saver_store, saver_cas):
        return CasCheckpointSaver(
            saver_cas,
            saver_store,
            graph_family=graph_family,
            owner_kind=owner_kind,
            owner_id=owner_id,
        )

    return AgentGraphInfrastructure(
        provider_registry=ProviderRegistry({}),
        context_manager=object(),
        tool_gateway=object(),
        runtime_store=store,
        host_cas=cas,
        cas_references=store if cas_references is None else cas_references,
        usage_sink=object(),
        run_checkpointer=saver(
            "run", "run", store if run_store is None else run_store,
            cas if run_cas is None else run_cas,
        ),
        draft_graph_checkpointer=saver(
            "draft", "draft", store if draft_store is None else draft_store, cas
        ),
        agent_run_checkpointer=saver(
            "agent", "run", store if agent_store is None else agent_store, cas
        ),
    )


def assembly_for(infra):
    return RuntimeGraphAssembly(
        infra,
        plan_stage_factory=lambda _infra, _actor: object(),
        verifier_factory=lambda _infra, _actor: object(),
        reporter_factory=lambda _infra, _actor: object(),
        draft_agent_runner_factory=lambda _infra, _owner: object(),
        create_run_service_factory=lambda _infra, _owner: object(),
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
    actor = SimpleNamespace(run_id=RunId(uuid4()))
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


def test_durable_run_graph_checkpointer_is_scoped_to_the_actor_run_id(tmp_path):
    infra = durable_infrastructure(tmp_path)
    assembly = assembly_for(infra)
    first_run_id = RunId(uuid4())
    second_run_id = RunId(uuid4())

    first = assembly.build_run_graph(SimpleNamespace(run_id=first_run_id))
    second = assembly.build_run_graph(SimpleNamespace(run_id=second_run_id))

    assert first.checkpointer is not second.checkpointer
    assert first.checkpointer.owner_kind == "run"
    assert first.checkpointer.owner_id == first_run_id
    assert second.checkpointer.owner_kind == "run"
    assert second.checkpointer.owner_id == second_run_id
    assert first.checkpointer.store is infra.runtime_store
    assert second.checkpointer.store is infra.runtime_store
    assert first.checkpointer.cas is infra.host_cas
    assert second.checkpointer.cas is infra.host_cas


def test_durable_draft_and_agent_checkpointers_are_scoped_to_the_owner(tmp_path):
    infra = durable_infrastructure(tmp_path)
    assembly = assembly_for(infra)
    first_draft_id = uuid4()
    second_draft_id = uuid4()

    first_draft = assembly.build_draft_graph(
        SimpleNamespace(draft_id=first_draft_id, freeze_receipt=None)
    )
    second_draft = assembly.build_draft_graph(
        SimpleNamespace(draft_id=second_draft_id, freeze_receipt=None)
    )
    run_agent_checkpointer = infra.agent_run_checkpointer_for("run", uuid4())
    draft_agent_checkpointer = infra.agent_run_checkpointer_for("draft", first_draft_id)

    assert first_draft.checkpointer.owner_kind == "draft"
    assert first_draft.checkpointer.owner_id == first_draft_id
    assert second_draft.checkpointer.owner_id == second_draft_id
    assert first_draft.checkpointer is not second_draft.checkpointer
    assert first_draft.agent_checkpointer.owner_kind == "draft"
    assert first_draft.agent_checkpointer.owner_id == first_draft_id
    assert run_agent_checkpointer.owner_kind == "run"
    assert draft_agent_checkpointer.owner_kind == "draft"
    assert run_agent_checkpointer.owner_id != draft_agent_checkpointer.owner_id


def test_draft_graph_thread_identity_is_stable_across_rebuilds(tmp_path):
    infra = durable_infrastructure(tmp_path)
    assembly = assembly_for(infra)
    draft_id = uuid4()
    owner = SimpleNamespace(draft_id=draft_id, freeze_receipt=None)

    first = assembly.build_draft_graph(owner)
    restarted = assembly.build_draft_graph(owner)

    assert first.thread_id == restarted.thread_id


def test_durable_run_saver_rejects_non_run_owner(tmp_path):
    infra = durable_infrastructure(tmp_path)

    with pytest.raises(ValueError, match="Run graph checkpoints require a Run owner"):
        infra.run_checkpointer.for_owner(owner_kind="draft", owner_id=uuid4())


def test_run_graph_starter_is_bound_to_assembly_and_requires_durable_attestation(tmp_path):
    assembly = assembly_for(durable_infrastructure(tmp_path))

    with pytest.raises(ValueError, match="durable checkpointer"):
        assembly.build_run_graph_starter(durable_checkpointer=False)  # type: ignore[arg-type]

    starter = assembly.build_run_graph_starter(durable_checkpointer=True)

    assert starter.receipt_idempotent is True
    assert starter._graph_factory.__self__ is assembly


@pytest.mark.parametrize(
    "mismatch",
    ["cas_references", "run_store", "draft_store", "agent_store", "run_cas"],
)
def test_durable_run_graph_starter_rejects_persistence_outside_assembly_boundary(
    tmp_path, mismatch
):
    other_store = InMemoryRuntimeStore()
    other_cas = FileHostCAS(tmp_path / "other-cas")
    overrides = {
        "cas_references": other_store,
        "run_store": other_store,
        "draft_store": other_store,
        "agent_store": other_store,
        "run_cas": other_cas,
    }
    assembly = assembly_for(durable_infrastructure(tmp_path, **{mismatch: overrides[mismatch]}))

    with pytest.raises(RuntimeGraphConfigurationError, match="application (RuntimeStore|host CAS)"):
        assembly.build_run_graph_starter(durable_checkpointer=True)

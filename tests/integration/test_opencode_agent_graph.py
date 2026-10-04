from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from codemigrator.core import ModelProfile, Phase, SessionKind, canonical_json_bytes, load_resource
from codemigrator.core.ids import new_uuid7
from codemigrator.runtime.agent_runs import AgentRun, AgentRunId, AgentRunReceipt
from codemigrator.runtime.binding import LockedModelBinding
from codemigrator.runtime.cas import CasLedger, FileHostCAS
from codemigrator.runtime.checkpointer import CasCheckpointSaver
from codemigrator.runtime.context import ContextEnvelope, ContextSegment
from codemigrator.runtime.draft import DraftFlow
from codemigrator.runtime.draft_graph import (
    DraftAgentCompletion,
    DraftFlowOwner,
    MigrationSessionGraph,
)
from codemigrator.runtime.draft_models import ExplorationReport
from codemigrator.runtime.langchain_agent import (
    agent_context_digest,
    agent_template_digest,
    agent_toolset_digest,
    create_bound_agent,
)
from codemigrator.runtime.loop_contracts import SessionExit, SessionState
from codemigrator.runtime.memory import ContextManager, DraftContextIdentity, FormulaNetInputCap
from codemigrator.runtime.provider import (
    OpenAICompatibleProvider,
    ProviderRegistry,
    provider_adapter_id_for_label,
    select_unique_provider_config,
)
from codemigrator.runtime.store import InMemoryRuntimeStore
from codemigrator.workspace import GatewayContext

pytestmark = pytest.mark.skipif(
    os.environ.get("CODEMIGRATOR_REAL_OPENCODE", "").casefold() not in {"1", "true"},
    reason="set CODEMIGRATOR_REAL_OPENCODE=1 to make one OpenCode free-model request",
)


class _ConservativeCounter:
    """Bound prompt size by UTF-8 bytes for this one-call transport smoke test."""

    def count(self, messages) -> int:
        return sum(max(1, len(message.content.encode("utf-8"))) for message in messages)

    def count_tool_schemas(self, tools) -> int:
        return sum(
            len(json.dumps(dict(tool.parameters), ensure_ascii=False).encode("utf-8"))
            for tool in tools
        )


class _UsageSink:
    def __init__(self, *, request_cap: int = 1) -> None:
        self._rounds: dict[AgentRunId, dict[str, int]] = {}
        self._request_cap = request_cap
        self.receipts: list[object] = []

    async def reserve_round(self, agent_run_id, call_id: str, *, max_rounds: int):
        calls = self._rounds.setdefault(agent_run_id, {})
        if call_id in calls:
            return calls[call_id]
        if len(calls) >= min(max_rounds, self._request_cap):
            return None
        calls[call_id] = len(calls) + 1
        return calls[call_id]

    async def record(self, agent_run_id, usage, receipt) -> None:
        self.receipts.append((agent_run_id, usage, receipt))


class _NoToolGateway:
    def __init__(self, context: GatewayContext) -> None:
        self.context = context

    def dispatch(self, raw_call, *, cancellation_token=None):
        raise AssertionError("the live smoke task must not dispatch tools")


def _opencode_config() -> dict[str, object]:
    configured_path = os.environ.get("CODEMIGRATOR_OPENCODE_CONFIG")
    candidates = (
        [Path(configured_path)]
        if configured_path
        else [
            Path(__file__).resolve().parents[2] / "my_space/model_api_key.json",
            Path("/home/xtc/project/CodeM/CodeMigrator/my_space/model_api_key.json"),
        ]
    )
    config_path = next((path for path in candidates if path.is_file()), None)
    if config_path is None:
        pytest.fail("local OpenCode provider config is unavailable")
    return select_unique_provider_config(config_path.read_text(encoding="utf-8"), "OpenCode")


@pytest.mark.asyncio
async def test_real_opencode_agent_run_reaches_durable_draft_graph_receipt() -> None:
    config = _opencode_config()
    provider_id = provider_adapter_id_for_label(str(config["Provider"]))
    model_id = str(config["模型"])
    if model_id != "space-bunny-free":
        pytest.fail("the live integration smoke is pinned to OpenCode space-bunny-free")
    endpoint = urlsplit(str(config["Base URL"]))
    if endpoint.scheme != "https" or endpoint.netloc.casefold() != "opencode.ai":
        pytest.fail("the live integration smoke is pinned to the OpenCode HTTPS endpoint")
    binding = LockedModelBinding(
        provider_id=provider_id,
        model_id=model_id,
        profile=ModelProfile.Reasoning,
        config_revision=hashlib.sha256(
            json.dumps(
                {key: value for key, value in config.items() if key != "API Key"},
                sort_keys=True,
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest(),
        context_window=int(config["Context Window"]),
        output_cap=min(512, int(config["模型输出上限"])),
    )
    provider = OpenAICompatibleProvider(
        endpoint=str(config["Base URL"]), api_key=str(config["API Key"])
    )
    store = InMemoryRuntimeStore()
    draft_id = new_uuid7()
    revision_id = new_uuid7()
    context_manager = ContextManager(
        token_counter=_ConservativeCounter(), net_input_cap=FormulaNetInputCap()
    )
    envelope = ContextEnvelope(
        stable=(ContextSegment("stable", "No source snapshot is attached."),)
    )
    template = (
        "You are a read-only CodeMigrator exploration assistant. "
        "No source snapshot is attached. Return one typed ExplorationReport through the "
        "required structured output channel. Set domain_path exactly to '.'; use one "
        "synthetic anchor and the only coverage path 'src/example.py'. Do not invoke "
        "repository tools or include prose."
    )
    usage_sink = _UsageSink(request_cap=1)

    with tempfile.TemporaryDirectory(prefix="codemigrator-opencode-agent-") as temp_dir:
        cas = FileHostCAS(Path(temp_dir) / "cas")
        draft_checkpointer = CasCheckpointSaver(
            cas, store, graph_family="draft", owner_kind="draft", owner_id=draft_id
        )
        agent_checkpointer = CasCheckpointSaver(
            cas, store, graph_family="agent", owner_kind="draft", owner_id=draft_id
        )

        class Runner:
            async def run(self, owner_id, logical_task_key, task, *, lifecycle):
                agent_run_id = AgentRunId(new_uuid7())
                context_identity = DraftContextIdentity(owner_id, revision_id, agent_run_id)
                template_sha = agent_template_digest(
                    session=SessionKind.ExploreCoordinator.value, template=template
                )
                record = AgentRun(
                    agent_run_id=agent_run_id,
                    owner_kind="draft",
                    owner_id=owner_id,
                    logical_task_key=logical_task_key,
                    phase=Phase.Plan,
                    session_kind=SessionKind.ExploreCoordinator,
                    thread_id=str(new_uuid7()),
                    model_binding_sha256=binding.digest,
                    context_sha256=agent_context_digest(
                        context_identity=context_identity,
                        envelope=envelope,
                        phase=Phase.Plan,
                        session_kind=SessionKind.ExploreCoordinator,
                        template_sha256=template_sha,
                        budget=context_manager.budget_catalog.profile("DRAFTING"),
                    ),
                    toolset_sha256=agent_toolset_digest(
                        phase=Phase.Plan,
                        session_kind=SessionKind.ExploreCoordinator,
                        owner_kind="draft",
                        response_format=ExplorationReport,
                    ),
                    template_sha256=template_sha,
                )
                record = await store.create_or_get_agent_run(record)
                await lifecycle.started(record)
                bound = create_bound_agent(
                    agent_run=record,
                    binding=binding,
                    registry=ProviderRegistry({provider_id: provider}),
                    context_manager=context_manager,
                    template=template,
                    envelope=envelope,
                    gateway=_NoToolGateway(
                        GatewayContext(
                            draft_id=owner_id,
                            agent_run_id=agent_run_id,
                            phase_policy_sha256=load_resource("core://phase-tool-policy/v2").sha256,
                            phase=Phase.Plan,
                            session_kind=SessionKind.ExploreCoordinator,
                        )
                    ),
                    usage_sink=usage_sink,
                    context_identity=context_identity,
                    checkpointer=agent_checkpointer,
                    response_format=ExplorationReport,
                )
                result = await bound.ainvoke(task=task)
                report = result.structured_response
                if (
                    result.exit is not SessionExit.Completed
                    or not isinstance(report, ExplorationReport)
                ):
                    raise AssertionError("live OpenCode AgentRun did not complete")
                result_body = canonical_json_bytes(report.model_dump(mode="json", by_alias=True))
                result_reference = await CasLedger(cas, store).put(
                    result_body,
                    "draft",
                    owner_id,
                    f"agent-result:{record.agent_run_id}",
                )
                indexes = await store.list_checkpoint_indexes(record.thread_id)
                terminal = replace(
                    record,
                    state=SessionState.Closed,
                    exit=SessionExit.Completed,
                    result_sha256=result_reference.digest,
                    checkpoint_sha256=indexes[0].object.digest if indexes else None,
                )
                receipt = AgentRunReceipt(
                    new_uuid7(), terminal.agent_run_id, "draft.exploration.completed"
                )
                await store.commit_agent_run_receipt(terminal, receipt)
                await lifecycle.terminal(terminal, receipt)
                return DraftAgentCompletion(
                    terminal,
                    receipt,
                    result_reference,
                    materialized=report,
                )

        graph = MigrationSessionGraph(
            owner=DraftFlowOwner(draft_id=draft_id, flow=DraftFlow(), store=store),
            agent_runs=store,
            checkpointer=draft_checkpointer,
            agent_checkpointer=agent_checkpointer,
            agent_runner=Runner(),
        )
        try:
            completion = await graph.explore_domain(
                ".",
                "Return one minimal typed ExplorationReport for a synthetic source example. "
                "Set domain_path exactly to '.' and use src/example.py as the only anchor "
                "and coverage path.",
            )
            assert isinstance(completion.materialized, ExplorationReport)
            assert len(usage_sink.receipts) == 1
            assert await store.load_agent_run(completion.record.agent_run_id) == completion.record
            assert (
                await store.load_agent_run_receipt(completion.record.agent_run_id)
                == completion.receipt
            )
        finally:
            await provider.aclose()

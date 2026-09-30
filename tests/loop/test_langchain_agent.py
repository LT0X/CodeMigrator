from __future__ import annotations

import json
from dataclasses import replace
from uuid import uuid4

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.tracers.context import tracing_v2_callback_var, tracing_v2_enabled
from langgraph.checkpoint.memory import InMemorySaver
from langsmith.utils import tracing_is_enabled

from codemigrator.core import (
    ContextPackIdentity,
    ModelProfile,
    Phase,
    SessionBudgetProfile,
    SessionKind,
)
from codemigrator.runtime.agent_runs import AgentRun
from codemigrator.runtime.binding import LockedModelBinding
from codemigrator.runtime.context import ContextEnvelope, ContextSegment
from codemigrator.runtime.langchain_agent import (
    agent_context_digest,
    agent_template_digest,
    agent_toolset_digest,
    allowed_tool_names,
)
from codemigrator.runtime.langchain_agent import (
    create_bound_agent as _create_bound_agent,
)
from codemigrator.runtime.loop_contracts import SessionExit, SessionState
from codemigrator.runtime.memory import (
    ContextBudgetError,
    ContextManager,
    DraftContextIdentity,
    FormulaNetInputCap,
    SessionBudgetCatalog,
)
from codemigrator.runtime.provider import (
    ProviderRegistry,
    ProviderRequest,
    ProviderResponse,
    ProviderToolCall,
    TokenUsage,
)


class ExactCounter:
    def count(self, messages):
        return sum(len(message.content) for message in messages)

    def count_tool_schemas(self, tools):
        return 20 if tools else 0


class FakeProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[ProviderRequest] = []
        self.tracing_enabled: list[bool] = []
        self.trace_callbacks: list[object] = []

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        self.tracing_enabled.append(tracing_is_enabled())
        self.trace_callbacks.append(tracing_v2_callback_var.get())
        return self.responses.pop(0)


class FakeGateway:
    def __init__(self):
        self.calls = []

    def dispatch(self, raw_call, *, cancellation_token=None):
        self.calls.append(raw_call)
        return {"ok": True}


class UsageSink:
    def __init__(self):
        self.receipts = []
        self.calls = {}

    async def reserve_round(self, agent_run_id, call_id, *, max_rounds):
        per_run = self.calls.setdefault(agent_run_id, {})
        if call_id in per_run:
            return per_run[call_id]
        if len(per_run) >= max_rounds:
            return None
        round_index = len(per_run) + 1
        per_run[call_id] = round_index
        return round_index

    async def record(self, agent_run_id, usage, receipt):
        self.receipts.append((agent_run_id, usage, receipt))


class CountingRegistry(ProviderRegistry):
    def __init__(self, adapters):
        super().__init__(adapters)
        self.resolve_calls = 0

    def resolve(self, binding):
        self.resolve_calls += 1
        return super().resolve(binding)


def _binding(profile=ModelProfile.Reasoning):
    return LockedModelBinding(
        provider_id="openai-compatible",
        model_id="fixed-model",
        profile=profile,
        config_revision="config-v1",
        context_window=2000,
        output_cap=200,
    )


def _run(binding, *, phase=Phase.Plan, session=SessionKind.PlanAuxiliary, owner_kind="run"):
    return AgentRun(
        agent_run_id=uuid4(),
        owner_kind=owner_kind,
        owner_id=uuid4(),
        logical_task_key="task-1",
        phase=phase,
        session_kind=session,
        thread_id=str(uuid4()),
        model_binding_sha256=binding.digest,
        context_sha256="1" * 64,
        toolset_sha256="2" * 64,
        template_sha256="3" * 64,
    )


def _response(content="done", *, tools=(), receipt="receipt-1"):
    return ProviderResponse(
        content=content,
        tool_calls=tuple(tools),
        finish_reason="tool_calls" if tools else "stop",
        usage=TokenUsage(4, 2),
        provider_receipt_id=receipt,
    )


def _context_identity(run, binding):
    return ContextPackIdentity(
        run_id=run.owner_id,
        phase=run.phase,
        session=run.session_kind,
        slice=run.slice_ref,
        spec_sha256="1" * 64,
        model_binding_sha256=binding.digest,
        phase_policy_sha256="2" * 64,
        contract_refs_sha256="3" * 64,
    )


_default_usage_sink = UsageSink()


def _prepare_agent_kwargs(kwargs):
    kwargs.setdefault("checkpointer", InMemorySaver())
    kwargs.setdefault("usage_sink", _default_usage_sink)
    run = kwargs["agent_run"]
    context_identity = kwargs.get("context_identity")
    if context_identity is not None:
        template = kwargs.get("template", "plan role")
        session = (
            context_identity.session.value
            if isinstance(context_identity, ContextPackIdentity)
            else run.session_kind.value
        )
        template_sha256 = agent_template_digest(session=session, template=template)
        if isinstance(context_identity, ContextPackIdentity):
            context_identity = context_identity.model_copy(
                update={"template_sha256": template_sha256}
            )
            kwargs["context_identity"] = context_identity
        kwargs["agent_run"] = replace(
            run,
            context_sha256=agent_context_digest(
                context_identity=context_identity,
                envelope=kwargs.get("envelope", ContextEnvelope()),
                phase=run.phase,
                session_kind=run.session_kind,
                template_sha256=template_sha256,
                budget=kwargs["context_manager"].budget_catalog.profile(
                    "DRAFTING" if run.owner_kind == "draft" else run.session_kind
                ),
            ),
            toolset_sha256=agent_toolset_digest(
                phase=run.phase,
                session_kind=run.session_kind,
                owner_kind=run.owner_kind,
            ),
            template_sha256=template_sha256,
        )
    return kwargs


def create_bound_agent(**kwargs):
    """Bind test records to the exact policy/context passed to the factory."""

    kwargs = _prepare_agent_kwargs(kwargs)
    return _create_bound_agent(**kwargs)


def _context_manager_with_round_limit(max_rounds):
    catalog = SessionBudgetCatalog.from_core()
    profiles = dict(catalog.profiles)
    profile = profiles[SessionKind.PlanAuxiliary.value]
    profiles[SessionKind.PlanAuxiliary.value] = SessionBudgetProfile(
        session=SessionKind.PlanAuxiliary,
        max_rounds=max_rounds,
        eviction_watermark_pct=profile.eviction_watermark_pct,
    )
    return ContextManager(
        token_counter=ExactCounter(),
        net_input_cap=FormulaNetInputCap(),
        budget_catalog=SessionBudgetCatalog(profiles, catalog.resource_sha256),
    )


@pytest.mark.asyncio
async def test_create_agent_keeps_binding_and_records_each_provider_receipt() -> None:
    binding = _binding()
    run = _run(binding)
    provider = FakeProvider(
        [
            _response(
                "",
                tools=(ProviderToolCall("ReadFile", json.dumps({"path": "safe.py"}), "call-1"),),
            ),
            _response(receipt="receipt-2"),
        ]
    )
    registry = CountingRegistry({"openai-compatible": provider})
    gateway = FakeGateway()
    sink = UsageSink()
    bound = create_bound_agent(
        agent_run=run,
        binding=binding,
        registry=registry,
        context_manager=ContextManager(
            token_counter=ExactCounter(), net_input_cap=FormulaNetInputCap()
        ),
        template="plan role",
        envelope=ContextEnvelope(
            stable=(ContextSegment("stable", "frozen facts"),),
            evolving=(ContextSegment("evolving", "accepted prior fact"),),
        ),
        gateway=gateway,
        usage_sink=sink,
        context_identity=_context_identity(run, binding),
    )
    result = await bound.ainvoke(task="Propose a migration plan")
    assert result.agent_run_id == run.agent_run_id
    assert result.exit is SessionExit.Completed
    assert result.state is SessionState.CheckpointPending
    assert [request.binding for request in provider.requests] == [binding, binding]
    assert registry.resolve_calls == 1
    assert [request.binding.config_revision for request in provider.requests] == ["config-v1"] * 2
    assert [tool.name for tool in provider.requests[0].tools] == [
        "ReadFile",
        "QuerySourceAst",
        "Exec",
    ]
    assert gateway.calls == [{"tool": "ReadFile", "path": "safe.py"}]
    assert all(
        "frozen facts" in " ".join(message.content for message in request.messages)
        and "accepted prior fact" in " ".join(message.content for message in request.messages)
        for request in provider.requests
    )
    assert '"ok": true' in " ".join(message.content for message in provider.requests[1].messages)
    assert [entry[2].call.provider_receipt_id for entry in sink.receipts] == [
        "receipt-1",
        "receipt-2",
    ]


@pytest.mark.asyncio
async def test_middleware_discards_unbudgeted_graph_history() -> None:
    binding = _binding()
    run = _run(binding)
    provider = FakeProvider([_response()])
    bound = create_bound_agent(
        agent_run=run,
        binding=binding,
        registry=ProviderRegistry({"openai-compatible": provider}),
        context_manager=ContextManager(
            token_counter=ExactCounter(), net_input_cap=FormulaNetInputCap()
        ),
        template="plan role",
        envelope=ContextEnvelope(stable=(ContextSegment("stable", "frozen facts"),)),
        gateway=FakeGateway(),
        context_identity=_context_identity(run, binding),
    )
    await bound.graph.ainvoke(
        {
            "messages": [
                HumanMessage(content="UNBUDGETED GRAPH HISTORY"),
                HumanMessage(content="Use the frozen plan facts", name="codemigrator_owner_task"),
            ]
        },
        config={"configurable": {"thread_id": run.thread_id}},
    )
    assert all(
        "UNBUDGETED GRAPH HISTORY" not in message.content
        for request in provider.requests
        for message in request.messages
    )
    assert "frozen facts" in " ".join(message.content for message in provider.requests[0].messages)
    assert "Use the frozen plan facts" in " ".join(
        message.content for message in provider.requests[0].messages
    )


def test_closed_phase_session_policy_excludes_draft_ask_user_and_deterministic_phases() -> None:
    assert allowed_tool_names(Phase.Plan, SessionKind.PlanAuxiliary, draft=False) == (
        "ReadFile",
        "QuerySourceAst",
        "Exec",
    )
    assert allowed_tool_names(Phase.Execute, SessionKind.Implementation, draft=False) == (
        "ReadFile",
        "WriteFile",
        "EditFile",
        "QuerySourceAst",
        "Shell",
        "Exec",
    )
    assert allowed_tool_names(Phase.Execute, SessionKind.ExecuteSupervisor, draft=False) == (
        "ReadFile",
        "QuerySourceAst",
    )
    assert allowed_tool_names(Phase.Verify, SessionKind.Implementation, draft=False) == ()
    assert allowed_tool_names(Phase.Report, SessionKind.Implementation, draft=False) == ()
    assert "AskUser" not in allowed_tool_names(
        Phase.Plan, SessionKind.ExploreCoordinator, draft=True
    )


def test_draft_agent_requires_matching_draft_context_and_no_run_identity() -> None:
    binding = _binding()
    run = _run(binding, session=SessionKind.ExploreCoordinator, owner_kind="draft")
    manager = ContextManager(token_counter=ExactCounter(), net_input_cap=FormulaNetInputCap())
    kwargs = dict(
        agent_run=run,
        binding=binding,
        registry=ProviderRegistry({"openai-compatible": FakeProvider([_response()])}),
        context_manager=manager,
        template="draft role",
        envelope=ContextEnvelope(stable=(ContextSegment("stable", "draft facts"),)),
        gateway=FakeGateway(),
    )
    with pytest.raises(ValueError, match="DraftContextIdentity"):
        create_bound_agent(**kwargs, context_identity=_context_identity(run, binding))
    draft_identity = DraftContextIdentity(
        draft_id=run.owner_id, revision_id=uuid4(), agent_run_id=run.agent_run_id
    )
    bound = create_bound_agent(**kwargs, context_identity=draft_identity)
    assert bound.agent_run.agent_run_id == run.agent_run_id


def test_run_agent_rejects_missing_frozen_context_identity() -> None:
    binding = _binding()
    run = _run(binding)
    run = replace(
        run,
        toolset_sha256=agent_toolset_digest(
            phase=run.phase,
            session_kind=run.session_kind,
            owner_kind=run.owner_kind,
        ),
    )
    with pytest.raises(ValueError, match="Run ContextPack identity"):
        create_bound_agent(
            agent_run=run,
            binding=binding,
            registry=ProviderRegistry({"openai-compatible": FakeProvider([_response()])}),
            context_manager=ContextManager(
                token_counter=ExactCounter(), net_input_cap=FormulaNetInputCap()
            ),
            template="plan role",
            envelope=ContextEnvelope(),
            gateway=FakeGateway(),
        )


@pytest.mark.asyncio
async def test_owner_task_is_exact_counted_and_overflow_fails_before_provider() -> None:
    binding = LockedModelBinding(
        provider_id="openai-compatible",
        model_id="fixed-model",
        profile=ModelProfile.Reasoning,
        config_revision="config-v1",
        context_window=250,
        output_cap=100,
    )
    run = _run(binding)
    provider = FakeProvider([_response()])
    bound = create_bound_agent(
        agent_run=run,
        binding=binding,
        registry=ProviderRegistry({"openai-compatible": provider}),
        context_manager=ContextManager(
            token_counter=ExactCounter(), net_input_cap=FormulaNetInputCap()
        ),
        template="plan role",
        envelope=ContextEnvelope(stable=(ContextSegment("stable", "facts"),)),
        gateway=FakeGateway(),
        context_identity=_context_identity(run, binding),
    )
    with pytest.raises(ContextBudgetError):
        await bound.ainvoke(task="required task " * 40)
    assert provider.requests == []


def test_agent_fails_closed_without_exact_tool_schema_counter() -> None:
    class MessagesOnlyCounter:
        def count(self, messages):
            return sum(len(message.content) for message in messages)

    binding = _binding()
    run = _run(binding)
    with pytest.raises(ContextBudgetError, match="tool schema"):
        create_bound_agent(
            agent_run=run,
            binding=binding,
            registry=ProviderRegistry({"openai-compatible": FakeProvider([_response()])}),
            context_manager=ContextManager(
                token_counter=MessagesOnlyCounter(), net_input_cap=FormulaNetInputCap()
            ),
            template="plan role",
            envelope=ContextEnvelope(),
            gateway=FakeGateway(),
            context_identity=_context_identity(run, binding),
        )


@pytest.mark.asyncio
async def test_create_agent_checkpoints_under_private_agent_run_thread() -> None:
    binding = _binding()
    run = _run(binding)
    saver = InMemorySaver()
    bound = create_bound_agent(
        agent_run=run,
        binding=binding,
        registry=ProviderRegistry({"openai-compatible": FakeProvider([_response()])}),
        context_manager=ContextManager(
            token_counter=ExactCounter(), net_input_cap=FormulaNetInputCap()
        ),
        template="plan role",
        envelope=ContextEnvelope(),
        gateway=FakeGateway(),
        context_identity=_context_identity(run, binding),
        checkpointer=saver,
    )
    await bound.ainvoke(task="Propose a plan")
    saved = await saver.aget_tuple({"configurable": {"thread_id": run.thread_id}})
    assert saved is not None


@pytest.mark.asyncio
async def test_plan_feedback_reuses_thread_and_governs_only_latest_owner_task() -> None:
    binding = _binding()
    run = _run(binding)
    provider = FakeProvider([_response("proposal"), _response("revised proposal")])
    bound = create_bound_agent(
        agent_run=run,
        binding=binding,
        registry=ProviderRegistry({"openai-compatible": provider}),
        context_manager=ContextManager(
            token_counter=ExactCounter(), net_input_cap=FormulaNetInputCap()
        ),
        template="plan role",
        envelope=ContextEnvelope(),
        gateway=FakeGateway(),
        context_identity=_context_identity(run, binding),
        checkpointer=InMemorySaver(),
    )
    await bound.ainvoke(task="initial proposal")
    await bound.ainvoke(task="validator feedback: revise proposal")
    second = " ".join(message.content for message in provider.requests[1].messages)
    assert "validator feedback: revise proposal" in second
    assert "initial proposal" not in second


@pytest.mark.asyncio
async def test_middleware_requires_audit_sink_before_evicting_targeted_content() -> None:
    binding = _binding()
    run = _run(binding)
    provider = FakeProvider(
        [
            _response(
                "",
                tools=(ProviderToolCall("ReadFile", '{"path":"safe.py"}', "call-1"),),
            ),
            _response(receipt="receipt-2"),
        ]
    )
    bound = create_bound_agent(
        agent_run=run,
        binding=binding,
        registry=ProviderRegistry({"openai-compatible": provider}),
        context_manager=ContextManager(
            token_counter=ExactCounter(), net_input_cap=FormulaNetInputCap()
        ),
        template="plan role",
        envelope=ContextEnvelope(
            stable=(ContextSegment("stable", "frozen"),),
            targeted=(
                ContextSegment(
                    "targeted",
                    "x" * 1450,
                    source_ref="cas://" + "a" * 64,
                    turn_index=0,
                ),
            ),
        ),
        gateway=FakeGateway(),
        context_identity=_context_identity(run, binding),
    )
    with pytest.raises(ContextBudgetError, match="audit"):
        await bound.ainvoke(task="plan")
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_middleware_evicts_only_cas_backed_targeted_content() -> None:
    binding = _binding()
    run = _run(binding)
    provider = FakeProvider(
        [
            _response(
                "",
                tools=(ProviderToolCall("ReadFile", '{"path":"safe.py"}', "call-1"),),
            ),
            _response(receipt="receipt-2"),
        ]
    )
    audits = []
    bound = create_bound_agent(
        agent_run=run,
        binding=binding,
        registry=ProviderRegistry({"openai-compatible": provider}),
        context_manager=ContextManager(
            token_counter=ExactCounter(), net_input_cap=FormulaNetInputCap()
        ),
        template="plan role",
        envelope=ContextEnvelope(
            stable=(ContextSegment("stable", "stable fact"),),
            evolving=(ContextSegment("evolving", "evolving fact"),),
            targeted=(
                ContextSegment(
                    "targeted",
                    "x" * 1450,
                    source_ref="cas://" + "a" * 64,
                    turn_index=0,
                ),
            ),
        ),
        gateway=FakeGateway(),
        context_identity=_context_identity(run, binding),
        eviction_audit_sink=audits.extend,
    )
    await bound.ainvoke(task="plan")
    second = " ".join(message.content for message in provider.requests[1].messages)
    assert "stable fact" in second and "evolving fact" in second
    assert "x" * 1450 not in second
    assert "content externalized" in second
    assert len(audits) == 1


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("context_sha256", "context digest"),
        ("toolset_sha256", "toolset digest"),
        ("template_sha256", "template digest"),
    ],
)
def test_factory_rejects_agent_run_digest_drift(field, message) -> None:
    binding = _binding()
    run = _run(binding)
    kwargs = _prepare_agent_kwargs(
        {
            "agent_run": run,
            "binding": binding,
            "registry": ProviderRegistry({"openai-compatible": FakeProvider([_response()])}),
            "context_manager": ContextManager(
                token_counter=ExactCounter(), net_input_cap=FormulaNetInputCap()
            ),
            "template": "plan role",
            "envelope": ContextEnvelope(stable=(ContextSegment("stable", "facts"),)),
            "gateway": FakeGateway(),
            "context_identity": _context_identity(run, binding),
        }
    )
    kwargs["agent_run"] = replace(kwargs["agent_run"], **{field: "f" * 64})
    with pytest.raises(ValueError, match=message):
        _create_bound_agent(**kwargs)


def test_factory_requires_checkpoint_and_durable_round_reservation() -> None:
    binding = _binding()
    run = _run(binding)
    kwargs = _prepare_agent_kwargs(
        {
            "agent_run": run,
            "binding": binding,
            "registry": ProviderRegistry({"openai-compatible": FakeProvider([_response()])}),
            "context_manager": ContextManager(
                token_counter=ExactCounter(), net_input_cap=FormulaNetInputCap()
            ),
            "template": "plan role",
            "envelope": ContextEnvelope(),
            "gateway": FakeGateway(),
            "context_identity": _context_identity(run, binding),
        }
    )
    with pytest.raises(ValueError, match="requires a LangGraph checkpointer"):
        _create_bound_agent(**{**kwargs, "checkpointer": None})
    with pytest.raises(ValueError, match="durable model-call usage sink"):
        _create_bound_agent(**{**kwargs, "usage_sink": None})


@pytest.mark.asyncio
async def test_agent_run_disables_inherited_langsmith_tracing(monkeypatch) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    binding = _binding()
    run = _run(binding)
    provider = FakeProvider([_response()])
    bound = create_bound_agent(
        agent_run=run,
        binding=binding,
        registry=ProviderRegistry({"openai-compatible": provider}),
        context_manager=ContextManager(
            token_counter=ExactCounter(), net_input_cap=FormulaNetInputCap()
        ),
        template="plan role",
        envelope=ContextEnvelope(),
        gateway=FakeGateway(),
        context_identity=_context_identity(run, binding),
    )
    with tracing_v2_enabled():
        await bound.ainvoke(task="Propose a plan")
    assert provider.tracing_enabled == [False]
    assert provider.trace_callbacks == [None]


@pytest.mark.asyncio
async def test_model_call_budget_is_persistent_and_stops_after_limit() -> None:
    binding = _binding()
    run = _run(binding)
    saver = InMemorySaver()
    usage = UsageSink()
    provider = FakeProvider([_response("proposal"), _response("unused")])
    kwargs = {
        "agent_run": run,
        "binding": binding,
        "registry": ProviderRegistry({"openai-compatible": provider}),
        "context_manager": _context_manager_with_round_limit(1),
        "template": "plan role",
        "envelope": ContextEnvelope(),
        "gateway": FakeGateway(),
        "context_identity": _context_identity(run, binding),
        "checkpointer": saver,
        "usage_sink": usage,
    }
    first = create_bound_agent(**kwargs)
    first_result = await first.ainvoke(task="Propose a plan")
    assert first_result.exit is SessionExit.Completed
    assert first_result.rounds == 1

    recovered = create_bound_agent(**kwargs)
    second_result = await recovered.ainvoke(task="Apply validator feedback")
    assert second_result.exit is SessionExit.SegmentStopped
    assert second_result.rounds == 1
    assert len(provider.requests) == 1
    assert len(usage.receipts) == 1
    assert await saver.aget_tuple({"configurable": {"thread_id": run.thread_id}}) is not None


@pytest.mark.asyncio
async def test_model_round_limit_allows_langgraph_loop_beyond_default_recursion() -> None:
    binding = _binding()
    run = _run(binding)
    round_limit = 16
    responses = [
        _response(
            "",
            tools=(
                ProviderToolCall(
                    "ReadFile", json.dumps({"path": f"file_{index}.py"}), f"call-{index}"
                ),
            ),
        )
        for index in range(round_limit)
    ]
    provider = FakeProvider(responses)
    gateway = FakeGateway()
    bound = create_bound_agent(
        agent_run=run,
        binding=binding,
        registry=ProviderRegistry({"openai-compatible": provider}),
        context_manager=_context_manager_with_round_limit(round_limit),
        template="plan role",
        envelope=ContextEnvelope(),
        gateway=gateway,
        context_identity=_context_identity(run, binding),
    )
    result = await bound.ainvoke(task="Inspect files in bounded steps")
    assert result.exit is SessionExit.SegmentStopped
    assert result.rounds == round_limit
    assert len(provider.requests) == round_limit
    assert len(gateway.calls) == round_limit

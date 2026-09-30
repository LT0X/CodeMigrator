"""One locked LangChain agent per AgentRun, with runtime-owned model and tools."""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol, cast

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, PrivateAttr

from codemigrator.core import ContextPackIdentity, Phase, SessionKind, load_resource
from codemigrator.workspace.models import (
    EditFileCall,
    ExecCall,
    QuerySourceAstCall,
    ReadFileCall,
    ShellCall,
    WriteFileCall,
)

from .agent_runs import AgentRun, AgentRunId
from .binding import LockedModelBinding
from .context import ContextEnvelope, ContextSegment, PromptMessage, render_prompt
from .loop import CancellationToken, SessionResult, ToolGatewayPort
from .loop_contracts import SessionExit, SessionState
from .memory import (
    CAS_URI_PATTERN,
    ContextBudgetError,
    ContextManager,
    DraftContextIdentity,
    EvictionAuditSink,
    EvictionEngine,
)
from .provider import (
    AsyncProvider,
    ProviderCallIdentity,
    ProviderRegistry,
    ProviderRequest,
    TokenUsage,
    ToolDefinition,
    UsageReceipt,
)

_TOOL_MODELS = {
    "ReadFile": ReadFileCall,
    "WriteFile": WriteFileCall,
    "EditFile": EditFileCall,
    "QuerySourceAst": QuerySourceAstCall,
    "Shell": ShellCall,
    "Exec": ExecCall,
}
_READ_TOOLS = frozenset(("ReadFile", "QuerySourceAst", "Exec"))


class AgentUsageSink(Protocol):
    async def record(
        self, agent_run_id: AgentRunId, usage: TokenUsage, receipt: UsageReceipt
    ) -> None: ...


def allowed_tool_names(phase: Phase, session_kind: SessionKind, *, draft: bool) -> tuple[str, ...]:
    """Project the frozen phase policy and session restrictions onto model schemas."""

    phase_policy = load_resource("core://phase-tool-policy/v2").payload
    permitted = set(phase_policy[phase.value])
    if draft or session_kind is SessionKind.ExploreCoordinator:
        permitted &= _READ_TOOLS
    elif session_kind is SessionKind.ExecuteSupervisor:
        permitted &= {"ReadFile", "QuerySourceAst"}
    return tuple(name for name in _TOOL_MODELS if name in permitted)


def _tool_definition(name: str) -> ToolDefinition:
    schema = dict(cast(type[BaseModel], _TOOL_MODELS[name]).model_json_schema())
    properties = dict(schema["properties"])
    properties.pop("tool", None)
    schema["properties"] = properties
    schema["required"] = [field for field in schema.get("required", []) if field != "tool"]
    return ToolDefinition(
        name=name,
        description=f"Invoke the closed CodeMigrator {name} tool.",
        parameters=schema,
    )


def _prompt_message(message: BaseMessage) -> PromptMessage:
    if isinstance(message, SystemMessage):
        return PromptMessage(role="system", content=str(message.content))
    if isinstance(message, ToolMessage):
        return PromptMessage(
            role="tool", content=str(message.content), tool_call_id=message.tool_call_id
        )
    return PromptMessage(
        role="assistant" if isinstance(message, AIMessage) else "user",
        content=str(message.content),
    )


class ProviderChatModel(BaseChatModel):
    """LangChain chat model that delegates every call to one resolved AsyncProvider."""

    _provider: AsyncProvider = PrivateAttr()
    _binding: LockedModelBinding = PrivateAttr()
    _agent_run_id: AgentRunId = PrivateAttr()
    _usage_sink: AgentUsageSink | None = PrivateAttr()
    _cancellation: CancellationToken = PrivateAttr()
    _tools: tuple[ToolDefinition, ...] = PrivateAttr()
    _usages: list[TokenUsage] = PrivateAttr(default_factory=list)
    _calls: int = PrivateAttr(default=0)

    def __init__(
        self,
        *,
        provider: AsyncProvider,
        binding: LockedModelBinding,
        agent_run_id: AgentRunId,
        tools: tuple[ToolDefinition, ...],
        usage_sink: AgentUsageSink | None,
        cancellation: CancellationToken,
    ) -> None:
        super().__init__()
        self._provider = provider
        self._binding = binding
        self._agent_run_id = agent_run_id
        self._tools = tools
        self._usage_sink = usage_sink
        self._cancellation = cancellation

    @property
    def _llm_type(self) -> str:
        return "codemigrator-locked-provider"

    @property
    def usages(self) -> tuple[TokenUsage, ...]:
        return tuple(self._usages)

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> ProviderChatModel:
        if {tool.name for tool in tools} != {tool.name for tool in self._tools}:
            raise ValueError("LangChain tool set differs from frozen AgentRun policy")
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise RuntimeError("AgentRun model requires asynchronous execution")

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self._cancellation.raise_if_cancelled()
        response = await self._provider.complete(
            ProviderRequest(
                binding=self._binding,
                messages=tuple(_prompt_message(message) for message in messages),
                tools=self._tools,
                cancellation=self._cancellation,
            )
        )
        self._cancellation.raise_if_cancelled()
        self._calls += 1
        call_id = f"{self._agent_run_id}:{self._calls}"
        receipt = UsageReceipt(
            run_id=self._agent_run_id,
            call=ProviderCallIdentity(
                call_id=call_id,
                provider_receipt_id=response.provider_receipt_id or call_id,
                provider_id=self._binding.provider_id,
                model_id=self._binding.model_id,
                session_key=str(self._agent_run_id),
            ),
            usage=response.usage,
        )
        self._usages.append(response.usage)
        if self._usage_sink is not None:
            await self._usage_sink.record(self._agent_run_id, response.usage, receipt)
        tool_calls = []
        for index, call in enumerate(response.tool_calls):
            arguments = json.loads(call.arguments)
            if not isinstance(arguments, dict):
                raise ValueError("provider tool arguments must be an object")
            tool_calls.append(
                {
                    "name": call.name,
                    "args": arguments,
                    "id": call.call_id or f"{call_id}:{index}",
                    "type": "tool_call",
                }
            )
        message = AIMessage(
            content=response.content,
            tool_calls=tool_calls,
            usage_metadata={
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "total_tokens": response.usage.total_tokens,
            },
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


class GovernedContextMiddleware(AgentMiddleware):
    """Replace graph history with an M-14 measured view before every model call."""

    def __init__(
        self,
        *,
        manager: ContextManager,
        binding: LockedModelBinding,
        template: str,
        envelope: ContextEnvelope,
        tool_schema_tokens: int,
        watermark_pct: int,
        eviction_audit_sink: EvictionAuditSink | None,
    ) -> None:
        self.manager = manager
        self.binding = binding
        self.template = template
        self.envelope = envelope
        self.tool_schema_tokens = tool_schema_tokens
        self.watermark_pct = watermark_pct
        self.eviction = EvictionEngine()
        self.eviction_audit_sink = eviction_audit_sink

    def _project(self, request: ModelRequest) -> list[AnyMessage]:
        owner_task_positions = [
            index
            for index, message in enumerate(request.messages)
            if isinstance(message, HumanMessage) and message.name == "codemigrator_owner_task"
        ]
        if not owner_task_positions:
            raise ValueError("AgentRun requires one owner task instruction")
        latest_task_position = owner_task_positions[-1]
        latest_task = request.messages[latest_task_position]
        if not isinstance(latest_task.content, str):
            raise ValueError("AgentRun owner task must be text")
        tool_results = [
            message
            for message in request.messages[latest_task_position + 1 :]
            if isinstance(message, ToolMessage)
        ]
        envelope = ContextEnvelope(
            stable=self.envelope.stable,
            evolving=self.envelope.evolving,
            targeted=(
                *(
                    segment
                    if segment.source_ref is not None
                    and CAS_URI_PATTERN.fullmatch(segment.source_ref)
                    else replace(segment, evictable=False)
                    for segment in self.envelope.targeted
                ),
                ContextSegment(
                    "targeted",
                    latest_task.content,
                    required=True,
                    evictable=False,
                    source_ref="owner:task",
                ),
                *(
                    ContextSegment(
                        "targeted",
                        str(message.content),
                        evictable=False,
                        source_ref=f"tool:{message.tool_call_id}",
                        turn_index=index,
                    )
                    for index, message in enumerate(tool_results)
                ),
            ),
        )
        messages = render_prompt(self.template, envelope)
        counter = self.manager.token_counter
        cap_port = self.manager.net_input_cap
        if counter is None or cap_port is None:
            raise ContextBudgetError("exact provider context capability is required")
        cap = cap_port.compute(
            context_window=self.binding.context_window,
            reserved_output=self.binding.output_cap,
            tool_schema_tokens=self.tool_schema_tokens,
            envelope_margin=0,
        )
        count = counter.count(messages)
        if count * 100 >= cap * self.watermark_pct:
            envelope = self.eviction.evict(
                envelope,
                current_turn=len(tool_results),
                current_tokens=count,
                net_input_cap=cap,
                watermark_pct=self.watermark_pct,
                measure=lambda view: counter.count(render_prompt(self.template, view)),
                audit_sink=self.eviction_audit_sink,
            ).envelope
            messages = render_prompt(self.template, envelope)
        self.manager.fit_messages(
            messages,
            context_window=self.binding.context_window,
            reserved_output=self.binding.output_cap,
            tool_schema_tokens=self.tool_schema_tokens,
            envelope_margin=0,
        )
        return [
            SystemMessage(content=message.content)
            if message.role == "system"
            else HumanMessage(content=message.content)
            for message in messages
        ]

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        return await handler(request.override(messages=self._project(request), system_message=None))


@dataclass(slots=True)
class BoundAgentRun:
    agent_run: AgentRun
    graph: Any
    model: ProviderChatModel

    async def ainvoke(self, *, task: str) -> SessionResult:
        if not isinstance(task, str) or not task.strip():
            raise ValueError("AgentRun owner task must be non-empty text")
        messages = [HumanMessage(content=task, name="codemigrator_owner_task")]
        output = await self.graph.ainvoke(
            {"messages": messages},
            config={"configurable": {"thread_id": self.agent_run.thread_id}},
        )
        last = output["messages"][-1]
        return SessionResult(
            state=SessionState.CheckpointPending,
            exit=SessionExit.Completed,
            assistant_texts=(str(last.content),),
            usages=self.model.usages,
            rounds=len(self.model.usages),
            agent_run_id=self.agent_run.agent_run_id,
        )


def create_bound_agent(
    *,
    agent_run: AgentRun,
    binding: LockedModelBinding,
    registry: ProviderRegistry,
    context_manager: ContextManager,
    template: str,
    envelope: ContextEnvelope,
    gateway: ToolGatewayPort,
    usage_sink: AgentUsageSink | None = None,
    cancellation: CancellationToken | None = None,
    context_identity: ContextPackIdentity | DraftContextIdentity | None = None,
    checkpointer: Any = None,
    eviction_audit_sink: EvictionAuditSink | None = None,
) -> BoundAgentRun:
    if binding.digest != agent_run.model_binding_sha256:
        raise ValueError("AgentRun model binding digest changed")
    if agent_run.phase in (Phase.Verify, Phase.Report):
        raise ValueError("deterministic phases cannot create an AgentRun model")
    tool_names = allowed_tool_names(
        agent_run.phase, agent_run.session_kind, draft=agent_run.owner_kind == "draft"
    )
    definitions = tuple(_tool_definition(name) for name in tool_names)
    counter = context_manager.token_counter
    count_schemas = getattr(counter, "count_tool_schemas", None)
    if definitions and not callable(count_schemas):
        raise ContextBudgetError(
            "exact provider tool schema counter is required", code="CONTEXT_CAPABILITY_INVALID"
        )
    tool_schema_tokens = (
        cast(Callable[[tuple[ToolDefinition, ...]], int], count_schemas)(definitions)
        if definitions
        else 0
    )
    if type(tool_schema_tokens) is not int or tool_schema_tokens < 0:
        raise ContextBudgetError(
            "exact provider tool schema count is invalid", code="CONTEXT_CAPABILITY_INVALID"
        )
    if agent_run.owner_kind == "draft":
        if not isinstance(context_identity, DraftContextIdentity):
            raise ValueError("Draft AgentRun requires DraftContextIdentity")
        if (
            context_identity.draft_id != agent_run.owner_id
            or context_identity.agent_run_id != agent_run.agent_run_id
        ):
            raise ValueError("Draft context owner differs from AgentRun")
        budget = context_manager.budget_catalog.profile("DRAFTING")
        context_manager.fit_draft(
            identity=context_identity,
            template=template,
            envelope=envelope,
            context_window=binding.context_window,
            reserved_output=binding.output_cap,
            tool_schema_tokens=tool_schema_tokens,
            envelope_margin=0,
        )
    else:
        if (
            not isinstance(context_identity, ContextPackIdentity)
            or context_identity.run_id != agent_run.owner_id
            or context_identity.phase is not agent_run.phase
            or context_identity.session is not agent_run.session_kind
            or context_identity.model_binding_sha256 != binding.digest
        ):
            raise ValueError("Run ContextPack identity differs from AgentRun")
        budget = context_manager.budget_catalog.profile(agent_run.session_kind)
        context_manager.fit(
            identity=context_identity,
            template=template,
            envelope=envelope,
            context_window=binding.context_window,
            reserved_output=binding.output_cap,
            tool_schema_tokens=tool_schema_tokens,
            envelope_margin=0,
        )
    token = cancellation or CancellationToken.create()
    model = ProviderChatModel(
        provider=registry.resolve(binding),
        binding=binding,
        agent_run_id=agent_run.agent_run_id,
        tools=definitions,
        usage_sink=usage_sink,
        cancellation=token,
    )
    tools = []
    for definition in definitions:

        async def dispatch_tool(_name: str = definition.name, **kwargs: Any) -> str:
            token.raise_if_cancelled()
            result = gateway.dispatch({"tool": _name, **kwargs}, cancellation_token=token)
            if inspect.isawaitable(result):
                result = await result
            token.raise_if_cancelled()
            if hasattr(result, "model_dump_json"):
                return str(cast(Any, result).model_dump_json())
            return json.dumps(result, default=str)

        tools.append(
            StructuredTool.from_function(
                name=definition.name,
                description=definition.description,
                coroutine=dispatch_tool,
                args_schema=dict(definition.parameters),
            )
        )
    middleware = GovernedContextMiddleware(
        manager=context_manager,
        binding=binding,
        template=template,
        envelope=envelope,
        tool_schema_tokens=tool_schema_tokens,
        watermark_pct=budget.eviction_watermark_pct,
        eviction_audit_sink=eviction_audit_sink,
    )
    graph = create_agent(model, tools, middleware=[middleware], checkpointer=checkpointer)
    return BoundAgentRun(agent_run, graph, model)


__all__ = [
    "BoundAgentRun",
    "GovernedContextMiddleware",
    "ProviderChatModel",
    "allowed_tool_names",
    "create_bound_agent",
]

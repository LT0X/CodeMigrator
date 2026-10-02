"""One locked LangChain agent per AgentRun, with runtime-owned model and tools."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, cast
from uuid import uuid4

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.agents.structured_output import ToolStrategy
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
from langchain_core.tracers.context import tracing_v2_callback_var
from langchain_core.utils.function_calling import convert_to_openai_function
from langgraph.checkpoint.base import BaseCheckpointSaver
from langsmith import tracing_context
from pydantic import BaseModel, PrivateAttr, ValidationError

from codemigrator.core import (
    ContextPackIdentity,
    Phase,
    SessionKind,
    canonical_json_bytes,
    load_resource,
)
from codemigrator.core.models.plan import PlanProposal
from codemigrator.workspace import GatewayContext
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
    BudgetProfile,
    ContextBudgetError,
    ContextManager,
    DraftContextIdentity,
    EvictionAuditSink,
    EvictionEngine,
)
from .plan_agent_output import PlanProposalAgentOutput, parse_plan_proposal_agent_output
from .provider import (
    AsyncProvider,
    ProviderCallIdentity,
    ProviderError,
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
_ROUND_LIMIT_MARKER = "codemigrator_round_limit"


@contextmanager
def _agent_run_tracing_boundary() -> Iterator[None]:
    """Prevent environment and inherited LangChain tracing from exporting state."""

    token = tracing_v2_callback_var.set(None)
    try:
        with tracing_context(enabled=False, parent=False, client=None, replicas=[]):
            yield
    finally:
        tracing_v2_callback_var.reset(token)


class AgentUsageSink(Protocol):
    async def reserve_round(
        self, agent_run_id: AgentRunId, call_id: str, *, max_rounds: int
    ) -> int | None: ...

    async def record(
        self, agent_run_id: AgentRunId, usage: TokenUsage, receipt: UsageReceipt
    ) -> None: ...


def agent_template_digest(*, session: str, template: str) -> str:
    """Digest the exact session template using the ContextManager encoding."""

    if not session or not template:
        raise ValueError("session and template are required for the template digest")
    return hashlib.sha256(
        canonical_json_bytes({"session": session, "template": template})
    ).hexdigest()


def agent_toolset_digest(
    *,
    phase: Phase,
    session_kind: SessionKind,
    owner_kind: str,
    response_format: type[BaseModel] | None = None,
) -> str:
    """Digest the closed tools and canonical response contract for one AgentRun."""

    if owner_kind not in {"run", "draft"}:
        raise ValueError("AgentRun owner kind must be run or draft")
    definitions = agent_tool_definitions(
        phase=phase,
        session_kind=session_kind,
        owner_kind=owner_kind,
    )
    toolset = tuple(
        {
            "name": definition.name,
            "description": definition.description,
            "parameters": dict(definition.parameters),
        }
        for definition in definitions
    )
    payload = {
        "policy_resource": "core://phase-tool-policy/v2",
        "owner_kind": owner_kind,
        "phase": phase.value,
        "session_kind": session_kind.value,
        "tools": toolset,
    }
    digest = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    if response_format is None:
        return digest
    output_tool = _raw_structured_output_tool_definition(response_format)
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "toolset_sha256": digest,
                "structured_output": {
                    "name": output_tool.name,
                    "description": output_tool.description,
                    "parameters": dict(output_tool.parameters),
                },
            }
        )
    ).hexdigest()


def agent_tool_definitions(
    *,
    phase: Phase,
    session_kind: SessionKind,
    owner_kind: str,
    response_format: type[BaseModel] | None = None,
) -> tuple[ToolDefinition, ...]:
    """Return the exact closed schemas used for one phase/session AgentRun."""

    if owner_kind not in {"run", "draft"}:
        raise ValueError("AgentRun owner kind must be run or draft")
    if response_format is not None and (
        not isinstance(response_format, type) or not issubclass(response_format, BaseModel)
    ):
        raise TypeError("structured response format must be a Pydantic model class")
    definitions = tuple(
        _tool_definition(name)
        for name in allowed_tool_names(phase, session_kind, draft=owner_kind == "draft")
    )
    if response_format is None:
        return definitions
    return (*definitions, _structured_output_tool_definition(response_format))


def _structured_output_tool_definition(schema: type[BaseModel]) -> ToolDefinition:
    wire_schema: type[BaseModel] = (
        PlanProposalAgentOutput if schema is PlanProposal else schema
    )
    raw_wire_schema = wire_schema.model_json_schema()
    if _has_dynamic_object_schemas(raw_wire_schema):
        raise ValueError("strict structured output requires explicit object fields")
    function_schema = convert_to_openai_function(wire_schema, strict=True)
    parameters = function_schema.get("parameters")
    if not isinstance(parameters, dict):
        raise ValueError("structured output schema must be a JSON object")
    _remove_schema_defaults(parameters)
    return ToolDefinition(
        name=schema.__name__,
        description=schema.__doc__ or "",
        parameters=parameters,
        strict=True,
    )


def _raw_structured_output_tool_definition(schema: type[BaseModel]) -> ToolDefinition:
    """Return the canonical Pydantic schema used by the persisted AgentRun digest."""

    return ToolDefinition(
        name=schema.__name__,
        description=schema.__doc__ or "",
        parameters=schema.model_json_schema(),
    )


def _remove_schema_defaults(schema: object) -> None:
    """Drop JSON Schema defaults from the strict provider-facing projection."""

    if isinstance(schema, dict):
        schema.pop("default", None)
        for value in schema.values():
            _remove_schema_defaults(value)
    elif isinstance(schema, list):
        for value in schema:
            _remove_schema_defaults(value)


def _has_dynamic_object_schemas(schema: object) -> bool:
    if isinstance(schema, dict):
        if schema.get("additionalProperties") is True or isinstance(
            schema.get("additionalProperties"), dict
        ):
            return True
        return any(_has_dynamic_object_schemas(value) for value in schema.values())
    if isinstance(schema, list):
        return any(_has_dynamic_object_schemas(value) for value in schema)
    return False


def agent_context_digest(
    *,
    context_identity: ContextPackIdentity | DraftContextIdentity,
    envelope: ContextEnvelope,
    phase: Phase,
    session_kind: SessionKind,
    template_sha256: str,
    budget: BudgetProfile,
) -> str:
    """Digest the frozen identity and initial context envelope without storing its body."""

    if isinstance(context_identity, ContextPackIdentity):
        identity: dict[str, object] = context_identity.model_dump(mode="json", by_alias=True)
        if identity.get("planning_material_sha256") == "0" * 64:
            # Preserve persisted digests for non-PLAN AgentRuns created before
            # this optional PLAN identity component existed.
            identity.pop("planning_material_sha256", None)
        identity["template_sha256"] = template_sha256
    elif isinstance(context_identity, DraftContextIdentity):
        identity = {
            "draft_id": str(context_identity.draft_id),
            "revision_id": str(context_identity.revision_id),
            "agent_run_id": str(context_identity.agent_run_id),
        }
    else:
        raise TypeError("unsupported AgentRun context identity")
    segments = {
        kind: [
            {
                "content": segment.content,
                "required": segment.required,
                "evictable": segment.evictable,
                "source_body": segment.source_body,
                "source_ref": segment.source_ref,
                "turn_index": segment.turn_index,
            }
            for segment in getattr(envelope, kind)
        ]
        for kind in ("stable", "evolving", "targeted")
    }
    payload = {
        "phase": phase.value,
        "session_kind": session_kind.value,
        "template_sha256": template_sha256,
        "context_identity": identity,
        "envelope": segments,
        "budget": {
            "session": (
                budget.session.value if isinstance(budget.session, SessionKind) else budget.session
            ),
            "max_rounds": budget.max_rounds,
            "eviction_watermark_pct": budget.eviction_watermark_pct,
        },
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


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
    _usage_sink: AgentUsageSink = PrivateAttr()
    _cancellation: CancellationToken = PrivateAttr()
    _tools: tuple[ToolDefinition, ...] = PrivateAttr()
    _request_tools: tuple[ToolDefinition, ...] = PrivateAttr()
    _request_tool_choice: str | None = PrivateAttr(default=None)
    _structured_output: type[BaseModel] | None = PrivateAttr(default=None)
    _max_rounds: int = PrivateAttr()
    _usages: list[TokenUsage] = PrivateAttr(default_factory=list)
    _calls: int = PrivateAttr(default=0)

    def __init__(
        self,
        *,
        provider: AsyncProvider,
        binding: LockedModelBinding,
        agent_run_id: AgentRunId,
        tools: tuple[ToolDefinition, ...],
        usage_sink: AgentUsageSink,
        cancellation: CancellationToken,
        max_rounds: int,
        structured_output: type[BaseModel] | None = None,
    ) -> None:
        super().__init__()
        self._provider = provider
        self._binding = binding
        self._agent_run_id = agent_run_id
        self._tools = tools
        self._request_tools = tools
        self._structured_output = structured_output
        self._usage_sink = usage_sink
        self._cancellation = cancellation
        self._max_rounds = max_rounds

    @property
    def _llm_type(self) -> str:
        return "codemigrator-locked-provider"

    @property
    def usages(self) -> tuple[TokenUsage, ...]:
        return tuple(self._usages)

    @property
    def rounds(self) -> int:
        return self._calls

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> ProviderChatModel:
        names = {tool.name for tool in tools}
        authorized_names = {tool.name for tool in self._tools}
        output_name = self._structured_output.__name__ if self._structured_output else None
        expected_names = authorized_names | ({output_name} if output_name else set())
        if names != expected_names:
            raise ValueError("LangChain tool set differs from frozen AgentRun policy")
        definitions = list(self._tools)
        if output_name is not None:
            output_tool = next(tool for tool in tools if tool.name == output_name)
            schema = output_tool.args_schema
            if hasattr(schema, "model_json_schema"):
                parameters = schema.model_json_schema()
            elif isinstance(schema, dict):
                parameters = schema
            else:
                raise ValueError("structured output tool has no closed JSON schema")
            expected_schema = _raw_structured_output_tool_definition(
                cast(type[BaseModel], self._structured_output)
            ).parameters
            if parameters.get("properties") != expected_schema.get("properties"):
                raise ValueError("structured output schema differs from the frozen AgentRun schema")
            definitions.append(
                _structured_output_tool_definition(cast(type[BaseModel], self._structured_output))
            )
        self._request_tools = tuple(definitions)
        tool_choice = kwargs.get("tool_choice")
        self._request_tool_choice = str(tool_choice) if isinstance(tool_choice, str) else None
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
        call_id = f"{self._agent_run_id}:{uuid4()}"
        round_index = await self._usage_sink.reserve_round(
            self._agent_run_id, call_id, max_rounds=self._max_rounds
        )
        if round_index is None:
            self._calls = max(self._calls, self._max_rounds)
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content="",
                            response_metadata={_ROUND_LIMIT_MARKER: True},
                        )
                    )
                ]
            )
        if type(round_index) is not int or not 1 <= round_index <= self._max_rounds:
            raise RuntimeError("AgentRun round reservation is invalid")
        self._calls = max(self._calls, round_index)
        response = await self._provider.complete(
            ProviderRequest(
                binding=self._binding,
                messages=tuple(_prompt_message(message) for message in messages),
                tools=self._request_tools,
                tool_choice=self._request_tool_choice,
                cancellation=self._cancellation,
            )
        )
        self._cancellation.raise_if_cancelled()
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
        await self._usage_sink.record(self._agent_run_id, response.usage, receipt)
        if response.finish_reason in {
            "length",
            "max_tokens",
            "model_context_window_exceeded",
        }:
            raise ProviderError(
                "provider request failed",
                retryable=False,
                failure_code="provider_response_truncated",
            )
        tool_calls = []
        for index, call in enumerate(response.tool_calls):
            try:
                arguments = json.loads(call.arguments)
            except (TypeError, ValueError):
                raise ProviderError(
                    "provider request failed",
                    retryable=False,
                    failure_code="invalid_tool_arguments_json",
                ) from None
            if not isinstance(arguments, dict):
                raise ProviderError(
                    "provider request failed",
                    retryable=False,
                    failure_code="invalid_tool_arguments_shape",
                )
            if (
                self._structured_output is PlanProposal
                and call.name == self._structured_output.__name__
            ):
                arguments = parse_plan_proposal_agent_output(arguments) or arguments
            tool_calls.append(
                {
                    "name": call.name,
                    "args": arguments,
                    "id": call.call_id or f"{call_id}:{index}",
                    "type": "tool_call",
                }
            )
        content = response.content
        if not tool_calls and self._structured_output is not None and content:
            try:
                content_value = json.loads(content)
            except (TypeError, ValueError):
                content_value = None
            if self._structured_output is PlanProposal:
                content_value = parse_plan_proposal_agent_output(content_value) or content_value
            structured_value = None
            if isinstance(content_value, dict):
                try:
                    structured_value = self._structured_output.model_validate(content_value)
                except ValidationError:
                    pass
            if structured_value is not None:
                tool_calls.append(
                    {
                        "name": self._structured_output.__name__,
                        "args": structured_value.model_dump(mode="json", by_alias=True),
                        "id": f"{call_id}:structured-output",
                        "type": "tool_call",
                    }
                )
                content = ""
        message = AIMessage(
            content=content,
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
    checkpointer: BaseCheckpointSaver[Any]
    max_rounds: int
    _invoke_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _segment_stopped: bool = field(default=False, init=False, repr=False)

    async def ainvoke(self, *, task: str) -> SessionResult:
        if not isinstance(task, str) or not task.strip():
            raise ValueError("AgentRun owner task must be non-empty text")
        async with self._invoke_lock:
            if self._segment_stopped:
                raise RuntimeError("a segment-stopped AgentRun cannot be invoked again")
            messages = [HumanMessage(content=task, name="codemigrator_owner_task")]
            usage_start = len(self.model.usages)
            config = {
                "configurable": {"thread_id": self.agent_run.thread_id},
                "callbacks": [],
                "recursion_limit": max(25, self.max_rounds * 2 + 5),
            }
            with _agent_run_tracing_boundary():
                output = await self.graph.ainvoke({"messages": messages}, config=config)
            last = output["messages"][-1]
            round_limited = bool(
                isinstance(last, AIMessage)
                and last.response_metadata.get(_ROUND_LIMIT_MARKER) is True
            )
            self._segment_stopped = round_limited
            return SessionResult(
                state=SessionState.CheckpointPending,
                exit=SessionExit.SegmentStopped if round_limited else SessionExit.Completed,
                assistant_texts=() if round_limited else (str(last.content),),
                usages=self.model.usages[usage_start:],
                rounds=self.model.rounds,
                agent_run_id=self.agent_run.agent_run_id,
                structured_response=output.get("structured_response"),
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
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    eviction_audit_sink: EvictionAuditSink | None = None,
    response_format: type[BaseModel] | None = None,
) -> BoundAgentRun:
    if checkpointer is None:
        raise ValueError("persistent AgentRun requires a LangGraph checkpointer")
    if usage_sink is None:
        raise ValueError("persistent AgentRun requires a durable model-call usage sink")
    if binding.digest != agent_run.model_binding_sha256:
        raise ValueError("AgentRun model binding digest changed")
    gateway_context = getattr(gateway, "context", None)
    slice_ref = agent_run.slice_ref
    if not isinstance(gateway_context, GatewayContext) or (
        gateway_context.agent_run_id != agent_run.agent_run_id
        or gateway_context.phase is not agent_run.phase
        or gateway_context.session_kind is not agent_run.session_kind
        or gateway_context.slice_id != (slice_ref.slice_id if slice_ref is not None else None)
        or gateway_context.generation != (slice_ref.generation if slice_ref is not None else None)
        or (
            agent_run.owner_kind == "run"
            and (
                gateway_context.run_id != agent_run.owner_id
                or gateway_context.draft_id is not None
            )
        )
        or (
            agent_run.owner_kind == "draft"
            and (
                gateway_context.draft_id != agent_run.owner_id
                or gateway_context.run_id is not None
            )
        )
    ):
        raise ValueError("gateway context differs from AgentRun")
    if agent_run.phase in (Phase.Verify, Phase.Report):
        raise ValueError("deterministic phases cannot create an AgentRun model")
    if response_format is not None and not isinstance(response_format, type):
        raise TypeError("structured response format must be a Pydantic model class")
    schema_definitions = agent_tool_definitions(
        phase=agent_run.phase,
        session_kind=agent_run.session_kind,
        owner_kind=agent_run.owner_kind,
        response_format=response_format,
    )
    definitions = schema_definitions[: len(allowed_tool_names(
        agent_run.phase, agent_run.session_kind, draft=agent_run.owner_kind == "draft"
    ))]
    if (
        agent_toolset_digest(
            phase=agent_run.phase,
            session_kind=agent_run.session_kind,
            owner_kind=agent_run.owner_kind,
            response_format=response_format,
        )
        != agent_run.toolset_sha256
    ):
        raise ValueError("AgentRun toolset digest differs from the authorized tool schemas")
    counter = context_manager.token_counter
    count_schemas = getattr(counter, "count_tool_schemas", None)
    if schema_definitions and not callable(count_schemas):
        raise ContextBudgetError(
            "exact provider tool schema counter is required", code="CONTEXT_CAPABILITY_INVALID"
        )
    tool_schema_tokens = (
        cast(Callable[[tuple[ToolDefinition, ...]], int], count_schemas)(schema_definitions)
        if schema_definitions
        else 0
    )
    if type(tool_schema_tokens) is not int or tool_schema_tokens < 0:
        raise ContextBudgetError(
            "exact provider tool schema count is invalid", code="CONTEXT_CAPABILITY_INVALID"
        )
    frozen_context_identity: ContextPackIdentity | DraftContextIdentity
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
        frozen_context_identity = context_identity
        frozen_template_sha256 = agent_template_digest(
            session=agent_run.session_kind.value, template=template
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
        assembly = context_manager.fit(
            identity=context_identity,
            template=template,
            envelope=envelope,
            context_window=binding.context_window,
            reserved_output=binding.output_cap,
            tool_schema_tokens=tool_schema_tokens,
            envelope_margin=0,
        )
        frozen_context_identity = assembly.pack.identity
        frozen_template_sha256 = str(assembly.pack.identity.template_sha256)
    if frozen_template_sha256 != agent_run.template_sha256:
        raise ValueError("AgentRun template digest differs from the selected session template")
    if (
        agent_context_digest(
            context_identity=frozen_context_identity,
            envelope=envelope,
            phase=agent_run.phase,
            session_kind=agent_run.session_kind,
            template_sha256=frozen_template_sha256,
            budget=budget,
        )
        != agent_run.context_sha256
    ):
        raise ValueError("AgentRun context digest differs from the frozen context envelope")
    token = cancellation or CancellationToken.create()
    model = ProviderChatModel(
        provider=registry.resolve(binding),
        binding=binding,
        agent_run_id=agent_run.agent_run_id,
        tools=definitions,
        usage_sink=usage_sink,
        cancellation=token,
        max_rounds=budget.max_rounds,
        structured_output=response_format,
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
    graph = create_agent(
        model,
        tools,
        middleware=[middleware],
        checkpointer=checkpointer,
        response_format=ToolStrategy(response_format) if response_format is not None else None,
    )
    return BoundAgentRun(agent_run, graph, model, checkpointer, budget.max_rounds)


__all__ = [
    "BoundAgentRun",
    "GovernedContextMiddleware",
    "ProviderChatModel",
    "agent_context_digest",
    "agent_template_digest",
    "agent_tool_definitions",
    "agent_toolset_digest",
    "allowed_tool_names",
    "create_bound_agent",
]

"""Small asynchronous adapters for OpenAI-compatible and Anthropic APIs."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol, cast

import httpx

from .binding import LockedModelBinding
from .context import PromptMessage


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int
    cost_micros: int = 0

    def __post_init__(self) -> None:
        if min(self.input_tokens, self.output_tokens, self.cost_micros) < 0:
            raise ValueError("usage values must not be negative")

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class ProviderToolCall:
    name: str
    arguments: str
    call_id: str = ""


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    parameters: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))


DEFAULT_TOOL_DEFINITIONS: tuple[ToolDefinition, ...] = tuple(
    ToolDefinition(
        name=name,
        description=f"Invoke the closed CodeMigrator {name} tool.",
        parameters={"type": "object", "additionalProperties": True},
    )
    for name in ("ReadFile", "WriteFile", "EditFile", "QuerySourceAst", "Shell", "Exec")
)


class CancellationSignal(Protocol):
    @property
    def cancelled(self) -> bool: ...

    async def wait(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    binding: LockedModelBinding
    messages: tuple[PromptMessage, ...]
    tools: tuple[ToolDefinition, ...] = DEFAULT_TOOL_DEFINITIONS
    cancellation: CancellationSignal | None = None


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    content: str
    tool_calls: tuple[ProviderToolCall, ...]
    finish_reason: str
    usage: TokenUsage
    model: str = ""
    provider_receipt_id: str = ""


@dataclass(frozen=True, slots=True)
class ProviderCallIdentity:
    call_id: str
    provider_receipt_id: str
    provider_id: str
    model_id: str
    session_key: str


@dataclass(frozen=True, slots=True)
class UsageReceipt:
    run_id: object
    call: ProviderCallIdentity
    usage: TokenUsage
    provenance: object | None = None

    @property
    def receipt_id(self) -> str:
        """Return the globally scoped id used for idempotent wallet writes."""

        return f"{self.run_id}:{self.call.provider_receipt_id}"


class ProviderError(RuntimeError):
    """A provider call failed without exposing credentials or response bodies."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        retry_delay_secs: int | None = None,
        cancelled: bool = False,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_delay_secs = retry_delay_secs
        self.cancelled = cancelled


class AsyncProvider(Protocol):
    async def complete(self, request: ProviderRequest) -> ProviderResponse: ...


class ProviderRegistry:
    """Resolve an adapter using only the provider frozen in the binding."""

    def __init__(self, adapters: Mapping[str, AsyncProvider]) -> None:
        self._adapters = dict(adapters)

    def resolve(self, binding: LockedModelBinding) -> AsyncProvider:
        try:
            return self._adapters[binding.provider_id]
        except KeyError as exc:
            raise ProviderError("provider binding is unavailable", retryable=False) from exc


def retry_delay_for_attempt(attempt: int) -> int:
    """Return the actor-facing retry hint for the 1st, 2nd, or later retry."""

    if type(attempt) is not int or attempt < 1:
        raise ValueError("retry attempt must be a positive integer")
    return (30, 60, 120)[min(attempt - 1, 2)]


def _provider_error(*, retryable: bool) -> ProviderError:
    return ProviderError(
        "provider request failed",
        retryable=retryable,
        retry_delay_secs=retry_delay_for_attempt(1) if retryable else None,
    )


async def _post_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: Mapping[str, str],
    payload: Mapping[str, object],
    cancellation: CancellationSignal | None = None,
) -> dict[str, Any]:
    request_task = asyncio.create_task(client.post(url, headers=headers, json=payload))
    cancellation_task: asyncio.Task[None] | None = None
    try:
        if cancellation is None:
            response = await request_task
        else:
            cancellation_task = asyncio.create_task(cancellation.wait())
            done, _ = await asyncio.wait(
                (request_task, cancellation_task), return_when=asyncio.FIRST_COMPLETED
            )
            if cancellation_task in done and request_task not in done:
                request_task.cancel()
                await asyncio.gather(request_task, return_exceptions=True)
                raise ProviderError("provider request cancelled", retryable=False, cancelled=True)
            response = await request_task
    except httpx.HTTPError as exc:
        raise _provider_error(retryable=True) from exc
    finally:
        if cancellation_task is not None and not cancellation_task.done():
            cancellation_task.cancel()
            await asyncio.gather(cancellation_task, return_exceptions=True)
    if response.status_code >= 500 or response.status_code == 429:
        raise _provider_error(retryable=True)
    if response.status_code >= 400:
        raise _provider_error(retryable=False)
    try:
        decoded = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise _provider_error(retryable=False) from exc
    if not isinstance(decoded, dict):
        raise _provider_error(retryable=False)
    return cast(dict[str, Any], decoded)


def _usage(
    payload: Mapping[str, object],
    *,
    input_key: str,
    output_key: str,
    binding: LockedModelBinding,
) -> TokenUsage:
    raw = payload.get("usage")
    if not isinstance(raw, Mapping):
        raise _provider_error(retryable=False)
    input_tokens = raw.get(input_key)
    output_tokens = raw.get(output_key)
    if type(input_tokens) is not int or type(output_tokens) is not int:
        raise _provider_error(retryable=False)
    raw_cost = raw.get("cost_micros")
    if raw_cost is None:
        cost_micros = math.ceil(input_tokens / 1000) * binding.input_cost_micros_per_1k
        cost_micros += math.ceil(output_tokens / 1000) * binding.output_cost_micros_per_1k
    elif type(raw_cost) is int and raw_cost >= 0:
        cost_micros = raw_cost
    else:
        raise _provider_error(retryable=False)
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_micros=cost_micros,
    )


def _content_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        parts: list[str] = []
        for block in value:
            if isinstance(block, Mapping) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    raise _provider_error(retryable=False)


class OpenAICompatibleProvider:
    """Call an OpenAI-compatible chat-completions endpoint."""

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not endpoint or not api_key:
            raise ValueError("provider endpoint and api key are required")
        self.endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._client = client or httpx.AsyncClient()

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        if request.binding.provider_id not in {"openai", "openai-compatible"}:
            raise _provider_error(retryable=False)
        payload = {
            "model": request.binding.model_id,
            "messages": _openai_messages(request.messages),
            "tools": _openai_tools(request.tools),
            "max_tokens": request.binding.output_cap,
        }
        decoded = await _post_json(
            self._client,
            f"{self.endpoint}/chat/completions",
            headers={"authorization": f"Bearer {self._api_key}"},
            payload=payload,
            cancellation=request.cancellation,
        )
        model = _response_model(decoded, request.binding.model_id)
        choices = decoded.get("choices")
        if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes)) or not choices:
            raise _provider_error(retryable=False)
        first = choices[0]
        if not isinstance(first, Mapping):
            raise _provider_error(retryable=False)
        message = first.get("message")
        if not isinstance(message, Mapping):
            raise _provider_error(retryable=False)
        tool_calls = _tool_calls(message.get("tool_calls"))
        finish_reason = first.get("finish_reason")
        if not isinstance(finish_reason, str):
            raise _provider_error(retryable=False)
        return ProviderResponse(
            content=_content_text(message.get("content")),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=_usage(
                decoded,
                input_key="prompt_tokens",
                output_key="completion_tokens",
                binding=request.binding,
            ),
            model=model,
            provider_receipt_id=_response_id(decoded),
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class AnthropicProvider:
    """Call the Anthropic Messages endpoint."""

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        anthropic_version: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not endpoint or not api_key or not anthropic_version:
            raise ValueError("provider endpoint, api key, and version are required")
        self.endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._version = anthropic_version
        self._client = client or httpx.AsyncClient()

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        if request.binding.provider_id != "anthropic":
            raise _provider_error(retryable=False)
        system_parts = _anthropic_system(request.messages)
        messages = _anthropic_messages(request.messages)
        decoded = await _post_json(
            self._client,
            f"{self.endpoint}/messages",
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": self._version,
            },
            payload={
                "model": request.binding.model_id,
                "system": system_parts,
                "messages": messages,
                "tools": _anthropic_tools(request.tools),
                "max_tokens": request.binding.output_cap,
            },
            cancellation=request.cancellation,
        )
        model = _response_model(decoded, request.binding.model_id)
        content = decoded.get("content")
        if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
            raise _provider_error(retryable=False)
        tool_calls = _tool_calls(
            tuple(
                {
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "input": block.get("input"),
                }
                for block in content
                if isinstance(block, Mapping) and block.get("type") == "tool_use"
            )
        )
        finish_reason = decoded.get("stop_reason")
        if not isinstance(finish_reason, str):
            raise _provider_error(retryable=False)
        return ProviderResponse(
            content=_content_text(content),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=_usage(
                decoded,
                input_key="input_tokens",
                output_key="output_tokens",
                binding=request.binding,
            ),
            model=model,
            provider_receipt_id=_response_id(decoded),
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def _tool_calls(value: object) -> tuple[ProviderToolCall, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise _provider_error(retryable=False)
    calls: list[ProviderToolCall] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise _provider_error(retryable=False)
        function = item.get("function")
        if isinstance(function, Mapping):
            name = function.get("name")
            arguments = function.get("arguments")
        else:
            name = item.get("name")
            arguments = item.get("input")
        if not isinstance(name, str) or not name:
            raise _provider_error(retryable=False)
        if isinstance(arguments, Mapping):
            arguments = json.dumps(arguments, separators=(",", ":"))
        if not isinstance(arguments, str):
            raise _provider_error(retryable=False)
        call_id = item.get("id")
        if not isinstance(call_id, str) or not call_id:
            call_id = f"call-{index + 1}"
        calls.append(ProviderToolCall(name=name, arguments=arguments, call_id=call_id))
    return tuple(calls)


def _response_model(payload: Mapping[str, object], expected: str) -> str:
    model = payload.get("model")
    if not isinstance(model, str) or model != expected:
        raise ProviderError("provider model identity mismatch", retryable=False)
    return model


def _response_id(payload: Mapping[str, object]) -> str:
    value = payload.get("id")
    if not isinstance(value, str) or not value:
        raise ProviderError("provider response receipt is missing", retryable=False)
    return value


def _openai_tools(tools: Sequence[ToolDefinition]) -> list[dict[str, object]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": dict(tool.parameters),
            },
        }
        for tool in tools
    ]


def _openai_messages(messages: Sequence[PromptMessage]) -> list[dict[str, object]]:
    rendered: list[dict[str, object]] = []
    for message in messages:
        item: dict[str, object] = {"role": message.role, "content": message.content}
        if message.role == "assistant" and message.native_tool_calls:
            item["tool_calls"] = [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }
                for call_id, name, arguments in message.native_tool_calls
            ]
        if message.role == "tool":
            if message.tool_call_id is None:
                raise ProviderError("tool message is missing call identity", retryable=False)
            item["tool_call_id"] = message.tool_call_id
        rendered.append(item)
    return rendered


def _anthropic_tools(tools: Sequence[ToolDefinition]) -> list[dict[str, object]]:
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": dict(tool.parameters),
        }
        for tool in tools
    ]


def _anthropic_system(messages: Sequence[PromptMessage]) -> list[dict[str, object]]:
    rendered: list[dict[str, object]] = [
        {"type": "text", "text": message.content}
        for message in messages
        if message.role == "system"
    ]
    if rendered:
        rendered[-1]["cache_control"] = {"type": "ephemeral"}
    return rendered


def _anthropic_messages(messages: Sequence[PromptMessage]) -> list[dict[str, object]]:
    non_system = [message for message in messages if message.role != "system"]
    last_stable = max(
        (index for index, message in enumerate(non_system) if message.segment_kind == "stable"),
        default=-1,
    )
    last_evolving = max(
        (index for index, message in enumerate(non_system) if message.segment_kind == "evolving"),
        default=-1,
    )
    rendered: list[dict[str, object]] = []
    for index, message in enumerate(non_system):
        if message.role == "assistant" and message.native_tool_calls:
            blocks: list[dict[str, object]] = []
            if message.content:
                blocks.append({"type": "text", "text": message.content})
            blocks.extend(
                {
                    "type": "tool_use",
                    "id": call_id,
                    "name": name,
                    "input": json.loads(arguments),
                }
                for call_id, name, arguments in message.native_tool_calls
            )
            rendered.append({"role": "assistant", "content": blocks})
            continue
        if message.role == "tool":
            if message.tool_call_id is None:
                raise ProviderError("tool message is missing call identity", retryable=False)
            rendered.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": message.tool_call_id,
                            "content": message.content,
                        }
                    ],
                }
            )
            continue
        block: dict[str, object] = {"type": "text", "text": message.content}
        if index in {last_stable, last_evolving}:
            block["cache_control"] = {"type": "ephemeral"}
        content: object = (
            [block] if message.segment_kind in {"stable", "evolving"} else message.content
        )
        rendered.append({"role": message.role, "content": content})
    return rendered


__all__ = [
    "AnthropicProvider",
    "AsyncProvider",
    "OpenAICompatibleProvider",
    "ProviderError",
    "ProviderRegistry",
    "ProviderRequest",
    "ProviderResponse",
    "ProviderToolCall",
    "ProviderCallIdentity",
    "TokenUsage",
    "ToolDefinition",
    "DEFAULT_TOOL_DEFINITIONS",
    "UsageReceipt",
    "retry_delay_for_attempt",
]

"""Small asynchronous adapters for OpenAI-compatible and Anthropic APIs."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    binding: LockedModelBinding
    messages: tuple[PromptMessage, ...]


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    content: str
    tool_calls: tuple[ProviderToolCall, ...]
    finish_reason: str
    usage: TokenUsage


class ProviderError(RuntimeError):
    """A provider call failed without exposing credentials or response bodies."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        retry_delay_secs: int | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_delay_secs = retry_delay_secs


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
) -> dict[str, Any]:
    try:
        response = await client.post(url, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        raise _provider_error(retryable=True) from exc
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


def _usage(payload: Mapping[str, object], *, input_key: str, output_key: str) -> TokenUsage:
    raw = payload.get("usage")
    if not isinstance(raw, Mapping):
        raise _provider_error(retryable=False)
    input_tokens = raw.get(input_key)
    output_tokens = raw.get(output_key)
    if type(input_tokens) is not int or type(output_tokens) is not int:
        raise _provider_error(retryable=False)
    return TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens)


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
            "messages": [
                {"role": message.role, "content": message.content}
                for message in request.messages
            ],
            "max_tokens": request.binding.output_cap,
        }
        decoded = await _post_json(
            self._client,
            f"{self.endpoint}/chat/completions",
            headers={"authorization": f"Bearer {self._api_key}"},
            payload=payload,
        )
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
            usage=_usage(decoded, input_key="prompt_tokens", output_key="completion_tokens"),
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
        system_parts = [message.content for message in request.messages if message.role == "system"]
        messages = [
            {"role": message.role, "content": message.content}
            for message in request.messages
            if message.role != "system"
        ]
        decoded = await _post_json(
            self._client,
            f"{self.endpoint}/messages",
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": self._version,
            },
            payload={
                "model": request.binding.model_id,
                "system": "\n\n".join(system_parts),
                "messages": messages,
                "max_tokens": request.binding.output_cap,
            },
        )
        content = decoded.get("content")
        if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
            raise _provider_error(retryable=False)
        tool_calls = _tool_calls(
            tuple(
                {
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
            usage=_usage(decoded, input_key="input_tokens", output_key="output_tokens"),
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def _tool_calls(value: object) -> tuple[ProviderToolCall, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise _provider_error(retryable=False)
    calls: list[ProviderToolCall] = []
    for item in value:
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
        calls.append(ProviderToolCall(name=name, arguments=arguments))
    return tuple(calls)


__all__ = [
    "AnthropicProvider",
    "AsyncProvider",
    "OpenAICompatibleProvider",
    "ProviderError",
    "ProviderRegistry",
    "ProviderRequest",
    "ProviderResponse",
    "ProviderToolCall",
    "TokenUsage",
    "retry_delay_for_attempt",
]

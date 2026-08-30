from __future__ import annotations

import json

import httpx
import pytest

from codemigrator.core import ModelProfile
from codemigrator.runtime.binding import LockedModelBinding
from codemigrator.runtime.context import PromptMessage
from codemigrator.runtime.provider import (
    AnthropicProvider,
    OpenAICompatibleProvider,
    ProviderError,
    ProviderRegistry,
    ProviderRequest,
    TokenUsage,
    retry_delay_for_attempt,
)


def _binding() -> LockedModelBinding:
    return LockedModelBinding(
        provider_id="openai",
        model_id="test-model",
        profile=ModelProfile.Code,
        config_revision="r1",
        context_window=1000,
        output_cap=200,
    )


def _request(binding: LockedModelBinding) -> ProviderRequest:
    return ProviderRequest(
        binding=binding,
        messages=(
            PromptMessage(role="system", content="role"),
            PromptMessage(role="user", content="data"),
        ),
    )


def test_provider_retry_hints_are_bounded_to_three_frozen_delays() -> None:
    assert [retry_delay_for_attempt(attempt) for attempt in (1, 2, 3, 4)] == [30, 60, 120, 120]


def test_provider_registry_resolves_only_the_locked_provider() -> None:
    class StubProvider:
        async def complete(self, request: ProviderRequest):
            raise AssertionError(request)

    provider = StubProvider()
    registry = ProviderRegistry({"openai": provider})

    assert registry.resolve(_binding()) is provider
    with pytest.raises(ProviderError, match="provider binding is unavailable"):
        registry.resolve(
            LockedModelBinding(
                provider_id="unknown",
                model_id="test-model",
                profile=ModelProfile.Code,
                config_revision="r1",
                context_window=1000,
                output_cap=200,
            )
        )


@pytest.mark.asyncio
async def test_openai_compatible_provider_maps_request_and_usage() -> None:
    binding = _binding()
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers["authorization"]
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    response = await OpenAICompatibleProvider(
        endpoint="https://provider.invalid/v1",
        api_key="secret",
        client=client,
    ).complete(_request(binding))
    await client.aclose()

    assert response.content == "done"
    assert response.usage == TokenUsage(input_tokens=12, output_tokens=4, cost_micros=0)
    assert seen["authorization"] == "Bearer secret"
    assert seen["payload"] == {
        "model": "test-model",
        "messages": [{"role": "system", "content": "role"}, {"role": "user", "content": "data"}],
        "max_tokens": 200,
    }


@pytest.mark.asyncio
async def test_anthropic_provider_keeps_system_separate_and_maps_usage() -> None:
    binding = LockedModelBinding(
        provider_id="anthropic",
        model_id="claude-test",
        profile=ModelProfile.Code,
        config_revision="r1",
        context_window=1000,
        output_cap=200,
    )
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        seen["key"] = request.headers["x-api-key"]
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "answer"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 8, "output_tokens": 3},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    response = await AnthropicProvider(
        endpoint="https://provider.invalid",
        api_key="secret",
        anthropic_version="2023-06-01",
        client=client,
    ).complete(_request(binding))
    await client.aclose()

    payload = seen["payload"]
    assert isinstance(payload, dict)
    assert payload["system"] == "role"
    assert payload["messages"] == [{"role": "user", "content": "data"}]
    assert response.content == "answer"
    assert response.usage.input_tokens == 8
    assert seen["key"] == "secret"


@pytest.mark.asyncio
async def test_provider_protocol_error_does_not_expose_response_body() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="private upstream detail")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(Exception) as caught:
        await OpenAICompatibleProvider(
            endpoint="https://provider.invalid",
            api_key="secret",
            client=client,
        ).complete(_request(_binding()))
    await client.aclose()

    assert "private upstream detail" not in str(caught.value)
    assert "secret" not in str(caught.value)

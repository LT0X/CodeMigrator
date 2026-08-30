from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from uuid import uuid4

import httpx
import pytest

from codemigrator.core import ModelProfile, SessionKind
from codemigrator.runtime.binding import LockedModelBinding
from codemigrator.runtime.budget import BudgetLimits, RunWallet
from codemigrator.runtime.context import ContextEnvelope, ContextSegment, PromptMessage
from codemigrator.runtime.loop import (
    AgentLoop,
    CancellationToken,
    CheckpointDecision,
    SessionExit,
    SessionProvenance,
)
from codemigrator.runtime.provider import (
    AnthropicProvider,
    OpenAICompatibleProvider,
    ProviderCallIdentity,
    ProviderRequest,
    ProviderResponse,
    ProviderToolCall,
    TokenUsage,
    UsageReceipt,
)

from .test_loop import FakeGateway, FakeProvider, _response, _spec


def test_prompt_messages_keep_segment_identity_for_cache_adapters() -> None:
    message = PromptMessage(role="user", content="facts", segment_kind="stable")
    assert message.segment_kind == "stable"


@pytest.mark.asyncio
async def test_second_provider_call_fails_closed_when_evolving_context_overflows() -> None:
    spec = _spec()
    spec = spec.__class__(
        identity=spec.identity,
        run_status=spec.run_status,
        binding=LockedModelBinding(
            provider_id=spec.binding.provider_id,
            model_id=spec.binding.model_id,
            profile=spec.binding.profile,
            config_revision=spec.binding.config_revision,
            context_window=100,
            output_cap=20,
        ),
        context_pack=spec.context_pack.model_copy(
            update={
                "identity": spec.context_pack.identity.model_copy(
                    update={"model_binding_sha256": ""}
                )
            }
        ),
        context=ContextEnvelope(targeted=(ContextSegment("targeted", "small"),)),
        template=spec.template,
    )
    spec = spec.__class__(
        identity=spec.identity,
        run_status=spec.run_status,
        binding=spec.binding,
        context_pack=spec.context_pack.model_copy(
            update={
                "identity": spec.context_pack.identity.model_copy(
                    update={"model_binding_sha256": spec.binding.digest}
                )
            }
        ),
        context=spec.context,
        template=spec.template,
    )
    provider = FakeProvider(
        [_response('{"tool":"ReadFile","path":"a.py"}'), _response('{"completed":true}')]
    )
    gateway = FakeGateway(["x" * 5000])

    result = await AgentLoop(provider=provider, gateway=gateway).run(spec)

    assert result.exit is SessionExit.Failed
    assert result.failure == "CONTEXT_OVERFLOW"
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_checkpoint_handoff_is_pending_until_commit_is_confirmed() -> None:
    provider = FakeProvider([_response('{"completed":true}')])

    @dataclass
    class PendingCheckpoint:
        states: list[object]

        async def precheck(self, spec: object, _: object) -> CheckpointDecision:
            self.states.append(spec.state)
            return CheckpointDecision(accepted=True, committed=False)

    checkpoint = PendingCheckpoint([])
    result = await AgentLoop(
        provider=provider,
        gateway=FakeGateway(),
        checkpoint=checkpoint,
    ).run(_spec())

    assert result.state.value == "CHECKPOINT_PENDING"
    assert result.outcome_published is False
    assert (
        checkpoint.states == ["CHECKPOINT_PENDING"]
        or checkpoint.states[0].value == "CHECKPOINT_PENDING"
    )


@pytest.mark.asyncio
async def test_budget_gate_stops_calls_after_wallet_closes() -> None:
    class ClosedBudget:
        async def admit(self, _: object) -> bool:
            return False

        async def record(self, _: UsageReceipt) -> bool:
            raise AssertionError("closed wallet must not record")

    provider = FakeProvider([_response('{"completed":true}')])
    result = await AgentLoop(provider=provider, gateway=FakeGateway(), budget=ClosedBudget()).run(
        _spec()
    )

    assert result.exit is SessionExit.BudgetExhausted
    assert result.failure == "BUDGET_EXHAUSTED"
    assert provider.requests == []


@pytest.mark.asyncio
async def test_run_wallet_deduplicates_receipts_and_stops_at_the_limit() -> None:
    wallet = RunWallet(BudgetLimits(input_tokens=10, output_tokens=10, cost_micros=100))
    run_id = uuid4()

    def receipt(call_id: str, input_tokens: int, output_tokens: int) -> UsageReceipt:
        return UsageReceipt(
            run_id=run_id,
            call=ProviderCallIdentity(
                call_id=call_id,
                provider_receipt_id=call_id,
                provider_id="openai",
                model_id="test-model",
                session_key="session",
            ),
            usage=TokenUsage(input_tokens, output_tokens, 10),
        )

    first = receipt("r1", 5, 2)
    assert await wallet.admit(first.run_id) is True
    assert await wallet.record(first) is True
    assert await wallet.record(first) is True
    assert await wallet.record(receipt("r2", 5, 8)) is False
    assert wallet.usage.input_tokens == 10
    assert wallet.usage.output_tokens == 10


@pytest.mark.asyncio
async def test_cancelled_inflight_cancellable_gateway_has_no_observation() -> None:
    class Gate:
        def __init__(self) -> None:
            self.cancelled = False

        async def allow(self, _: object) -> bool:
            return not self.cancelled

    class CancellableGateway:
        def __init__(self, gate: Gate) -> None:
            self.gate = gate
            self.started = asyncio.Event()
            self.calls = 0

        def dispatch(self, _: object, *, cancellation_token: CancellationToken) -> object:
            self.calls += 1
            self.started.set()
            while not self.gate.cancelled:
                pass
            cancellation_token.raise_if_cancelled()
            return "unreachable"

    gate = Gate()
    gateway = CancellableGateway(gate)
    provider = FakeProvider([_response('{"tool":"ReadFile","path":"a.py"}')])
    task = asyncio.create_task(
        AgentLoop(provider=provider, gateway=gateway, cancellation=gate).run(_spec())
    )
    await gateway.started.wait()
    gate.cancelled = True
    result = await asyncio.wait_for(task, timeout=2)

    assert result.exit is SessionExit.Invalidated
    assert result.observations == ()


@pytest.mark.asyncio
async def test_openai_native_tool_payload_and_model_identity_are_checked() -> None:
    binding = LockedModelBinding(
        provider_id="openai",
        model_id="test-model",
        profile=ModelProfile.Code,
        config_revision="r1",
        context_window=1000,
        output_cap=200,
    )
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "resp-1",
                "model": "test-model",
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "ReadFile",
                                        "arguments": '{"path":"a.py"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    response = await OpenAICompatibleProvider(
        endpoint="https://provider.invalid/v1", api_key="secret", client=client
    ).complete(
        ProviderRequest(
            binding=binding,
            messages=(PromptMessage(role="user", content="data"),),
        )
    )
    await client.aclose()

    payload = seen["payload"]
    assert isinstance(payload, dict)
    assert payload["tools"]
    assert response.tool_calls == (ProviderToolCall("ReadFile", '{"path":"a.py"}', "call-1"),)
    assert response.model == "test-model"
    assert response.provider_receipt_id == "resp-1"


@pytest.mark.asyncio
async def test_native_tool_call_round_trip_preserves_assistant_and_tool_messages() -> None:
    provider = FakeProvider(
        [
            ProviderResponse(
                content="",
                tool_calls=(ProviderToolCall("ReadFile", '{"path":"a.py"}', "call-1"),),
                finish_reason="tool_calls",
                usage=TokenUsage(input_tokens=2, output_tokens=1),
            ),
            _response('{"completed":true}'),
        ]
    )
    result = await AgentLoop(provider=provider, gateway=FakeGateway()).run(_spec())

    assert result.exit is SessionExit.Completed
    assert provider.requests[1].messages[-1].role == "tool"
    assert provider.requests[1].messages[-1].tool_call_id == "call-1"
    assistant = next(
        message for message in provider.requests[1].messages if message.role == "assistant"
    )
    assert provider.requests[1].messages[0].native_tool_calls == ()
    assert assistant.native_tool_calls == (("call-1", "ReadFile", '{"path":"a.py"}'),)


@pytest.mark.asyncio
async def test_anthropic_payload_has_explicit_stable_and_evolving_cache_boundaries() -> None:
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
        return httpx.Response(
            200,
            json={
                "id": "msg-1",
                "model": "claude-test",
                "content": [{"type": "text", "text": "done"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 8, "output_tokens": 3},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await AnthropicProvider(
        endpoint="https://provider.invalid",
        api_key="secret",
        anthropic_version="2023-06-01",
        client=client,
    ).complete(
        ProviderRequest(
            binding=binding,
            messages=(
                PromptMessage(role="user", content="stable", segment_kind="stable"),
                PromptMessage(role="user", content="evolving", segment_kind="evolving"),
                PromptMessage(role="user", content="targeted", segment_kind="targeted"),
            ),
        )
    )
    await client.aclose()

    payload = seen["payload"]
    assert isinstance(payload, dict)
    blocks = [block for message in payload["messages"] for block in message["content"]]
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert blocks[1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in blocks[2]


def test_session_result_provenance_marks_generated_test_sessions() -> None:
    provenance = SessionProvenance(
        identity=_spec().identity.__class__(
            run_id=_spec().identity.run_id,
            phase=_spec().identity.phase,
            session_kind=SessionKind.TestGeneration,
            slice_ref=_spec().identity.slice_ref,
        )
    )
    assert provenance.generated is True

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import uuid4

import pytest

from codemigrator.core import (
    ContextPack,
    ContextPackIdentity,
    ModelProfile,
    Phase,
    RunStatus,
    SessionBudgetProfile,
    SessionKind,
    SliceGenerationRef,
)
from codemigrator.runtime.binding import BindingError, ContextOverflowError, LockedModelBinding
from codemigrator.runtime.context import ContextEnvelope, ContextSegment
from codemigrator.runtime.loop import (
    AgentLoop,
    CheckpointDecision,
)
from codemigrator.runtime.loop_contracts import (
    SessionExit,
    SessionIdentity,
    SessionSpec,
    SessionState,
)
from codemigrator.runtime.memory import ContextManager, FormulaNetInputCap
from codemigrator.runtime.provider import ProviderRequest, ProviderResponse, TokenUsage


def _binding(profile: ModelProfile) -> LockedModelBinding:
    return LockedModelBinding(
        provider_id="openai",
        model_id="test-model",
        profile=profile,
        config_revision="r1",
        context_window=1000,
        output_cap=200,
    )


def _spec(*, execute: bool = True, generation: int = 0) -> SessionSpec:
    run_id = uuid4()
    phase = Phase.Execute if execute else Phase.Plan
    session = SessionKind.Implementation if execute else SessionKind.PlanAuxiliary
    binding = _binding(ModelProfile.Code if execute else ModelProfile.Reasoning)
    slice_ref = (
        SliceGenerationRef(slice_id=uuid4(), generation=generation, baseline_candidate_oid="a" * 40)
        if execute
        else None
    )
    return SessionSpec(
        identity=SessionIdentity(run_id, phase, session, slice_ref),
        run_status=RunStatus.Executing if execute else RunStatus.Planning,
        binding=binding,
        context_pack=ContextPack(
            identity=ContextPackIdentity(
                run_id=run_id,
                phase=phase,
                session=session,
                slice=slice_ref,
                spec_sha256="1" * 64,
                model_binding_sha256=binding.digest,
                phase_policy_sha256="3" * 64,
                contract_refs_sha256="4" * 64,
            ),
            budget=SessionBudgetProfile(session=session, max_rounds=3, eviction_watermark_pct=80),
            assembled_tokens=10,
        ),
        context=ContextEnvelope(
            stable=(ContextSegment("stable", "facts"),),
            targeted=(ContextSegment("targeted", "source text"),),
        ),
        template="implementation role",
    )


def _response(content: str, *, finish_reason: str = "tool_calls") -> ProviderResponse:
    return ProviderResponse(
        content=content,
        tool_calls=(),
        finish_reason=finish_reason,
        usage=TokenUsage(input_tokens=2, output_tokens=1),
    )


class ExactContextCounter:
    def count(self, messages):
        return sum(len(message.content) for message in messages)


class FakeProvider:
    def __init__(self, responses: list[ProviderResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[ProviderRequest] = []
        self.tasks: list[asyncio.Task[object] | None] = []

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        self.tasks.append(asyncio.current_task())
        return self.responses.pop(0)


class FakeGateway:
    def __init__(self, results: list[object] | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.results = list(results or [])

    def dispatch(
        self,
        raw_call: dict[str, object],
        *,
        cancellation_token: object | None = None,
    ) -> object:
        if cancellation_token is not None and getattr(cancellation_token, "cancelled", False):
            raise RuntimeError("cancelled")
        self.calls.append(raw_call)
        return self.results.pop(0) if self.results else {"ok": True}


class FakeGate:
    def __init__(self) -> None:
        self.valid = True
        self.cancelled = False

    async def allow(self, _: SessionIdentity) -> bool:
        return self.valid and not self.cancelled


@dataclass
class FakeCheckpoint:
    decisions: list[CheckpointDecision]
    calls: int = 0

    async def precheck(self, *_: object) -> CheckpointDecision:
        self.calls += 1
        return self.decisions.pop(0)


class FakeUsageSink:
    def __init__(self) -> None:
        self.values: list[TokenUsage] = []

    async def record(self, *_: object) -> None:
        self.values.append(_[1])


@pytest.mark.asyncio
async def test_loop_runs_in_one_session_task_and_closes_after_checkpoint() -> None:
    provider = FakeProvider(
        [
            _response('{"tool":"ReadFile","path":"a.py"}'),
            _response('{"completed":true}'),
        ]
    )
    gateway = FakeGateway()
    checkpoint = FakeCheckpoint([CheckpointDecision(accepted=True, committed=True)])
    usage = FakeUsageSink()

    result = await AgentLoop(
        provider=provider,
        gateway=gateway,
        checkpoint=checkpoint,
        usage_sink=usage,
    ).run(_spec())

    assert result.state is SessionState.Closed
    assert result.exit.value == "COMPLETED"
    assert [call["path"] for call in gateway.calls] == ["a.py"]
    assert checkpoint.calls == 1
    assert usage.values == [TokenUsage(input_tokens=2, output_tokens=1)] * 2
    assert provider.tasks[0] is not asyncio.current_task()
    assert provider.tasks[0] is provider.tasks[1]


@pytest.mark.asyncio
async def test_loop_uses_injected_exact_context_manager_for_each_request() -> None:
    provider = FakeProvider([_response('{"completed":true}')])
    manager = ContextManager(
        token_counter=ExactContextCounter(), net_input_cap=FormulaNetInputCap()
    )

    result = await AgentLoop(
        provider=provider,
        gateway=FakeGateway(),
        context_manager=manager,
    ).run(_spec())

    assert result.exit is SessionExit.Completed
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_one_failed_action_does_not_swallow_later_action() -> None:
    provider = FakeProvider(
        [
            _response(
                "\n".join(
                    (
                        "[cm:action]",
                        '{"tool":"ReadFile","path":"bad.py"}',
                        "[cm:/action]",
                        "[cm:action]",
                        '{"tool":"ReadFile","path":"good.py"}',
                        "[cm:/action]",
                    )
                )
            ),
            _response('{"completed":true}'),
        ]
    )
    gateway = FakeGateway([RuntimeError("tool failure"), {"ok": True}])
    checkpoint = FakeCheckpoint([CheckpointDecision(accepted=True, committed=True)])

    result = await AgentLoop(provider=provider, gateway=gateway, checkpoint=checkpoint).run(_spec())

    assert result.state is SessionState.Closed
    assert [call["path"] for call in gateway.calls] == ["bad.py", "good.py"]
    assert len(result.observations) == 2


@pytest.mark.asyncio
async def test_checkpoint_rejection_is_data_and_allows_one_self_correction() -> None:
    provider = FakeProvider([_response('{"completed":true}'), _response('{"completed":true}')])
    gateway = FakeGateway()
    checkpoint = FakeCheckpoint(
        [
            CheckpointDecision(accepted=False, rejection_reasons=("scope",)),
            CheckpointDecision(accepted=True, committed=True),
        ]
    )

    result = await AgentLoop(provider=provider, gateway=gateway, checkpoint=checkpoint).run(_spec())

    assert result.state is SessionState.Closed
    assert checkpoint.calls == 2
    assert "scope" in provider.requests[1].messages[-1].content


@pytest.mark.asyncio
async def test_cancellation_or_identity_invalidation_publishes_no_outcome() -> None:
    provider = FakeProvider([_response('{"tool":"ReadFile","path":"a.py"}')])
    gateway = FakeGateway()
    gate = FakeGate()

    original_complete = provider.complete

    async def complete(request: ProviderRequest) -> ProviderResponse:
        response = await original_complete(request)
        gate.valid = False
        return response

    provider.complete = complete  # type: ignore[method-assign]
    result = await AgentLoop(provider=provider, gateway=gateway, cancellation=gate).run(_spec())

    assert result.state is SessionState.Invalidated
    assert result.outcome_published is False
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_plan_uses_admission_and_verify_like_specs_are_rejected_before_provider() -> None:
    provider = FakeProvider([_response('{"completed":true}')])
    with pytest.raises(BindingError):
        await AgentLoop(provider=provider, gateway=FakeGateway()).run(
            _spec(execute=False).__class__(
                identity=SessionIdentity(
                    _spec(execute=False).identity.run_id,
                    Phase.Verify,
                    SessionKind.Implementation,
                    None,
                ),
                run_status=RunStatus.Verifying,
                binding=_binding(ModelProfile.Code),
                context_pack=_spec(execute=False).context_pack,
                context=_spec(execute=False).context,
                template="role",
            )
        )
    assert provider.requests == []


@pytest.mark.asyncio
async def test_max_rounds_returns_segment_stopped_for_actor_continuation() -> None:
    spec = _spec()
    spec = spec.__class__(
        identity=spec.identity,
        run_status=spec.run_status,
        binding=spec.binding,
        context_pack=spec.context_pack.model_copy(
            update={"budget": spec.context_pack.budget.model_copy(update={"max_rounds": 1})}
        ),
        context=spec.context,
        template=spec.template,
    )
    provider = FakeProvider([_response("not complete", finish_reason="stop")])

    result = await AgentLoop(provider=provider, gateway=FakeGateway()).run(spec)

    assert result.exit.value == "SEGMENT_STOPPED"
    assert result.state is SessionState.Closed


@pytest.mark.asyncio
async def test_physical_context_overflow_rejects_before_first_provider_call() -> None:
    spec = _spec()
    binding = LockedModelBinding(
        provider_id=spec.binding.provider_id,
        model_id=spec.binding.model_id,
        profile=spec.binding.profile,
        config_revision=spec.binding.config_revision,
        context_window=30,
        output_cap=20,
    )
    spec = spec.__class__(
        identity=spec.identity,
        run_status=spec.run_status,
        binding=binding,
        context_pack=spec.context_pack.model_copy(
            update={
                "identity": spec.context_pack.identity.model_copy(
                    update={"model_binding_sha256": binding.digest}
                )
            }
        ),
        context=ContextEnvelope(targeted=(ContextSegment("targeted", "x" * 100),)),
        template=spec.template,
    )
    provider = FakeProvider([])

    with pytest.raises(ContextOverflowError):
        await AgentLoop(provider=provider, gateway=FakeGateway()).run(spec)
    assert provider.requests == []

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

import pytest

from codemigrator.core import (
    AdviceKind,
    AttributionReliability,
    Phase,
    RepairEvidence,
    RunStatus,
)
from codemigrator.runtime.provider import (
    ProviderRequest,
    ProviderResponse,
    ProviderToolCall,
    TokenUsage,
)
from codemigrator.runtime.supervisor import (
    SupervisorAdviceKind,
    SupervisorProjection,
    SupervisorTrigger,
)
from codemigrator.runtime.supervisor_session import SupervisorSession

from .conftest import make_spec


def _projection() -> SupervisorProjection:
    return SupervisorProjection(
        repair_evidence=RepairEvidence(
            candidate_slice_set=[uuid4(), uuid4()],
            reliability=AttributionReliability.Reliable,
            strong_coupling=False,
            cross_generation_recurrence=False,
            conservation_signal_summary={"candidate_count": 2},
        ),
        failed_test_refs=("test::one",),
        diagnostic_summary={"error_count": 1},
        slice_states={str(uuid4()): "FAILED"},
        prior_repair_decision_refs=(),
    )


def _repair_projection() -> tuple[SupervisorProjection, tuple[object, object]]:
    candidates = (uuid4(), uuid4())
    return (
        SupervisorProjection(
            repair_evidence=RepairEvidence(
                candidate_slice_set=list(candidates),
                reliability=AttributionReliability.Reliable,
                strong_coupling=False,
                cross_generation_recurrence=False,
                conservation_signal_summary={"candidate_count": 2},
            ),
            failed_test_refs=("test::one",),
            diagnostic_summary={"error_count": 2},
            slice_states={str(candidates[0]): "FAILED", str(candidates[1]): "FAILED"},
            prior_repair_decision_refs=(),
        ),
        candidates,
    )


def _response(content: str, *, tool_calls=()) -> ProviderResponse:
    return ProviderResponse(
        content=content,
        tool_calls=tool_calls,
        finish_reason="stop",
        usage=TokenUsage(input_tokens=2, output_tokens=3),
    )


class FakeProvider:
    def __init__(self, response: ProviderResponse | Exception) -> None:
        self.response = response
        self.requests: list[ProviderRequest] = []

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@dataclass
class AdviceSink:
    values: list[object]

    async def publish(self, advice: object) -> None:
        self.values.append(advice)


@dataclass
class EventSink:
    values: list[object]

    async def append(self, event: object) -> None:
        self.values.append(event)


class ClosedBudget:
    async def admit(self, _identity: object) -> bool:
        return False


@pytest.mark.asyncio
async def test_session_uses_one_execute_supervisor_call_with_no_tools() -> None:
    provider = FakeProvider(
        _response(
            '{"trigger_event_refs":["event-1"],"failure_class":"TEST_FAILURE",'
            '"suggested_route":"clarify","target_slice_id":null,"rationale":"need input"}'
        )
    )
    advice_sink = AdviceSink([])
    event_sink = EventSink([])
    spec = make_spec()
    trigger = SupervisorTrigger(
        SupervisorAdviceKind.RouteSuggestion,
        "SLICE_SESSION_STOPPED",
        trigger_event_refs=("event-1",),
    )

    result = await SupervisorSession(
        provider=provider, advice_sink=advice_sink, event_sink=event_sink
    ).run(spec, trigger, _projection())

    assert result.advice is not None
    assert result.advice.kind is AdviceKind.RouteSuggestion
    assert len(provider.requests) == 1
    assert provider.requests[0].tools == ()
    assert advice_sink.values == [result.advice]
    assert [event.event_type for event in event_sink.values] == ["advice.proposed"]


@pytest.mark.asyncio
async def test_provider_failure_and_budget_closure_mechanically_reduce_without_advice() -> None:
    spec = make_spec()
    trigger = SupervisorTrigger(SupervisorAdviceKind.RouteSuggestion, "SLICE_SESSION_STOPPED")
    provider = FakeProvider(RuntimeError("provider unavailable"))
    sink = AdviceSink([])
    failed = await SupervisorSession(provider=provider, advice_sink=sink).run(
        spec, trigger, _projection()
    )
    assert failed.advice is None
    assert failed.fallback == "MECHANICAL_REDUCTION"
    assert sink.values == []

    closed_provider = FakeProvider(_response("{}"))
    closed = await SupervisorSession(
        provider=closed_provider, advice_sink=sink, budget=ClosedBudget()
    ).run(spec, trigger, _projection())
    assert closed.advice is None
    assert closed.fallback == "MECHANICAL_REDUCTION"
    assert closed_provider.requests == []


@pytest.mark.asyncio
async def test_repair_decision_is_core_validated_and_emits_two_redacted_events() -> None:
    projection, candidates = _repair_projection()
    content = (
        '{"repair_set":["'
        + str(candidates[0])
        + '","'
        + str(candidates[1])
        + '"],"domain_split":{"'
        + str(candidates[0])
        + '":["src/a.py"],"'
        + str(candidates[1])
        + '":["src/b.py"]},"brief_refs":[{"sha256":"'
        + "a" * 64
        + '","size":3,"media_type":"text/plain"}]}'
    )
    provider = FakeProvider(_response(content))
    trigger = SupervisorTrigger(
        SupervisorAdviceKind.RepairDecision,
        "AMBIGUOUS_ATTRIBUTION_CANDIDATES",
        trigger_event_refs=("event-1",),
    )
    event_sink = EventSink([])

    result = await SupervisorSession(provider=provider, event_sink=event_sink).run(
        make_spec(), trigger, projection
    )

    assert result.advice is not None
    assert result.advice.kind is AdviceKind.RepairDecision
    assert result.advice.payload["repair_set"] == [str(candidates[0]), str(candidates[1])]
    assert [event.event_type for event in event_sink.values] == [
        "advice.proposed",
        "repair.decision",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        '{"trigger_event_refs":["event-1"],"failure_class":"X",'
        '"suggested_route":"clarify","target_slice_id":null,"rationale":"a",'
        '"rationale":"b"}',
        '{"trigger_event_refs":["event-1"],"failure_class":"X",'
        '"suggested_route":"clarify","target_slice_id":null,"rationale":NaN}',
        '{"trigger_event_refs":["event-1"],"failure_class":"X",'
        '"suggested_route":"unknown","target_slice_id":null,"rationale":"a"}',
        '{"trigger_event_refs":["event-1"],"failure_class":"X",'
        '"suggested_route":"clarify","target_slice_id":null,"rationale":"a",'
        '"write":true}',
    ],
)
async def test_strict_output_rejects_duplicate_nonfinite_unknown_and_write_fields(
    content: str,
) -> None:
    provider = FakeProvider(_response(content))
    result = await SupervisorSession(provider=provider).run(
        make_spec(),
        SupervisorTrigger(
            SupervisorAdviceKind.RouteSuggestion,
            "SLICE_SESSION_STOPPED",
            trigger_event_refs=("event-1",),
        ),
        _projection(),
    )
    assert result.advice is None
    assert result.failure


@pytest.mark.asyncio
async def test_natural_language_or_native_tool_output_is_rejected() -> None:
    spec = make_spec()
    trigger = SupervisorTrigger(SupervisorAdviceKind.RouteSuggestion, "SLICE_SESSION_STOPPED")
    provider = FakeProvider(_response("please retry"))
    result = await SupervisorSession(provider=provider).run(spec, trigger, _projection())
    assert result.advice is None
    assert result.fallback == "MECHANICAL_REDUCTION"

    native_provider = FakeProvider(
        _response(
            "{}",
            tool_calls=(ProviderToolCall("ReadFile", '{"path":"secret.py"}'),),
        )
    )
    native_result = await SupervisorSession(provider=native_provider).run(
        spec, trigger, _projection()
    )
    assert native_result.advice is None
    assert native_result.fallback == "MECHANICAL_REDUCTION"


@pytest.mark.asyncio
async def test_advice_delivery_failure_never_returns_delivered_advice() -> None:
    class FailingAdviceSink:
        async def publish(self, _advice: object) -> None:
            raise RuntimeError("actor mailbox unavailable")

    provider = FakeProvider(
        _response(
            '{"trigger_event_refs":["event-1"],"failure_class":"TEST_FAILURE",'
            '"suggested_route":"clarify","target_slice_id":null,"rationale":"need input"}'
        )
    )
    result = await SupervisorSession(provider=provider, advice_sink=FailingAdviceSink()).run(
        make_spec(),
        SupervisorTrigger(
            SupervisorAdviceKind.RouteSuggestion,
            "SLICE_SESSION_STOPPED",
            trigger_event_refs=("event-1",),
        ),
        _projection(),
    )
    assert result.advice is None
    assert result.fallback == "MECHANICAL_REDUCTION"
    assert result.failure is not None and result.failure.startswith("delivery_error")


@pytest.mark.asyncio
async def test_verify_identity_is_rejected_before_provider() -> None:
    provider = FakeProvider(_response("{}"))
    spec = make_spec()
    invalid_identity = spec.identity.__class__(
        spec.identity.run_id,
        Phase.Verify,
        spec.identity.session_kind,
        None,
    )
    invalid = spec.__class__(
        identity=invalid_identity,
        run_status=RunStatus.Verifying,
        binding=spec.binding,
        context_pack=spec.context_pack.model_copy(
            update={
                "identity": spec.context_pack.identity.model_copy(
                    update={"phase": Phase.Verify, "slice": None}
                )
            }
        ),
        context=spec.context,
        template=spec.template,
    )
    result = await SupervisorSession(provider=provider).run(
        invalid,
        SupervisorTrigger(SupervisorAdviceKind.RouteSuggestion, "SLICE_SESSION_STOPPED"),
        _projection(),
    )
    assert result.advice is None
    assert provider.requests == []

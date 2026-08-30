"""One-shot, no-tool Supervisor Advice sessions."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from codemigrator.core import (
    Advice,
    AdviceId,
    AdviceKind,
    ModelProfile,
    Phase,
    RepairDecision,
    ResidentRole,
    RunStatus,
    SessionKind,
    Sha256,
    new_uuid7,
)

from .advice import advice_proposal_hash
from .binding import ContextOverflowError, ensure_context_fits, validate_session_admission
from .context import prompt_text, render_prompt
from .contracts import EventSpec
from .loop import BudgetGate, CancellationGate, CancellationToken, SessionProvenance, UsageSink
from .loop_contracts import SessionSpec
from .provider import (
    AsyncProvider,
    ProviderCallIdentity,
    ProviderError,
    ProviderRequest,
    UsageReceipt,
)
from .supervisor import (
    RouteSuggestion,
    SuggestedRoute,
    SupervisorAdviceKind,
    SupervisorProjection,
    SupervisorTrigger,
    build_proposed_event,
    build_repair_decision_event,
)


class AdviceSink(Protocol):
    async def publish(self, advice: Advice) -> None: ...


class SupervisorEventSink(Protocol):
    async def append(self, event: EventSpec) -> None: ...


@dataclass(frozen=True, slots=True)
class SupervisorResult:
    advice: Advice | None
    events: tuple[EventSpec, ...] = ()
    fallback: str | None = None
    failure: str | None = None


class _ProtocolError(ValueError):
    pass


def _duplicate_key_reject(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _ProtocolError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> object:
    raise _ProtocolError(f"non-finite JSON number: {value}")


def _strict_object(content: str) -> dict[str, object]:
    if not isinstance(content, str) or not content.strip():
        raise _ProtocolError("Supervisor output must be a JSON object")
    try:
        decoded = json.loads(
            content,
            object_pairs_hook=_duplicate_key_reject,
            parse_constant=_reject_non_finite,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _ProtocolError("Supervisor output is not valid strict JSON") from exc
    if type(decoded) is not dict:
        raise _ProtocolError("Supervisor output must be a JSON object")
    return decoded


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _ProtocolError(f"{name} must be non-empty text")
    return value


def _string_list(value: object, name: str, *, nonempty: bool = True) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise _ProtocolError(f"{name} must be a string list")
    result = list(value)
    if nonempty and not result:
        raise _ProtocolError(f"{name} must not be empty")
    if any(not isinstance(item, str) or not item for item in result):
        raise _ProtocolError(f"{name} must contain non-empty strings")
    return result


def _parse_route(data: Mapping[str, object], trigger: SupervisorTrigger) -> RouteSuggestion:
    expected = {
        "trigger_event_refs",
        "failure_class",
        "suggested_route",
        "target_slice_id",
        "rationale",
    }
    if set(data) != expected:
        raise _ProtocolError("RouteSuggestion fields are not exact")
    refs = tuple(_string_list(data["trigger_event_refs"], "trigger_event_refs"))
    if trigger.trigger_event_refs and refs != trigger.trigger_event_refs:
        raise _ProtocolError("RouteSuggestion event refs do not match trigger")
    target = data["target_slice_id"]
    if target is not None and not isinstance(target, str):
        raise _ProtocolError("target_slice_id must be null or UUID text")
    try:
        return RouteSuggestion(
            trigger_event_refs=refs,
            failure_class=_required_text(data["failure_class"], "failure_class"),
            suggested_route=SuggestedRoute(data["suggested_route"]),
            target_slice_id=target,
            rationale=_required_text(data["rationale"], "rationale"),
        )
    except (TypeError, ValueError) as exc:
        raise _ProtocolError("RouteSuggestion schema is invalid") from exc


def _parse_repair(
    data: Mapping[str, object], spec: SessionSpec, projection: SupervisorProjection
) -> RepairDecision:
    expected = {"repair_set", "domain_split", "brief_refs"}
    if set(data) != expected:
        raise _ProtocolError("RepairDecision fields are not exact")
    repair_set = _string_list(data["repair_set"], "repair_set")
    if len(repair_set) != len(set(repair_set)):
        raise _ProtocolError("repair_set must not contain duplicates")
    evidence = projection.repair_evidence
    if evidence is None:
        raise _ProtocolError("RepairDecision requires RepairEvidence")
    candidate_ids = {str(item) for item in evidence.candidate_slice_set}
    if not set(repair_set).issubset(candidate_ids):
        raise _ProtocolError("repair_set is outside attribution candidates")
    domain_split = data["domain_split"]
    brief_refs = data["brief_refs"]
    if not isinstance(domain_split, dict):
        raise _ProtocolError("domain_split must be an object")
    if isinstance(brief_refs, (str, bytes)) or not isinstance(brief_refs, list):
        raise _ProtocolError("brief_refs must be an array")
    try:
        return RepairDecision.model_validate(
            {
                "decision_id": str(new_uuid7()),
                "run_id": str(spec.identity.run_id),
                "repair_set": repair_set,
                "domain_split": domain_split,
                "brief_refs": brief_refs,
            }
        )
    except (TypeError, ValueError) as exc:
        raise _ProtocolError("RepairDecision schema is invalid") from exc


def _advice(spec: SessionSpec, kind: AdviceKind, payload: dict[str, object]) -> Advice:
    advice = Advice(
        advice_id=AdviceId(new_uuid7()),
        kind=kind,
        run_id=spec.identity.run_id,
        role=ResidentRole.ExecuteSupervisor,
        payload=payload,
        proposal_hash=Sha256("0" * 64),
    )
    return advice.model_copy(update={"proposal_hash": advice_proposal_hash(advice)})


def _parse_advice(
    content: str,
    spec: SessionSpec,
    trigger: SupervisorTrigger,
    projection: SupervisorProjection,
) -> Advice:
    data = _strict_object(content)
    if trigger.kind is SupervisorAdviceKind.RouteSuggestion:
        return _advice(
            spec,
            AdviceKind.RouteSuggestion,
            _parse_route(data, trigger).to_payload(),
        )
    decision = _parse_repair(data, spec, projection)
    return _advice(spec, AdviceKind.RepairDecision, decision.model_dump(mode="json"))


class SupervisorSession:
    """Execute one model turn and hand Advice back to the actor boundary."""

    def __init__(
        self,
        *,
        provider: AsyncProvider,
        advice_sink: AdviceSink | None = None,
        event_sink: SupervisorEventSink | None = None,
        cancellation: CancellationGate | None = None,
        budget: BudgetGate | None = None,
        usage_sink: UsageSink | None = None,
    ) -> None:
        self.provider = provider
        self.advice_sink = advice_sink
        self.event_sink = event_sink
        self.cancellation = cancellation
        self.budget = budget
        self.usage_sink = usage_sink

    async def run(
        self,
        spec: SessionSpec,
        trigger: SupervisorTrigger,
        projection: SupervisorProjection,
    ) -> SupervisorResult:
        try:
            validate_session_admission(spec)
            self._validate_supervisor_identity(spec)
            if self.cancellation is not None and not await self.cancellation.allow(spec.identity):
                return self._fallback("cancelled")
            if self.budget is not None and not await self.budget.admit(spec.identity):
                return self._fallback("budget_closed")
            messages = render_prompt(spec.template, projection.to_context())
            ensure_context_fits(prompt_text(messages), spec.binding)
        except ContextOverflowError:
            return self._fallback("context_rejected")
        except Exception as exc:
            return self._fallback("invalid_identity", failure=self._failure(exc))

        token = CancellationToken.create()
        call_id = f"{spec.identity.run_id}:supervisor"
        try:
            response = await self.provider.complete(
                ProviderRequest(
                    binding=spec.binding,
                    messages=messages,
                    tools=(),
                    cancellation=token,
                )
            )
        except ProviderError as exc:
            return self._fallback("cancelled" if exc.cancelled else "provider_error")
        except Exception:
            return self._fallback("provider_error")
        if token.cancelled or (
            self.cancellation is not None and not await self.cancellation.allow(spec.identity)
        ):
            return self._fallback("cancelled")
        if response.tool_calls:
            return self._fallback("protocol_error", failure="native tool calls are forbidden")

        receipt = UsageReceipt(
            run_id=spec.identity.run_id,
            call=ProviderCallIdentity(
                call_id=call_id,
                provider_receipt_id=response.provider_receipt_id or call_id,
                provider_id=spec.binding.provider_id,
                model_id=spec.binding.model_id,
                session_key=call_id,
            ),
            usage=response.usage,
            provenance=SessionProvenance(spec.identity),
        )
        try:
            if self.usage_sink is not None:
                await self.usage_sink.record(spec.identity.run_id, response.usage, receipt)
            if self.budget is not None and not await self.budget.record(receipt):
                return self._fallback("budget_closed")
            advice = _parse_advice(response.content, spec, trigger, projection)
        except _ProtocolError as exc:
            return self._fallback("protocol_error", failure=str(exc))
        except Exception as exc:
            return self._fallback("protocol_error", failure=self._failure(exc))

        events = [build_proposed_event(advice)]
        if advice.kind is AdviceKind.RepairDecision:
            events.append(build_repair_decision_event(advice))
        try:
            if self.event_sink is not None:
                for event in events:
                    await self.event_sink.append(event)
            if self.advice_sink is not None:
                await self.advice_sink.publish(advice)
        except Exception as exc:
            return SupervisorResult(
                advice=None,
                events=tuple(events),
                fallback="MECHANICAL_REDUCTION",
                failure=f"delivery_error: {self._failure(exc)}",
            )
        return SupervisorResult(advice=advice, events=tuple(events))

    @staticmethod
    def _validate_supervisor_identity(spec: SessionSpec) -> None:
        identity = spec.identity
        if identity.phase is not Phase.Execute:
            raise ValueError("Supervisor requires EXECUTE phase")
        if spec.run_status is not RunStatus.Executing:
            raise ValueError("Supervisor requires EXECUTING run")
        if spec.binding.profile is not ModelProfile.Code:
            raise ValueError("Supervisor requires CODE model profile")
        if identity.session_kind is not SessionKind.ExecuteSupervisor:
            raise ValueError("Supervisor requires ExecuteSupervisor session kind")

    @staticmethod
    def _failure(exc: Exception) -> str:
        message = str(exc).strip().replace("\n", " ")
        return message[:160] or exc.__class__.__name__

    @staticmethod
    def _fallback(reason: str, *, failure: str | None = None) -> SupervisorResult:
        return SupervisorResult(
            advice=None,
            fallback="MECHANICAL_REDUCTION",
            failure=failure or reason,
        )


__all__ = [
    "AdviceSink",
    "SupervisorEventSink",
    "SupervisorResult",
    "SupervisorSession",
]

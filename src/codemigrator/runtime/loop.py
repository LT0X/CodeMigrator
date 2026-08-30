"""One-task Agent Loop execution with explicit handoff boundaries."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Protocol, cast

from codemigrator.core import SessionKind

from .binding import ContextOverflowError, ensure_context_fits, validate_session_admission
from .context import PromptMessage, prompt_text, render_prompt
from .loop_contracts import SessionExit, SessionIdentity, SessionSpec, SessionState
from .memory import ContextBudgetError, ContextManager
from .normalizer import NormalizationError, normalize_response
from .provider import (
    AsyncProvider,
    ProviderCallIdentity,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
    TokenUsage,
    UsageReceipt,
)


class SessionCancelled(RuntimeError):
    """The session lost its cancellation or candidate identity gate."""


class ToolGatewayPort(Protocol):
    def dispatch(
        self,
        raw_call: Mapping[str, object],
        *,
        cancellation_token: CancellationToken | None = None,
    ) -> object | Awaitable[object]: ...


class CancellationGate(Protocol):
    async def allow(self, identity: SessionIdentity) -> bool: ...


class BudgetGate(Protocol):
    """Actor-owned atomic wallet admission and receipt recording."""

    async def admit(self, identity: SessionIdentity) -> bool: ...

    async def record(self, receipt: UsageReceipt) -> bool: ...


@dataclass(slots=True)
class CancellationToken:
    """Cancellation signal forwarded to provider and cancellation-aware gateways."""

    _event: asyncio.Event

    @classmethod
    def create(cls) -> CancellationToken:
        return cls(asyncio.Event())

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    async def wait(self) -> None:
        await self._event.wait()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise SessionCancelled("session cancellation was requested")


@dataclass(frozen=True, slots=True)
class CheckpointDecision:
    accepted: bool
    committed: bool = False
    rejection_reasons: tuple[str, ...] = ()


class CheckpointPort(Protocol):
    async def precheck(
        self, spec: SessionSpec, observations: tuple[ToolObservation, ...]
    ) -> CheckpointDecision: ...


class UsageSink(Protocol):
    async def record(self, run_id: object, usage: TokenUsage, receipt: UsageReceipt) -> None: ...


@dataclass(frozen=True, slots=True)
class SessionProvenance:
    identity: SessionIdentity

    @property
    def generated(self) -> bool:
        return self.identity.session_kind is SessionKind.TestGeneration


@dataclass(frozen=True, slots=True)
class ToolObservation:
    round_index: int
    segment_index: int
    action: Mapping[str, object]
    result: object
    provenance: SessionProvenance | None = None


@dataclass(frozen=True, slots=True)
class SessionResult:
    state: SessionState
    exit: SessionExit
    observations: tuple[ToolObservation, ...] = ()
    assistant_texts: tuple[str, ...] = ()
    usages: tuple[TokenUsage, ...] = ()
    outcome_published: bool = False
    rounds: int = 0
    failure: str | None = None
    provenance: SessionProvenance | None = None

    @property
    def generated(self) -> bool:
        return self.provenance.generated if self.provenance is not None else False


class AgentLoop:
    """Run one frozen session and hand all decisions back to the actor."""

    def __init__(
        self,
        *,
        provider: AsyncProvider,
        gateway: ToolGatewayPort,
        checkpoint: CheckpointPort | None = None,
        cancellation: CancellationGate | None = None,
        budget: BudgetGate | None = None,
        usage_sink: UsageSink | None = None,
        max_self_corrections: int = 3,
        context_manager: ContextManager | None = None,
        context_tool_schema_tokens: int = 0,
        context_envelope_margin: int = 0,
    ) -> None:
        if max_self_corrections < 0:
            raise ValueError("max self corrections must not be negative")
        self.provider = provider
        self.gateway = gateway
        self.checkpoint = checkpoint
        self.cancellation = cancellation
        self.budget = budget
        self.usage_sink = usage_sink
        self.max_self_corrections = max_self_corrections
        if context_tool_schema_tokens < 0 or context_envelope_margin < 0:
            raise ValueError("context measurement margins must not be negative")
        self.context_manager = context_manager
        self.context_tool_schema_tokens = context_tool_schema_tokens
        self.context_envelope_margin = context_envelope_margin

    async def run(self, spec: SessionSpec) -> SessionResult:
        """Execute the session in its own task, separate from the caller/actor."""

        task = asyncio.create_task(
            self._run(spec), name=f"codemigrator-loop-{spec.identity.run_id}"
        )
        return await task

    async def _run(self, spec: SessionSpec) -> SessionResult:
        validate_session_admission(spec)
        provenance = SessionProvenance(spec.identity)
        messages = list(render_prompt(spec.template, spec.context))
        self._ensure_context_fits(messages, spec)
        observations: list[ToolObservation] = []
        assistant_texts: list[str] = []
        usages: list[TokenUsage] = []
        self_corrections = 0
        for round_index in range(1, spec.context_pack.budget.max_rounds + 1):
            if not await self._allowed(spec.identity):
                return self._invalidated(
                    observations, assistant_texts, usages, round_index - 1, provenance
                )
            if self.budget is not None and not await self.budget.admit(spec.identity):
                return self._budget_exhausted(
                    observations, assistant_texts, usages, round_index - 1, provenance
                )
            try:
                self._ensure_context_fits(messages, spec)
            except (ContextOverflowError, ContextBudgetError):
                return self._failed(
                    observations,
                    assistant_texts,
                    usages,
                    round_index,
                    "CONTEXT_OVERFLOW",
                    provenance,
                )
            token = CancellationToken.create()
            call_id = f"{spec.identity.run_id}:{round_index}"
            try:
                response = await self._provider_call(
                    ProviderRequest(
                        binding=spec.binding,
                        messages=tuple(messages),
                        cancellation=token,
                    ),
                    spec.identity,
                    token,
                )
            except SessionCancelled:
                return self._invalidated(
                    observations, assistant_texts, usages, round_index, provenance
                )
            except ProviderError as exc:
                if exc.cancelled:
                    return self._invalidated(
                        observations, assistant_texts, usages, round_index, provenance
                    )
                return self._failed(
                    observations,
                    assistant_texts,
                    usages,
                    round_index,
                    "RETRYABLE_PROVIDER_ERROR" if exc.retryable else "PROVIDER_ERROR",
                    provenance,
                )
            except Exception:
                return self._failed(
                    observations,
                    assistant_texts,
                    usages,
                    round_index,
                    "PROVIDER_ERROR",
                    provenance,
                )
            if token.cancelled or not await self._allowed(spec.identity):
                return self._invalidated(
                    observations, assistant_texts, usages, round_index, provenance
                )
            receipt = UsageReceipt(
                run_id=spec.identity.run_id,
                call=ProviderCallIdentity(
                    call_id=call_id,
                    provider_receipt_id=response.provider_receipt_id or call_id,
                    provider_id=spec.binding.provider_id,
                    model_id=spec.binding.model_id,
                    session_key=self._session_key(spec.identity),
                ),
                usage=response.usage,
                provenance=provenance,
            )
            usages.append(response.usage)
            if self.usage_sink is not None:
                await self.usage_sink.record(spec.identity.run_id, response.usage, receipt)
            if self.budget is not None and not await self.budget.record(receipt):
                return self._budget_exhausted(
                    observations, assistant_texts, usages, round_index, provenance
                )
            try:
                turn = normalize_response(response)
            except NormalizationError:
                return self._failed(
                    observations,
                    assistant_texts,
                    usages,
                    round_index,
                    "NORMALIZATION_ERROR",
                    provenance,
                )
            if response.content or response.tool_calls:
                messages.append(
                    PromptMessage(
                        role="assistant",
                        content=response.content,
                        native_tool_calls=tuple(
                            (
                                call.call_id or f"{call_id}:{index}",
                                call.name,
                                call.arguments,
                            )
                            for index, call in enumerate(response.tool_calls)
                        ),
                    )
                )
                if response.content:
                    assistant_texts.append(response.content)
            if token.cancelled or not await self._allowed(spec.identity):
                return self._invalidated(
                    observations, assistant_texts, usages, round_index, provenance
                )
            for segment_index, action in enumerate(turn.actions):
                if token.cancelled or not await self._allowed(spec.identity):
                    return self._invalidated(
                        observations, assistant_texts, usages, round_index, provenance
                    )
                try:
                    result = await self._dispatch(action.payload, spec.identity, token)
                except SessionCancelled:
                    return self._invalidated(
                        observations, assistant_texts, usages, round_index, provenance
                    )
                except Exception as exc:
                    result = exc
                if token.cancelled or not await self._allowed(spec.identity):
                    return self._invalidated(
                        observations, assistant_texts, usages, round_index, provenance
                    )
                observations.append(
                    ToolObservation(
                        round_index=round_index,
                        segment_index=segment_index,
                        action=action.payload,
                        result=result,
                        provenance=provenance,
                    )
                )
                messages.append(
                    self._observation_message(
                        segment_index,
                        action.call_id,
                        action.payload,
                        result,
                    )
                )
            if turn.declared_complete:
                if token.cancelled or not await self._allowed(spec.identity):
                    return self._invalidated(
                        observations, assistant_texts, usages, round_index, provenance
                    )
                pending_spec = replace(spec, state=SessionState.CheckpointPending)
                decision = CheckpointDecision(accepted=True, committed=True)
                if self.checkpoint is not None:
                    try:
                        decision = await self.checkpoint.precheck(pending_spec, tuple(observations))
                    except Exception:
                        return self._failed(
                            observations,
                            assistant_texts,
                            usages,
                            round_index,
                            "CHECKPOINT_ERROR",
                            provenance,
                        )
                if decision.accepted and decision.committed:
                    return self._result(
                        SessionState.Closed,
                        SessionExit.Completed,
                        observations,
                        assistant_texts,
                        usages,
                        round_index,
                        provenance,
                        outcome_published=True,
                    )
                if decision.accepted:
                    return self._result(
                        SessionState.CheckpointPending,
                        SessionExit.Completed,
                        observations,
                        assistant_texts,
                        usages,
                        round_index,
                        provenance,
                    )
                self_corrections += 1
                if self_corrections > self.max_self_corrections:
                    return self._failed(
                        observations,
                        assistant_texts,
                        usages,
                        round_index,
                        "CHECKPOINT_REJECTED_TOO_MANY_TIMES",
                        provenance,
                    )
                reasons = ", ".join(decision.rejection_reasons) or "checkpoint rejected"
                messages.append(
                    PromptMessage(role="user", content=f"[checkpoint.rejection]\n{reasons}")
                )
                continue
            if not turn.actions and response.finish_reason in {"stop", "end_turn"}:
                return self._result(
                    SessionState.Closed,
                    SessionExit.SegmentStopped,
                    observations,
                    assistant_texts,
                    usages,
                    round_index,
                    provenance,
                )
        return self._result(
            SessionState.Closed,
            SessionExit.SegmentStopped,
            observations,
            assistant_texts,
            usages,
            spec.context_pack.budget.max_rounds,
            provenance,
        )

    def _ensure_context_fits(
        self, messages: list[PromptMessage], spec: SessionSpec
    ) -> int:
        if self.context_manager is not None:
            return self.context_manager.fit_messages(
                messages,
                context_window=spec.binding.context_window,
                reserved_output=spec.binding.output_cap,
                tool_schema_tokens=self.context_tool_schema_tokens,
                envelope_margin=self.context_envelope_margin,
            )
        if spec.context_pack.identity.template_sha256 != "0" * 64:
            raise ContextBudgetError(
                "a frozen M-14 context pack requires the unified ContextManager",
                code="CONTEXT_CAPABILITY_INVALID",
            )
        return ensure_context_fits(prompt_text(messages), spec.binding)

    async def _provider_call(
        self,
        request: ProviderRequest,
        identity: SessionIdentity,
        token: CancellationToken,
    ) -> ProviderResponse:
        watcher = self._watch_cancellation(identity, token)
        watch_task = asyncio.create_task(watcher)
        try:
            response = await self.provider.complete(request)
        finally:
            watch_task.cancel()
            await asyncio.gather(watch_task, return_exceptions=True)
        token.raise_if_cancelled()
        if not await self._allowed(identity):
            token.cancel()
            await self._notify_cancel(identity)
            raise SessionCancelled("session identity is no longer valid")
        return response

    async def _dispatch(
        self,
        payload: Mapping[str, object],
        identity: SessionIdentity,
        token: CancellationToken,
    ) -> object:
        token.raise_if_cancelled()
        work = asyncio.create_task(asyncio.to_thread(self._dispatch_sync, payload, token))
        watch_task = asyncio.create_task(self._watch_cancellation(identity, token))
        try:
            result = await work
        finally:
            watch_task.cancel()
            await asyncio.gather(watch_task, return_exceptions=True)
        token.raise_if_cancelled()
        if not await self._allowed(identity):
            token.cancel()
            await self._notify_cancel(identity)
            raise SessionCancelled("session identity is no longer valid")
        if inspect.isawaitable(result):
            result = await result
        return result

    def _dispatch_sync(
        self, payload: Mapping[str, object], token: CancellationToken
    ) -> object | Awaitable[object]:
        dispatch = cast(Callable[..., object | Awaitable[object]], self.gateway.dispatch)
        token.raise_if_cancelled()
        return dispatch(payload, cancellation_token=token)

    async def _watch_cancellation(
        self, identity: SessionIdentity, token: CancellationToken
    ) -> None:
        if self.cancellation is None:
            return
        while not token.cancelled:
            if not await self.cancellation.allow(identity):
                token.cancel()
                await self._notify_cancel(identity)
                return
            await asyncio.sleep(0.01)

    async def _notify_cancel(self, identity: SessionIdentity) -> None:
        if self.cancellation is None:
            return
        cancel = getattr(self.cancellation, "cancel", None)
        if cancel is None:
            return
        result = cancel(identity)
        if inspect.isawaitable(result):
            await result

    async def _allowed(self, identity: SessionIdentity) -> bool:
        return True if self.cancellation is None else await self.cancellation.allow(identity)

    @staticmethod
    def _session_key(identity: SessionIdentity) -> str:
        slice_key = "none" if identity.slice_ref is None else str(identity.slice_ref.slice_id)
        generation = "none" if identity.slice_ref is None else str(identity.slice_ref.generation)
        return (
            f"{identity.run_id}:{identity.phase.value}:{identity.session_kind.value}:"
            f"{slice_key}:{generation}"
        )

    @staticmethod
    def _observation_message(
        segment_index: int,
        call_id: str | None,
        action: Mapping[str, object],
        result: object,
    ) -> PromptMessage:
        if isinstance(result, BaseException):
            value = f"tool execution failed: {type(result).__name__}"
        else:
            value = str(result)
        if call_id is None:
            return PromptMessage(role="user", content=f"[tool.result:{segment_index}]\n{value}")
        return PromptMessage(
            role="tool",
            content=value,
            tool_call_id=call_id,
            tool_name=str(action.get("tool", "")),
        )

    @staticmethod
    def _result(
        state: SessionState,
        exit: SessionExit,
        observations: list[ToolObservation],
        assistant_texts: list[str],
        usages: list[TokenUsage],
        rounds: int,
        provenance: SessionProvenance,
        *,
        outcome_published: bool = False,
    ) -> SessionResult:
        return SessionResult(
            state=state,
            exit=exit,
            observations=tuple(observations),
            assistant_texts=tuple(assistant_texts),
            usages=tuple(usages),
            outcome_published=outcome_published,
            rounds=rounds,
            provenance=provenance,
        )

    @classmethod
    def _failed(
        cls,
        observations: list[ToolObservation],
        assistant_texts: list[str],
        usages: list[TokenUsage],
        rounds: int,
        failure: str,
        provenance: SessionProvenance,
    ) -> SessionResult:
        result = cls._result(
            SessionState.Failed,
            SessionExit.Failed,
            observations,
            assistant_texts,
            usages,
            rounds,
            provenance,
        )
        return replace(result, failure=failure)

    @classmethod
    def _budget_exhausted(
        cls,
        observations: list[ToolObservation],
        assistant_texts: list[str],
        usages: list[TokenUsage],
        rounds: int,
        provenance: SessionProvenance,
    ) -> SessionResult:
        result = cls._result(
            SessionState.Failed,
            SessionExit.BudgetExhausted,
            observations,
            assistant_texts,
            usages,
            rounds,
            provenance,
        )
        return replace(result, failure="BUDGET_EXHAUSTED")

    @staticmethod
    def _invalidated(
        observations: list[ToolObservation],
        assistant_texts: list[str],
        usages: list[TokenUsage],
        rounds: int,
        provenance: SessionProvenance,
    ) -> SessionResult:
        return AgentLoop._result(
            SessionState.Invalidated,
            SessionExit.Invalidated,
            observations,
            assistant_texts,
            usages,
            rounds,
            provenance,
        )


__all__ = [
    "AgentLoop",
    "BudgetGate",
    "CancellationGate",
    "CancellationToken",
    "CheckpointDecision",
    "CheckpointPort",
    "SessionCancelled",
    "SessionExit",
    "SessionProvenance",
    "SessionResult",
    "ToolGatewayPort",
    "ToolObservation",
    "UsageSink",
]

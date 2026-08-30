"""One-task Agent Loop execution with no ownership of Run state."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .binding import ensure_context_fits, validate_session_admission
from .context import PromptMessage, render_prompt
from .loop_contracts import SessionExit, SessionIdentity, SessionSpec, SessionState
from .normalizer import NormalizationError, normalize_response
from .provider import AsyncProvider, ProviderError, ProviderRequest, TokenUsage


class SessionCancelled(RuntimeError):
    """The session lost its cancellation or candidate identity gate."""


class ToolGatewayPort(Protocol):
    def dispatch(self, raw_call: Mapping[str, object]) -> object | Awaitable[object]: ...


class CancellationGate(Protocol):
    async def allow(self, identity: SessionIdentity) -> bool: ...


@dataclass(frozen=True, slots=True)
class CheckpointDecision:
    accepted: bool
    rejection_reasons: tuple[str, ...] = ()


class CheckpointPort(Protocol):
    async def precheck(
        self, spec: SessionSpec, observations: tuple[ToolObservation, ...]
    ) -> CheckpointDecision: ...


class UsageSink(Protocol):
    async def record(self, run_id: object, usage: TokenUsage) -> None: ...


@dataclass(frozen=True, slots=True)
class ToolObservation:
    round_index: int
    segment_index: int
    action: Mapping[str, object]
    result: object


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


class AgentLoop:
    """Run one frozen session and hand all decisions back to the actor."""

    def __init__(
        self,
        *,
        provider: AsyncProvider,
        gateway: ToolGatewayPort,
        checkpoint: CheckpointPort | None = None,
        cancellation: CancellationGate | None = None,
        usage_sink: UsageSink | None = None,
        max_self_corrections: int = 3,
    ) -> None:
        if max_self_corrections < 0:
            raise ValueError("max self corrections must not be negative")
        self.provider = provider
        self.gateway = gateway
        self.checkpoint = checkpoint
        self.cancellation = cancellation
        self.usage_sink = usage_sink
        self.max_self_corrections = max_self_corrections

    async def run(self, spec: SessionSpec) -> SessionResult:
        """Execute the session in its own task, separate from the caller/actor."""

        task = asyncio.create_task(
            self._run(spec), name=f"codemigrator-loop-{spec.identity.run_id}"
        )
        return await task

    async def _run(self, spec: SessionSpec) -> SessionResult:
        validate_session_admission(spec)
        messages = list(render_prompt(spec.template, spec.context))
        ensure_context_fits("\n\n".join(message.content for message in messages), spec.binding)
        observations: list[ToolObservation] = []
        assistant_texts: list[str] = []
        usages: list[TokenUsage] = []
        self_corrections = 0
        for round_index in range(1, spec.context_pack.budget.max_rounds + 1):
            if not await self._allowed(spec.identity):
                return self._invalidated(observations, assistant_texts, usages, round_index - 1)
            try:
                response = await self.provider.complete(
                    ProviderRequest(binding=spec.binding, messages=tuple(messages))
                )
            except ProviderError as exc:
                return SessionResult(
                    state=SessionState.Failed,
                    exit=SessionExit.Failed,
                    observations=tuple(observations),
                    assistant_texts=tuple(assistant_texts),
                    usages=tuple(usages),
                    rounds=round_index,
                    failure="RETRYABLE_PROVIDER_ERROR" if exc.retryable else "PROVIDER_ERROR",
                )
            except Exception:
                return SessionResult(
                    state=SessionState.Failed,
                    exit=SessionExit.Failed,
                    observations=tuple(observations),
                    assistant_texts=tuple(assistant_texts),
                    usages=tuple(usages),
                    rounds=round_index,
                    failure="PROVIDER_ERROR",
                )
            usages.append(response.usage)
            if self.usage_sink is not None:
                await self.usage_sink.record(spec.identity.run_id, response.usage)
            try:
                turn = normalize_response(response)
            except NormalizationError:
                return SessionResult(
                    state=SessionState.Failed,
                    exit=SessionExit.Failed,
                    observations=tuple(observations),
                    assistant_texts=tuple(assistant_texts),
                    usages=tuple(usages),
                    rounds=round_index,
                    failure="NORMALIZATION_ERROR",
                )
            if turn.assistant_text:
                assistant_texts.append(turn.assistant_text)
                messages.append(PromptMessage(role="assistant", content=turn.assistant_text))
            if not await self._allowed(spec.identity):
                return self._invalidated(observations, assistant_texts, usages, round_index)
            for segment_index, action in enumerate(turn.actions):
                if not await self._allowed(spec.identity):
                    return self._invalidated(observations, assistant_texts, usages, round_index)
                try:
                    result = await self._dispatch(action.payload)
                except Exception as exc:
                    result = exc
                observations.append(
                    ToolObservation(
                        round_index=round_index,
                        segment_index=segment_index,
                        action=action.payload,
                        result=result,
                    )
                )
                messages.append(
                    PromptMessage(
                        role="user",
                        content=self._observation_message(segment_index, result),
                    )
                )
            if turn.declared_complete:
                if not await self._allowed(spec.identity):
                    return self._invalidated(observations, assistant_texts, usages, round_index)
                decision = (
                    CheckpointDecision(accepted=True)
                    if self.checkpoint is None
                    else await self.checkpoint.precheck(spec, tuple(observations))
                )
                if decision.accepted:
                    return SessionResult(
                        state=SessionState.Closed,
                        exit=SessionExit.Completed,
                        observations=tuple(observations),
                        assistant_texts=tuple(assistant_texts),
                        usages=tuple(usages),
                        outcome_published=True,
                        rounds=round_index,
                    )
                self_corrections += 1
                if self_corrections > self.max_self_corrections:
                    return SessionResult(
                        state=SessionState.Failed,
                        exit=SessionExit.Failed,
                        observations=tuple(observations),
                        assistant_texts=tuple(assistant_texts),
                        usages=tuple(usages),
                        rounds=round_index,
                        failure="CHECKPOINT_REJECTED_TOO_MANY_TIMES",
                    )
                reasons = ", ".join(decision.rejection_reasons) or "checkpoint rejected"
                messages.append(
                    PromptMessage(role="user", content=f"[checkpoint.rejection]\n{reasons}")
                )
                continue
            if not turn.actions and response.finish_reason in {"stop", "end_turn"}:
                return SessionResult(
                    state=SessionState.Closed,
                    exit=SessionExit.SegmentStopped,
                    observations=tuple(observations),
                    assistant_texts=tuple(assistant_texts),
                    usages=tuple(usages),
                    rounds=round_index,
                )
        return SessionResult(
            state=SessionState.Closed,
            exit=SessionExit.SegmentStopped,
            observations=tuple(observations),
            assistant_texts=tuple(assistant_texts),
            usages=tuple(usages),
            rounds=spec.context_pack.budget.max_rounds,
        )

    async def _allowed(self, identity: SessionIdentity) -> bool:
        return True if self.cancellation is None else await self.cancellation.allow(identity)

    async def _dispatch(self, payload: Mapping[str, object]) -> object:
        result: Any = await asyncio.to_thread(self.gateway.dispatch, payload)
        if inspect.isawaitable(result):
            return await result
        return result

    @staticmethod
    def _observation_message(segment_index: int, result: object) -> str:
        if isinstance(result, BaseException):
            value = f"tool execution failed: {type(result).__name__}"
        else:
            value = str(result)
        return f"[tool.result:{segment_index}]\n{value}"

    @staticmethod
    def _invalidated(
        observations: list[ToolObservation],
        assistant_texts: list[str],
        usages: list[TokenUsage],
        rounds: int,
    ) -> SessionResult:
        return SessionResult(
            state=SessionState.Invalidated,
            exit=SessionExit.Invalidated,
            observations=tuple(observations),
            assistant_texts=tuple(assistant_texts),
            usages=tuple(usages),
            rounds=rounds,
        )


__all__ = [
    "AgentLoop",
    "CancellationGate",
    "CheckpointDecision",
    "CheckpointPort",
    "SessionCancelled",
    "SessionResult",
    "ToolGatewayPort",
    "ToolObservation",
    "UsageSink",
]

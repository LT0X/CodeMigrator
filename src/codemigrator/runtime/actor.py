"""Single-writer Run actor and its typed mailbox reduction loop."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Protocol

from codemigrator.core import (
    ActiveDispatch,
    Advice,
    CreateRun,
    FailureReason,
    RunId,
    RunStatus,
    canonical_json_bytes,
)

from .advice import AdviceValidationContext, evaluate_advice
from .budget import BudgetLimits, evaluate_budget
from .contracts import (
    AdviceMessage,
    ApiCommand,
    BudgetEventMessage,
    CancelCommand,
    CreateRunCommand,
    EventSpec,
    ExecutionReceiptMessage,
    RecoveryCommandMessage,
    RunState,
    RuntimeMessage,
    SessionInputCommand,
)
from .integration import IntegrationCoordinator
from .recovery import RecoveryCoordinator, RecoveryTrigger
from .store import RuntimeStore, StoreCommitError


class CheckpointWriter(Protocol):
    async def write(self, state: RunState) -> None:
        """Persist a cursor checkpoint before budget termination."""


class CancellationPort(Protocol):
    async def cancel(self, run_id: RunId) -> None:
        """Stop provider and sandbox work associated with a Run."""


class ContinuationPort(Protocol):
    async def schedule(self, run_id: RunId, generation: int) -> None:
        """Dispatch a same-generation continuation from the latest checkpoint."""


class ArchivePort(Protocol):
    async def archive(self, run_id: RunId) -> None:
        """Archive unverified candidate material before budget failure."""


class _Stop:
    pass


class RunActor:
    """Own one Run's state decisions and serialize them through an asyncio queue."""

    max_continuations_per_generation = 3

    def __init__(
        self,
        run_id: RunId,
        store: RuntimeStore,
        *,
        budget_limits: BudgetLimits | None = None,
        advice_context: AdviceValidationContext | None = None,
        checkpoint_writer: CheckpointWriter | None = None,
        cancellation_port: CancellationPort | None = None,
        continuation_port: ContinuationPort | None = None,
        archive_port: ArchivePort | None = None,
        integration_coordinator: IntegrationCoordinator | None = None,
    ) -> None:
        self.run_id = run_id
        self.store = store
        self.budget_limits = budget_limits or BudgetLimits(100_000, 100_000, 1_000_000)
        self.advice_context = advice_context or AdviceValidationContext()
        self.checkpoint_writer = checkpoint_writer
        self.cancellation_port = cancellation_port
        self.continuation_port = continuation_port
        self.archive_port = archive_port
        self.integration_coordinator = integration_coordinator
        self._state: RunState | None = None
        self._queue: asyncio.Queue[RuntimeMessage | _Stop] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self.last_error: Exception | None = None
        self._last_dispatch_acceptance: bool | None = None

    @property
    def state(self) -> RunState | None:
        return self._state

    async def start(self) -> None:
        if self._task is not None:
            return
        snapshot = await self.store.load(self.run_id)
        self._state = snapshot.state if snapshot is not None else None
        self._task = asyncio.create_task(self._run(), name=f"codemigrator-run-{self.run_id}")

    async def stop(self) -> None:
        if self._task is None:
            return
        await self._queue.put(_Stop())
        await self._task
        self._task = None

    async def submit(self, message: RuntimeMessage) -> None:
        if self._task is None:
            raise RuntimeError("actor is not started")
        await self._queue.put(message)

    async def join(self) -> None:
        await self._queue.join()

    async def create(self, create_run: CreateRun) -> bool:
        await self.submit(
            ApiCommand(CreateRunCommand(run_id=self.run_id, create_run=create_run))
        )
        await self.join()
        return self._state is not None

    async def dispatch_started(self, dispatch: ActiveDispatch | None) -> bool:
        if dispatch is None:
            return False
        self._last_dispatch_acceptance = None
        await self.submit(
            ExecutionReceiptMessage(run_id=self.run_id, dispatch=dispatch, started=True)
        )
        await self.join()
        return self._last_dispatch_acceptance is True

    async def execution_receipt(self, dispatch: ActiveDispatch, status: str) -> bool:
        """Submit a receipt and report whether it matched the active dispatch."""

        self._last_dispatch_acceptance = None
        await self.submit(
            ExecutionReceiptMessage(run_id=self.run_id, dispatch=dispatch, result_status=status)
        )
        await self.join()
        return self._last_dispatch_acceptance is True

    async def _run(self) -> None:
        while True:
            message = await self._queue.get()
            try:
                if isinstance(message, _Stop):
                    return
                await self._handle(message)
            except Exception as exc:  # Keep the mailbox alive; the failed commit is observable.
                self.last_error = exc
            finally:
                self._queue.task_done()

    async def _handle(self, message: RuntimeMessage) -> None:
        if isinstance(message, ApiCommand):
            await self._handle_api(message.command)
        elif isinstance(message, ExecutionReceiptMessage):
            await self._handle_execution(message)
        elif isinstance(message, BudgetEventMessage):
            await self._handle_budget(message)
        elif isinstance(message, RecoveryCommandMessage):
            await self._handle_recovery(message)
        elif isinstance(message, AdviceMessage):
            await self._handle_advice(message.advice)

    async def _handle_api(self, command: object) -> None:
        if isinstance(command, CreateRunCommand):
            await self._handle_create(command)
        elif isinstance(command, CancelCommand):
            await self._handle_cancel(command)
        elif isinstance(command, SessionInputCommand):
            await self._handle_session_input(command)

    async def _handle_create(self, command: CreateRunCommand) -> None:
        if command.run_id != self.run_id or self._state is not None:
            return
        state = RunState(run_id=self.run_id, status=RunStatus.Planning, version=1)
        snapshot = await self.store.create(
            state,
            (EventSpec("run.created", {"status": state.status.value}),),
        )
        self._state = snapshot.state

    async def _handle_cancel(self, command: CancelCommand) -> None:
        state = self._state
        if state is None or state.status in _TERMINAL_STATUSES:
            return
        if command.expected_version != state.version:
            return
        next_state = replace(
            state,
            status=RunStatus.Cancelled,
            cancel_requested=True,
            new_calls_enabled=False,
            active_dispatches=(),
            version=state.version + 1,
        )
        if await self._commit(next_state, (EventSpec("run.cancelled"),)):
            if self.integration_coordinator is not None:
                self.integration_coordinator.cancel_run(str(self.run_id))
            if self.cancellation_port is not None:
                try:
                    await self.cancellation_port.cancel(self.run_id)
                except Exception as exc:
                    self.last_error = exc

    async def _handle_session_input(self, command: SessionInputCommand) -> None:
        state = self._state
        if state is None or state.status in _TERMINAL_STATUSES:
            return
        if command.kind == "confirm_advice":
            await self._handle_advice_confirmation(command.payload)
            return
        if command.kind == "segment_stopped":
            await self._handle_segment_stopped(command.payload)
            return
        next_state = replace(state, version=state.version + 1)
        await self._commit(
            next_state,
            (EventSpec("session.input.accepted", {"kind": command.kind}),),
        )

    async def _handle_advice_confirmation(self, payload: dict[str, object]) -> None:
        state = self._state
        assert state is not None
        advice_id = str(payload.get("advice_id", ""))
        if advice_id not in state.pending_advice_ids:
            await self._commit(
                state,
                (EventSpec("advice.confirmation_rejected", {"advice_id": advice_id}),),
            )
            return
        next_state = replace(
            state,
            pending_advice_ids=tuple(
                item for item in state.pending_advice_ids if item != advice_id
            ),
            adopted_advice_ids=(*state.adopted_advice_ids, advice_id),
            version=state.version + 1,
        )
        await self._commit(
            next_state,
            (EventSpec("advice.confirmed", {"advice_id": advice_id}),),
        )

    async def _handle_segment_stopped(self, payload: dict[str, object]) -> None:
        state = self._state
        assert state is not None
        generation = payload.get("generation", 0)
        if type(generation) is not int or generation not in (0, 1, 2):
            await self._commit(
                replace(state, version=state.version + 1),
                (EventSpec("slice.terminal_failed", {"reason": "INVALID_GENERATION"}),),
            )
            return
        progress = payload.get("material_progress") is True or bool(payload.get("checkpoint_diff"))
        current_counts = dict(state.continuation_counts)
        current = current_counts.get(generation, 0)
        eligible = (
            state.new_calls_enabled
            and progress
            and current < self.max_continuations_per_generation
        )
        current_counts[generation] = current + 1 if eligible else current
        slice_id = str(payload.get("slice_id", "unknown"))
        next_state = replace(
            state,
            continuation_counts=tuple(sorted(current_counts.items())),
            terminal_slice_failures=(
                (*state.terminal_slice_failures, slice_id)
                if not eligible and slice_id not in state.terminal_slice_failures
                else state.terminal_slice_failures
            ),
            version=state.version + 1,
        )
        event = (
            EventSpec(
                "session.continuation_scheduled",
                {"generation": generation, "continuation": current + 1},
            )
            if eligible
            else EventSpec(
                "slice.terminal_failed",
                {"generation": generation, "reason": "INDEPENDENT_SLICE_TERMINAL_FAILURE"},
            )
        )
        committed = await self._commit(next_state, (event,))
        if committed and eligible and self.continuation_port is not None:
            try:
                await self.continuation_port.schedule(self.run_id, generation)
            except Exception as exc:
                self.last_error = exc

    async def _handle_execution(self, message: ExecutionReceiptMessage) -> None:
        state = self._state
        if state is None:
            self._last_dispatch_acceptance = False
            return
        if message.run_id != self.run_id:
            if not message.started:
                await self._commit(
                    state,
                    (
                        EventSpec(
                            "LATE_DISPATCH_RESULT",
                            {"reason": "RUN_ID_MISMATCH"},
                        ),
                    ),
                )
            self._last_dispatch_acceptance = False
            return
        if message.started:
            if state.status in _TERMINAL_STATUSES or not state.new_calls_enabled:
                self._last_dispatch_acceptance = False
                return
            await self._start_dispatch(message.dispatch)
            return
        await self._finish_dispatch(message.dispatch, message.result_status)

    async def _start_dispatch(self, dispatch: ActiveDispatch) -> None:
        state = self._state
        assert state is not None
        if state.cancel_requested or not state.new_calls_enabled:
            self._last_dispatch_acceptance = False
            return
        key = _dispatch_key(dispatch, self.run_id)
        if any(_dispatch_key(active, self.run_id) == key for active in state.active_dispatches):
            self._last_dispatch_acceptance = False
            return
        next_state = replace(
            state,
            status=RunStatus.Executing,
            active_dispatches=(*state.active_dispatches, dispatch),
            version=state.version + 1,
        )
        committed = await self._commit(
            next_state,
            (
                EventSpec(
                    "dispatch.started",
                    {
                        "attempt_id": str(dispatch.dispatch_attempt_id),
                        "check_id": str(dispatch.check_id),
                    },
                ),
            ),
        )
        self._last_dispatch_acceptance = committed

    async def _finish_dispatch(self, dispatch: ActiveDispatch, result_status: str | None) -> None:
        state = self._state
        assert state is not None
        key = _dispatch_key(dispatch, self.run_id)
        active = next(
            (
                candidate
                for candidate in state.active_dispatches
                if _dispatch_key(candidate, self.run_id) == key
            ),
            None,
        )
        if active is None or active != dispatch:
            await self._commit(
                state,
                (
                    EventSpec(
                        "LATE_DISPATCH_RESULT",
                        {"attempt_id": str(dispatch.dispatch_attempt_id)},
                    ),
                ),
            )
            self._last_dispatch_acceptance = False
            return
        next_state = replace(
            state,
            active_dispatches=tuple(item for item in state.active_dispatches if item != dispatch),
            version=state.version + 1,
        )
        await self._commit(
            next_state,
            (EventSpec("dispatch.completed", {"status": result_status or "UNKNOWN"}),),
        )
        self._last_dispatch_acceptance = True

    async def _handle_budget(self, message: BudgetEventMessage) -> None:
        state = self._state
        if state is None or state.status in _TERMINAL_STATUSES:
            return
        evaluation = evaluate_budget(
            state.budget_usage,
            self.budget_limits,
            input_tokens=message.input_tokens,
            output_tokens=message.output_tokens,
            cost_micros=message.cost_micros,
            warning_already_emitted=state.budget_warning_emitted,
        )
        events: list[EventSpec] = []
        warning_emitted = state.budget_warning_emitted
        if evaluation.warning:
            warning_emitted = True
            events.append(EventSpec("budget.warning", {"utilization": evaluation.utilization}))
        next_state = replace(
            state,
            budget_usage=evaluation.usage,
            budget_warning_emitted=warning_emitted,
            version=state.version + 1,
        )
        if evaluation.exhausted:
            pre_checkpoint_state = replace(
                next_state,
                new_calls_enabled=False,
            )
            events.append(EventSpec("checkpoint.pre"))
            if self.checkpoint_writer is not None:
                try:
                    await self.checkpoint_writer.write(pre_checkpoint_state)
                except Exception as exc:
                    self.last_error = exc
                    events.append(EventSpec("checkpoint.write_failed"))
            if self.archive_port is not None:
                try:
                    await self.archive_port.archive(self.run_id)
                except Exception as exc:
                    self.last_error = exc
                    events.append(EventSpec("archive.failed"))
            events.extend(
                (
                    EventSpec("run.archived"),
                    EventSpec("run.failed", {"reason": "BUDGET_EXHAUSTED"}),
                    EventSpec("budget.exhausted"),
                )
            )
            next_state = replace(
                next_state,
                status=RunStatus.Failed,
                failure_reason=FailureReason.BudgetExhausted,
                new_calls_enabled=False,
                active_dispatches=(),
            )
        await self._commit(next_state, tuple(events))

    async def _handle_recovery(self, message: RecoveryCommandMessage) -> None:
        state = self._state
        if state is None:
            return
        trigger = RecoveryTrigger(message.trigger)
        plan = RecoveryCoordinator().trigger(
            trigger,
            active_dispatch_ids=message.active_dispatch_ids,
            missing_intent_ids=message.missing_intent_ids,
            checkpoint_corrupt=message.checkpoint_corrupt,
            ref_drift=message.ref_drift,
        )
        events: list[EventSpec] = []
        for item in plan.events:
            if item.startswith("dispatch.interrupted:"):
                events.append(
                    EventSpec(
                        "dispatch.interrupted",
                        {"dispatch_id": item.split(":", 1)[1]},
                    )
                )
            else:
                events.append(EventSpec(item))
        next_state = replace(
            state,
            active_dispatches=(),
            reporting_halted=plan.report_halted,
            version=state.version + 1,
        )
        await self._commit(next_state, tuple(events))

    async def _handle_advice(self, advice: Advice) -> None:
        state = self._state
        if state is None:
            return
        next_state = state
        if advice.run_id != self.run_id:
            result_reason = "RUN_ID_MISMATCH"
            event_type = "advice.discarded"
            result_hash = ""
        else:
            result = evaluate_advice(advice, self.advice_context)
            result_reason = result.reason
            event_type = {
                "AUTO_ADOPTED": "advice.adopted",
                "CONFIRMATION_REQUIRED": "advice.confirmation_required",
                "DISCARDED": "advice.discarded",
            }[result.disposition.value]
            result_hash = result.proposal_hash
            advice_id = str(advice.advice_id)
            if result.disposition.value == "AUTO_ADOPTED":
                if advice_id in state.adopted_advice_ids:
                    event_type = "advice.duplicate"
                else:
                    next_state = replace(
                        state,
                        adopted_advice_ids=(*state.adopted_advice_ids, advice_id),
                        version=state.version + 1,
                    )
            elif result.disposition.value == "CONFIRMATION_REQUIRED":
                if advice_id in state.pending_advice_ids:
                    event_type = "advice.duplicate"
                else:
                    next_state = replace(
                        state,
                        pending_advice_ids=(*state.pending_advice_ids, advice_id),
                        version=state.version + 1,
                    )
        await self._commit(
            next_state,
            (
                EventSpec(
                    event_type,
                    {"reason": result_reason, "proposal_hash": result_hash},
                ),
            ),
        )

    async def _commit(self, state: RunState, events: tuple[EventSpec, ...]) -> bool:
        try:
            snapshot = await self.store.commit(state, events)
        except StoreCommitError as exc:
            self.last_error = exc
            return False
        self._state = snapshot.state
        return True


_TERMINAL_STATUSES = frozenset(
    {RunStatus.Completed, RunStatus.PartiallyCompleted, RunStatus.Failed, RunStatus.Cancelled}
)


def _dispatch_key(dispatch: ActiveDispatch, run_id: RunId) -> tuple[str, bytes, str]:
    subject = dispatch.subject.model_dump(mode="json", by_alias=True)
    return str(run_id), canonical_json_bytes(subject), str(dispatch.check_id)


class ActorRegistry:
    """Keep exactly one in-memory actor for each non-terminal Run."""

    def __init__(self, store: RuntimeStore) -> None:
        self.store = store
        self._actors: dict[RunId, RunActor] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(self, run_id: RunId) -> RunActor | None:
        async with self._lock:
            actor = self._actors.get(run_id)
            if actor is not None:
                return actor
            snapshot = await self.store.load(run_id)
            if snapshot is not None and snapshot.state.status in _TERMINAL_STATUSES:
                return None
            actor = RunActor(run_id, self.store)
            await actor.start()
            self._actors[run_id] = actor
            return actor

    async def close(self) -> None:
        actors = tuple(self._actors.values())
        self._actors.clear()
        for actor in actors:
            await actor.stop()

    async def rebuild(self, run_id: RunId) -> RunActor | None:
        """Replace one actor from durable facts after an explicit recovery trigger."""

        async with self._lock:
            actor = self._actors.pop(run_id, None)
            if actor is not None:
                await actor.stop()
        return await self.get_or_create(run_id)


__all__ = [
    "ActorRegistry",
    "ArchivePort",
    "CancellationPort",
    "CheckpointWriter",
    "ContinuationPort",
    "RunActor",
]

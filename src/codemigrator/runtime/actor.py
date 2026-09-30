"""Single-writer Run actor and its typed mailbox reduction loop."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Protocol, cast
from uuid import UUID

from codemigrator.core import (
    ActiveDispatch,
    Advice,
    AdviceKind,
    CreateRun,
    FailureReason,
    Phase,
    RunId,
    RunStatus,
    SessionKind,
    canonical_json_bytes,
)
from codemigrator.planning import FrozenPlan
from codemigrator.workspace import CheckpointReceipt, checkpoint_receipt_digest

from .advice import AdviceValidationContext, evaluate_advice
from .agent_runs import AgentRunId
from .budget import BudgetLimits, evaluate_budget
from .contracts import (
    ActorPhaseReceipt,
    AdviceMessage,
    ApiCommand,
    BudgetEventMessage,
    CancelCommand,
    CandidateCheckpointClaim,
    CandidateCheckpointFact,
    CreateRunCommand,
    EventSpec,
    ExecuteRoundResult,
    ExecutionReceiptMessage,
    ExecutionRoundDecision,
    ExecutionRoundFinishedMessage,
    RecoveryCommandMessage,
    ReportSummary,
    RunCreatedReceipt,
    RunState,
    RuntimeEvent,
    RuntimeMessage,
    RuntimeSnapshot,
    RuntimeStoreTransaction,
    SessionInputCommand,
    VerificationSummary,
    WorkflowCommandMessage,
    agent_run_lifecycle_spec,
)
from .integration import IntegrationCoordinator
from .loop_contracts import SessionExit, SessionState
from .recovery import RecoveryCoordinator, RecoveryTrigger, has_committed_owner_receipt
from .run_graph import PlanAgentCompletion
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


class RepairAdvicePort(Protocol):
    async def dispatch_adopted(self, advice: Advice) -> None:
        """Materialize and dispatch an adopted global repair decision."""


class ExecutionSchedulerPort(Protocol):
    async def advance_one_round(
        self,
        run_id: RunId,
        logical_key: str,
        *,
        on_agent_run_started: Callable[[RunId, AgentRunId], Awaitable[ActorPhaseReceipt]],
    ) -> ExecutionRoundDecision:
        """Idempotently schedule ready Slice AgentRuns and return round status."""


class CandidateCheckpointVerifierPort(Protocol):
    def is_committed_receipt(self, receipt: CheckpointReceipt) -> bool: ...


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
        repair_advice_port: RepairAdvicePort | None = None,
        execution_scheduler: ExecutionSchedulerPort | None = None,
        candidate_checkpoint_verifier: CandidateCheckpointVerifierPort | None = None,
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
        self.repair_advice_port = repair_advice_port
        self.execution_scheduler = execution_scheduler
        self.candidate_checkpoint_verifier = candidate_checkpoint_verifier
        self._state: RunState | None = None
        self._queue: asyncio.Queue[RuntimeMessage | _Stop] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self.last_error: Exception | None = None
        self._last_dispatch_acceptance: bool | None = None
        self._create_receipt: RunCreatedReceipt | None = None
        self._execution_round_responses: dict[str, list[asyncio.Future[object]]] = {}
        self._execution_round_tasks: dict[str, asyncio.Task[None]] = {}

    @property
    def state(self) -> RunState | None:
        return self._state

    async def start(self) -> None:
        if self._task is not None:
            return
        snapshot = await self.store.load(self.run_id)
        self._state = snapshot.state if snapshot is not None else None
        self._create_receipt = _run_created_receipt(snapshot, self.run_id)
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

    async def create(
        self,
        create_run: CreateRun,
        *,
        transaction: RuntimeStoreTransaction | None = None,
    ) -> RunCreatedReceipt | None:
        self._create_receipt = None
        await self.submit(
            ApiCommand(
                CreateRunCommand(
                    run_id=self.run_id,
                    create_run=create_run,
                    transaction=transaction,
                )
            )
        )
        await self.join()
        return self._create_receipt

    async def has_receipt(self, run_id: RunId, receipt_key: str) -> bool:
        if run_id != self.run_id:
            return False
        snapshot = await self.store.load(run_id)
        return snapshot is not None and has_committed_owner_receipt(snapshot.events, receipt_key)

    async def accept_plan(
        self, run_id: RunId, completion: PlanAgentCompletion, frozen_plan: FrozenPlan
    ) -> ActorPhaseReceipt:
        return cast(
            ActorPhaseReceipt,
            await self._workflow_command(
                "accept_plan",
                run_id,
                f"run.plan.accepted:{run_id}",
                (completion, frozen_plan),
            ),
        )

    async def record_agent_run_started(
        self, run_id: RunId, agent_run_id: AgentRunId
    ) -> ActorPhaseReceipt:
        receipt = cast(
            ActorPhaseReceipt,
            await self._workflow_command(
                "agent_run_started",
                run_id,
                f"agent_run.started:{agent_run_id}",
                agent_run_id,
            ),
        )
        if receipt.run_id != run_id or not await self.has_receipt(run_id, receipt.receipt_key):
            raise StoreCommitError("AgentRun cannot proceed without its committed start receipt")
        return receipt

    async def advance_execution_round(self, run_id: RunId, logical_key: str) -> ExecuteRoundResult:
        return cast(
            ExecuteRoundResult,
            await self._workflow_command("execute_round", run_id, logical_key, None),
        )

    async def commit_verification(
        self, run_id: RunId, result: VerificationSummary, logical_key: str
    ) -> ActorPhaseReceipt:
        return cast(
            ActorPhaseReceipt,
            await self._workflow_command("verification", run_id, logical_key, result),
        )

    async def commit_report(
        self, run_id: RunId, result: ReportSummary, logical_key: str
    ) -> ActorPhaseReceipt:
        return cast(
            ActorPhaseReceipt,
            await self._workflow_command("report", run_id, logical_key, result),
        )

    async def _workflow_command(
        self, kind: str, run_id: RunId, logical_key: str, payload: object
    ) -> object:
        loop = asyncio.get_running_loop()
        response: asyncio.Future[object] = loop.create_future()
        await self.submit(WorkflowCommandMessage(kind, run_id, logical_key, payload, response))
        return await response

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
                if isinstance(message, WorkflowCommandMessage) and not message.response.done():
                    message.response.set_exception(exc)
                elif isinstance(message, ExecutionRoundFinishedMessage):
                    self._finish_execution_round(message.logical_key, error=exc)
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
        elif isinstance(message, WorkflowCommandMessage):
            await self._handle_workflow_command(message)
        elif isinstance(message, ExecutionRoundFinishedMessage):
            await self._handle_execution_round_finished(message)

    async def _handle_workflow_command(self, command: WorkflowCommandMessage) -> None:
        if command.run_id != self.run_id:
            raise ValueError("workflow command Run identity mismatch")
        snapshot = await self.store.load(self.run_id)
        event = _event_for_receipt(snapshot, command.logical_key)
        if event is not None:
            receipt = ActorPhaseReceipt(self.run_id, command.logical_key, event.sequence)
            if command.kind == "execute_round":
                command.response.set_result(
                    ExecuteRoundResult(receipt, event.data.get("complete") is True)
                )
            else:
                command.response.set_result(receipt)
            return
        state = self._state
        if state is None or snapshot is None:
            raise StoreCommitError("workflow owner Run does not exist")
        if command.kind == "accept_plan":
            await self._accept_plan(command, state)
        elif command.kind == "execute_round":
            self._schedule_execution_round(command, state)
        elif command.kind == "agent_run_started":
            await self._record_agent_run_started(command, state)
        elif command.kind == "verification":
            await self._commit_verification(command, state)
        elif command.kind == "report":
            await self._commit_report(command, state)
        else:
            raise ValueError("unsupported Run workflow command")

    async def _record_agent_run_started(
        self, command: WorkflowCommandMessage, state: RunState
    ) -> None:
        if not isinstance(command.payload, UUID):
            raise ValueError("AgentRun start payload is invalid")
        agent_run_id = AgentRunId(command.payload)
        record = await self.store.load_agent_run(agent_run_id)
        if (
            record is None
            or record.owner_kind != "run"
            or record.owner_id != self.run_id
            or record.phase not in {Phase.Plan, Phase.Execute}
            or record.is_terminal
            or state.status not in {RunStatus.Planning, RunStatus.Executing}
        ):
            raise StoreCommitError("AgentRun start facts do not match the active Run owner")
        next_state = replace(state, version=state.version + 1)
        if not await self._commit(next_state, (agent_run_lifecycle_spec(record),)):
            raise StoreCommitError("AgentRun start receipt could not be committed")
        event = _event_for_receipt(await self.store.load(self.run_id), command.logical_key)
        if event is None:
            raise StoreCommitError("committed AgentRun start receipt cannot be reloaded")
        command.response.set_result(
            ActorPhaseReceipt(self.run_id, command.logical_key, event.sequence)
        )

    async def _accept_plan(self, command: WorkflowCommandMessage, state: RunState) -> None:
        if not isinstance(command.payload, tuple) or len(command.payload) != 2:
            raise ValueError("PLAN acceptance payload is invalid")
        completion, frozen_plan = command.payload
        completion = cast(PlanAgentCompletion, completion)
        frozen_plan = cast(FrozenPlan, frozen_plan)
        record = completion.record
        agent_receipt = completion.receipt
        result_object = completion.result_object
        plan_object = completion.plan_object
        plan_hash = frozen_plan.plan_hash
        validation = frozen_plan.validation
        owner_snapshot = await self.store.load(self.run_id)
        if (
            state.status is not RunStatus.Planning
            or record is None
            or record.owner_kind != "run"
            or record.owner_id != self.run_id
            or record.phase is not Phase.Plan
            or record.session_kind is not SessionKind.PlanAuxiliary
            or record.state is not SessionState.Closed
            or record.exit is not SessionExit.Completed
            or agent_receipt.agent_run_id != record.agent_run_id
            or agent_receipt.category != "plan.accepted"
            or record.result_sha256 != result_object.digest
            or not _has_agent_run_event(owner_snapshot, "agent_run.started", record.agent_run_id)
            or hashlib.sha256(frozen_plan.canonical_payload()).hexdigest() != plan_object.digest
            or not validation.accepted
        ):
            raise ValueError("PLAN acceptance facts failed deterministic owner checks")
        terminal_event = agent_run_lifecycle_spec(record, agent_receipt)
        events = (
            terminal_event,
            EventSpec(
                "run.plan.accepted",
                {
                    "receipt_key": command.logical_key,
                    "agent_run_id": str(record.agent_run_id),
                    "plan_sha256": plan_hash,
                },
            ),
        )
        next_state = replace(
            state,
            status=RunStatus.Executing,
            frozen_plan_sha256=plan_hash,
            version=state.version + 1,
        )
        await self.store.commit_agent_run_receipt(
            record,
            agent_receipt,
            state=next_state,
            events=events,
            cas_references=(
                (f"agent-result:{record.agent_run_id}", result_object),
                ("frozen-plan", plan_object),
            ),
        )
        self._state = next_state
        snapshot = await self.store.load(self.run_id)
        event = _event_for_receipt(snapshot, command.logical_key)
        if event is None:
            raise StoreCommitError("committed PLAN owner receipt cannot be reloaded")
        command.response.set_result(
            ActorPhaseReceipt(self.run_id, command.logical_key, event.sequence)
        )

    def _schedule_execution_round(self, command: WorkflowCommandMessage, state: RunState) -> None:
        if (
            state.status is not RunStatus.Executing
            or state.frozen_plan_sha256 is None
            or self.execution_scheduler is None
        ):
            raise StoreCommitError("EXECUTE requires an active Run and Actor scheduler")
        self._execution_round_responses.setdefault(command.logical_key, []).append(
            command.response
        )
        if command.logical_key in self._execution_round_tasks:
            return
        self._execution_round_tasks[command.logical_key] = asyncio.create_task(
            self._run_execution_scheduler(command.logical_key),
            name=f"codemigrator-execute-{self.run_id}-{command.logical_key}",
        )

    async def _run_execution_scheduler(self, logical_key: str) -> None:
        try:
            if self.execution_scheduler is None:
                raise StoreCommitError("EXECUTE scheduler is unavailable")
            decision = await self.execution_scheduler.advance_one_round(
                self.run_id,
                logical_key,
                on_agent_run_started=self.record_agent_run_started,
            )
            if not isinstance(decision, ExecutionRoundDecision):
                raise TypeError("Actor scheduler returned an invalid execution round")
            await self.submit(ExecutionRoundFinishedMessage(logical_key, decision=decision))
        except Exception as exc:
            await self.submit(ExecutionRoundFinishedMessage(logical_key, error=exc))

    async def _handle_execution_round_finished(
        self, message: ExecutionRoundFinishedMessage
    ) -> None:
        if message.error is not None:
            self._finish_execution_round(message.logical_key, error=message.error)
            return
        state = self._state
        if state is None or message.decision is None:
            self._finish_execution_round(
                message.logical_key,
                error=StoreCommitError("EXECUTE round completion is missing its Run facts"),
            )
            return
        try:
            result = await self._commit_execution_round(
                message.logical_key, state, message.decision
            )
        except Exception as exc:
            self._finish_execution_round(message.logical_key, error=exc)
        else:
            self._finish_execution_round(message.logical_key, result=result)

    def _finish_execution_round(
        self,
        logical_key: str,
        *,
        result: ExecuteRoundResult | None = None,
        error: Exception | None = None,
    ) -> None:
        responses = self._execution_round_responses.pop(logical_key, [])
        self._execution_round_tasks.pop(logical_key, None)
        for response in responses:
            if response.done():
                continue
            if error is not None:
                response.set_exception(error)
            elif result is not None:
                response.set_result(result)
            else:
                response.set_exception(StoreCommitError("EXECUTE round has no durable result"))

    async def _commit_execution_round(
        self, logical_key: str, state: RunState, decision: ExecutionRoundDecision
    ) -> ExecuteRoundResult:
        if not isinstance(decision, ExecutionRoundDecision):
            raise TypeError("Actor scheduler returned an invalid execution round")
        if state.status is not RunStatus.Executing or state.frozen_plan_sha256 is None:
            raise StoreCommitError("EXECUTE result arrived after its Run stopped executing")
        completed_ids = set(decision.completed_write_agent_run_ids)
        claimed_ids = {claim.agent_run_id for claim in decision.candidate_claims}
        terminal_ids = set(decision.terminal_agent_run_ids)
        if (
            len(claimed_ids) != len(decision.candidate_claims)
            or len(completed_ids) != len(decision.completed_write_agent_run_ids)
            or len(terminal_ids) != len(decision.terminal_agent_run_ids)
            or completed_ids != claimed_ids
            or not completed_ids.issubset(terminal_ids)
        ):
            raise StoreCommitError(
                "EXECUTE terminal or candidate checkpoint claim set is incomplete"
            )
        candidate_facts = list(state.candidate_checkpoints)
        terminal_events: list[EventSpec] = []
        candidate_events: list[EventSpec] = []
        snapshot = await self.store.load(self.run_id)
        for agent_run_id in decision.terminal_agent_run_ids:
            record = await self.store.load_agent_run(agent_run_id)
            receipt = await self.store.load_agent_run_receipt(agent_run_id)
            if (
                record is None
                or record.owner_kind != "run"
                or record.owner_id != self.run_id
                or record.phase is not Phase.Execute
                or not record.is_terminal
                or receipt is None
                or receipt.agent_run_id != record.agent_run_id
                or receipt.category != "session.terminal"
            ):
                raise StoreCommitError("EXECUTE terminal AgentRun has no matching terminal receipt")
            if not _has_agent_run_event(snapshot, "agent_run.started", agent_run_id):
                raise StoreCommitError("terminal EXECUTE AgentRun has no committed start receipt")
            if not _has_agent_run_event(snapshot, "agent_run.terminal", agent_run_id):
                terminal_events.append(agent_run_lifecycle_spec(record, receipt))
        for claim in decision.candidate_claims:
            fact = await self._validate_candidate_claim(claim, candidate_facts)
            current = next(
                (
                    item
                    for item in candidate_facts
                    if item.slice_id == fact.slice_id and item.generation == fact.generation
                ),
                None,
            )
            if current is not None and current.receipt_sha256 == fact.receipt_sha256:
                continue
            if current is None:
                candidate_facts.append(fact)
            else:
                candidate_facts[candidate_facts.index(current)] = fact
            candidate_events.append(
                EventSpec(
                    "slice.candidate.accepted",
                    {
                        "agent_run_id": str(fact.agent_run_id),
                        "slice_id": str(fact.slice_id),
                        "generation": fact.generation,
                    },
                )
            )
        next_state = replace(
            state,
            status=RunStatus.Verifying if decision.complete else RunStatus.Executing,
            candidate_checkpoints=tuple(candidate_facts),
            version=state.version + 1,
        )
        committed = await self._commit(
            next_state,
            (
                *terminal_events,
                *candidate_events,
                EventSpec(
                    "run.execute.round",
                    {
                        "receipt_key": logical_key,
                        "complete": decision.complete,
                        "dispatch_count": decision.dispatch_count,
                    },
                ),
            ),
        )
        if not committed:
            raise StoreCommitError("EXECUTE actor receipt could not be committed")
        event = _event_for_receipt(await self.store.load(self.run_id), logical_key)
        assert event is not None
        return ExecuteRoundResult(
            ActorPhaseReceipt(self.run_id, logical_key, event.sequence),
            decision.complete,
        )

    async def _validate_candidate_claim(
        self,
        claim: CandidateCheckpointClaim,
        accepted: list[CandidateCheckpointFact],
    ) -> CandidateCheckpointFact:
        receipt = claim.receipt
        if receipt is None or self.candidate_checkpoint_verifier is None:
            raise StoreCommitError("completed write AgentRun has no candidate checkpoint receipt")
        record = await self.store.load_agent_run(claim.agent_run_id)
        terminal_receipt = await self.store.load_agent_run_receipt(claim.agent_run_id)
        slice_ref = record.slice_ref if record is not None else None
        if (
            record is None
            or record.owner_kind != "run"
            or record.owner_id != self.run_id
            or record.phase is not Phase.Execute
            or record.session_kind
            not in {
                SessionKind.Contract,
                SessionKind.Implementation,
                SessionKind.TestTranslation,
                SessionKind.TestGeneration,
                SessionKind.RepairSession,
            }
            or record.state is not SessionState.Closed
            or record.exit is not SessionExit.Completed
            or record.result_sha256 is None
            or record.write_scope_sha256 is None
            or terminal_receipt is None
            or terminal_receipt.agent_run_id != record.agent_run_id
            or terminal_receipt.category != "session.terminal"
            or slice_ref is None
            or record.candidate_checkpoint_sha256 != checkpoint_receipt_digest(receipt)
            or slice_ref.baseline_candidate_oid is None
            or receipt.run_id != self.run_id
            or receipt.slice_id != slice_ref.slice_id
            or receipt.generation != slice_ref.generation
            or receipt.expected_candidate_oid != str(slice_ref.baseline_candidate_oid)
            or receipt.manifest.slice_candidate.run_id != self.run_id
            or receipt.manifest.slice_candidate.slice_id != slice_ref.slice_id
            or receipt.manifest.slice_candidate.generation != slice_ref.generation
            or str(receipt.manifest.slice_candidate.candidate_commit_oid)
            != receipt.expected_candidate_oid
            or not receipt.manifest.scope_check_passed
            or receipt.new_candidate_oid == receipt.expected_candidate_oid
        ):
            raise StoreCommitError("candidate checkpoint receipt failed owner validation")
        if not await asyncio.to_thread(
            self.candidate_checkpoint_verifier.is_committed_receipt, receipt
        ):
            raise StoreCommitError("candidate checkpoint receipt is not committed by M-08")
        candidate_digest = checkpoint_receipt_digest(receipt)
        previous_receipt = next(
            (item for item in accepted if item.receipt_sha256 == candidate_digest),
            None,
        )
        if previous_receipt is not None:
            if (
                previous_receipt.agent_run_id != record.agent_run_id
                or previous_receipt.candidate_oid != receipt.new_candidate_oid
                or previous_receipt.slice_id != receipt.slice_id
                or previous_receipt.generation != receipt.generation
            ):
                raise StoreCommitError("candidate checkpoint receipt was previously claimed")
            return previous_receipt
        current = next(
            (
                item
                for item in accepted
                if item.slice_id == slice_ref.slice_id and item.generation == slice_ref.generation
            ),
            None,
        )
        expected_current = (
            current.candidate_oid if current is not None else str(slice_ref.baseline_candidate_oid)
        )
        if receipt.expected_candidate_oid != expected_current:
            raise StoreCommitError("candidate checkpoint does not extend the accepted OID")
        return CandidateCheckpointFact(
            agent_run_id=record.agent_run_id,
            slice_id=slice_ref.slice_id,
            generation=int(slice_ref.generation),
            expected_candidate_oid=receipt.expected_candidate_oid,
            candidate_oid=receipt.new_candidate_oid,
            receipt_sha256=candidate_digest,
        )

    async def _commit_verification(self, command: WorkflowCommandMessage, state: RunState) -> None:
        result = command.payload
        if state.status is not RunStatus.Verifying or not isinstance(result, VerificationSummary):
            raise StoreCommitError("VERIFY requires deterministic verification facts")
        next_state = replace(state, status=RunStatus.Reporting, version=state.version + 1)
        committed = await self._commit(
            next_state,
            (
                EventSpec(
                    "run.verify.completed",
                    {
                        "receipt_key": command.logical_key,
                        "passed": result.passed,
                        "result_sha256": result.result_sha256,
                    },
                ),
            ),
        )
        if not committed:
            raise StoreCommitError("VERIFY actor receipt could not be committed")
        event = _event_for_receipt(await self.store.load(self.run_id), command.logical_key)
        assert event is not None
        command.response.set_result(
            ActorPhaseReceipt(self.run_id, command.logical_key, event.sequence)
        )

    async def _commit_report(self, command: WorkflowCommandMessage, state: RunState) -> None:
        result = command.payload
        if state.status is not RunStatus.Reporting or not isinstance(result, ReportSummary):
            raise StoreCommitError("REPORT requires deterministic report facts")
        next_state = replace(
            state,
            status=result.status,
            version=state.version + 1,
        )
        committed = await self._commit(
            next_state,
            (
                EventSpec(
                    "run.report.completed",
                    {
                        "receipt_key": command.logical_key,
                        "status": result.status.value,
                        "result_sha256": result.result_sha256,
                    },
                ),
            ),
        )
        if not committed:
            raise StoreCommitError("REPORT actor receipt could not be committed")
        event = _event_for_receipt(await self.store.load(self.run_id), command.logical_key)
        assert event is not None
        command.response.set_result(
            ActorPhaseReceipt(self.run_id, command.logical_key, event.sequence)
        )

    async def _handle_api(self, command: object) -> None:
        if isinstance(command, CreateRunCommand):
            await self._handle_create(command)
        elif isinstance(command, CancelCommand):
            await self._handle_cancel(command)
        elif isinstance(command, SessionInputCommand):
            await self._handle_session_input(command)

    async def _handle_create(self, command: CreateRunCommand) -> None:
        if command.run_id != self.run_id:
            return
        if self._state is not None:
            if self._state.create_request != command.create_run:
                self._create_receipt = None
                return
            snapshot = await self.store.load(self.run_id)
            self._create_receipt = _run_created_receipt(snapshot, self.run_id)
            return
        state = RunState(
            run_id=self.run_id,
            status=RunStatus.Planning,
            version=1,
            create_request=command.create_run,
        )
        transaction = command.transaction
        if transaction is not None and not isinstance(transaction, RuntimeStoreTransaction):
            raise StoreCommitError("RunActor received an unsupported store transaction")
        snapshot = await self.store.create(
            state,
            (
                EventSpec(
                    "run.created",
                    {
                        "status": state.status.value,
                        "receipt_key": f"run.created:{self.run_id}",
                        "state_version": state.version,
                    },
                ),
            ),
            transaction=transaction,
        )
        self._state = snapshot.state
        self._create_receipt = _run_created_receipt(snapshot, self.run_id)
        if transaction is not None:
            transaction.after_rollback(self._rollback_uncommitted_create)

    def _rollback_uncommitted_create(self) -> None:
        if self._state is not None and self._state.run_id == self.run_id:
            self._state = None
            self._create_receipt = None

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
            state.new_calls_enabled and progress and current < self.max_continuations_per_generation
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
        committed = await self._commit(
            next_state,
            (
                EventSpec(
                    event_type,
                    {"reason": result_reason, "proposal_hash": result_hash},
                ),
            ),
        )
        if (
            committed
            and event_type == "advice.adopted"
            and advice.kind is AdviceKind.RepairDecision
            and self.repair_advice_port is not None
        ):
            try:
                await self.repair_advice_port.dispatch_adopted(advice)
            except Exception as exc:
                self.last_error = exc

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


def _run_created_receipt(
    snapshot: RuntimeSnapshot | None, run_id: RunId
) -> RunCreatedReceipt | None:
    if snapshot is None:
        return None
    events = getattr(snapshot, "events", ())
    state = getattr(snapshot, "state", None)
    for event in events:
        if (
            event.event_type == "run.created"
            and event.data.get("receipt_key") == f"run.created:{run_id}"
            and state is not None
        ):
            return RunCreatedReceipt(
                run_id=run_id,
                receipt_key=f"run.created:{run_id}",
                event_sequence=event.sequence,
                state_version=int(event.data.get("state_version", 1)),
            )
    return None


def _event_for_receipt(snapshot: RuntimeSnapshot | None, receipt_key: str) -> RuntimeEvent | None:
    if snapshot is None:
        return None
    return next(
        (
            event
            for event in reversed(getattr(snapshot, "events", ()))
            if event.data.get("receipt_key") == receipt_key
        ),
        None,
    )


def _has_agent_run_event(
    snapshot: RuntimeSnapshot | None,
    event_type: str,
    agent_run_id: AgentRunId,
) -> bool:
    if snapshot is None:
        return False
    return any(
        event.event_type == event_type
        and event.data.get("agent_run_id") == str(agent_run_id)
        for event in snapshot.events
    )


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
    "ExecutionSchedulerPort",
    "RepairAdvicePort",
    "RunActor",
]

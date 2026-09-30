"""Receipt-gated LangGraph orchestration for one Run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, TypedDict, cast
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph

from codemigrator.core import Phase, RunId
from codemigrator.planning import (
    FrozenPlan,
    PlanLedger,
    PlanningInputs,
    PlanProposal,
    PlanValidator,
)

from .agent_runs import AgentRun, AgentRunReceipt
from .cas import CasObject
from .contracts import ActorPhaseReceipt, ExecuteRoundResult, RunCreatedReceipt
from .loop_contracts import SessionExit, SessionState


class _RunState(TypedDict, total=False):
    run_id: str
    phase: str
    receipt_key: str
    execute_round: int
    execution_complete: bool


class RunGraphActorPort(Protocol):
    async def has_receipt(self, run_id: RunId, receipt_key: str) -> bool: ...

    async def advance_execution_round(
        self, run_id: RunId, logical_key: str
    ) -> ExecuteRoundResult: ...

    async def commit_verification(
        self, run_id: RunId, result: object, logical_key: str
    ) -> ActorPhaseReceipt: ...

    async def commit_report(
        self, run_id: RunId, result: object, logical_key: str
    ) -> ActorPhaseReceipt: ...


class PlanStagePort(Protocol):
    async def run(self, run_id: RunId) -> ActorPhaseReceipt: ...


class DeterministicStagePort(Protocol):
    async def run(self, run_id: RunId) -> object: ...


@dataclass(frozen=True, slots=True)
class PlanAgentCompletion:
    record: AgentRun
    receipt: AgentRunReceipt
    result_object: CasObject
    plan_object: CasObject


class PlanAgentSessionPort(Protocol):
    agent_run: AgentRun
    inputs: PlanningInputs

    async def propose(self, feedback: tuple[object, ...]) -> PlanProposal: ...

    async def complete(
        self, proposal: PlanProposal, frozen_plan: FrozenPlan
    ) -> PlanAgentCompletion: ...


class PlanAgentSessionFactory(Protocol):
    async def get_or_create(self, run_id: RunId, logical_task_key: str) -> PlanAgentSessionPort: ...


class PlanOwnerPort(Protocol):
    async def accept_plan(
        self, run_id: RunId, completion: PlanAgentCompletion, frozen_plan: FrozenPlan
    ) -> ActorPhaseReceipt: ...

    async def has_receipt(self, run_id: RunId, receipt_key: str) -> bool: ...


class PlanProposalRejected(RuntimeError):
    """The fixed number of M-07 validation feedback attempts was exhausted."""


class PlanProposalWorkflow:
    """Run one persistent PLAN AgentRun through deterministic M-07 validation."""

    feedback_limit = 3

    def __init__(
        self,
        *,
        sessions: PlanAgentSessionFactory,
        validator: PlanValidator,
        ledger: PlanLedger,
        owner: PlanOwnerPort,
    ) -> None:
        self.sessions = sessions
        self.validator = validator
        self.ledger = ledger
        self.owner = owner

    async def run(self, run_id: RunId) -> ActorPhaseReceipt:
        logical_task_key = f"plan:{run_id}"
        session = await self.sessions.get_or_create(run_id, logical_task_key)
        agent_run = session.agent_run
        if (
            agent_run.owner_kind != "run"
            or agent_run.owner_id != run_id
            or agent_run.phase is not Phase.Plan
            or agent_run.logical_task_key != logical_task_key
        ):
            raise ValueError("PLAN AgentRun identity does not match its Run owner")
        feedback: tuple[object, ...] = ()
        for attempt in range(self.feedback_limit + 1):
            proposal = await session.propose(feedback)
            validation = self.validator.validate(proposal, session.inputs)
            if validation.accepted:
                frozen_plan = self.ledger.freeze(proposal, session.inputs)
                completion = await session.complete(proposal, frozen_plan)
                self._validate_completion(agent_run, completion)
                receipt = await self.owner.accept_plan(run_id, completion, frozen_plan)
                if receipt.run_id != run_id or not await self.owner.has_receipt(
                    run_id, receipt.receipt_key
                ):
                    raise ValueError("PLAN cannot advance without its committed owner receipt")
                return receipt
            feedback = tuple(validation.violations)
            if attempt == self.feedback_limit:
                raise PlanProposalRejected("PLAN validation feedback limit exhausted")
        raise AssertionError("unreachable PLAN feedback loop")

    @staticmethod
    def _validate_completion(created: AgentRun, completion: PlanAgentCompletion) -> None:
        record = completion.record
        if (
            not created.same_creation_identity(record)
            or record.state is not SessionState.Closed
            or record.exit is not SessionExit.Completed
            or record.result_sha256 != completion.result_object.digest
            or completion.receipt.agent_run_id != record.agent_run_id
            or completion.receipt.category != "plan.accepted"
        ):
            raise ValueError("PLAN AgentRun completion does not match the accepted proposal")


class RunWorkflowGraph:
    """Advance deterministic Run phases only after the RunActor has a receipt."""

    def __init__(
        self,
        *,
        actor: RunGraphActorPort,
        planner: PlanStagePort,
        verifier: DeterministicStagePort,
        reporter: DeterministicStagePort,
        checkpointer: BaseCheckpointSaver[Any],
    ) -> None:
        self.actor = actor
        self.planner = planner
        self.verifier = verifier
        self.reporter = reporter
        self.checkpointer = checkpointer
        builder = StateGraph(_RunState)
        builder.add_node("plan", self._plan)
        builder.add_node("execute", self._execute)
        builder.add_node("verify", self._verify)
        builder.add_node("report", self._report)
        builder.set_entry_point("plan")
        builder.add_edge("plan", "execute")
        builder.add_conditional_edges(
            "execute",
            self._after_execute,
            {"execute": "execute", "verify": "verify"},
        )
        builder.add_edge("verify", "report")
        builder.add_edge("report", END)
        self._graph = builder.compile(checkpointer=checkpointer)

    async def start(self, receipt: RunCreatedReceipt) -> dict[str, object]:
        run_id = receipt.run_id
        if not await self.actor.has_receipt(run_id, receipt.receipt_key):
            raise ValueError("RunWorkflowGraph requires a persisted RunCreated receipt")
        config: RunnableConfig = {"configurable": {"thread_id": str(run_id)}}
        checkpoint = await self.checkpointer.aget_tuple(config)
        if checkpoint is not None:
            return cast(
                dict[str, object],
                await self._graph.ainvoke(None, config=config, version="v1"),
            )
        initial: _RunState = {
            "run_id": str(run_id),
            "phase": "PLAN",
            "receipt_key": receipt.receipt_key,
            "execute_round": 0,
            "execution_complete": False,
        }
        return cast(
            dict[str, object],
            await self._graph.ainvoke(initial, config=config, version="v1"),
        )

    async def _plan(self, state: _RunState) -> _RunState:
        run_id = RunId(UUID(state["run_id"]))
        logical_key = f"run.plan.accepted:{run_id}"
        if await self.actor.has_receipt(run_id, logical_key):
            return {"phase": "EXECUTE", "receipt_key": logical_key}
        receipt = await self.planner.run(run_id)
        if (
            receipt.run_id != run_id
            or receipt.receipt_key != logical_key
            or not await self.actor.has_receipt(run_id, receipt.receipt_key)
        ):
            raise ValueError("PLAN cannot advance without its committed owner receipt")
        return {"phase": "EXECUTE", "receipt_key": receipt.receipt_key}

    async def _execute(self, state: _RunState) -> _RunState:
        run_id = RunId(UUID(state["run_id"]))
        round_index = state.get("execute_round", 0)
        logical_key = f"run.execute.round:{run_id}:{round_index}"
        result = await self.actor.advance_execution_round(run_id, logical_key)
        if result.receipt.run_id != run_id or result.receipt.receipt_key != logical_key:
            raise ValueError("EXECUTE returned a mismatched actor receipt")
        if not await self.actor.has_receipt(run_id, logical_key):
            raise ValueError("EXECUTE cannot advance without its committed actor receipt")
        next_phase = "VERIFY" if result.complete else "EXECUTE"
        return {
            "phase": next_phase,
            "receipt_key": result.receipt.receipt_key,
            "execute_round": round_index + 1,
            "execution_complete": result.complete,
        }

    async def _verify(self, state: _RunState) -> _RunState:
        run_id = RunId(UUID(state["run_id"]))
        logical_key = f"run.verify.completed:{run_id}"
        if await self.actor.has_receipt(run_id, logical_key):
            return {"phase": "REPORT", "receipt_key": logical_key}
        result = await self.verifier.run(run_id)
        receipt = await self.actor.commit_verification(run_id, result, logical_key)
        if receipt.run_id != run_id or receipt.receipt_key != logical_key:
            raise ValueError("VERIFY returned a mismatched actor receipt")
        if not await self.actor.has_receipt(run_id, logical_key):
            raise ValueError("VERIFY cannot advance without its committed actor receipt")
        return {"phase": "REPORT", "receipt_key": logical_key}

    async def _report(self, state: _RunState) -> _RunState:
        run_id = RunId(UUID(state["run_id"]))
        logical_key = f"run.report.completed:{run_id}"
        if await self.actor.has_receipt(run_id, logical_key):
            return {"phase": "DONE", "receipt_key": logical_key}
        result = await self.reporter.run(run_id)
        receipt = await self.actor.commit_report(run_id, result, logical_key)
        if receipt.run_id != run_id or receipt.receipt_key != logical_key:
            raise ValueError("REPORT returned a mismatched actor receipt")
        if not await self.actor.has_receipt(run_id, logical_key):
            raise ValueError("REPORT cannot complete without its committed actor receipt")
        return {"phase": "DONE", "receipt_key": logical_key}

    @staticmethod
    def _after_execute(state: _RunState) -> str:
        return "verify" if state.get("execution_complete") else "execute"


__all__ = [
    "ActorPhaseReceipt",
    "ExecuteRoundResult",
    "RunWorkflowGraph",
]

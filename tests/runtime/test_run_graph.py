from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from codemigrator.runtime.contracts import RunCreatedReceipt
from codemigrator.runtime.run_graph import (
    ActorPhaseReceipt,
    PlanProposalWorkflow,
    RunWorkflowGraph,
)


@dataclass(frozen=True)
class RoundResult:
    receipt: ActorPhaseReceipt
    complete: bool


class RecordingCheckpointer(InMemorySaver):
    def __init__(self) -> None:
        super().__init__()
        self.thread_ids: set[str] = set()

    async def aput(self, config, checkpoint, metadata, new_versions):
        self.thread_ids.add(config["configurable"]["thread_id"])
        return await super().aput(config, checkpoint, metadata, new_versions)


class FakeActor:
    def __init__(self, run_id, *, execute_rounds: int = 2) -> None:
        self.run_id = run_id
        self.receipts = {f"run.created:{run_id}"}
        self.round_limit = execute_rounds
        self.round_keys: list[str] = []
        self.verifications: list[str] = []
        self.reports: list[str] = []

    async def has_receipt(self, run_id, receipt_key: str) -> bool:
        return run_id == self.run_id and receipt_key in self.receipts

    async def advance_execution_round(self, run_id, logical_key: str) -> RoundResult:
        self.round_keys.append(logical_key)
        self.receipts.add(logical_key)
        return RoundResult(
            ActorPhaseReceipt(run_id, logical_key, len(self.round_keys)),
            complete=len(self.round_keys) >= self.round_limit,
        )

    async def commit_verification(self, run_id, result, logical_key: str) -> ActorPhaseReceipt:
        self.verifications.append(logical_key)
        self.receipts.add(logical_key)
        return ActorPhaseReceipt(run_id, logical_key, len(self.receipts))

    async def commit_report(self, run_id, result, logical_key: str) -> ActorPhaseReceipt:
        self.reports.append(logical_key)
        self.receipts.add(logical_key)
        return ActorPhaseReceipt(run_id, logical_key, len(self.receipts))


class FakePlanner:
    def __init__(
        self,
        actor: FakeActor,
        *,
        persist: bool = True,
        fail_after_persist: bool = False,
    ) -> None:
        self.actor = actor
        self.persist = persist
        self.fail_after_persist = fail_after_persist
        self.calls = 0

    async def run(self, run_id) -> ActorPhaseReceipt:
        self.calls += 1
        key = f"run.plan.accepted:{run_id}"
        if self.persist:
            self.actor.receipts.add(key)
        if self.fail_after_persist:
            self.fail_after_persist = False
            raise RuntimeError("graph cursor checkpoint did not commit")
        return ActorPhaseReceipt(run_id, key, 2)


class DeterministicService:
    def __init__(self, result: str) -> None:
        self.result = result
        self.calls: list[str] = []

    async def run(self, run_id) -> str:
        self.calls.append(str(run_id))
        return self.result


class FakePlanSession:
    def __init__(self, run_id, responses) -> None:
        from codemigrator.core import Phase, SessionKind
        from codemigrator.runtime.agent_runs import AgentRun, AgentRunId

        self.agent_run = AgentRun(
            agent_run_id=AgentRunId(uuid4()),
            owner_kind="run",
            owner_id=run_id,
            logical_task_key=f"plan:{run_id}",
            phase=Phase.Plan,
            session_kind=SessionKind.PlanAuxiliary,
            thread_id=str(uuid4()),
            model_binding_sha256="a" * 64,
            context_sha256="b" * 64,
            toolset_sha256="c" * 64,
            template_sha256="d" * 64,
        )
        self.inputs = object()
        self.responses = list(responses)
        self.feedback: list[tuple[object, ...]] = []
        self.identities: list[tuple[object, str]] = []
        self.completed = []

    async def propose(self, feedback):
        self.feedback.append(tuple(feedback))
        self.identities.append((self.agent_run.agent_run_id, self.agent_run.thread_id))
        return self.responses.pop(0)

    async def complete(self, proposal, frozen_plan):
        from dataclasses import replace

        from codemigrator.runtime.agent_runs import AgentRunReceipt
        from codemigrator.runtime.cas import CasObject
        from codemigrator.runtime.loop_contracts import SessionExit, SessionState
        from codemigrator.runtime.run_graph import PlanAgentCompletion

        self.completed.append(frozen_plan)
        digest = "e" * 64
        record = replace(
            self.agent_run,
            state=SessionState.Closed,
            exit=SessionExit.Completed,
            result_sha256=digest,
        )
        return PlanAgentCompletion(
            record=record,
            receipt=AgentRunReceipt(uuid4(), record.agent_run_id, "plan.accepted"),
            result_object=CasObject(digest=digest, size=42),
            plan_object=CasObject(digest=frozen_plan.plan_hash, size=128),
        )


class FakePlanSessionFactory:
    def __init__(self, session) -> None:
        self.session = session
        self.calls: list[tuple[object, str]] = []

    async def get_or_create(self, run_id, logical_task_key: str):
        self.calls.append((run_id, logical_task_key))
        return self.session


class FakePlanValidator:
    def __init__(self, accepted: list[bool]) -> None:
        self.accepted = list(accepted)
        self.calls: list[tuple[object, object]] = []

    def validate(self, proposal, inputs):
        self.calls.append((proposal, inputs))
        accepted = self.accepted.pop(0)
        return SimpleNamespace(
            accepted=accepted,
            violations=() if accepted else (f"violation-{len(self.calls)}",),
        )


class FakePlanLedger:
    def __init__(self) -> None:
        self.proposals = []

    def freeze(self, proposal, inputs):
        self.proposals.append(proposal)
        return SimpleNamespace(plan_hash=f"{len(self.proposals):064x}")


class FakePlanOwner:
    def __init__(self, *, persist: bool = True) -> None:
        self.persist = persist
        self.accepted = []
        self.receipts: set[str] = set()

    async def accept_plan(self, run_id, completion, frozen_plan):
        self.accepted.append((run_id, completion, frozen_plan))
        key = f"run.plan.accepted:{run_id}"
        if self.persist:
            self.receipts.add(key)
        return ActorPhaseReceipt(run_id, key, 2)

    async def has_receipt(self, run_id, receipt_key: str) -> bool:
        return receipt_key in self.receipts


@pytest.mark.asyncio
async def test_run_graph_requires_durable_run_created_receipt_before_start(run_id) -> None:
    actor = FakeActor(run_id)
    actor.receipts.clear()
    planner = FakePlanner(actor)
    graph = RunWorkflowGraph(
        actor=actor,
        planner=planner,
        verifier=DeterministicService("verified"),
        reporter=DeterministicService("report"),
        checkpointer=RecordingCheckpointer(),
    )

    with pytest.raises(ValueError, match="RunCreated receipt"):
        await graph.start(
            RunCreatedReceipt(run_id, f"run.created:{run_id}", event_sequence=1, state_version=1)
        )

    assert planner.calls == 0


@pytest.mark.asyncio
async def test_graph_waits_for_plan_owner_receipt_and_does_not_advance_on_missing_receipt(
    run_id,
) -> None:
    actor = FakeActor(run_id)
    planner = FakePlanner(actor, persist=False)
    verifier = DeterministicService("verified")
    reporter = DeterministicService("report")
    graph = RunWorkflowGraph(
        actor=actor,
        planner=planner,
        verifier=verifier,
        reporter=reporter,
        checkpointer=RecordingCheckpointer(),
    )

    with pytest.raises(ValueError, match="owner receipt"):
        await graph.start(
            RunCreatedReceipt(run_id, f"run.created:{run_id}", event_sequence=1, state_version=1)
        )

    assert actor.round_keys == []
    assert verifier.calls == reporter.calls == []


@pytest.mark.asyncio
async def test_graph_delegates_each_execute_round_to_actor_and_uses_run_id_thread(run_id) -> None:
    actor = FakeActor(run_id, execute_rounds=3)
    planner = FakePlanner(actor)
    verifier = DeterministicService("verified")
    reporter = DeterministicService("report")
    saver = RecordingCheckpointer()
    graph = RunWorkflowGraph(
        actor=actor,
        planner=planner,
        verifier=verifier,
        reporter=reporter,
        checkpointer=saver,
    )

    state = await graph.start(
        RunCreatedReceipt(run_id, f"run.created:{run_id}", event_sequence=1, state_version=1)
    )

    assert planner.calls == 1
    assert len(actor.round_keys) == 3
    assert len(set(actor.round_keys)) == 3
    assert verifier.calls == [str(run_id)]
    assert reporter.calls == [str(run_id)]
    assert state["phase"] == "DONE"
    assert saver.thread_ids == {str(run_id)}
    replay = await graph.start(
        RunCreatedReceipt(run_id, f"run.created:{run_id}", event_sequence=1, state_version=1)
    )
    assert replay["phase"] == "DONE"
    assert len(actor.round_keys) == 3
    assert verifier.calls == reporter.calls == [str(run_id)]


@pytest.mark.asyncio
async def test_plan_feedback_reuses_one_agent_run_and_caps_retries_at_three(run_id) -> None:
    from codemigrator.core.models.plan import PlanProposal

    proposals = [PlanProposal.model_construct() for _ in range(4)]
    session = FakePlanSession(run_id, proposals)
    factory = FakePlanSessionFactory(session)
    validator = FakePlanValidator([False, False, False, True])
    ledger = FakePlanLedger()
    owner = FakePlanOwner()
    workflow = PlanProposalWorkflow(
        sessions=factory,
        validator=validator,
        ledger=ledger,
        owner=owner,
    )

    receipt = await workflow.run(run_id)

    assert receipt.receipt_key == f"run.plan.accepted:{run_id}"
    assert factory.calls == [(run_id, f"plan:{run_id}")]
    assert len(session.feedback) == 4
    assert [len(feedback) for feedback in session.feedback] == [0, 1, 1, 1]
    assert len(set(session.identities)) == 1
    assert len(ledger.proposals) == len(owner.accepted) == 1


@pytest.mark.asyncio
async def test_plan_is_not_accepted_without_owner_receipt(run_id) -> None:
    from codemigrator.core.models.plan import PlanProposal

    session = FakePlanSession(run_id, [PlanProposal.model_construct()])
    owner = FakePlanOwner(persist=False)
    workflow = PlanProposalWorkflow(
        sessions=FakePlanSessionFactory(session),
        validator=FakePlanValidator([True]),
        ledger=FakePlanLedger(),
        owner=owner,
    )

    with pytest.raises(ValueError, match="owner receipt"):
        await workflow.run(run_id)

    assert len(owner.accepted) == 1


@pytest.mark.asyncio
async def test_plan_feedback_exhaustion_stops_after_initial_plus_three_proposals(run_id) -> None:
    from codemigrator.core.models.plan import PlanProposal
    from codemigrator.runtime.run_graph import PlanProposalRejected

    session = FakePlanSession(run_id, [PlanProposal.model_construct() for _ in range(4)])
    owner = FakePlanOwner()
    ledger = FakePlanLedger()
    workflow = PlanProposalWorkflow(
        sessions=FakePlanSessionFactory(session),
        validator=FakePlanValidator([False, False, False, False]),
        ledger=ledger,
        owner=owner,
    )

    with pytest.raises(PlanProposalRejected):
        await workflow.run(run_id)

    assert len(session.feedback) == 4
    assert ledger.proposals == []
    assert owner.accepted == []


@pytest.mark.asyncio
async def test_committed_plan_receipt_repairs_missing_graph_cursor_without_replanning(run_id):
    actor = FakeActor(run_id, execute_rounds=1)
    planner = FakePlanner(actor, fail_after_persist=True)
    graph = RunWorkflowGraph(
        actor=actor,
        planner=planner,
        verifier=DeterministicService("verified"),
        reporter=DeterministicService("report"),
        checkpointer=RecordingCheckpointer(),
    )
    run_receipt = RunCreatedReceipt(
        run_id, f"run.created:{run_id}", event_sequence=1, state_version=1
    )

    with pytest.raises(RuntimeError, match="cursor checkpoint"):
        await graph.start(run_receipt)
    assert await actor.has_receipt(run_id, f"run.plan.accepted:{run_id}")

    state = await graph.start(run_receipt)

    assert state["phase"] == "DONE"
    assert planner.calls == 1
    assert len(actor.round_keys) == 1

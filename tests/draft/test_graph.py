from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from codemigrator.core import (
    CreateRun,
    Phase,
    SessionKind,
)
from codemigrator.core.ids import new_uuid7
from codemigrator.runtime.agent_runs import AgentRun, AgentRunId, AgentRunReceipt
from codemigrator.runtime.cas import CasLedger, FileHostCAS
from codemigrator.runtime.checkpointer import CasCheckpointSaver
from codemigrator.runtime.contracts import RunCreatedReceipt
from codemigrator.runtime.create_run import CreateRunRejected, CreateRunService
from codemigrator.runtime.draft import DraftConflictError, DraftFlow
from codemigrator.runtime.draft_graph import (
    DraftAgentCompletion,
    DraftFlowOwner,
    MigrationSessionGraph,
    coordinator_task_key,
    exploration_task_key,
    trial_translation_task_key,
)
from codemigrator.runtime.draft_models import (
    AskUserAnswer,
    AskUserQuestion,
    ExplorationReport,
    QuestionOption,
)
from codemigrator.runtime.store import InMemoryRuntimeStore


def _flow(artifacts) -> tuple[DraftFlow, AskUserQuestion]:
    flow = DraftFlow()
    flow.submit_report(
        ExplorationReport(
            domain_path="src",
            anchors=[
                {
                    "file_path": "src/a.py",
                    "start": {"line": 1, "column": 0},
                    "end": {"line": 1, "column": 5},
                }
            ],
            coverage=["src/a.py"],
            confidence_reason="The fixture has one source file.",
        )
    )
    flow.finish_exploration(["src/a.py"])
    revision = flow.seed_artifacts(artifacts)
    question = AskUserQuestion(
        revision_id=revision.revision_id,
        prompt="Keep the domain boundary?",
        options=(
            QuestionOption(
                key="keep",
                label="Keep",
                impact="Preserves the source boundary.",
                recommended=True,
            ),
            QuestionOption(
                key="merge",
                label="Merge",
                impact="Broadens the source boundary.",
                recommended=False,
            ),
        ),
    )
    return flow, question


def _confirm(flow: DraftFlow):
    flow.finalize_alignment()
    flow.begin_calibration()
    paths = ["src/a.py", "src/b.py"]
    flow.trial_translate(
        paths,
        {path: "constrained" for path in paths},
        {path: "freeform" for path in paths},
    )
    return flow.confirm()


def _create_request(frozen_artifacts, *, branch_prefix: str = "migration") -> CreateRun:
    return CreateRun.model_construct(
        source=None,
        branch_prefix=branch_prefix,
        frozen_artifacts=frozen_artifacts,
    )


def _savers(tmp_path: Path, store: InMemoryRuntimeStore, draft_id):
    cas = FileHostCAS(tmp_path / "cas")
    draft = CasCheckpointSaver(
        cas, store, graph_family="draft", owner_kind="draft", owner_id=draft_id
    )
    agent = CasCheckpointSaver(
        cas, store, graph_family="agent", owner_kind="draft", owner_id=draft_id
    )
    return cas, draft, agent


def _agent_run(draft_id, logical_task_key: str, *, thread_id: str | None = None) -> AgentRun:
    digest = "a" * 64
    return AgentRun(
        agent_run_id=AgentRunId(new_uuid7()),
        owner_kind="draft",
        owner_id=draft_id,
        logical_task_key=logical_task_key,
        phase=Phase.Plan,
        session_kind=SessionKind.ExploreCoordinator,
        thread_id=thread_id or str(new_uuid7()),
        model_binding_sha256=digest,
        context_sha256="b" * 64,
        toolset_sha256="c" * 64,
        template_sha256="d" * 64,
    )


@pytest.mark.asyncio
async def test_draft_graph_interrupts_and_resumes_after_durable_answer_receipt(
    tmp_path, artifacts
) -> None:
    store = InMemoryRuntimeStore()
    draft_id = new_uuid7()
    flow, question = _flow(artifacts)
    cas, draft_saver, agent_saver = _savers(tmp_path, store, draft_id)
    graph = MigrationSessionGraph(
        owner=DraftFlowOwner(draft_id=draft_id, flow=flow, store=store),
        agent_runs=store,
        checkpointer=draft_saver,
        agent_checkpointer=agent_saver,
        thread_id=str(new_uuid7()),
    )

    question_receipt = await graph.ask_user(question)
    question_events = await store.read_draft_session_events(draft_id, 0)
    assert [(event.event_type, event.data) for event in question_events] == [
        ("session.question.asked", {"question_id": str(question.question_id)})
    ]
    paused = await graph._graph.aget_state(graph.config)
    assert paused is not None
    assert paused.values["draft_id"] == str(draft_id)
    assert paused.values["question_id"] == str(question.question_id)
    assert paused.next == ("ask_user",)
    assert paused.tasks and paused.tasks[0].interrupts
    assert question_receipt.receipt_key == f"draft.question:{question.question_id}"
    assert question.prompt not in repr(paused.values)
    assert graph.thread_id != str(draft_id)
    assert (await store.list_checkpoint_indexes(graph.thread_id))[0].graph_family == "draft"

    restarted_draft_saver = CasCheckpointSaver(
        cas, store, graph_family="draft", owner_kind="draft", owner_id=draft_id
    )
    resumed_graph = MigrationSessionGraph(
        owner=DraftFlowOwner(draft_id=draft_id, flow=flow, store=store),
        agent_runs=store,
        checkpointer=restarted_draft_saver,
        agent_checkpointer=agent_saver,
        thread_id=graph.thread_id,
    )
    replayed_question = await resumed_graph.ask_user(question)
    assert replayed_question == question_receipt
    assert len(flow.ledger.questions) == 1

    answer = AskUserAnswer(
        question_id=question.question_id,
        revision_id=question.revision_id,
        selected_option="keep",
    )
    answer_receipt = await resumed_graph.answer_user(answer)
    answer_events = await store.read_draft_session_events(draft_id, 1)
    assert [(event.sequence, event.event_type, event.data) for event in answer_events] == [
        (2, "session.question.answered", {"question_id": str(question.question_id)})
    ]
    completed = await resumed_graph._graph.aget_state(resumed_graph.config)
    assert completed is not None and completed.next == ()
    assert completed.values["answer_receipt_key"] == answer_receipt.receipt_key
    assert "keep" not in repr(completed.values)
    assert len(flow.ledger.answers) == 1
    assert await resumed_graph.ask_user(question) == question_receipt
    assert (await resumed_graph._graph.aget_state(resumed_graph.config)).next == ()
    assert await resumed_graph.answer_user(answer) == answer_receipt
    assert len(flow.ledger.answers) == 1


@pytest.mark.asyncio
async def test_draft_agent_runs_are_owner_scoped_reusable_and_thread_separate(
    tmp_path, artifacts
) -> None:
    store = InMemoryRuntimeStore()
    draft_id = new_uuid7()
    flow, _ = _flow(artifacts)
    _, draft_saver, agent_saver = _savers(tmp_path, store, draft_id)
    graph = MigrationSessionGraph(
        owner=DraftFlowOwner(draft_id=draft_id, flow=flow, store=store),
        agent_runs=store,
        checkpointer=draft_saver,
        agent_checkpointer=agent_saver,
        thread_id=str(new_uuid7()),
    )
    first_domain = _agent_run(draft_id, exploration_task_key("src/a"))
    replay_domain = _agent_run(draft_id, exploration_task_key("src/a"))
    second_domain = _agent_run(draft_id, exploration_task_key("src/b"))
    coordinator = _agent_run(draft_id, coordinator_task_key())
    trial = _agent_run(draft_id, trial_translation_task_key("src/a.py"))

    stored_first = await graph.get_or_create_agent_run(first_domain)
    assert await graph.get_or_create_agent_run(replay_domain) == stored_first
    stored_records = [
        await graph.get_or_create_agent_run(record)
        for record in (second_domain, coordinator, trial)
    ]

    assert len({record.agent_run_id for record in [stored_first, *stored_records]}) == 4
    assert all(
        record.owner_kind == "draft" and record.owner_id == draft_id
        for record in [stored_first, *stored_records]
    )
    assert all(record.thread_id != graph.thread_id for record in [stored_first, *stored_records])
    assert len(await store.list_agent_runs_by_owner("draft", draft_id)) == 4


@pytest.mark.asyncio
async def test_draft_exploration_coordinator_and_trial_use_committed_agentruns(
    tmp_path, artifacts
) -> None:
    from dataclasses import replace

    from codemigrator.runtime.loop_contracts import SessionExit, SessionState

    class Runner:
        def __init__(self, store, owner_id) -> None:
            self.store = store
            self.owner_id = owner_id
            self.keys: list[str] = []

        async def run(self, draft_id, logical_task_key, task):
            assert draft_id == self.owner_id
            assert task
            self.keys.append(logical_task_key)
            category = {
                "draft.explore:": "draft.exploration.completed",
                "draft.explore.coordinator": "draft.coordinator.completed",
                "draft.trial:": "draft.trial.completed",
            }
            receipt_category = next(
                value for prefix, value in category.items() if logical_task_key.startswith(prefix)
            )
            candidate = _agent_run(draft_id, logical_task_key)
            created = await self.store.create_or_get_agent_run(candidate)
            terminal = replace(
                created,
                state=SessionState.Closed,
                exit=SessionExit.Completed,
                result_sha256="e" * 64,
            )
            receipt = AgentRunReceipt(uuid4(), terminal.agent_run_id, receipt_category)
            persisted = await self.store.commit_agent_run_receipt(terminal, receipt)
            assert persisted == receipt
            return DraftAgentCompletion(terminal, receipt, {"task_key": logical_task_key})

    store = InMemoryRuntimeStore()
    draft_id = new_uuid7()
    flow, _ = _flow(artifacts)
    _, draft_saver, agent_saver = _savers(tmp_path, store, draft_id)
    runner = Runner(store, draft_id)
    graph = MigrationSessionGraph(
        owner=DraftFlowOwner(draft_id=draft_id, flow=flow, store=store),
        agent_runs=store,
        checkpointer=draft_saver,
        agent_checkpointer=agent_saver,
        agent_runner=runner,
    )

    await graph.explore_domain("src/a", "inspect domain a")
    await graph.explore_domain("src/b", "inspect domain b")
    await graph.coordinate_exploration("merge domain reports")
    await graph.trial_translate("src/a.py", "compare translation approaches")

    assert runner.keys == [
        exploration_task_key("src/a"),
        exploration_task_key("src/b"),
        coordinator_task_key(),
        trial_translation_task_key("src/a.py"),
    ]
    assert len(await store.list_agent_runs_by_owner("draft", draft_id)) == 4


@pytest.mark.asyncio
async def test_close_releases_draft_checkpoint_only_after_last_cas_reference(
    tmp_path, artifacts
) -> None:
    store = InMemoryRuntimeStore()
    draft_id = new_uuid7()
    flow, question = _flow(artifacts)
    cas, draft_saver, agent_saver = _savers(tmp_path, store, draft_id)
    graph = MigrationSessionGraph(
        owner=DraftFlowOwner(draft_id=draft_id, flow=flow, store=store),
        agent_runs=store,
        checkpointer=draft_saver,
        agent_checkpointer=agent_saver,
        thread_id=str(new_uuid7()),
    )
    await graph.ask_user(question)
    agent_run = await graph.get_or_create_agent_run(
        _agent_run(draft_id, exploration_task_key("src/a"))
    )
    agent_thread = agent_run.thread_id
    await agent_saver.aput(
        {"configurable": {"thread_id": agent_thread, "checkpoint_ns": ""}},
        {
            "v": 4,
            "ts": "2026-09-29T00:00:00+00:00",
            "id": str(new_uuid7()),
            "channel_values": {"value": "private"},
            "channel_versions": {"value": 1},
            "versions_seen": {},
        },
        {"source": "input", "step": 0},
        {"value": 1},
    )
    checkpoint = (await store.list_checkpoint_indexes(agent_thread))[0].object
    shared_owner = new_uuid7()
    await store.add_cas_reference(checkpoint, "run", shared_owner, "held-by-run")

    closed_receipt = await graph.close()
    assert (await store.read_draft_session_events(draft_id, 0))[-1].event_type == "session.closed"
    assert (
        await store.is_draft_session_terminal(
            draft_id, (await store.read_draft_session_events(draft_id, 0))[-1].sequence
        )
        is True
    )

    assert await store.list_checkpoint_indexes(graph.thread_id) == ()
    assert await store.list_checkpoint_indexes(agent_thread) == ()
    assert cas.read(checkpoint) is not None
    assert await graph.close() == closed_receipt
    with pytest.raises(DraftConflictError, match="closed or attached"):
        await graph.get_or_create_agent_run(
            _agent_run(draft_id, exploration_task_key("src/after-close"))
        )
    with pytest.raises(DraftConflictError, match="closed or attached"):
        await graph.ask_user(question)
    assert await CasLedger(cas, store).release("run", shared_owner, "held-by-run")
    assert not cas.path_for(checkpoint.digest).exists()


@pytest.mark.asyncio
async def test_create_run_rejection_keeps_draft_without_run_side_effects(
    tmp_path, artifacts
) -> None:
    class RejectingPreflight:
        async def verify_descriptor_lock(self, request) -> None:
            raise CreateRunRejected("descriptor lock rejected")

        async def verify_preindex(self, request) -> None:
            raise AssertionError("preindex must not run after rejection")

        async def verify_dossier_consistency(self, request) -> None:
            raise AssertionError("dossier check must not run after rejection")

    class NoRunActor:
        calls = 0

        async def create(self, request):
            self.calls += 1
            raise AssertionError("CreateRun actor must not be called")

    class NoRunGraph:
        async def start(self, run_id, receipt) -> None:
            raise AssertionError("Run graph must not start")

    store = InMemoryRuntimeStore()
    draft_id = new_uuid7()
    flow, _ = _flow(artifacts)
    freeze = _confirm(flow)
    _, draft_saver, agent_saver = _savers(tmp_path, store, draft_id)
    actor = NoRunActor()
    service = CreateRunService(
        preflight=RejectingPreflight(), actor=actor, graph_starter=NoRunGraph()
    )
    graph = MigrationSessionGraph(
        owner=DraftFlowOwner(draft_id=draft_id, flow=flow, store=store),
        agent_runs=store,
        checkpointer=draft_saver,
        agent_checkpointer=agent_saver,
        create_run_service=service,
    )
    run_id = new_uuid7()

    with pytest.raises(DraftConflictError, match="confirmed Draft artifact freeze"):
        await graph.attach_to_run(run_id, _create_request(None))
    with pytest.raises(CreateRunRejected):
        await graph.attach_to_run(run_id, _create_request(freeze.frozen_artifact_bundle))

    assert actor.calls == 0
    assert await store.load_draft_owner_fact(draft_id, "draft.attached") is None
    assert await store.list_agent_runs_by_owner("draft", draft_id) == ()
    assert await store.load(run_id) is None


@pytest.mark.asyncio
async def test_successful_attach_commits_once_then_releases_draft_threads(
    tmp_path, artifacts
) -> None:
    class PassingPreflight:
        async def verify_descriptor_lock(self, request) -> None:
            return None

        async def verify_preindex(self, request) -> None:
            return None

        async def verify_dossier_consistency(self, request) -> None:
            return None

    class RecordingActor:
        calls = 0

        async def create(self, request):
            self.calls += 1
            return receipt

    class RecordingGraphStarter:
        calls = 0

        async def start(self, run_id, created_receipt) -> None:
            self.calls += 1

    store = InMemoryRuntimeStore()
    draft_id = new_uuid7()
    flow, question = _flow(artifacts)
    _, draft_saver, agent_saver = _savers(tmp_path, store, draft_id)
    run_id = new_uuid7()
    receipt = RunCreatedReceipt(
        run_id=run_id,
        receipt_key=f"run.created:{run_id}",
        event_sequence=1,
        state_version=1,
    )
    actor = RecordingActor()
    starter = RecordingGraphStarter()
    service = CreateRunService(preflight=PassingPreflight(), actor=actor, graph_starter=starter)
    graph = MigrationSessionGraph(
        owner=DraftFlowOwner(draft_id=draft_id, flow=flow, store=store),
        agent_runs=store,
        checkpointer=draft_saver,
        agent_checkpointer=agent_saver,
        create_run_service=service,
    )
    await graph.ask_user(question)
    answer = AskUserAnswer(
        question_id=question.question_id,
        revision_id=question.revision_id,
        selected_option="keep",
    )
    await graph.answer_user(answer)
    freeze = _confirm(flow)
    graph_indexes = await store.list_checkpoint_indexes(graph.thread_id)
    assert graph_indexes

    request = _create_request(freeze.frozen_artifact_bundle)
    attached = await graph.attach_to_run(run_id, request)
    assert (await store.read_draft_session_events(draft_id, 0))[-1].data == {"run_id": str(run_id)}

    assert attached == receipt
    assert actor.calls == starter.calls == 1
    assert await store.list_checkpoint_indexes(graph.thread_id) == ()
    assert await graph.attach_to_run(run_id, request) == receipt
    assert actor.calls == starter.calls == 1
    with pytest.raises(DraftConflictError, match="changed the CreateRun request"):
        await graph.attach_to_run(
            run_id,
            _create_request(freeze.frozen_artifact_bundle, branch_prefix="alternate"),
        )
    with pytest.raises(DraftConflictError, match="another Run"):
        await graph.attach_to_run(new_uuid7(), request)
    with pytest.raises(DraftConflictError, match="attached Draft"):
        await graph.close()

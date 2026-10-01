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
from codemigrator.runtime.store import InMemoryRuntimeStore, StoreCommitError


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


def _exploration_result_bytes(domain_path: str, file_path: str) -> bytes:
    import json

    return json.dumps(
        {
            "domain_path": domain_path,
            "anchors": [
                {
                    "file_path": file_path,
                    "start": {"line": 1, "column": 0},
                    "end": {"line": 1, "column": 6},
                }
            ],
            "coverage": [file_path],
            "confidence_reason": "The fixture has one source file.",
            "unresolved_conflict_count": 0,
        }
    ).encode("utf-8")


def _coordinator_result_bytes(domain_path: str = "src") -> bytes:
    import json

    return json.dumps(
        {
            "op": "refocus",
            "domain_paths": [domain_path],
            "reason_summary": "The existing domain report remains the correct focus.",
            "focus_brief": {
                "domain_paths": [domain_path],
                "highlights": [],
                "budget_hint": "Inspect the reported domain.",
            },
        }
    ).encode("utf-8")


def _trial_result_bytes(file_path: str) -> bytes:
    import json

    return json.dumps(
        {
            "file_path": file_path,
            "constrained_output": "constrained translation",
            "freeform_output": "freeform translation",
            "profile": "CODE",
            "discarded": True,
        }
    ).encode("utf-8")


def _exploration_merge_bytes(reports: list[dict[str, object]]) -> bytes:
    import json

    return json.dumps(
        {
            "reports": reports,
            "coverage": {
                "valid": True,
                "missing_files": [],
                "duplicate_files": [],
                "unknown_files": [],
            },
            "unresolved_conflict_count": 0,
        }
    ).encode("utf-8")


def _split_reassignment_bytes() -> bytes:
    import json

    return json.dumps(
        {
            "op": "split",
            "domain_paths": ["src/a"],
            "reason_summary": "Separate the second source domain for coverage.",
            "focus_brief": {
                "domain_paths": ["src/a", "src/b"],
                "highlights": [],
                "budget_hint": "Inspect each source domain once.",
            },
        }
    ).encode("utf-8")


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
async def test_draft_graph_restore_rebuilds_owner_ledger_and_interrupt_after_restart(
    tmp_path, artifacts
) -> None:
    store = InMemoryRuntimeStore()
    draft_id = new_uuid7()
    flow, question = _flow(artifacts)
    cas, draft_saver, agent_saver = _savers(tmp_path, store, draft_id)
    first_graph = MigrationSessionGraph(
        owner=DraftFlowOwner(draft_id=draft_id, flow=flow, store=store),
        agent_runs=store,
        checkpointer=draft_saver,
        agent_checkpointer=agent_saver,
        thread_id=str(new_uuid7()),
    )
    await first_graph.ask_user(question)

    restarted_flow = DraftFlow()
    restarted_graph = MigrationSessionGraph(
        owner=DraftFlowOwner(draft_id=draft_id, flow=restarted_flow, store=store),
        agent_runs=store,
        checkpointer=CasCheckpointSaver(
            cas, store, graph_family="draft", owner_kind="draft", owner_id=draft_id
        ),
        agent_checkpointer=CasCheckpointSaver(
            cas, store, graph_family="agent", owner_kind="draft", owner_id=draft_id
        ),
        thread_id=first_graph.thread_id,
    )

    snapshot = await restarted_graph.restore()

    assert snapshot is not None
    assert snapshot.next == ("ask_user",)
    assert snapshot.values["question_id"] == str(question.question_id)
    assert restarted_flow.ledger.current_revision == flow.ledger.current_revision
    assert restarted_flow.ledger.questions == (question,)


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
    trial = _agent_run(
        draft_id,
        trial_translation_task_key("src/a.py", flow.ledger.current_revision.revision_id),
    )

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
async def test_trial_group_waits_for_every_selected_file_and_reuses_partial_results(
    tmp_path, artifacts
) -> None:
    from dataclasses import replace

    from codemigrator.runtime.loop_contracts import SessionExit, SessionState

    paths = ("src/a.py", "src/b.py", "src/c.py")
    class Runner:
        def __init__(self) -> None:
            self.calls: dict[str, int] = {}

        async def run(self, draft_id, logical_task_key, task, *, lifecycle):
            self.calls[logical_task_key] = self.calls.get(logical_task_key, 0) + 1
            created = await store.create_or_get_agent_run(
                _agent_run(draft_id, logical_task_key)
            )
            await lifecycle.started(created)
            if logical_task_key == trial_translation_task_key("src/c.py", revision_id) and (
                self.calls[logical_task_key] == 1
            ):
                raise RuntimeError("third file is temporarily unavailable")
            result_reference = await CasLedger(cas, store).put(
                _trial_result_bytes(task_keys[logical_task_key]),
                "draft",
                draft_id,
                f"agent-result:{created.agent_run_id}",
            )
            terminal = replace(
                created,
                state=SessionState.Closed,
                exit=SessionExit.Completed,
                result_sha256=result_reference.digest,
            )
            receipt = AgentRunReceipt(
                uuid4(), terminal.agent_run_id, "draft.trial.completed"
            )
            await store.commit_agent_run_receipt(terminal, receipt)
            await lifecycle.terminal(terminal, receipt)
            return DraftAgentCompletion(terminal, receipt, result_reference)

    store = InMemoryRuntimeStore()
    draft_id = new_uuid7()
    flow, _ = _flow(artifacts)
    flow.finalize_alignment()
    flow.begin_calibration()
    revision_id = flow.ledger.current_revision.revision_id
    task_keys = {trial_translation_task_key(path, revision_id): path for path in paths}
    owner = DraftFlowOwner(draft_id=draft_id, flow=flow, store=store)
    cas, draft_saver, agent_saver = _savers(tmp_path, store, draft_id)
    runner = Runner()
    graph = MigrationSessionGraph(
        owner=owner,
        agent_runs=store,
        checkpointer=draft_saver,
        agent_checkpointer=agent_saver,
        agent_runner=runner,
    )
    tasks_by_file = {path: f"translate {path}" for path in paths}
    risk_hotspots = ("src/c.py", "src/a.py", "src/b.py")

    with pytest.raises(RuntimeError, match="third file is temporarily unavailable"):
        await graph.trial_translate(risk_hotspots, tasks_by_file)
    with pytest.raises(DraftConflictError, match="completed trial translation"):
        flow.confirm()

    completions = await graph.trial_translate(risk_hotspots, tasks_by_file)

    assert tuple(item.materialized.file_path for item in completions) == paths
    assert runner.calls == {
        trial_translation_task_key("src/a.py", revision_id): 1,
        trial_translation_task_key("src/b.py", revision_id): 1,
        trial_translation_task_key("src/c.py", revision_id): 2,
    }
    for completion in completions:
        owner_fact = await owner.load_fact(
            f"draft.agent.result:{completion.record.agent_run_id}"
        )
        assert owner_fact is not None
        assert owner_fact[1]["trial_group_paths"] == list(paths)
    assert len(flow._trial_results) == 3

    previous_revision = flow.ledger.current_revision
    assert previous_revision is not None
    revised_blueprint = previous_revision.artifacts.target_project_blueprint.model_copy(
        update={"version": previous_revision.artifacts.target_project_blueprint.version + 1}
    )
    revised_artifacts = previous_revision.artifacts.model_copy(
        update={"target_project_blueprint": revised_blueprint}
    )
    next_revision = flow.revise_artifacts(revised_artifacts)
    assert trial_translation_task_key("src/a.py", previous_revision.revision_id) != (
        trial_translation_task_key("src/a.py", next_revision.revision_id)
    )
    await owner.persist_current_revision()
    await owner.restore_ledger()

    assert flow.ledger.current_revision == next_revision
    assert flow._trial_results == ()
    assert owner._trial_agent_results == {}


@pytest.mark.asyncio
async def test_coordinator_rounds_support_reassignment_then_merge(tmp_path) -> None:
    from dataclasses import replace

    from codemigrator.runtime.loop_contracts import SessionExit, SessionState

    class Runner:
        def __init__(self) -> None:
            self.calls: dict[str, int] = {}

        async def run(self, draft_id, logical_task_key, task, *, lifecycle):
            self.calls[logical_task_key] = self.calls.get(logical_task_key, 0) + 1
            created = await store.create_or_get_agent_run(
                _agent_run(draft_id, logical_task_key)
            )
            await lifecycle.started(created)
            if logical_task_key == exploration_task_key("src/a"):
                result_body = _exploration_result_bytes("src/a", "src/a/a.py")
            elif logical_task_key == exploration_task_key("src/b"):
                result_body = _exploration_result_bytes("src/b", "src/b/b.py")
            elif logical_task_key == coordinator_task_key(1):
                result_body = _split_reassignment_bytes()
            elif logical_task_key == coordinator_task_key(2):
                import json

                result_body = _exploration_merge_bytes(
                    [
                        json.loads(_exploration_result_bytes("src/a", "src/a/a.py")),
                        json.loads(_exploration_result_bytes("src/b", "src/b/b.py")),
                    ]
                )
            else:
                raise AssertionError(f"unexpected coordinator round: {logical_task_key}")
            result_reference = await CasLedger(cas, store).put(
                result_body,
                "draft",
                draft_id,
                f"agent-result:{created.agent_run_id}",
            )
            terminal = replace(
                created,
                state=SessionState.Closed,
                exit=SessionExit.Completed,
                result_sha256=result_reference.digest,
            )
            category = (
                "draft.exploration.completed"
                if logical_task_key.startswith("draft.explore:")
                else "draft.coordinator.completed"
            )
            receipt = AgentRunReceipt(uuid4(), terminal.agent_run_id, category)
            await store.commit_agent_run_receipt(terminal, receipt)
            await lifecycle.terminal(terminal, receipt)
            return DraftAgentCompletion(terminal, receipt, result_reference)

    store = InMemoryRuntimeStore()
    draft_id = new_uuid7()
    flow = DraftFlow(
        module_files={"src/a": ["src/a/a.py"], "src/b": ["src/b/b.py"]}
    )
    owner = DraftFlowOwner(draft_id=draft_id, flow=flow, store=store)
    cas, draft_saver, agent_saver = _savers(tmp_path, store, draft_id)
    runner = Runner()
    graph = MigrationSessionGraph(
        owner=owner,
        agent_runs=store,
        checkpointer=draft_saver,
        agent_checkpointer=agent_saver,
        agent_runner=runner,
    )

    await graph.explore_domain("src/a", "inspect first domain")
    reassignment = await graph.coordinate_exploration(
        "split the exploration", round_number=1
    )
    await graph.explore_domain("src/b", "inspect reassigned domain")
    merged = await graph.coordinate_exploration("merge complete reports", round_number=2)
    replayed = await graph.coordinate_exploration("split the exploration", round_number=1)

    assert reassignment.materialized is not None
    assert merged.materialized is not None
    assert merged.record.agent_run_id != reassignment.record.agent_run_id
    assert replayed.record == reassignment.record
    assert {
        key: count
        for key, count in runner.calls.items()
        if key.startswith("draft.explore.coordinator:")
    } == {
        coordinator_task_key(1): 1,
        coordinator_task_key(2): 1,
    }
    assert flow.merged_exploration is not None
    await owner.restore_ledger()
    assert flow.merged_exploration is not None


@pytest.mark.asyncio
async def test_exploration_result_is_materialized_by_draft_owner_after_receipt_gate(
    tmp_path,
) -> None:
    import json
    from dataclasses import replace

    from codemigrator.runtime.loop_contracts import SessionExit, SessionState

    report_body = {
        "domain_path": "src/a",
        "anchors": [
            {
                "file_path": "src/a/module.py",
                "start": {"line": 1, "column": 0},
                "end": {"line": 1, "column": 6},
            }
        ],
        "coverage": ["src/a/module.py"],
        "confidence_reason": "The module has one source file.",
        "unresolved_conflict_count": 0,
    }

    class Runner:
        async def run(self, draft_id, logical_task_key, task, *, lifecycle):
            created = await store.create_or_get_agent_run(_agent_run(draft_id, logical_task_key))
            await lifecycle.started(created)
            result_reference = await CasLedger(cas, store).put(
                json.dumps(report_body).encode("utf-8"),
                "draft",
                draft_id,
                f"agent-result:{created.agent_run_id}",
            )
            terminal = replace(
                created,
                state=SessionState.Closed,
                exit=SessionExit.Completed,
                result_sha256=result_reference.digest,
            )
            receipt = AgentRunReceipt(uuid4(), terminal.agent_run_id, "draft.exploration.completed")
            await store.commit_agent_run_receipt(terminal, receipt)
            await lifecycle.terminal(terminal, receipt)
            return DraftAgentCompletion(terminal, receipt, result_reference)

    store = InMemoryRuntimeStore()
    draft_id = new_uuid7()
    flow = DraftFlow(module_files={"src/a": ["src/a/module.py"]})
    owner = DraftFlowOwner(draft_id=draft_id, flow=flow, store=store)
    cas, draft_saver, agent_saver = _savers(tmp_path, store, draft_id)
    graph = MigrationSessionGraph(
        owner=owner,
        agent_runs=store,
        checkpointer=draft_saver,
        agent_checkpointer=agent_saver,
        agent_runner=Runner(),
    )

    completion = await graph.explore_domain("src/a", "inspect the module")

    assert completion.result is not None
    assert completion.materialized == ExplorationReport.model_validate(report_body)
    assert len(flow.reports) == 1
    assert flow.reports[0].domain_path == "src/a"
    assert flow.reports[0].coverage == ("src/a/module.py",)
    result_fact = await owner.load_fact(f"draft.agent.result:{completion.record.agent_run_id}")
    assert result_fact is not None
    assert result_fact[0].category == "draft.agent.result"
    assert result_fact[1]["result_sha256"] == completion.result.digest
    snapshot = await graph._graph.aget_state(graph.config)
    assert snapshot is not None
    assert snapshot.values["cursor"] == "DRAFT_AGENT_COMPLETED"
    assert "The module has one source file." not in repr(snapshot.values)
    assert "inspect the module" not in repr(snapshot.values)

    restored_flow = DraftFlow(module_files={"src/a": ["src/a/module.py"]})
    restored_owner = DraftFlowOwner(draft_id=draft_id, flow=restored_flow, store=store)
    await restored_owner.restore_ledger()
    assert restored_flow.reports == (completion.materialized,)


@pytest.mark.asyncio
async def test_malformed_agent_result_does_not_become_a_draft_owner_fact(tmp_path) -> None:
    from dataclasses import replace

    from codemigrator.runtime.draft_graph import DraftAgentResultInvalid
    from codemigrator.runtime.loop_contracts import SessionExit, SessionState

    class Runner:
        async def run(self, draft_id, logical_task_key, task, *, lifecycle):
            created = await store.create_or_get_agent_run(_agent_run(draft_id, logical_task_key))
            await lifecycle.started(created)
            result_reference = await CasLedger(cas, store).put(
                b"not a typed Draft result",
                "draft",
                draft_id,
                f"agent-result:{created.agent_run_id}",
            )
            terminal = replace(
                created,
                state=SessionState.Closed,
                exit=SessionExit.Completed,
                result_sha256=result_reference.digest,
            )
            receipt = AgentRunReceipt(uuid4(), terminal.agent_run_id, "draft.exploration.completed")
            await store.commit_agent_run_receipt(terminal, receipt)
            await lifecycle.terminal(terminal, receipt)
            return DraftAgentCompletion(terminal, receipt, result_reference)

    store = InMemoryRuntimeStore()
    draft_id = new_uuid7()
    owner = DraftFlowOwner(
        draft_id=draft_id,
        flow=DraftFlow(module_files={"src/a": ["src/a/module.py"]}),
        store=store,
    )
    cas, draft_saver, agent_saver = _savers(tmp_path, store, draft_id)
    graph = MigrationSessionGraph(
        owner=owner,
        agent_runs=store,
        checkpointer=draft_saver,
        agent_checkpointer=agent_saver,
        agent_runner=Runner(),
    )

    with pytest.raises(DraftAgentResultInvalid, match="typed Draft result"):
        await graph.explore_domain("src/a", "inspect the module")

    records = await store.list_agent_runs_by_owner("draft", draft_id)
    assert len(records) == 1
    assert await owner.load_fact(f"draft.agent.result:{records[0].agent_run_id}") is None
    assert owner.flow.reports == ()


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

        async def run(self, draft_id, logical_task_key, task, *, lifecycle):
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
            await lifecycle.started(created)
            assert (await self.store.read_draft_session_events(draft_id, 0))[
                -1
            ].event_type == "agent_run.started"
            if logical_task_key == exploration_task_key("src/a"):
                result_body = _exploration_result_bytes("src/a", "src/a/module.py")
            elif logical_task_key == exploration_task_key("src/b"):
                result_body = _exploration_result_bytes("src/b", "src/b/module.py")
            elif logical_task_key == coordinator_task_key():
                import json

                result_body = json.dumps(
                    {
                        "reports": [
                            json.loads(_exploration_result_bytes("src/a", "src/a/module.py")),
                            json.loads(_exploration_result_bytes("src/b", "src/b/module.py")),
                        ],
                        "coverage": {
                            "valid": True,
                            "missing_files": [],
                            "duplicate_files": [],
                            "unknown_files": [],
                        },
                        "unresolved_conflict_count": 0,
                    }
                ).encode("utf-8")
            elif logical_task_key == trial_translation_task_key(
                "src/a/module.py", flow.ledger.current_revision.revision_id
            ):
                result_body = _trial_result_bytes("src/a/module.py")
            else:
                result_body = _trial_result_bytes("src/b/module.py")
            result_reference = await CasLedger(cas, self.store).put(
                result_body,
                "draft",
                draft_id,
                f"agent-result:{created.agent_run_id}",
            )
            terminal = replace(
                created,
                state=SessionState.Closed,
                exit=SessionExit.Completed,
                result_sha256=result_reference.digest,
            )
            receipt = AgentRunReceipt(uuid4(), terminal.agent_run_id, receipt_category)
            persisted = await self.store.commit_agent_run_receipt(terminal, receipt)
            assert persisted == receipt
            await lifecycle.terminal(terminal, receipt)
            return DraftAgentCompletion(terminal, receipt, result_reference)

    store = InMemoryRuntimeStore()
    draft_id = new_uuid7()
    flow = DraftFlow(
        module_files={
            "src/a": ["src/a/module.py"],
            "src/b": ["src/b/module.py"],
        }
    )
    cas, draft_saver, agent_saver = _savers(tmp_path, store, draft_id)
    runner = Runner(store, draft_id)
    graph = MigrationSessionGraph(
        owner=DraftFlowOwner(draft_id=draft_id, flow=flow, store=store),
        agent_runs=store,
        checkpointer=draft_saver,
        agent_checkpointer=agent_saver,
        agent_runner=runner,
    )

    first = await graph.explore_domain("src/a", "inspect domain a")
    assert first.result.digest == first.record.result_sha256
    assert first.materialized is not None
    assert len(flow.reports) == 1
    assert (
        await store.get_cas_reference(
            "draft", draft_id, f"agent-result:{first.record.agent_run_id}"
        )
        == first.result
    )
    await graph.explore_domain("src/b", "inspect domain b")
    await graph.coordinate_exploration("merge domain reports")
    flow.seed_artifacts(artifacts)
    flow.finalize_alignment()
    flow.begin_calibration()
    events = await store.read_draft_session_events(draft_id, 0)
    assert [event.event_type for event in events] == [
        "agent_run.started",
        "agent_run.terminal",
        "agent_run.started",
        "agent_run.terminal",
        "agent_run.started",
        "agent_run.terminal",
    ]
    revision_id = flow.ledger.current_revision.revision_id
    await graph.trial_translate(
        ("src/a/module.py", "src/b/module.py"),
        {
            "src/a/module.py": "compare translation approaches",
            "src/b/module.py": "compare translation approaches",
        },
    )

    assert runner.keys == [
        exploration_task_key("src/a"),
        exploration_task_key("src/b"),
        coordinator_task_key(),
        trial_translation_task_key("src/a/module.py", revision_id),
        trial_translation_task_key("src/b/module.py", revision_id),
    ]
    assert len(await store.list_agent_runs_by_owner("draft", draft_id)) == 5


@pytest.mark.asyncio
async def test_draft_agent_lifecycle_recovery_replays_without_new_events(
    tmp_path, artifacts
) -> None:
    from dataclasses import replace

    from codemigrator.runtime.loop_contracts import SessionExit, SessionState

    class Runner:
        created = None
        terminal_record = None
        receipt = None
        result_reference = None
        provider_calls = 0

        async def run(self, draft_id, logical_task_key, task, *, lifecycle):
            if self.created is None:
                self.created = await store.create_or_get_agent_run(
                    _agent_run(draft_id, logical_task_key)
                )
            await lifecycle.started(self.created)
            if self.terminal_record is None:
                self.provider_calls += 1
                self.result_reference = await CasLedger(cas, store).put(
                    _coordinator_result_bytes(),
                    "draft",
                    draft_id,
                    f"agent-result:{self.created.agent_run_id}",
                )
                self.terminal_record = replace(
                    self.created,
                    state=SessionState.Closed,
                    exit=SessionExit.Completed,
                    result_sha256=self.result_reference.digest,
                )
                self.receipt = AgentRunReceipt(
                    uuid4(), self.created.agent_run_id, "draft.coordinator.completed"
                )
                await store.commit_agent_run_receipt(self.terminal_record, self.receipt)
            await lifecycle.terminal(self.terminal_record, self.receipt)
            return DraftAgentCompletion(self.terminal_record, self.receipt, self.result_reference)

    store = InMemoryRuntimeStore()
    draft_id = new_uuid7()
    flow, _ = _flow(artifacts)
    cas, draft_saver, agent_saver = _savers(tmp_path, store, draft_id)
    runner = Runner()
    graph = MigrationSessionGraph(
        owner=DraftFlowOwner(draft_id=draft_id, flow=flow, store=store),
        agent_runs=store,
        checkpointer=draft_saver,
        agent_checkpointer=agent_saver,
        agent_runner=runner,
    )
    first = await graph.coordinate_exploration("merge")
    events = await store.read_draft_session_events(draft_id, 0)
    recovered = await graph.coordinate_exploration("merge")
    assert recovered.record == first.record
    assert recovered.receipt == first.receipt
    assert await store.read_draft_session_events(draft_id, 0) == events
    assert [event.sequence for event in events] == [1, 2]
    assert runner.provider_calls == 1


@pytest.mark.asyncio
async def test_fresh_draft_runner_recovers_terminal_receipt_after_crash(
    tmp_path, artifacts
) -> None:
    from dataclasses import replace

    from codemigrator.runtime.loop_contracts import SessionExit, SessionState

    class CrashedRunner:
        calls = 0
        result_reference = None

        async def run(self, draft_id, logical_task_key, task, *, lifecycle):
            self.calls += 1
            created = await store.create_or_get_agent_run(_agent_run(draft_id, logical_task_key))
            await lifecycle.started(created)
            self.result_reference = await CasLedger(cas, store).put(
                _coordinator_result_bytes(),
                "draft",
                draft_id,
                f"agent-result:{created.agent_run_id}",
            )
            terminal = replace(
                created,
                state=SessionState.Closed,
                exit=SessionExit.Completed,
                result_sha256=self.result_reference.digest,
            )
            receipt = AgentRunReceipt(uuid4(), terminal.agent_run_id, "draft.coordinator.completed")
            await store.commit_agent_run_receipt(terminal, receipt)
            raise RuntimeError("process died before Draft checkpoint")

    class FreshRunner:
        calls = 0

        async def run(self, draft_id, logical_task_key, task, *, lifecycle):
            self.calls += 1
            raise AssertionError("provider/tool execution must not recur")

    store = InMemoryRuntimeStore()
    draft_id = new_uuid7()
    flow, _ = _flow(artifacts)
    cas, draft_saver, agent_saver = _savers(tmp_path, store, draft_id)
    owner = DraftFlowOwner(draft_id=draft_id, flow=flow, store=store)
    first_runner = CrashedRunner()
    first_graph = MigrationSessionGraph(
        owner=owner,
        agent_runs=store,
        checkpointer=draft_saver,
        agent_checkpointer=agent_saver,
        agent_runner=first_runner,
    )
    with pytest.raises(RuntimeError, match="process died"):
        await first_graph.coordinate_exploration("merge")
    before = await store.read_draft_session_events(draft_id, 0)
    assert [event.event_type for event in before] == ["agent_run.started"]
    persisted = (await store.list_agent_runs_by_owner("draft", draft_id))[0]
    receipt = await store.load_agent_run_receipt(persisted.agent_run_id)

    fresh_runner = FreshRunner()
    fresh_graph = MigrationSessionGraph(
        owner=owner,
        agent_runs=store,
        checkpointer=draft_saver,
        agent_checkpointer=agent_saver,
        agent_runner=fresh_runner,
        thread_id=first_graph.thread_id,
    )
    recovered = await fresh_graph.coordinate_exploration("merge")
    assert recovered.record == persisted
    assert recovered.receipt == receipt
    assert recovered.result == first_runner.result_reference
    assert recovered.result.digest == persisted.result_sha256
    assert fresh_runner.calls == 0
    assert first_runner.calls == 1
    events = await store.read_draft_session_events(draft_id, 0)
    assert [event.sequence for event in events] == [1, 2]
    assert [event.event_type for event in events] == ["agent_run.started", "agent_run.terminal"]
    terminal_fact = await owner.load_fact(f"draft.agent.terminal:{persisted.agent_run_id}")
    assert terminal_fact is not None
    assert terminal_fact[0].category == "draft.agent.terminal"
    assert terminal_fact[1] == {
        "agent_run_id": str(persisted.agent_run_id),
        "receipt_id": str(receipt.receipt_id),
    }
    replayed = await fresh_graph.coordinate_exploration("merge")
    assert replayed.receipt.receipt_id == receipt.receipt_id
    assert replayed.result == recovered.result
    assert await store.read_draft_session_events(draft_id, 0) == events


@pytest.mark.asyncio
async def test_terminal_draft_agent_without_start_owner_receipt_fails_closed(
    tmp_path, artifacts
) -> None:
    from dataclasses import replace

    from codemigrator.runtime.loop_contracts import SessionExit, SessionState

    class FreshRunner:
        calls = 0

        async def run(self, draft_id, logical_task_key, task, *, lifecycle):
            self.calls += 1
            raise AssertionError("provider/tool execution must not recur")

    store = InMemoryRuntimeStore()
    draft_id = new_uuid7()
    flow, _ = _flow(artifacts)
    _, draft_saver, agent_saver = _savers(tmp_path, store, draft_id)
    created = await store.create_or_get_agent_run(_agent_run(draft_id, coordinator_task_key()))
    terminal = replace(created, state=SessionState.Closed, exit=SessionExit.Completed)
    await store.commit_agent_run_receipt(
        terminal, AgentRunReceipt(uuid4(), created.agent_run_id, "draft.coordinator.completed")
    )
    runner = FreshRunner()
    graph = MigrationSessionGraph(
        owner=DraftFlowOwner(draft_id=draft_id, flow=flow, store=store),
        agent_runs=store,
        checkpointer=draft_saver,
        agent_checkpointer=agent_saver,
        agent_runner=runner,
    )
    with pytest.raises(ValueError, match="start receipt"):
        await graph.coordinate_exploration("merge")
    assert runner.calls == 0
    assert await store.read_draft_session_events(draft_id, 0) == ()


@pytest.mark.asyncio
async def test_terminal_draft_agent_without_result_cas_reference_fails_closed(
    tmp_path, artifacts
) -> None:
    from dataclasses import replace

    from codemigrator.runtime.draft_graph import DraftAgentResultUnavailable
    from codemigrator.runtime.loop_contracts import SessionExit, SessionState

    class FreshRunner:
        calls = 0

        async def run(self, draft_id, logical_task_key, task, *, lifecycle):
            self.calls += 1
            raise AssertionError("provider/tool execution must not recur")

    store = InMemoryRuntimeStore()
    draft_id = new_uuid7()
    flow, _ = _flow(artifacts)
    _, draft_saver, agent_saver = _savers(tmp_path, store, draft_id)
    owner = DraftFlowOwner(draft_id=draft_id, flow=flow, store=store)
    created = await store.create_or_get_agent_run(_agent_run(draft_id, coordinator_task_key()))
    await owner.commit_agent_started(created)
    terminal = replace(
        created,
        state=SessionState.Closed,
        exit=SessionExit.Completed,
        result_sha256="e" * 64,
    )
    await store.commit_agent_run_receipt(
        terminal,
        AgentRunReceipt(uuid4(), created.agent_run_id, "draft.coordinator.completed"),
    )
    runner = FreshRunner()
    graph = MigrationSessionGraph(
        owner=owner,
        agent_runs=store,
        checkpointer=draft_saver,
        agent_checkpointer=agent_saver,
        agent_runner=runner,
    )

    with pytest.raises(DraftAgentResultUnavailable, match="durable CAS result reference"):
        await graph.coordinate_exploration("merge")

    assert runner.calls == 0
    events = await store.read_draft_session_events(draft_id, 0)
    assert [event.event_type for event in events] == ["agent_run.started"]


@pytest.mark.asyncio
async def test_new_draft_completion_requires_matching_result_cas_reference(
    tmp_path, artifacts
) -> None:
    from dataclasses import replace

    from codemigrator.runtime.draft_graph import DraftAgentResultUnavailable
    from codemigrator.runtime.loop_contracts import SessionExit, SessionState

    class Runner:
        calls = 0

        async def run(self, draft_id, logical_task_key, task, *, lifecycle):
            self.calls += 1
            created = await store.create_or_get_agent_run(_agent_run(draft_id, logical_task_key))
            await lifecycle.started(created)
            result_reference = await CasLedger(cas, store).put(
                b"different-result",
                "draft",
                draft_id,
                f"agent-result:{created.agent_run_id}",
            )
            terminal = replace(
                created,
                state=SessionState.Closed,
                exit=SessionExit.Completed,
                result_sha256="e" * 64,
            )
            receipt = AgentRunReceipt(uuid4(), terminal.agent_run_id, "draft.coordinator.completed")
            await store.commit_agent_run_receipt(terminal, receipt)
            await lifecycle.terminal(terminal, receipt)
            return DraftAgentCompletion(terminal, receipt, result_reference)

    store = InMemoryRuntimeStore()
    draft_id = new_uuid7()
    flow, _ = _flow(artifacts)
    cas, draft_saver, agent_saver = _savers(tmp_path, store, draft_id)
    runner = Runner()
    graph = MigrationSessionGraph(
        owner=DraftFlowOwner(draft_id=draft_id, flow=flow, store=store),
        agent_runs=store,
        checkpointer=draft_saver,
        agent_checkpointer=agent_saver,
        agent_runner=runner,
    )

    with pytest.raises(DraftAgentResultUnavailable, match="matching durable CAS result"):
        await graph.coordinate_exploration("merge")

    assert runner.calls == 1
    assert [event.event_type for event in await store.read_draft_session_events(draft_id, 0)] == [
        "agent_run.started"
    ]


@pytest.mark.asyncio
async def test_recovered_failed_draft_agent_publishes_terminal_without_rerun(
    tmp_path, artifacts
) -> None:
    from dataclasses import replace

    from codemigrator.runtime.draft_graph import DraftAgentExecutionFailed
    from codemigrator.runtime.loop_contracts import SessionExit, SessionState

    class FreshRunner:
        calls = 0

        async def run(self, draft_id, logical_task_key, task, *, lifecycle):
            self.calls += 1
            raise AssertionError("provider/tool execution must not recur")

    store = InMemoryRuntimeStore()
    draft_id = new_uuid7()
    flow, _ = _flow(artifacts)
    _, draft_saver, agent_saver = _savers(tmp_path, store, draft_id)
    owner = DraftFlowOwner(draft_id=draft_id, flow=flow, store=store)
    created = await store.create_or_get_agent_run(_agent_run(draft_id, coordinator_task_key()))
    await owner.commit_agent_started(created)
    failed = replace(created, state=SessionState.Failed, exit=SessionExit.Failed)
    receipt = AgentRunReceipt(uuid4(), created.agent_run_id, "draft.coordinator.failed")
    await store.commit_agent_run_receipt(failed, receipt)
    runner = FreshRunner()
    graph = MigrationSessionGraph(
        owner=owner,
        agent_runs=store,
        checkpointer=draft_saver,
        agent_checkpointer=agent_saver,
        agent_runner=runner,
    )

    with pytest.raises(DraftAgentExecutionFailed, match="FAILED"):
        await graph.coordinate_exploration("merge")
    events = await store.read_draft_session_events(draft_id, 0)
    assert [event.event_type for event in events] == [
        "agent_run.started",
        "agent_run.terminal",
    ]
    assert [event.sequence for event in events] == [1, 2]

    with pytest.raises(DraftAgentExecutionFailed, match="FAILED"):
        await graph.coordinate_exploration("merge")
    assert runner.calls == 0
    assert await store.read_draft_session_events(draft_id, 0) == events


@pytest.mark.parametrize(
    ("exit_name", "state_name", "category"),
    [
        ("BudgetExhausted", "Failed", "draft.coordinator.failed"),
        ("SegmentStopped", "Closed", "draft.coordinator.segment_stopped"),
        ("Invalidated", "Invalidated", "draft.coordinator.invalidated"),
    ],
)
@pytest.mark.asyncio
async def test_recovered_noncompleted_draft_agent_publishes_terminal_without_rerun(
    tmp_path, artifacts, exit_name, state_name, category
) -> None:
    from dataclasses import replace

    from codemigrator.runtime.draft_graph import (
        DraftAgentExecutionFailed,
        DraftAgentExecutionTerminated,
    )
    from codemigrator.runtime.loop_contracts import SessionExit, SessionState

    class FreshRunner:
        calls = 0

        async def run(self, draft_id, logical_task_key, task, *, lifecycle):
            self.calls += 1
            raise AssertionError("provider/tool execution must not recur")

    session_exit = getattr(SessionExit, exit_name)
    session_state = getattr(SessionState, state_name)
    store = InMemoryRuntimeStore()
    draft_id = new_uuid7()
    flow, _ = _flow(artifacts)
    _, draft_saver, agent_saver = _savers(tmp_path, store, draft_id)
    owner = DraftFlowOwner(draft_id=draft_id, flow=flow, store=store)
    created = await store.create_or_get_agent_run(_agent_run(draft_id, coordinator_task_key()))
    await owner.commit_agent_started(created)
    terminal = replace(created, state=session_state, exit=session_exit)
    receipt = AgentRunReceipt(uuid4(), created.agent_run_id, category)
    await store.commit_agent_run_receipt(terminal, receipt)
    runner = FreshRunner()
    graph = MigrationSessionGraph(
        owner=owner,
        agent_runs=store,
        checkpointer=draft_saver,
        agent_checkpointer=agent_saver,
        agent_runner=runner,
    )

    with pytest.raises(DraftAgentExecutionTerminated) as first_error:
        await graph.coordinate_exploration("merge")
    events = await store.read_draft_session_events(draft_id, 0)
    assert [event.sequence for event in events] == [1, 2]
    assert [event.event_type for event in events] == [
        "agent_run.started",
        "agent_run.terminal",
    ]
    assert events[1].data["exit"] == session_exit.value
    assert events[1].data["receipt_category"] == category
    terminal_fact = await owner.load_fact(f"draft.agent.terminal:{terminal.agent_run_id}")
    assert terminal_fact is not None
    assert terminal_fact[1] == {
        "agent_run_id": str(terminal.agent_run_id),
        "receipt_id": str(receipt.receipt_id),
    }

    with pytest.raises(DraftAgentExecutionTerminated) as replayed_error:
        await graph.coordinate_exploration("merge")

    assert first_error.value.exit is session_exit
    assert replayed_error.value.exit is session_exit
    if session_exit in {SessionExit.Failed, SessionExit.BudgetExhausted}:
        assert isinstance(first_error.value, DraftAgentExecutionFailed)
        assert isinstance(replayed_error.value, DraftAgentExecutionFailed)
    else:
        assert type(first_error.value) is DraftAgentExecutionTerminated
        assert type(replayed_error.value) is DraftAgentExecutionTerminated
    assert runner.calls == 0
    assert await store.read_draft_session_events(draft_id, 0) == events


@pytest.mark.parametrize(
    ("exit_name", "state_name", "category"),
    [
        ("SegmentStopped", "Closed", "draft.coordinator.segment_stopped"),
        ("Invalidated", "Invalidated", "draft.coordinator.invalidated"),
    ],
)
@pytest.mark.asyncio
async def test_returned_noncompleted_draft_agent_uses_same_typed_outcome_on_replay(
    tmp_path, artifacts, exit_name, state_name, category
) -> None:
    from dataclasses import replace

    from codemigrator.runtime.draft_graph import DraftAgentExecutionTerminated
    from codemigrator.runtime.loop_contracts import SessionExit, SessionState

    class Runner:
        calls = 0

        async def run(self, draft_id, logical_task_key, task, *, lifecycle):
            self.calls += 1
            created = await store.create_or_get_agent_run(_agent_run(draft_id, logical_task_key))
            await lifecycle.started(created)
            terminal = replace(
                created,
                state=getattr(SessionState, state_name),
                exit=getattr(SessionExit, exit_name),
            )
            receipt = AgentRunReceipt(uuid4(), terminal.agent_run_id, category)
            await store.commit_agent_run_receipt(terminal, receipt)
            await lifecycle.terminal(terminal, receipt)
            return DraftAgentCompletion(terminal, receipt, None)

    store = InMemoryRuntimeStore()
    draft_id = new_uuid7()
    flow, _ = _flow(artifacts)
    _, draft_saver, agent_saver = _savers(tmp_path, store, draft_id)
    runner = Runner()
    graph = MigrationSessionGraph(
        owner=DraftFlowOwner(draft_id=draft_id, flow=flow, store=store),
        agent_runs=store,
        checkpointer=draft_saver,
        agent_checkpointer=agent_saver,
        agent_runner=runner,
    )

    with pytest.raises(DraftAgentExecutionTerminated) as first_error:
        await graph.coordinate_exploration("merge")
    events = await store.read_draft_session_events(draft_id, 0)
    with pytest.raises(DraftAgentExecutionTerminated) as replayed_error:
        await graph.coordinate_exploration("merge")

    assert first_error.value.exit is getattr(SessionExit, exit_name)
    assert replayed_error.value.exit is first_error.value.exit
    assert runner.calls == 1
    assert [event.event_type for event in events] == [
        "agent_run.started",
        "agent_run.terminal",
    ]
    assert await store.read_draft_session_events(draft_id, 0) == events


@pytest.mark.asyncio
async def test_failed_draft_terminal_category_mismatch_is_not_published(
    tmp_path, artifacts
) -> None:
    from dataclasses import replace

    from codemigrator.runtime.loop_contracts import SessionExit, SessionState

    class Runner:
        async def run(self, draft_id, logical_task_key, task, *, lifecycle):
            created = await store.create_or_get_agent_run(_agent_run(draft_id, logical_task_key))
            await lifecycle.started(created)
            failed = replace(created, state=SessionState.Failed, exit=SessionExit.Failed)
            receipt = AgentRunReceipt(uuid4(), failed.agent_run_id, "draft.coordinator.completed")
            await store.commit_agent_run_receipt(failed, receipt)
            await lifecycle.terminal(failed, receipt)
            raise AssertionError("invalid terminal receipt should not be accepted")

    store = InMemoryRuntimeStore()
    draft_id = new_uuid7()
    flow, _ = _flow(artifacts)
    _, draft_saver, agent_saver = _savers(tmp_path, store, draft_id)
    owner = DraftFlowOwner(draft_id=draft_id, flow=flow, store=store)
    graph = MigrationSessionGraph(
        owner=owner,
        agent_runs=store,
        checkpointer=draft_saver,
        agent_checkpointer=agent_saver,
        agent_runner=Runner(),
    )

    with pytest.raises(ValueError, match="failure category"):
        await graph.coordinate_exploration("merge")

    events = await store.read_draft_session_events(draft_id, 0)
    assert [event.event_type for event in events] == ["agent_run.started"]
    assert await owner.load_fact(f"draft.agent.terminal:{events[0].data['agent_run_id']}") is None


@pytest.mark.parametrize(
    ("exit_name", "state_name", "category", "error_match"),
    [
        (
            "SegmentStopped",
            "Closed",
            "draft.coordinator.invalidated",
            "segment-stopped category",
        ),
        (
            "Invalidated",
            "Invalidated",
            "draft.coordinator.segment_stopped",
            "invalidated category",
        ),
    ],
)
@pytest.mark.asyncio
async def test_noncompleted_draft_terminal_category_mismatch_is_not_published(
    tmp_path, artifacts, exit_name, state_name, category, error_match
) -> None:
    from dataclasses import replace

    from codemigrator.runtime.loop_contracts import SessionExit, SessionState

    class Runner:
        async def run(self, draft_id, logical_task_key, task, *, lifecycle):
            created = await store.create_or_get_agent_run(_agent_run(draft_id, logical_task_key))
            await lifecycle.started(created)
            terminal = replace(
                created,
                state=getattr(SessionState, state_name),
                exit=getattr(SessionExit, exit_name),
            )
            receipt = AgentRunReceipt(uuid4(), terminal.agent_run_id, category)
            await store.commit_agent_run_receipt(terminal, receipt)
            await lifecycle.terminal(terminal, receipt)
            raise AssertionError("invalid terminal receipt should not be accepted")

    store = InMemoryRuntimeStore()
    draft_id = new_uuid7()
    flow, _ = _flow(artifacts)
    _, draft_saver, agent_saver = _savers(tmp_path, store, draft_id)
    owner = DraftFlowOwner(draft_id=draft_id, flow=flow, store=store)
    graph = MigrationSessionGraph(
        owner=owner,
        agent_runs=store,
        checkpointer=draft_saver,
        agent_checkpointer=agent_saver,
        agent_runner=Runner(),
    )

    with pytest.raises(ValueError, match=error_match):
        await graph.coordinate_exploration("merge")

    events = await store.read_draft_session_events(draft_id, 0)
    assert [event.event_type for event in events] == ["agent_run.started"]
    assert await owner.load_fact(f"draft.agent.terminal:{events[0].data['agent_run_id']}") is None


@pytest.mark.asyncio
async def test_draft_agent_terminal_failure_is_published_before_error(tmp_path, artifacts) -> None:
    from dataclasses import replace

    from codemigrator.runtime.loop_contracts import SessionExit, SessionState

    class FailingRunner:
        async def run(self, draft_id, logical_task_key, task, *, lifecycle):
            created = await store.create_or_get_agent_run(_agent_run(draft_id, logical_task_key))
            await lifecycle.started(created)
            assert [
                event.event_type for event in await store.read_draft_session_events(draft_id, 0)
            ] == ["agent_run.started"]
            failed = replace(created, state=SessionState.Failed, exit=SessionExit.Failed)
            receipt = AgentRunReceipt(uuid4(), created.agent_run_id, "draft.coordinator.failed")
            await store.commit_agent_run_receipt(failed, receipt)
            await lifecycle.terminal(failed, receipt)
            raise RuntimeError("synthetic provider failure")

    store = InMemoryRuntimeStore()
    draft_id = new_uuid7()
    flow, _ = _flow(artifacts)
    _, draft_saver, agent_saver = _savers(tmp_path, store, draft_id)
    graph = MigrationSessionGraph(
        owner=DraftFlowOwner(draft_id=draft_id, flow=flow, store=store),
        agent_runs=store,
        checkpointer=draft_saver,
        agent_checkpointer=agent_saver,
        agent_runner=FailingRunner(),
    )
    with pytest.raises(RuntimeError, match="synthetic provider failure"):
        await graph.coordinate_exploration("merge")
    assert [event.event_type for event in await store.read_draft_session_events(draft_id, 0)] == [
        "agent_run.started",
        "agent_run.terminal",
    ]


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
        async def verify_descriptor_lock(self, request, transaction=None) -> None:
            raise CreateRunRejected("descriptor lock rejected")

        async def verify_preindex(self, request, transaction=None) -> None:
            raise AssertionError("preindex must not run after rejection")

        async def verify_dossier_consistency(self, request, transaction=None) -> None:
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
    await graph.owner.persist_current_revision()
    store.fail_next_commit()
    with pytest.raises(StoreCommitError, match="injected commit failure"):
        await graph.attach_to_run(run_id, _create_request(freeze.frozen_artifact_bundle))
    assert await store.load_draft_owner_fact(draft_id, "draft.freeze") is None
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
        async def verify_descriptor_lock(self, request, transaction=None) -> None:
            return None

        async def verify_preindex(self, request, transaction=None) -> None:
            return None

        async def verify_dossier_consistency(self, request, transaction=None) -> None:
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
    freeze_fact = await store.load_draft_owner_fact(draft_id, "draft.freeze")
    assert freeze_fact is not None
    assert freeze_fact[0].category == "draft.freeze"
    assert freeze_fact[1] == freeze.model_dump(mode="json", by_alias=True)
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

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from runtime.test_agent_runs_postgres import isolated_store

from codemigrator.runtime.draft import DraftConflictError, DraftFlow, DraftLedger
from codemigrator.runtime.draft_graph import DraftFlowOwner
from codemigrator.runtime.draft_models import (
    AskUserAnswer,
    AskUserQuestion,
    ExplorationReport,
    QuestionOption,
)
from codemigrator.runtime.store import PostgreSQLRuntimeStore


def _flow(artifacts) -> DraftFlow:
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
    flow.seed_artifacts(artifacts)
    return flow


@pytest.mark.asyncio
async def test_postgres_draft_owner_ledger_restores_after_new_store_instance(artifacts) -> None:
    async with isolated_store() as store:
        draft_id = uuid4()
        flow = _flow(artifacts)
        revision = flow.ledger.current_revision
        assert revision is not None
        owner = DraftFlowOwner(draft_id=draft_id, flow=flow, store=store)
        question = AskUserQuestion(
            revision_id=revision.revision_id,
            prompt="Keep this module boundary?",
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
                    impact="Combines neighboring modules.",
                    recommended=False,
                ),
            ),
        )
        await owner.commit_question(question)
        answer = AskUserAnswer(
            question_id=question.question_id,
            revision_id=question.revision_id,
            selected_option="keep",
        )
        await owner.commit_answer(answer)
        freeze = flow.ledger.freeze(revision.revision_id)
        await owner.persist_freeze_receipt()

        restarted_store = PostgreSQLRuntimeStore(store.pool)
        restarted_flow = _flow(artifacts)
        restarted_owner = DraftFlowOwner(
            draft_id=draft_id,
            flow=restarted_flow,
            store=restarted_store,
        )
        await restarted_owner.restore_ledger()

        assert restarted_flow.ledger.current_revision == revision
        assert restarted_flow.ledger.questions == (question,)
        assert restarted_flow.ledger.answers == (answer,)
        assert restarted_owner.freeze_receipt == freeze


@pytest.mark.asyncio
async def test_postgres_freeze_rejects_question_committed_after_freeze_snapshot(
    artifacts, monkeypatch
) -> None:
    async with isolated_store() as store:
        draft_id = uuid4()
        freeze_flow = _flow(artifacts)
        revision = freeze_flow.ledger.current_revision
        assert revision is not None
        freeze_owner = DraftFlowOwner(draft_id=draft_id, flow=freeze_flow, store=store)
        await freeze_owner.persist_current_revision()
        freeze_flow.ledger.freeze(revision.revision_id)

        stale_ledger = DraftLedger.restore(revisions=(revision,), questions=(), answers=())
        question_flow = _flow(artifacts)
        question_flow.ledger = stale_ledger
        question_owner = DraftFlowOwner(
            draft_id=draft_id,
            flow=question_flow,
            store=store,
        )
        question = AskUserQuestion(
            revision_id=revision.revision_id,
            prompt="Did a question arrive during freeze?",
            options=(
                QuestionOption(
                    key="keep",
                    label="Keep",
                    impact="Preserves the current boundary.",
                    recommended=True,
                ),
                QuestionOption(
                    key="merge",
                    label="Merge",
                    impact="Combines neighboring boundaries.",
                    recommended=False,
                ),
            ),
        )
        freeze_ready = asyncio.Event()
        release_freeze = asyncio.Event()
        commit_fact = store.commit_draft_owner_fact

        async def delayed_freeze_commit(
            owner_id,
            receipt_key,
            category,
            fact,
            *,
            events=(),
            transaction=None,
            expected_existing_facts=None,
        ):
            if category == "draft.freeze":
                freeze_ready.set()
                await release_freeze.wait()
            kwargs = {}
            if expected_existing_facts is not None:
                kwargs["expected_existing_facts"] = expected_existing_facts
            return await commit_fact(
                owner_id,
                receipt_key,
                category,
                fact,
                events=events,
                transaction=transaction,
                **kwargs,
            )

        monkeypatch.setattr(store, "commit_draft_owner_fact", delayed_freeze_commit)
        freeze_task = asyncio.create_task(freeze_owner.persist_freeze_receipt())
        try:
            await freeze_ready.wait()
            await question_owner.commit_question(question)
        finally:
            release_freeze.set()

        with pytest.raises(DraftConflictError, match="changed before freeze"):
            await freeze_task

        assert freeze_owner.freeze_receipt is None
        persisted = await store.list_draft_owner_facts(draft_id)
        assert all(receipt.receipt_key != "draft.freeze" for receipt, _ in persisted)
        assert any(receipt.category == "draft.ask_user.question" for receipt, _ in persisted)

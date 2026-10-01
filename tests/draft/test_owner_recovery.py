from __future__ import annotations

from uuid import uuid4

import pytest

from codemigrator.runtime.draft import DraftConflictError, DraftFlow, DraftLedger
from codemigrator.runtime.draft_graph import DraftFlowOwner
from codemigrator.runtime.draft_models import (
    AskUserAnswer,
    AskUserQuestion,
    DraftStage,
    ExplorationReport,
    QuestionOption,
)
from codemigrator.runtime.store import InMemoryRuntimeStore, StoreCommitError


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


def _question(revision_id, prompt: str) -> AskUserQuestion:  # type: ignore[no-untyped-def]
    return AskUserQuestion(
        revision_id=revision_id,
        prompt=prompt,
        options=(
            QuestionOption(
                key="keep",
                label="Keep",
                impact="Preserves current boundaries.",
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


@pytest.mark.asyncio
async def test_draft_owner_persists_and_restores_revision_qa_and_freeze(artifacts) -> None:
    store = InMemoryRuntimeStore()
    draft_id = uuid4()
    flow = _flow(artifacts)
    owner = DraftFlowOwner(draft_id=draft_id, flow=flow, store=store)
    first_revision = flow.ledger.current_revision
    assert first_revision is not None

    first_question = _question(first_revision.revision_id, "Keep this boundary?")
    question_receipt = await owner.commit_question(first_question)
    first_answer = AskUserAnswer(
        question_id=first_question.question_id,
        revision_id=first_question.revision_id,
        selected_option="keep",
    )
    await owner.commit_answer(first_answer)

    flow.finalize_alignment()
    revised_artifacts = artifacts.model_copy(
        update={
            "migration_rulebook": artifacts.migration_rulebook.model_copy(update={"version": 2})
        }
    )
    second_revision = flow.revise_artifacts(revised_artifacts)
    second_question = _question(second_revision.revision_id, "Keep the revised boundary?")
    await owner.commit_question(second_question)
    second_answer = AskUserAnswer(
        question_id=second_question.question_id,
        revision_id=second_question.revision_id,
        selected_option="keep",
    )
    await owner.commit_answer(second_answer)

    freeze = flow.ledger.freeze(second_revision.revision_id)
    freeze_receipt = await owner.persist_freeze_receipt()
    fact_count = len(await store.list_draft_owner_facts(draft_id))
    assert question_receipt.receipt_key == f"draft.question:{first_question.question_id}"

    restarted_flow = _flow(artifacts)
    restarted_flow.finalize_alignment()
    restarted_owner = DraftFlowOwner(draft_id=draft_id, flow=restarted_flow, store=store)
    await restarted_owner.restore_ledger()

    assert restarted_flow.stage is DraftStage.Draft
    assert restarted_flow.ledger.revisions == (first_revision, second_revision)
    assert restarted_flow.ledger.current_revision == second_revision
    assert restarted_flow.ledger.questions == (first_question, second_question)
    assert restarted_flow.ledger.answers == (first_answer, second_answer)
    assert restarted_owner.freeze_receipt == freeze
    assert freeze_receipt.receipt_key == "draft.freeze"

    second_question_receipt = await store.load_draft_owner_fact(
        draft_id, f"draft.question:{second_question.question_id}"
    )
    second_answer_receipt = await store.load_draft_owner_fact(
        draft_id, f"draft.answer:{second_answer.question_id}"
    )
    assert second_question_receipt is not None and second_answer_receipt is not None
    assert await restarted_owner.commit_question(second_question) == second_question_receipt[0]
    assert await restarted_owner.commit_answer(second_answer) == second_answer_receipt[0]
    with pytest.raises(DraftConflictError, match="already frozen"):
        await restarted_owner.commit_question(
            _question(second_revision.revision_id, "Add a question after freeze?")
        )

    persisted_revision = await store.load_draft_owner_fact(draft_id, "draft.revision:2")
    assert persisted_revision is not None
    assert await restarted_owner.persist_current_revision() == persisted_revision[0]
    assert await restarted_owner.persist_freeze_receipt() == freeze_receipt
    assert len(await store.list_draft_owner_facts(draft_id)) == fact_count


@pytest.mark.asyncio
async def test_stale_draft_owner_cannot_commit_question_after_freeze(artifacts) -> None:
    store = InMemoryRuntimeStore()
    draft_id = uuid4()
    confirmed_flow = _flow(artifacts)
    revision = confirmed_flow.ledger.current_revision
    assert revision is not None
    confirmed_owner = DraftFlowOwner(draft_id=draft_id, flow=confirmed_flow, store=store)
    existing_question = _question(revision.revision_id, "Keep this boundary?")
    await confirmed_owner.commit_question(existing_question)
    await confirmed_owner.commit_answer(
        AskUserAnswer(
            question_id=existing_question.question_id,
            revision_id=revision.revision_id,
            selected_option="keep",
        )
    )
    freeze = confirmed_flow.ledger.freeze(revision.revision_id)
    await confirmed_owner.persist_freeze_receipt()

    stale_ledger = DraftLedger.restore(revisions=(revision,), questions=(), answers=())
    stale_flow = _flow(artifacts)
    stale_flow.ledger = stale_ledger
    stale_owner = DraftFlowOwner(
        draft_id=draft_id,
        flow=stale_flow,
        store=store,
    )
    attempted_question = _question(revision.revision_id, "Write after the Draft froze?")

    with pytest.raises(DraftConflictError, match="already frozen"):
        await stale_owner.commit_question(attempted_question)

    recovered_owner = DraftFlowOwner(draft_id=draft_id, flow=_flow(artifacts), store=store)
    await recovered_owner.restore_ledger()
    assert recovered_owner.freeze_receipt == freeze
    assert recovered_owner.flow.ledger.questions == (existing_question,)


@pytest.mark.asyncio
async def test_stale_draft_owner_cannot_commit_answer_after_freeze(artifacts) -> None:
    store = InMemoryRuntimeStore()
    draft_id = uuid4()
    confirmed_flow = _flow(artifacts)
    revision = confirmed_flow.ledger.current_revision
    assert revision is not None
    confirmed_owner = DraftFlowOwner(draft_id=draft_id, flow=confirmed_flow, store=store)
    freeze = confirmed_flow.ledger.freeze(revision.revision_id)
    await confirmed_owner.persist_freeze_receipt()

    stale_question = _question(revision.revision_id, "Question known to a stale owner?")
    stale_ledger = DraftLedger.restore(
        revisions=(revision,), questions=(stale_question,), answers=()
    )
    stale_flow = _flow(artifacts)
    stale_flow.ledger = stale_ledger
    stale_owner = DraftFlowOwner(
        draft_id=draft_id,
        flow=stale_flow,
        store=store,
    )
    attempted_answer = AskUserAnswer(
        question_id=stale_question.question_id,
        revision_id=revision.revision_id,
        selected_option="keep",
    )

    with pytest.raises(DraftConflictError, match="already frozen"):
        await stale_owner.commit_answer(attempted_answer)

    recovered_owner = DraftFlowOwner(draft_id=draft_id, flow=_flow(artifacts), store=store)
    await recovered_owner.restore_ledger()
    assert recovered_owner.freeze_receipt == freeze
    assert recovered_owner.flow.ledger.questions == ()
    assert recovered_owner.flow.ledger.answers == ()


@pytest.mark.asyncio
async def test_draft_owner_restore_rejects_orphan_question_fact(artifacts) -> None:
    store = InMemoryRuntimeStore()
    draft_id = uuid4()
    flow = _flow(artifacts)
    revision = flow.ledger.current_revision
    assert revision is not None
    question = _question(revision.revision_id, "Persisted without its revision?")
    await store.commit_draft_owner_fact(
        draft_id,
        f"draft.question:{question.question_id}",
        "draft.ask_user.question",
        question.model_dump(mode="json"),
    )

    owner = DraftFlowOwner(draft_id=draft_id, flow=flow, store=store)
    with pytest.raises(StoreCommitError, match="inconsistent"):
        await owner.restore_ledger()


@pytest.mark.asyncio
async def test_stale_draft_owner_cannot_persist_an_older_current_revision(artifacts) -> None:
    store = InMemoryRuntimeStore()
    draft_id = uuid4()
    current_flow = _flow(artifacts)
    first_revision = current_flow.ledger.current_revision
    assert first_revision is not None
    current_owner = DraftFlowOwner(draft_id=draft_id, flow=current_flow, store=store)
    await current_owner.persist_current_revision()

    stale_ledger = DraftLedger.restore(revisions=(first_revision,), questions=(), answers=())
    stale_owner = DraftFlowOwner(
        draft_id=draft_id,
        flow=DraftFlow(ledger=stale_ledger),
        store=store,
    )

    current_flow.finalize_alignment()
    revised_artifacts = artifacts.model_copy(
        update={
            "migration_rulebook": artifacts.migration_rulebook.model_copy(update={"version": 2})
        }
    )
    current_flow.revise_artifacts(revised_artifacts)
    await current_owner.persist_current_revision()

    with pytest.raises(DraftConflictError, match="no longer current"):
        await stale_owner.persist_current_revision()

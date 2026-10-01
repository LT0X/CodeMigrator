from __future__ import annotations

import pytest

from codemigrator.core import TaskDraftRevisionId
from codemigrator.core.ids import new_uuid7
from codemigrator.runtime.draft import DraftConflictError, DraftLedger
from codemigrator.runtime.draft_models import (
    AskUserAnswer,
    AskUserQuestion,
    QuestionOption,
)


def _question(revision_id: TaskDraftRevisionId, prompt: str) -> AskUserQuestion:
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


def _answer(question: AskUserQuestion) -> AskUserAnswer:
    return AskUserAnswer(
        question_id=question.question_id,
        revision_id=question.revision_id,
        selected_option="keep",
    )


def test_draft_ledger_round_trips_revisions_questions_answers_and_freeze(artifacts) -> None:
    ledger = DraftLedger()
    first = ledger.create_revision(artifacts)
    first_question = _question(first.revision_id, "Keep the first boundary?")
    ledger.append_question(first_question)
    ledger.answer_question(_answer(first_question))

    changed = artifacts.model_copy(
        update={
            "migration_rulebook": artifacts.migration_rulebook.model_copy(update={"version": 2})
        }
    )
    second = ledger.create_revision(changed)
    second_question = _question(second.revision_id, "Keep the revised boundary?")
    ledger.append_question(second_question)
    ledger.answer_question(_answer(second_question))
    freeze = ledger.freeze(second.revision_id)

    restored = DraftLedger.restore(
        revisions=ledger.revisions,
        questions=ledger.questions,
        answers=ledger.answers,
        freeze_receipt=freeze,
    )

    assert restored.revisions == ledger.revisions
    assert restored.current_revision == second
    assert restored.questions == ledger.questions
    assert restored.answers == ledger.answers
    assert restored.freeze_receipt == freeze


def test_draft_ledger_restore_rejects_gapped_revisions_and_changed_snapshots(artifacts) -> None:
    ledger = DraftLedger()
    first = ledger.create_revision(artifacts)
    changed = artifacts.model_copy(
        update={
            "migration_rulebook": artifacts.migration_rulebook.model_copy(update={"version": 2})
        }
    )
    second = ledger.create_revision(changed)

    with pytest.raises(DraftConflictError, match="contiguous"):
        DraftLedger.restore(revisions=(second,), questions=(), answers=())

    changed_snapshot = second.model_copy(
        update={
            "artifact_snapshots": (
                *second.artifact_snapshots[:-1],
                second.artifact_snapshots[-1].model_copy(update={"sha256": "f" * 64}),
            )
        }
    )
    with pytest.raises(DraftConflictError, match="snapshot"):
        DraftLedger.restore(revisions=(first, changed_snapshot), questions=(), answers=())


def test_draft_ledger_restore_rejects_answers_bound_to_another_revision(artifacts) -> None:
    ledger = DraftLedger()
    revision = ledger.create_revision(artifacts)
    question = _question(revision.revision_id, "Keep this boundary?")
    ledger.append_question(question)
    answer = _answer(question)
    wrong_revision_answer = answer.model_copy(
        update={"revision_id": TaskDraftRevisionId(new_uuid7())}
    )

    with pytest.raises(DraftConflictError, match="revision"):
        DraftLedger.restore(
            revisions=ledger.revisions,
            questions=(question,),
            answers=(wrong_revision_answer,),
        )


def test_draft_ledger_restore_rejects_freeze_receipt_that_does_not_match(artifacts) -> None:
    ledger = DraftLedger()
    revision = ledger.create_revision(artifacts)
    freeze = ledger.freeze(revision.revision_id)
    changed_freeze = freeze.model_copy(update={"revision_number": freeze.revision_number + 1})

    with pytest.raises(DraftConflictError, match="freeze receipt"):
        DraftLedger.restore(
            revisions=ledger.revisions,
            questions=(),
            answers=(),
            freeze_receipt=changed_freeze,
        )

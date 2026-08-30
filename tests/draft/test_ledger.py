import pytest

from codemigrator.runtime.draft import DraftConflictError, DraftLedger
from codemigrator.runtime.draft_models import AskUserAnswer, AskUserQuestion, QuestionOption


def make_question(revision_id: object) -> AskUserQuestion:
    return AskUserQuestion(
        revision_id=revision_id,
        prompt="Should the two modules remain separate?",
        options=(
            QuestionOption(
                key="separate",
                label="Keep separate",
                impact="Preserves independent validation and is recommended.",
                recommended=True,
            ),
            QuestionOption(
                key="merge",
                label="Merge modules",
                impact="Reduces boundaries but broadens change scope.",
                recommended=False,
            ),
        ),
        allow_free_text=True,
    )


def test_artifact_change_creates_revision_but_answer_does_not(artifacts) -> None:
    ledger = DraftLedger()
    first = ledger.create_revision(artifacts)
    question = ledger.append_question(make_question(first.revision_id))
    answered = ledger.answer_question(
        AskUserAnswer(
            question_id=question.question_id,
            revision_id=first.revision_id,
            selected_option="separate",
        )
    )

    assert ledger.current_revision == first
    assert ledger.answer_question(answered) == answered

    changed = artifacts.model_copy(
        update={
            "migration_rulebook": artifacts.migration_rulebook.model_copy(
                update={"version": 2}
            )
        }
    )
    second = ledger.revise(first.revision_id, changed)
    assert second.revision_number == 2
    assert second.revision_id != first.revision_id
    assert second.artifact_snapshots != first.artifact_snapshots


def test_stale_revision_and_conflicting_answer_are_rejected(artifacts) -> None:
    ledger = DraftLedger()
    first = ledger.create_revision(artifacts)
    question = ledger.append_question(make_question(first.revision_id))
    answer = AskUserAnswer(
        question_id=question.question_id,
        revision_id=first.revision_id,
        selected_option="separate",
    )
    ledger.answer_question(answer)

    with pytest.raises(DraftConflictError, match="answer"):
        ledger.answer_question(
            AskUserAnswer(
                question_id=question.question_id,
                revision_id=first.revision_id,
                selected_option="merge",
            )
        )

    second = ledger.revise(
        first.revision_id,
        artifacts.model_copy(
            update={
                "migration_rulebook": artifacts.migration_rulebook.model_copy(
                    update={"version": 2}
                )
            }
        ),
    )
    assert second.revision_id != first.revision_id
    with pytest.raises(DraftConflictError, match="current revision"):
        ledger.answer_question(
            AskUserAnswer(
                question_id=question.question_id,
                revision_id=first.revision_id,
                free_text="late answer",
            )
        )


def test_freeze_binds_current_revision_and_answer_pointers(artifacts) -> None:
    ledger = DraftLedger()
    revision = ledger.create_revision(artifacts)
    question = ledger.append_question(make_question(revision.revision_id))

    with pytest.raises(DraftConflictError, match="unanswered"):
        ledger.freeze(revision.revision_id)

    ledger.answer_question(
        AskUserAnswer(
            question_id=question.question_id,
            revision_id=revision.revision_id,
            selected_option="separate",
        )
    )
    receipt = ledger.freeze(revision.revision_id)

    assert receipt.revision_id == revision.revision_id
    assert receipt.answer_question_ids == (question.question_id,)
    assert receipt.frozen_artifact_bundle.spec.sha256 == next(
        snapshot.sha256
        for snapshot in receipt.artifact_snapshots
        if snapshot.name == "spec"
    )
    with pytest.raises(DraftConflictError, match="frozen"):
        ledger.revise(revision.revision_id, artifacts.model_copy(deep=True))

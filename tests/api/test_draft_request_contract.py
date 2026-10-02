from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from codemigrator.api.dto import SessionAnswerRequest, SessionCreateRequest


def test_draft_session_create_requires_registered_project_snapshot_and_goal() -> None:
    project_id = uuid4()
    snapshot_id = uuid4()

    request = SessionCreateRequest.model_validate(
        {
            "kind": "DRAFT",
            "payload": {
                "source": {
                    "project_id": str(project_id),
                    "snapshot_id": str(snapshot_id),
                },
                "goal": "Translate the service from TypeScript to Python.",
            },
        }
    )

    assert request.kind == "DRAFT"
    assert request.payload.source.project_id == project_id
    assert request.payload.source.snapshot_id == snapshot_id
    assert request.payload.goal == "Translate the service from TypeScript to Python."

    with pytest.raises(ValidationError):
        SessionCreateRequest.model_validate(
            {
                "kind": "DRAFT",
                "payload": {
                    "source_path": "/home/user/project",
                    "goal": "Translate this project.",
                },
            }
        )


@pytest.mark.parametrize(
    "answer",
    (
        {"selected_option": "keep"},
        {"free_text": "Keep the public API unchanged."},
    ),
)
def test_draft_answer_accepts_exactly_one_user_answer_form(answer: dict[str, str]) -> None:
    request = SessionAnswerRequest(
        question_id=uuid4(), answer=answer, revision=1
    )
    assert request.answer.model_dump(exclude_none=True) == answer


@pytest.mark.parametrize(
    "answer",
    (
        {},
        {"selected_option": "keep", "free_text": "also keep it"},
        {"selected_option": 12},
        {"free_text": ""},
        {"selected_option": "keep", "unexpected": True},
    ),
)
def test_draft_answer_rejects_ambiguous_or_untyped_answer_forms(
    answer: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        SessionAnswerRequest(question_id=uuid4(), answer=answer, revision=1)

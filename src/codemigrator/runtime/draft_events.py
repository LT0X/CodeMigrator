"""User-facing Draft event payloads derived from validated owner facts."""

from __future__ import annotations

from codemigrator.core import TaskDraftRevisionId

from .draft_models import AskUserQuestion, TaskDraftRevision


def revision_created_event(revision: TaskDraftRevision) -> dict[str, object]:
    spec = revision.artifacts.spec
    artifacts = revision.artifacts
    return {
        "revision": revision.revision_number,
        "artifacts": {
            "spec": {
                "spec": spec.spec.model_dump(mode="json", by_alias=True),
                "canonical_sha256": spec.canonical_sha256,
            },
            "understanding_dossier": artifacts.understanding_dossier.model_dump(
                mode="json", by_alias=True
            ),
            "target_project_blueprint": artifacts.target_project_blueprint.model_dump(
                mode="json", by_alias=True
            ),
            "migration_rulebook": artifacts.migration_rulebook.model_dump(
                mode="json", by_alias=True
            ),
        },
        "artifact_snapshots": [
            snapshot.model_dump(mode="json", by_alias=True)
            for snapshot in revision.artifact_snapshots
        ],
    }


def question_asked_event(
    question: AskUserQuestion, revision_number: int
) -> dict[str, object]:
    if type(revision_number) is not int or revision_number < 1:
        raise ValueError("AskUser revision number must be positive")
    return {
        "question_id": str(question.question_id),
        "revision": revision_number,
        "prompt": question.prompt,
        "options": [option.model_dump(mode="json", by_alias=True) for option in question.options],
        "allow_free_text": question.allow_free_text,
    }


def revision_confirmed_event(
    revision_id: TaskDraftRevisionId, revision_number: int
) -> dict[str, object]:
    del revision_id
    if type(revision_number) is not int or revision_number < 1:
        raise ValueError("confirmed Draft revision number must be positive")
    return {"revision": revision_number}


__all__ = [
    "question_asked_event",
    "revision_confirmed_event",
    "revision_created_event",
]

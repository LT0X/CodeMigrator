from __future__ import annotations

import uuid

import pytest

from codemigrator.core import (
    ArtifactRef,
    BranchPrefix,
    CreateRun,
    FrozenArtifactBundle,
    GitRefName,
    RemoteRepository,
    RepositoryUrl,
    RunId,
    Sha256,
)
from codemigrator.runtime.contracts import DraftSessionEventSpec


def uid() -> uuid.UUID:
    return uuid.uuid4()


def artifact(fill: str = "a") -> ArtifactRef:
    return ArtifactRef(sha256=Sha256(fill * 64), size=1, media_type="application/json")


def create_run() -> CreateRun:
    return CreateRun(
        source=RemoteRepository(
            repository_url=RepositoryUrl("https://github.com/example/source"),
            base_ref=GitRefName("main"),
        ),
        branch_prefix=BranchPrefix("migration"),
        frozen_artifacts=FrozenArtifactBundle(
            spec=artifact(),
            understanding_dossier=artifact("b"),
            target_project_blueprint=artifact("c"),
            migration_rulebook=artifact("d"),
        ),
    )


def draft_question_event(question_id: str | None = None) -> DraftSessionEventSpec:
    """Build the complete public AskUser event accepted by the Draft allowlist."""

    return DraftSessionEventSpec(
        "session.question.asked",
        {
            "question_id": question_id or str(uid()),
            "revision": 1,
            "prompt": "Choose how this migration should handle compatibility.",
            "options": [
                {
                    "key": "preserve",
                    "label": "Preserve behavior",
                    "impact": "Keeps current user-visible behavior.",
                    "recommended": True,
                },
                {
                    "key": "simplify",
                    "label": "Simplify behavior",
                    "impact": "May change existing edge cases.",
                    "recommended": False,
                },
            ],
            "allow_free_text": True,
        },
    )


@pytest.fixture
def run_id() -> RunId:
    return RunId(uid())


__all__ = ["artifact", "create_run", "draft_question_event", "uid"]

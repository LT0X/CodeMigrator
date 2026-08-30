"""Closed contracts for the deterministic, pre-Run drafting workflow."""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum
from typing import Literal

from pydantic import Field, StrictInt, field_validator, model_validator

from codemigrator.analysis import SourceRange
from codemigrator.core import (
    FrozenArtifactBundle,
    MigrationRulebook,
    MigrationSpec,
    QuestionId,
    RepoRelativePath,
    TargetProjectBlueprint,
    TaskDraftRevisionId,
    UnderstandingDossier,
)
from codemigrator.core._base import CoreModel
from codemigrator.core.ids import new_uuid7
from codemigrator.core.paths import _validate_repo_relative_path, normalize_repo_relative_paths


class _FrozenDraftModel(CoreModel):
    model_config = {"frozen": True}


class ReadOnlyDraftTool(str, Enum):
    """Tools that can be orchestrated by a draft session."""

    ReadFile = "ReadFile"
    QuerySourceAst = "QuerySourceAst"
    Exec = "Exec"


class DraftStage(str, Enum):
    Explore = "EXPLORE"
    Align = "ALIGN"
    Draft = "DRAFT"
    Calibrate = "CALIBRATE"
    Confirmed = "CONFIRMED"


class FocusHighlight(_FrozenDraftModel):
    path: RepoRelativePath
    kind: Literal["risk_hotspot", "large_file", "import_weight"]
    reason: str = Field(min_length=1, max_length=1024)

    @field_validator("path", mode="before")
    @classmethod
    def path_is_safe(cls, value: object) -> str:
        return _validate_repo_relative_path(value)


class FocusBrief(_FrozenDraftModel):
    domain_paths: tuple[RepoRelativePath, ...] = Field(min_length=1)
    highlights: tuple[FocusHighlight, ...] = ()
    budget_hint: str = Field(min_length=1, max_length=256)

    @field_validator("domain_paths", mode="before")
    @classmethod
    def domain_paths_are_normalized(cls, value: object) -> tuple[str, ...]:
        return tuple(normalize_repo_relative_paths(value))


class ExploreReassignment(_FrozenDraftModel):
    op: Literal["merge", "split", "refocus"]
    domain_paths: tuple[RepoRelativePath, ...] = Field(min_length=1)
    reason_summary: str = Field(min_length=1, max_length=2048)
    focus_brief: FocusBrief

    @field_validator("domain_paths", mode="before")
    @classmethod
    def domain_paths_are_normalized(cls, value: object) -> tuple[str, ...]:
        return tuple(normalize_repo_relative_paths(value))

    def as_advice_payload(self) -> dict[str, object]:
        """Project the closed reassignment contract into core Advice.payload."""

        return self.model_dump(mode="json")


class ExplorationReport(_FrozenDraftModel):
    domain_path: RepoRelativePath
    anchors: tuple[SourceRange, ...] = Field(min_length=1)
    coverage: tuple[RepoRelativePath, ...] = Field(min_length=1)
    confidence_reason: str = Field(min_length=1, max_length=4096)
    unresolved_conflict_count: StrictInt = Field(default=0, ge=0)

    @field_validator("domain_path", mode="before")
    @classmethod
    def domain_path_is_safe(cls, value: object) -> str:
        return _validate_repo_relative_path(value)

    @field_validator("coverage", mode="before")
    @classmethod
    def coverage_is_normalized(cls, value: object) -> tuple[str, ...]:
        return tuple(normalize_repo_relative_paths(value))


class DomainSkeleton(_FrozenDraftModel):
    domain_path: RepoRelativePath
    files: tuple[RepoRelativePath, ...] = Field(min_length=1)

    @field_validator("domain_path", mode="before")
    @classmethod
    def domain_path_is_safe(cls, value: object) -> str:
        return _validate_repo_relative_path(value)

    @field_validator("files", mode="before")
    @classmethod
    def files_are_normalized(cls, value: object) -> tuple[str, ...]:
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise TypeError("domain files must be a sequence")
        return tuple(_validate_repo_relative_path(path) for path in value)


class CoverageResult(_FrozenDraftModel):
    valid: bool
    missing_files: tuple[RepoRelativePath, ...] = ()
    duplicate_files: tuple[RepoRelativePath, ...] = ()
    unknown_files: tuple[RepoRelativePath, ...] = ()


class DossierConsistencyResult(_FrozenDraftModel):
    valid: bool
    reasons: tuple[str, ...] = ()
    unresolved_conflict_count: StrictInt = Field(ge=0)
    reason_code: Literal["DOSSIER_INCONSISTENT"] | None = None


class DraftArtifacts(_FrozenDraftModel):
    """The four core-owned artifacts carried by a draft revision."""

    spec: MigrationSpec
    understanding_dossier: UnderstandingDossier
    target_project_blueprint: TargetProjectBlueprint
    migration_rulebook: MigrationRulebook


DraftArtifactName = Literal[
    "spec",
    "understanding_dossier",
    "target_project_blueprint",
    "migration_rulebook",
]


class ArtifactSnapshot(_FrozenDraftModel):
    name: DraftArtifactName
    version: StrictInt = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: StrictInt = Field(ge=0)
    media_type: str = Field(min_length=1, max_length=256)


class TaskDraftRevision(_FrozenDraftModel):
    revision_id: TaskDraftRevisionId
    revision_number: StrictInt = Field(ge=1)
    artifacts: DraftArtifacts
    artifact_snapshots: tuple[ArtifactSnapshot, ...] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def snapshots_have_unique_names(self) -> TaskDraftRevision:
        names = [snapshot.name for snapshot in self.artifact_snapshots]
        if len(set(names)) != 4:
            raise ValueError("a draft revision must snapshot each artifact exactly once")
        return self


class QuestionOption(_FrozenDraftModel):
    key: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=256)
    impact: str = Field(min_length=1, max_length=1024)
    recommended: bool


class AskUserQuestion(_FrozenDraftModel):
    question_id: QuestionId = Field(default_factory=lambda: QuestionId(new_uuid7()))
    revision_id: TaskDraftRevisionId
    prompt: str = Field(min_length=1, max_length=4096)
    options: tuple[QuestionOption, ...] = Field(min_length=2)
    allow_free_text: bool = False

    @model_validator(mode="after")
    def options_are_mutually_exclusive(self) -> AskUserQuestion:
        keys = [option.key for option in self.options]
        if len(set(keys)) != len(keys):
            raise ValueError("AskUser options must have unique keys")
        if sum(option.recommended for option in self.options) != 1:
            raise ValueError("AskUser must have exactly one recommended option")
        return self


class AskUserAnswer(_FrozenDraftModel):
    question_id: QuestionId
    revision_id: TaskDraftRevisionId
    selected_option: str | None = Field(default=None, min_length=1, max_length=64)
    free_text: str | None = Field(default=None, min_length=1, max_length=4096)

    @model_validator(mode="after")
    def exactly_one_answer_form(self) -> AskUserAnswer:
        if (self.selected_option is None) == (self.free_text is None):
            raise ValueError("an AskUser answer must select one option or provide free text")
        return self


class DraftFreezeReceipt(_FrozenDraftModel):
    revision_id: TaskDraftRevisionId
    revision_number: StrictInt = Field(ge=1)
    artifact_snapshots: tuple[ArtifactSnapshot, ...] = Field(min_length=4, max_length=4)
    frozen_artifact_bundle: FrozenArtifactBundle
    answer_question_ids: tuple[QuestionId, ...] = ()


class TrialTranslation(_FrozenDraftModel):
    file_path: RepoRelativePath
    constrained_output: str = Field(min_length=1)
    freeform_output: str = Field(min_length=1)
    discarded: Literal[True] = True

    @field_validator("file_path", mode="before")
    @classmethod
    def file_path_is_safe(cls, value: object) -> str:
        return _validate_repo_relative_path(value)


__all__ = [
    "ArtifactSnapshot",
    "AskUserAnswer",
    "AskUserQuestion",
    "CoverageResult",
    "DomainSkeleton",
    "DraftArtifacts",
    "DraftFreezeReceipt",
    "DraftStage",
    "DossierConsistencyResult",
    "ExploreReassignment",
    "ExplorationReport",
    "FocusBrief",
    "FocusHighlight",
    "QuestionOption",
    "ReadOnlyDraftTool",
    "TaskDraftRevision",
    "TrialTranslation",
]

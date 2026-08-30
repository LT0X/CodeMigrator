"""Slice output, candidate, and dispatch models."""

from __future__ import annotations

from pydantic import field_validator, model_validator

from .._base import CoreModel
from ..enums import SliceKind
from ..ids import (
    CandidateGeneration,
    CheckId,
    DispatchAttemptId,
    GitOid,
    ProjectModuleId,
    RepoRelativePath,
    RunId,
    SliceId,
    validate_candidate_generation,
)
from ..paths import normalize_repo_relative_paths
from .common import ArtifactRef
from .descriptor import RequiredCheck
from .verification import ExecutionSubject, _subject_commit_oid


class WriteScopeOut(CoreModel):
    write_paths: list[RepoRelativePath]
    create_roots: list[RepoRelativePath]

    @field_validator("write_paths", "create_roots", mode="before")
    @classmethod
    def paths_are_safe(cls, value: object) -> list[str]:
        return normalize_repo_relative_paths(value)  # type: ignore[arg-type]


class WriteScope(CoreModel):
    out: WriteScopeOut


class MigrationSlice(CoreModel):
    id: SliceId
    kind: SliceKind
    source_modules: list[ProjectModuleId]
    write_scope: WriteScope
    required_checks: list[RequiredCheck]
    integration_rank: int
    proposal_ref: ArtifactRef | None


class SliceCandidate(CoreModel):
    run_id: RunId
    slice_id: SliceId
    generation: CandidateGeneration
    base_verified_oid: GitOid
    candidate_commit_oid: GitOid

    @field_validator("generation", mode="before")
    @classmethod
    def generation_is_supported(cls, value: object) -> int:
        return validate_candidate_generation(value)


class ActiveDispatch(CoreModel):
    dispatch_attempt_id: DispatchAttemptId
    subject: ExecutionSubject
    check_id: CheckId
    tested_commit_oid: GitOid

    @model_validator(mode="after")
    def tested_commit_matches_subject(self) -> ActiveDispatch:
        if self.tested_commit_oid != _subject_commit_oid(self.subject):
            raise ValueError("tested commit must match the dispatch subject")
        return self

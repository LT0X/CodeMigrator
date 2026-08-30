"""Slice output, candidate, and dispatch models."""

from __future__ import annotations

from pydantic import field_validator

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
from .descriptor import RequiredCheck
from .verification import ExecutionSubject
from .common import ArtifactRef


class WriteScopeOut(CoreModel):
    write_paths: list[RepoRelativePath]
    create_roots: list[RepoRelativePath]


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

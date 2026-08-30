"""Verification subjects, results, diagnostics, and repair evidence."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import Field, field_validator, model_validator

from .._base import CoreModel
from ..enums import AttributionReliability, CheckStatus, DiagnosticSeverity
from ..ids import (
    CandidateGeneration,
    CheckId,
    GitOid,
    ReceiptId,
    RepoRelativePath,
    RunId,
    Sha256,
    SliceId,
    validate_candidate_generation,
)
from ..paths import _validate_repo_relative_path
from .common import ArtifactRef


class FileLine(CoreModel):
    kind: Literal["FILE_LINE"]
    file_path: RepoRelativePath
    line: int

    @field_validator("file_path", mode="before")
    @classmethod
    def file_path_is_safe(cls, value: object) -> str:
        return _validate_repo_relative_path(value)


class TestIdentity(CoreModel):
    kind: Literal["TEST_IDENTITY"]
    test_name: str


class Unknown(CoreModel):
    kind: Literal["UNKNOWN"]


DiagnosticTarget: TypeAlias = Annotated[
    FileLine | TestIdentity | Unknown,
    Field(discriminator="kind"),
]


class DiagnosticMapping(CoreModel):
    severity: DiagnosticSeverity
    target: DiagnosticTarget
    code: str
    message_hash: Sha256


class CheckResult(CoreModel):
    check_id: CheckId
    invocation_hash: Sha256
    status: CheckStatus
    receipt_id: ReceiptId
    stdout: ArtifactRef
    stderr: ArtifactRef
    diagnostics: list[DiagnosticMapping]


class LocalCandidate(CoreModel):
    kind: Literal["LOCAL_CANDIDATE"]
    slice_id: SliceId
    generation: CandidateGeneration
    candidate_commit_oid: GitOid

    @field_validator("generation", mode="before")
    @classmethod
    def generation_is_supported(cls, value: object) -> int:
        return validate_candidate_generation(value)


class ProspectiveIntegration(CoreModel):
    kind: Literal["PROSPECTIVE_INTEGRATION"]
    slice_id: SliceId
    generation: CandidateGeneration
    expected_verified_oid: GitOid
    prospective_commit_oid: GitOid

    @field_validator("generation", mode="before")
    @classmethod
    def generation_is_supported(cls, value: object) -> int:
        return validate_candidate_generation(value)


class FinalVerified(CoreModel):
    kind: Literal["FINAL_VERIFIED"]
    verified_commit_oid: GitOid


VerificationSubject: TypeAlias = Annotated[
    LocalCandidate | ProspectiveIntegration | FinalVerified,
    Field(discriminator="kind"),
]
ExecutionSubject: TypeAlias = VerificationSubject


def _subject_commit_oid(subject: VerificationSubject) -> GitOid:
    if isinstance(subject, LocalCandidate):
        return subject.candidate_commit_oid
    if isinstance(subject, ProspectiveIntegration):
        return subject.prospective_commit_oid
    return subject.verified_commit_oid


class VerificationOutcome(CoreModel):
    run_id: RunId
    subject: VerificationSubject
    tested_commit_oid: GitOid
    frozen_required_checks_sha256: Sha256
    check_results: list[CheckResult]
    verification_fingerprint: Sha256

    @model_validator(mode="after")
    def tested_commit_matches_subject(self) -> VerificationOutcome:
        if self.tested_commit_oid != _subject_commit_oid(self.subject):
            raise ValueError("tested commit must match the verification subject")
        return self


class DerivedVerificationGuard(CoreModel):
    all_required_checks_passed: bool
    error_unknown_count: int


class IntegrationIntent(CoreModel):
    run_id: RunId
    slice_id: SliceId
    generation: CandidateGeneration
    expected_verified_oid: GitOid
    prospective_commit_oid: GitOid
    guard_sha256: Sha256
    verification_fingerprint: Sha256
    idempotency_key: Sha256

    @field_validator("generation", mode="before")
    @classmethod
    def generation_is_supported(cls, value: object) -> int:
        return validate_candidate_generation(value)


class RepairEvidence(CoreModel):
    candidate_slice_set: list[SliceId]
    reliability: AttributionReliability
    strong_coupling: bool
    cross_generation_recurrence: bool
    conservation_signal_summary: dict[str, object]

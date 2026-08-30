"""Verification subjects, results, diagnostics, and repair evidence."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import Field, field_validator

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
from .common import ArtifactRef


class FileLine(CoreModel):
    kind: Literal["FILE_LINE"]
    file_path: RepoRelativePath
    line: int


class TestIdentity(CoreModel):
    kind: Literal["TEST_IDENTITY"]
    test_name: str


class Unknown(CoreModel):
    kind: Literal["UNKNOWN"]


DiagnosticTarget: TypeAlias = Annotated[FileLine | TestIdentity | Unknown, Field(discriminator="kind")]


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


class VerificationOutcome(CoreModel):
    run_id: RunId
    subject: VerificationSubject
    tested_commit_oid: GitOid
    frozen_required_checks_sha256: Sha256
    check_results: list[CheckResult]
    verification_fingerprint: Sha256


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

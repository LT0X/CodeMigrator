"""Frozen context identity and structural session budget models."""

from __future__ import annotations

from pydantic import field_validator

from .._base import CoreModel
from ..enums import Phase, SessionKind
from ..ids import CandidateGeneration, GitOid, RunId, Sha256, SliceId, validate_candidate_generation


class SliceGenerationRef(CoreModel):
    slice_id: SliceId
    generation: CandidateGeneration
    baseline_candidate_oid: GitOid | None

    @field_validator("generation", mode="before")
    @classmethod
    def generation_is_supported(cls, value: object) -> int:
        return validate_candidate_generation(value)


class ContextPackIdentity(CoreModel):
    run_id: RunId
    phase: Phase
    session: SessionKind
    slice: SliceGenerationRef | None
    spec_sha256: Sha256
    model_binding_sha256: Sha256
    phase_policy_sha256: Sha256
    contract_refs_sha256: Sha256


class SessionBudgetProfile(CoreModel):
    session: SessionKind
    max_rounds: int
    eviction_watermark_pct: int


class ContextPack(CoreModel):
    identity: ContextPackIdentity
    budget: SessionBudgetProfile
    assembled_tokens: int

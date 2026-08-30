"""Frozen context identity and structural session budget models."""

from __future__ import annotations

from pydantic import ConfigDict, Field, field_validator

from .._base import CoreModel
from ..enums import CheckAction, Phase, SessionKind
from ..ids import (
    CandidateGeneration,
    GitOid,
    RunId,
    Sha256,
    SliceId,
    validate_candidate_generation,
)


class SliceGenerationRef(CoreModel):
    model_config = ConfigDict(frozen=True)

    slice_id: SliceId
    generation: CandidateGeneration
    baseline_candidate_oid: GitOid | None

    @field_validator("generation", mode="before")
    @classmethod
    def generation_is_supported(cls, value: object) -> int:
        return validate_candidate_generation(value)


class ContextPackIdentity(CoreModel):
    model_config = ConfigDict(frozen=True)

    run_id: RunId
    phase: Phase
    session: SessionKind
    slice: SliceGenerationRef | None
    spec_sha256: Sha256
    model_binding_sha256: Sha256
    phase_policy_sha256: Sha256
    contract_refs_sha256: Sha256
    plan_revision_sha256: Sha256 = Field(default=Sha256("0" * 64), pattern=r"^[0-9a-f]{64}$")
    skill_catalog_sha256: Sha256 = Field(default=Sha256("0" * 64), pattern=r"^[0-9a-f]{64}$")
    # Zero is the backwards-compatible value for callers that predate the
    # trusted template catalog. ContextManager replaces it with the catalog
    # digest before a pack is admitted to a provider.
    template_sha256: Sha256 = Field(default=Sha256("0" * 64), pattern=r"^[0-9a-f]{64}$")


class SessionBudgetProfile(CoreModel):
    model_config = ConfigDict(frozen=True)

    session: SessionKind
    max_rounds: int = Field(ge=1)
    eviction_watermark_pct: int = Field(ge=1, le=100)


class ContextPack(CoreModel):
    model_config = ConfigDict(frozen=True)

    identity: ContextPackIdentity
    budget: SessionBudgetProfile
    assembled_tokens: int = Field(ge=0)


class CheckpointSummary(CoreModel):
    """Deterministic checkpoint facts used by a recovery brief."""

    model_config = ConfigDict(frozen=True)

    candidate_commit_oid: GitOid
    file_count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)


class CheckFeedbackSummary(CoreModel):
    """A compact check receipt; full output remains in CAS/audit storage."""

    model_config = ConfigDict(frozen=True)

    action: CheckAction
    exit_code: int
    output_digest: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")


class SegmentProgressSummary(CoreModel):
    """Checkpoint-derived progress facts for a structural continuation."""

    model_config = ConfigDict(frozen=True)

    completed_items: tuple[str, ...] = ()
    remaining_task_hints: tuple[str, ...] = ()

    @field_validator("completed_items", "remaining_task_hints", mode="before")
    @classmethod
    def progress_items_are_text(cls, value: object) -> tuple[str, ...]:
        if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
            raise TypeError("progress items must be a sequence of text")
        if any(not isinstance(item, str) or not item.strip() for item in value):
            raise ValueError("progress items must contain non-empty text")
        return tuple(value)


class RecoveryBrief(CoreModel):
    """Fact-only recovery material; it deliberately has no dialogue field."""

    model_config = ConfigDict(frozen=True)

    slice: SliceGenerationRef
    latest_checkpoint: CheckpointSummary | None = None
    recent_check_feedback: tuple[CheckFeedbackSummary, ...] = ()
    discarded_turns: int = Field(ge=0)
    segment_progress: SegmentProgressSummary | None = None

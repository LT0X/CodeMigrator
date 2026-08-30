"""Closed proposal and frozen-plan models owned by the planning package."""

from __future__ import annotations

import copy
import re
from collections.abc import Sequence
from enum import Enum
from typing import Annotated, cast

from pydantic import ConfigDict, Field, StrictInt, field_validator, model_validator

from codemigrator.analysis import AnalysisResult
from codemigrator.core import (
    ArtifactKind,
    DossierEntry,
    FrozenArtifactBundle,
    MigrationRulebook,
    MigrationSlice,
    MigrationSpec,
    PlanEdge,
    PlanEdgeKind,
    ProjectModuleId,
    RepoRelativePath,
    RequiredCheck,
    SliceKind,
    StableErrorCode,
    TargetProjectBlueprint,
    UnderstandingDossier,
    canonical_json_bytes,
    integration_key,
)
from codemigrator.core._base import CoreModel
from codemigrator.core.ids import SliceId
from codemigrator.core.paths import normalize_repo_relative_paths


class EdgeProvenance(str, Enum):
    Structural = "Structural"
    ImportStatic = "ImportStatic"
    ImportUnknown = "ImportUnknown"
    Coverage = "Coverage"
    WriteScopeConflict = "WriteScopeConflict"


class ArtifactAction(str, Enum):
    Generate = "GENERATE"
    Translate = "TRANSLATE"
    Copy = "COPY"


class PlanSliceProposal(CoreModel):
    """A model-produced slice proposal before server-side ID allocation."""

    local_ref: str = Field(min_length=1, max_length=64)
    kind: SliceKind
    source_modules: tuple[ProjectModuleId, ...] = ()
    write_paths: tuple[RepoRelativePath, ...] = ()
    create_roots: tuple[RepoRelativePath, ...] = ()
    rationale: tuple[DossierEntry, ...] = ()
    required_checks: tuple[RequiredCheck, ...] = ()
    artifact_tasks: tuple[ArtifactTask, ...] = ()
    generated: bool = False
    generation_tag: str | None = None
    minimum_nontrivial_assertions: int = Field(default=0, ge=0)
    information_firewall: bool = False

    @field_validator("local_ref")
    @classmethod
    def local_ref_is_safe(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", value) is None:
            raise ValueError("local_ref must be an ASCII identifier")
        return value

    @field_validator("source_modules", mode="before")
    @classmethod
    def source_modules_are_unique(cls, value: object) -> tuple[ProjectModuleId, ...]:
        if value is None:
            return ()
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise TypeError("source_modules must be a sequence")
        modules = tuple(cast(Sequence[ProjectModuleId], value))
        if len(modules) != len(set(modules)):
            raise ValueError("source_modules must not contain duplicates")
        return modules

    @field_validator("write_paths", "create_roots", mode="before")
    @classmethod
    def paths_are_normalized(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        try:
            return tuple(normalize_repo_relative_paths(value))
        except (TypeError, ValueError) as exc:
            raise ValueError(str(exc)) from exc

    @model_validator(mode="after")
    def generation_contract_matches_kind(self) -> PlanSliceProposal:
        if self.kind is SliceKind.TestGeneration:
            self.generated = True
            self.generation_tag = "GENERATED"
            self.minimum_nontrivial_assertions = max(self.minimum_nontrivial_assertions, 1)
            self.information_firewall = True
        elif self.generated or self.generation_tag is not None or self.information_firewall:
            raise ValueError("GENERATED metadata is only valid for TestGeneration slices")
        return self


class PlanEdgeProposal(CoreModel):
    """A typed edge using local references until a plan is frozen."""

    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from", min_length=1, max_length=64)
    to: str = Field(min_length=1, max_length=64)
    kind: PlanEdgeKind
    provenance: EdgeProvenance

    @field_validator("from_", "to")
    @classmethod
    def edge_ref_is_safe(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", value) is None:
            raise ValueError("edge endpoint must be an ASCII local_ref")
        return value


class ArtifactTask(CoreModel):
    """Data-driven handling for one M-06 classified artifact."""

    kind: ArtifactKind
    action: ArtifactAction
    source_path: RepoRelativePath
    target_path: RepoRelativePath

    @field_validator("source_path", "target_path", mode="before")
    @classmethod
    def paths_are_normalized(cls, value: object) -> str:
        try:
            return normalize_repo_relative_paths([value])[0]
        except (TypeError, ValueError) as exc:
            raise ValueError(str(exc)) from exc

    @model_validator(mode="after")
    def action_matches_kind(self) -> ArtifactTask:
        expected = {
            ArtifactKind.GeneratedCode: ArtifactAction.Generate,
            ArtifactKind.DeclarativeConfig: ArtifactAction.Translate,
            ArtifactKind.ResourceFile: ArtifactAction.Copy,
        }[self.kind]
        if self.action is not expected:
            raise ValueError(f"{self.kind.value} artifacts require {expected.value} action")
        return self

    @property
    def generated(self) -> bool:
        return self.kind is ArtifactKind.GeneratedCode

    @property
    def translation(self) -> bool:
        return self.kind is ArtifactKind.DeclarativeConfig


class PlanProposal(CoreModel):
    """The complete structured proposal submitted to machine validation."""

    slices: tuple[PlanSliceProposal, ...]
    edges: tuple[PlanEdgeProposal, ...]
    integration_ranks: dict[str, StrictInt]
    planner_rationale: tuple[DossierEntry, ...]

    @model_validator(mode="after")
    def local_refs_are_unique(self) -> PlanProposal:
        refs = [slice_proposal.local_ref for slice_proposal in self.slices]
        if len(refs) != len(set(refs)):
            raise ValueError("slice local_ref values must be unique")
        return self


class PlanningLimits(CoreModel):
    max_slices: Annotated[int, Field(ge=1)] = 100
    max_edges: Annotated[int, Field(ge=0)] = 500
    max_write_paths_per_slice: Annotated[int, Field(ge=0)] = 200
    max_total_write_paths: Annotated[int, Field(ge=0)] = 2000


class PlanningInputs(CoreModel):
    """Frozen artifacts plus immutable analysis facts consumed by validation."""

    frozen_artifacts: FrozenArtifactBundle
    spec: MigrationSpec
    understanding_dossier: UnderstandingDossier
    target_project_blueprint: TargetProjectBlueprint
    migration_rulebook: MigrationRulebook
    analysis: AnalysisResult
    snapshot_oid: str = Field(min_length=1)
    limits: PlanningLimits = Field(default_factory=PlanningLimits)

class PlanViolation(CoreModel):
    code: StableErrorCode
    pointer: str = Field(min_length=1)
    message: str = Field(min_length=1)
    details: dict[str, object] = Field(default_factory=dict)


class PlanValidation(CoreModel):
    accepted: bool
    violations: tuple[PlanViolation, ...] = ()
    source_coverage: dict[str, str] = Field(default_factory=dict)
    checked_scope_pairs: int = Field(ge=0)
    cycle_check: str
    blueprint_check: str
    rank_check: str
    size_check: str


class FrozenPlan(CoreModel):
    """The immutable planning result exposed to runtime consumers."""

    model_config = ConfigDict(frozen=True)

    snapshot_oid: str
    slices: tuple[MigrationSlice, ...]
    edges: tuple[PlanEdge, ...]
    edge_provenance: tuple[EdgeProvenance, ...]
    validation: PlanValidation
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    integration_order: tuple[SliceId, ...]
    local_ref_to_id: dict[str, SliceId]
    artifact_tasks: dict[str, tuple[ArtifactTask, ...]]
    planner_rationale: tuple[DossierEntry, ...]
    frozen_artifacts: FrozenArtifactBundle
    proposal: PlanProposal

    def __getattribute__(self, name: str) -> object:
        value = super().__getattribute__(name)
        if name in {
            "slices",
            "edges",
            "edge_provenance",
            "validation",
            "integration_order",
            "local_ref_to_id",
            "artifact_tasks",
            "planner_rationale",
            "frozen_artifacts",
            "proposal",
        }:
            return copy.deepcopy(value)
        return value

    @property
    def integration_keys(self) -> tuple[tuple[int, bytes], ...]:
        ranks = self.proposal.integration_ranks
        return tuple(
            integration_key(ranks[local_ref], self.local_ref_to_id[local_ref])
            for local_ref in sorted(
                self.local_ref_to_id,
                key=lambda ref: integration_key(ranks[ref], self.local_ref_to_id[ref]),
            )
        )

    def canonical_payload(self) -> bytes:
        """Return the canonical payload used to calculate and audit this plan."""

        payload = self.model_dump(mode="json", exclude={"plan_hash"})
        return canonical_json_bytes(payload)


__all__ = [
    "ArtifactAction",
    "ArtifactTask",
    "EdgeProvenance",
    "FrozenPlan",
    "PlanEdgeProposal",
    "PlanProposal",
    "PlanSliceProposal",
    "PlanValidation",
    "PlanViolation",
    "PlanningInputs",
    "PlanningLimits",
]

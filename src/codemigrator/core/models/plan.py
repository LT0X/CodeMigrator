"""Canonical plan proposal, validation, and graph-edge contracts."""

from __future__ import annotations

import re
from collections.abc import Sequence
from enum import Enum
from typing import TypedDict, cast

from pydantic import (
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic_core.core_schema import SerializationInfo, SerializerFunctionWrapHandler

from .._base import CoreModel
from ..enums import ArtifactKind, PlanEdgeKind, SliceKind
from ..errors import StableErrorCode
from ..ids import ProjectModuleId, RepoRelativePath, SliceId
from ..paths import normalize_repo_relative_paths
from .common import DossierEntry
from .descriptor import RequiredCheck

_PlanEdgeJson = TypedDict(
    "_PlanEdgeJson",
    {"from": str, "to": str, "kind": str},
)


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


class PlanEdgeEvidence(CoreModel):
    """Auditable reason and source location for conservative planning edges."""

    unknown_reason: str | None = Field(default=None, min_length=1)
    evidence_location: str | None = Field(default=None, min_length=1)


class ArtifactTask(CoreModel):
    """Data-driven handling for one classified artifact fact."""

    kind: ArtifactKind
    action: ArtifactAction
    source_path: RepoRelativePath
    target_path: RepoRelativePath
    artifact_path: RepoRelativePath | None = None

    @field_validator("source_path", "target_path", "artifact_path", mode="before")
    @classmethod
    def paths_are_normalized(cls, value: object) -> str | None:
        if value is None:
            return None
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
    evidence: PlanEdgeEvidence = Field(default_factory=PlanEdgeEvidence)

    @field_validator("from_", "to")
    @classmethod
    def edge_ref_is_safe(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", value) is None:
            raise ValueError("edge endpoint must be an ASCII local_ref")
        return value

    @model_validator(mode="after")
    def conservative_edges_require_evidence(self) -> PlanEdgeProposal:
        has_reason = self.evidence.unknown_reason is not None
        has_location = self.evidence.evidence_location is not None
        if self.provenance is EdgeProvenance.ImportUnknown and not (has_reason and has_location):
            raise ValueError("unknown import edges require a reason and evidence location")
        if self.provenance is EdgeProvenance.WriteScopeConflict and not has_location:
            raise ValueError("write conflict edges require an evidence location")
        if self.provenance not in {
            EdgeProvenance.ImportUnknown,
            EdgeProvenance.WriteScopeConflict,
        } and (has_reason or has_location):
            raise ValueError("edge evidence is only valid for conservative edge provenance")
        return self


class PlanProposal(CoreModel):
    """The complete structured proposal submitted to machine validation."""

    slices: list[PlanSliceProposal]
    edges: list[PlanEdgeProposal]
    integration_ranks: dict[str, StrictInt]
    planner_rationale: list[DossierEntry]

    @model_validator(mode="after")
    def local_refs_are_unique(self) -> PlanProposal:
        refs = [slice_proposal.local_ref for slice_proposal in self.slices]
        if len(refs) != len(set(refs)):
            raise ValueError("slice local_ref values must be unique")
        return self


class PlanViolation(CoreModel):
    code: StableErrorCode
    pointer: str = Field(min_length=1)
    message: str = Field(min_length=1)
    details: dict[str, object] = Field(default_factory=dict)


class PlanValidation(CoreModel):
    accepted: bool
    violations: tuple[PlanViolation, ...]
    source_coverage: dict[str, str] = Field(default_factory=dict)
    checked_scope_pairs: int = Field(ge=0)
    cycle_check: str
    size_check: str
    blueprint_check: str = "NOT_RUN"
    rank_check: str = "NOT_RUN"


class PlanEdge(CoreModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        arbitrary_types_allowed=True,
    )

    from_: SliceId = Field(alias="from")
    to: SliceId
    kind: PlanEdgeKind

    @model_serializer(mode="wrap", when_used="json")
    def serialize_with_json_alias(
        self,
        handler: SerializerFunctionWrapHandler,
        info: SerializationInfo,
    ) -> _PlanEdgeJson:
        data = handler(self)
        if isinstance(data, dict) and not info.by_alias and "from_" in data:
            data["from"] = data.pop("from_")
        return cast(_PlanEdgeJson, data)


__all__ = [
    "ArtifactAction",
    "ArtifactTask",
    "EdgeProvenance",
    "PlanEdge",
    "PlanEdgeEvidence",
    "PlanEdgeProposal",
    "PlanProposal",
    "PlanSliceProposal",
    "PlanValidation",
    "PlanViolation",
]

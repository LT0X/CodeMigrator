"""Planning inputs and frozen outputs built on the canonical core contracts."""

from __future__ import annotations

import copy
from typing import Annotated

from pydantic import ConfigDict, Field

from codemigrator.analysis import AnalysisResult
from codemigrator.core import (
    DossierEntry,
    FrozenArtifactBundle,
    MigrationRulebook,
    MigrationSlice,
    MigrationSpec,
    PlanEdge,
    TargetProjectBlueprint,
    UnderstandingDossier,
    canonical_json_bytes,
    integration_key,
)
from codemigrator.core._base import CoreModel
from codemigrator.core.ids import SliceId
from codemigrator.core.models.plan import (
    ArtifactAction,
    ArtifactTask,
    EdgeProvenance,
    PlanEdgeEvidence,
    PlanEdgeProposal,
    PlanProposal,
    PlanSliceProposal,
    PlanValidation,
    PlanViolation,
)


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


class FrozenPlan(CoreModel):
    """The immutable planning result exposed to runtime consumers."""

    model_config = ConfigDict(frozen=True)

    snapshot_oid: str
    slices: tuple[MigrationSlice, ...]
    edges: tuple[PlanEdge, ...]
    edge_provenance: tuple[EdgeProvenance, ...]
    edge_evidence: tuple[PlanEdgeEvidence, ...]
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
            "edge_evidence",
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
    "PlanEdgeEvidence",
    "PlanEdgeProposal",
    "PlanProposal",
    "PlanSliceProposal",
    "PlanValidation",
    "PlanViolation",
    "PlanningInputs",
    "PlanningLimits",
]

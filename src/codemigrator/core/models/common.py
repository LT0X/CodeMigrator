"""Common artifact and frozen knowledge models."""

from __future__ import annotations

from typing import NewType

from pydantic import model_validator

from .._base import CoreModel
from ..enums import DossierBudgetTier
from ..ids import Sha256

# These slots are intentionally opaque in M-00: the producing analysis and
# planning modules own their detailed vocabularies and may extend them.
DossierEntryKind = NewType("DossierEntryKind", str)
RulebookEntryKind = NewType("RulebookEntryKind", str)
RuleEntrySource = NewType("RuleEntrySource", str)
CoverageSelfReport = dict[str, object]
CodeAnchor = dict[str, object]


class ArtifactRef(CoreModel):
    sha256: Sha256
    size: int
    media_type: str


class DossierEntry(CoreModel):
    kind: DossierEntryKind
    content: str
    anchors: list[CodeAnchor]
    advisory: bool

    @model_validator(mode="after")
    def unanchored_entries_are_advisory(self) -> DossierEntry:
        if not self.anchors and not self.advisory:
            raise ValueError("unanchored dossier entries must be advisory")
        return self


class UnderstandingDossier(CoreModel):
    architecture_narrative: list[DossierEntry]
    semantic_modules: list[DossierEntry]
    dependency_resolutions: list[DossierEntry]
    test_map: list[DossierEntry]
    risk_hotspots: list[DossierEntry]
    strategy_advice: list[DossierEntry]
    coverage_self_report: CoverageSelfReport
    budget_tier: DossierBudgetTier


class RulebookEntry(CoreModel):
    kind: RulebookEntryKind
    content: str
    source: RuleEntrySource
    rationale_ref: ArtifactRef | None
    advisory: bool


class MigrationRulebook(CoreModel):
    entries: list[RulebookEntry]
    version: int


class TargetProjectBlueprint(CoreModel):
    module_boundaries: list[dict[str, object]]
    granularity_principles: list[str]
    target_layout_principles: list[str]
    parallelism_rules: list[str]
    generated_artifact_policy: str
    version: int

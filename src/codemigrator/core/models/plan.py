"""Plan proposal and graph-edge models."""

from __future__ import annotations

from pydantic import ConfigDict, Field

from .._base import CoreModel
from ..enums import PlanEdgeKind
from ..ids import Sha256, SliceId
from .common import DossierEntry


class PlanProposal(CoreModel):
    slices: list[dict]
    edges: list[dict]
    integration_ranks: dict[str, int]
    planner_rationale: list[DossierEntry]


class PlanValidation(CoreModel):
    accepted: bool
    violations: list[dict]
    source_coverage: dict[str, str]
    checked_scope_pairs: int
    cycle_check: str
    size_check: str


class PlanEdge(CoreModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, arbitrary_types_allowed=True)

    from_: SliceId = Field(alias="from")
    to: SliceId
    kind: PlanEdgeKind


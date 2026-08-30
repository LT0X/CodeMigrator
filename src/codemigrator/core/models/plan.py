"""Plan proposal and graph-edge models."""

from __future__ import annotations

from typing import TypedDict, cast

from pydantic import ConfigDict, Field, model_serializer
from pydantic_core.core_schema import SerializationInfo, SerializerFunctionWrapHandler

from .._base import CoreModel
from ..enums import PlanEdgeKind
from ..ids import SliceId
from .common import DossierEntry

_PlanEdgeJson = TypedDict(
    "_PlanEdgeJson",
    {"from": str, "to": str, "kind": str},
)


class PlanProposal(CoreModel):
    slices: list[dict[str, object]]
    edges: list[dict[str, object]]
    integration_ranks: dict[str, int]
    planner_rationale: list[DossierEntry]


class PlanValidation(CoreModel):
    accepted: bool
    violations: list[dict[str, object]]
    source_coverage: dict[str, str]
    checked_scope_pairs: int
    cycle_check: str
    size_check: str


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

"""Strict provider-facing PLAN output projected to the canonical core proposal."""

from __future__ import annotations

import json
from typing import Any

from pydantic import Field, StrictInt, ValidationError, field_validator, model_validator

from codemigrator.core._base import CoreModel
from codemigrator.core.enums import SliceKind
from codemigrator.core.ids import ProjectModuleId, RepoRelativePath
from codemigrator.core.models.common import DossierEntryKind
from codemigrator.core.models.descriptor import RequiredCheck
from codemigrator.core.models.plan import (
    ArtifactTask,
    PlanEdgeProposal,
    PlanProposal,
)


class _DossierEntryAgentOutput(CoreModel):
    """Encode opaque M-00 anchors as JSON text for strict tool schemas."""

    kind: DossierEntryKind
    content: str
    anchors: list[str]
    advisory: bool

    @field_validator("anchors")
    @classmethod
    def anchors_are_json_objects(cls, values: list[str]) -> list[str]:
        for value in values:
            try:
                decoded = json.loads(value)
            except (TypeError, ValueError):
                raise ValueError("PLAN dossier anchors must contain JSON objects") from None
            if not isinstance(decoded, dict):
                raise ValueError("PLAN dossier anchors must contain JSON objects")
        return values


class _PlanSliceAgentOutput(CoreModel):
    local_ref: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    kind: SliceKind
    source_modules: tuple[ProjectModuleId, ...] = ()
    write_paths: tuple[RepoRelativePath, ...] = ()
    create_roots: tuple[RepoRelativePath, ...] = ()
    rationale: tuple[_DossierEntryAgentOutput, ...] = ()
    required_checks: tuple[RequiredCheck, ...] = ()
    artifact_tasks: tuple[ArtifactTask, ...] = ()
    generated: bool = False
    generation_tag: str | None = None
    minimum_nontrivial_assertions: int = Field(default=0, ge=0)
    information_firewall: bool = False


class _IntegrationRankAgentOutput(CoreModel):
    local_ref: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    rank: StrictInt


class PlanProposalAgentOutput(CoreModel):
    """Wire form for strict model output; converted before M-07 validation."""

    slices: list[_PlanSliceAgentOutput]
    edges: list[PlanEdgeProposal]
    integration_ranks: list[_IntegrationRankAgentOutput]
    planner_rationale: list[_DossierEntryAgentOutput]

    @model_validator(mode="after")
    def refs_are_unique(self) -> PlanProposalAgentOutput:
        slice_refs = [item.local_ref for item in self.slices]
        if len(slice_refs) != len(set(slice_refs)):
            raise ValueError("slice local_ref values must be unique")
        rank_refs = [entry.local_ref for entry in self.integration_ranks]
        if len(rank_refs) != len(set(rank_refs)):
            raise ValueError("integration rank local_ref values must be unique")
        return self

    def to_plan_proposal(self) -> PlanProposal:
        payload = self.model_dump(mode="json", by_alias=True)
        payload["integration_ranks"] = {
            entry.local_ref: entry.rank for entry in self.integration_ranks
        }
        payload["slices"] = [
            {
                **slice_payload,
                "rationale": [
                    _decode_dossier_entry(entry)
                    for entry in slice_payload.get("rationale", [])
                ],
            }
            for slice_payload in payload["slices"]
        ]
        payload["planner_rationale"] = [
            _decode_dossier_entry(entry) for entry in payload["planner_rationale"]
        ]
        return PlanProposal.model_validate(payload)


def parse_plan_proposal_agent_output(raw: object) -> dict[str, Any] | None:
    """Convert the PLAN wire shape, or accept a legacy canonical tool payload."""

    if not isinstance(raw, dict):
        return None
    try:
        wire_output = PlanProposalAgentOutput.model_validate(raw)
    except ValidationError:
        try:
            proposal = PlanProposal.model_validate(raw)
        except ValidationError:
            return None
        return proposal.model_dump(mode="json", by_alias=True)
    try:
        proposal = wire_output.to_plan_proposal()
    except (ValidationError, ValueError):
        return None
    return proposal.model_dump(mode="json", by_alias=True)


def _decode_dossier_entry(payload: dict[str, Any]) -> dict[str, Any]:
    decoded: list[dict[str, Any]] = []
    for anchor_json in payload.get("anchors", []):
        try:
            anchor = json.loads(anchor_json)
        except (TypeError, ValueError):
            raise ValueError("PLAN dossier anchors must contain JSON objects") from None
        if not isinstance(anchor, dict):
            raise ValueError("PLAN dossier anchors must contain JSON objects")
        decoded.append(anchor)
    return {**payload, "anchors": decoded}


__all__ = ["PlanProposalAgentOutput", "parse_plan_proposal_agent_output"]

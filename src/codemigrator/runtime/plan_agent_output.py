"""Strict provider-facing PLAN output projected to the canonical core proposal."""

from __future__ import annotations

import json
from copy import deepcopy
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
    normalized = _normalize_agent_output_wire(raw)
    try:
        wire_output = PlanProposalAgentOutput.model_validate(normalized)
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


def _normalize_agent_output_wire(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize equivalent provider representations to the strict PLAN wire form.

    Some OpenAI-compatible endpoints return the canonical object representation
    of M-00's opaque anchors even though strict JSON tool schemas require each
    anchor to be carried as a JSON string. They may also omit the redundant
    ``kind`` in the ``planner_rationale`` collection; that field is implied by
    the collection's owner. Generated-test annotations are derived from the
    authoritative slice kind, matching the core model's own normalization.
    An unanchored entry is downgraded to advisory when the provider incorrectly
    marks it as factual, preserving the core invariant that unsupported
    rationale cannot be accepted as anchored evidence. Other missing or
    malformed fields remain untouched for Pydantic to reject.
    """

    normalized = deepcopy(payload)
    slices = normalized.get("slices")
    if isinstance(slices, list):
        for item in slices:
            if isinstance(item, dict):
                _normalize_generated_metadata(item)
                _normalize_dossier_entries(item.get("rationale"))
    planner_rationale = normalized.get("planner_rationale")
    _normalize_dossier_entries(planner_rationale, planner_owned=True)
    return normalized


def _normalize_generated_metadata(slice_payload: dict[str, Any]) -> None:
    """Re-derive test-generation annotations from the authoritative slice kind."""

    kind = slice_payload.get("kind")
    if kind == SliceKind.TestGeneration.value:
        slice_payload["generated"] = True
        slice_payload["generation_tag"] = "GENERATED"
        minimum = slice_payload.get("minimum_nontrivial_assertions", 0)
        if type(minimum) is int:
            slice_payload["minimum_nontrivial_assertions"] = max(minimum, 1)
        slice_payload["information_firewall"] = True
    elif kind in {item.value for item in SliceKind}:
        slice_payload["generated"] = False
        slice_payload["generation_tag"] = None
        slice_payload["minimum_nontrivial_assertions"] = 0
        slice_payload["information_firewall"] = False


def _normalize_dossier_entries(entries: object, *, planner_owned: bool = False) -> None:
    if not isinstance(entries, list):
        return
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if planner_owned:
            entry.setdefault("kind", "planner")
        anchors = entry.get("anchors")
        if not isinstance(anchors, list):
            continue
        valid_anchors = [
            normalized_anchor
            for anchor in anchors
            if (normalized_anchor := _canonical_anchor_json(anchor)) is not None
        ]
        entry["anchors"] = valid_anchors
        if not valid_anchors and entry.get("advisory") is False:
            entry["advisory"] = True


def _canonical_anchor_json(anchor: object) -> str | None:
    """Return a JSON-object anchor string without inventing missing evidence."""

    decoded: object = anchor
    if isinstance(anchor, str):
        for _ in range(2):
            try:
                decoded = json.loads(anchor)
            except (TypeError, ValueError):
                return None
            if isinstance(decoded, dict):
                break
            if isinstance(decoded, str):
                anchor = decoded
                continue
            return None
        else:
            return None
    if not isinstance(decoded, dict):
        return None
    return json.dumps(decoded, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


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

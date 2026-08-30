"""Judgement-layer advice and repair coordination contracts."""

from __future__ import annotations

from pydantic import field_validator

from .._base import CoreModel
from ..enums import AdviceKind, ResidentRole
from ..ids import AdviceId, RepairDecisionId, RepoRelativePath, RunId, Sha256, SliceId
from ..paths import normalize_repo_relative_paths
from .common import ArtifactRef
from .slice import WriteScope


class Advice(CoreModel):
    advice_id: AdviceId
    kind: AdviceKind
    run_id: RunId
    role: ResidentRole
    payload: dict[str, object]
    proposal_hash: Sha256


class RepairDecision(CoreModel):
    decision_id: RepairDecisionId
    run_id: RunId
    repair_set: list[SliceId]
    domain_split: dict[SliceId, list[RepoRelativePath]]
    brief_refs: list[ArtifactRef]

    @field_validator("domain_split", mode="before")
    @classmethod
    def domain_paths_are_safe(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        try:
            return {key: normalize_repo_relative_paths(paths) for key, paths in value.items()}
        except (TypeError, ValueError) as exc:
            raise ValueError(str(exc)) from exc


class GlobalRepairSession(CoreModel):
    repair_decision_id: RepairDecisionId
    run_id: RunId
    joint_write_scope: WriteScope

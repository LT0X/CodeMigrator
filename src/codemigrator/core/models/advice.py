"""Judgement-layer advice and repair coordination contracts."""

from __future__ import annotations

from .._base import CoreModel
from ..enums import AdviceKind, ResidentRole
from ..ids import AdviceId, RepairDecisionId, RepoRelativePath, RunId, Sha256, SliceId
from .common import ArtifactRef
from .slice import WriteScope


class Advice(CoreModel):
    advice_id: AdviceId
    kind: AdviceKind
    run_id: RunId
    role: ResidentRole
    payload: dict
    proposal_hash: Sha256


class RepairDecision(CoreModel):
    decision_id: RepairDecisionId
    run_id: RunId
    repair_set: list[SliceId]
    domain_split: dict[SliceId, list[RepoRelativePath]]
    brief_refs: list[ArtifactRef]


class GlobalRepairSession(CoreModel):
    repair_decision_id: RepairDecisionId
    run_id: RunId
    joint_write_scope: WriteScope

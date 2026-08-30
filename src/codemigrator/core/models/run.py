"""Run creation, source identity, and Git reference models."""

from __future__ import annotations

from typing import TypeAlias

from .._base import CoreModel
from ..ids import (
    BranchPrefix,
    GitOid,
    GitRefName,
    ProjectId,
    ProjectSnapshotId,
    RepositoryUrl,
)
from .common import ArtifactRef


class GitRunRefs(CoreModel):
    base_commit_oid: GitOid
    verified_commit_oid: GitOid


class FrozenArtifactBundle(CoreModel):
    spec: ArtifactRef
    understanding_dossier: ArtifactRef
    target_project_blueprint: ArtifactRef
    migration_rulebook: ArtifactRef


class RemoteRepository(CoreModel):
    repository_url: RepositoryUrl
    base_ref: GitRefName


class RegisteredProject(CoreModel):
    project_id: ProjectId
    snapshot_id: ProjectSnapshotId


CreateRunSource: TypeAlias = RemoteRepository | RegisteredProject


class CreateRun(CoreModel):
    source: CreateRunSource
    branch_prefix: BranchPrefix
    frozen_artifacts: FrozenArtifactBundle

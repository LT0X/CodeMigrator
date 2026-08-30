"""Typed identifiers and primitive validation helpers."""

from __future__ import annotations

import uuid
from typing import NewType

from uuid_utils import uuid7

RunId = NewType("RunId", uuid.UUID)
SpecId = NewType("SpecId", uuid.UUID)
SliceId = NewType("SliceId", uuid.UUID)
TaskId = NewType("TaskId", uuid.UUID)
CheckId = NewType("CheckId", uuid.UUID)
ReceiptId = NewType("ReceiptId", uuid.UUID)
RequestId = NewType("RequestId", uuid.UUID)
DispatchAttemptId = NewType("DispatchAttemptId", uuid.UUID)
SessionId = NewType("SessionId", uuid.UUID)
MessageId = NewType("MessageId", uuid.UUID)
QuestionId = NewType("QuestionId", uuid.UUID)
TaskDraftRevisionId = NewType("TaskDraftRevisionId", uuid.UUID)
CorrectionIntentId = NewType("CorrectionIntentId", uuid.UUID)
PlanRevisionId = NewType("PlanRevisionId", uuid.UUID)
ProjectId = NewType("ProjectId", uuid.UUID)
ProjectSnapshotId = NewType("ProjectSnapshotId", uuid.UUID)
OutputWorkspaceId = NewType("OutputWorkspaceId", uuid.UUID)
ProjectModuleId = NewType("ProjectModuleId", uuid.UUID)
RepairDecisionId = NewType("RepairDecisionId", uuid.UUID)
AdviceId = NewType("AdviceId", uuid.UUID)

CandidateGeneration = NewType("CandidateGeneration", int)
BranchPrefix = NewType("BranchPrefix", str)
RepoRelativePath = NewType("RepoRelativePath", str)

Sha256 = NewType("Sha256", str)
GitOid = NewType("GitOid", str)
RepositoryUrl = NewType("RepositoryUrl", str)
GitRefName = NewType("GitRefName", str)
LanguageId = NewType("LanguageId", str)


def new_uuid7() -> uuid.UUID:
    """Return a new UUID version 7."""

    return uuid.UUID(str(uuid7()))


def validate_candidate_generation(value: object) -> int:
    """Validate the only supported candidate generations: 0, 1, and 2."""

    if type(value) is not int or value not in (0, 1, 2):
        raise ValueError("candidate generation must be one of 0, 1, or 2")
    return value

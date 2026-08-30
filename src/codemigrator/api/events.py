"""Stable event vocabulary and projection helpers."""

from __future__ import annotations

from enum import Enum


class RunEventType(str, Enum):
    RunStatusChanged = "run.status_changed"
    SliceStatusChanged = "slice.status_changed"
    ContractWaveCompleted = "execute.contract_wave_completed"
    CandidateGenerationStarted = "candidate.generation_started"
    CandidateGenerationInvalidated = "candidate.generation_invalidated"
    DispatchStarted = "dispatch.started"
    DispatchInterrupted = "dispatch.interrupted"
    DispatchDiscarded = "dispatch.discarded"
    DispatchCompleted = "dispatch.completed"
    VerificationCompleted = "verification.completed"
    TestFailureAttributed = "test.failure_attributed"
    FlakyTestObserved = "test.flaky_observed"
    ToolCallPre = "tool.call.pre"
    ToolCallPost = "tool.call.post"
    CheckpointPre = "checkpoint.pre"
    IntegrationQueued = "integration.queued"
    IntegrationStarted = "integration.started"
    IntegrationCompleted = "integration.completed"
    VerifiedAdvanced = "verified.advanced"
    ReportCompleted = "report.completed"
    DeliveryStatusChanged = "delivery.status_changed"
    SliceSegmentContinued = "slice.segment_continued"
    AdviceProposed = "advice.proposed"
    AdviceAdopted = "advice.adopted"
    RepairDecision = "repair.decision"
    RepairSessionStarted = "repair.session.started"
    RepairSessionCompleted = "repair.session.completed"
    RepairSessionBlocked = "repair.session.blocked"
    RepairSessionFailed = "repair.session.failed"


__all__ = ["RunEventType"]

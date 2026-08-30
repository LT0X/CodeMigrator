from __future__ import annotations

from codemigrator.api.events import RunEventType
from codemigrator.core import CORE_METRIC_DESCRIPTORS, DIAGNOSTIC_METRIC_DESCRIPTORS, SecretRegistry
from codemigrator.runtime.observability import (
    DEFAULT_SENTINEL_SINKS,
    ObservationPipeline,
)


def test_observation_contract_does_not_reintroduce_retired_v3_metric_names() -> None:
    names = {item.name for item in (*CORE_METRIC_DESCRIPTORS, *DIAGNOSTIC_METRIC_DESCRIPTORS)}

    assert not any("patch" in name or "replay" in name or "intent" in name for name in names)
    assert set(DEFAULT_SENTINEL_SINKS) >= {
        "stdout",
        "jsonl",
        "run_events",
        "sse",
        "problem_details",
        "tool_output",
        "sandbox_output",
        "report_delivery",
        "metric_exemplar",
    }


def test_run_event_sources_share_one_redaction_pipeline() -> None:
    pipeline = ObservationPipeline(SecretRegistry())
    event_types = (
        RunEventType.ToolCallPre.value,
        RunEventType.ToolCallPost.value,
        RunEventType.CheckpointPre.value,
        RunEventType.AdviceProposed.value,
        RunEventType.AdviceAdopted.value,
        RunEventType.RepairDecision.value,
        RunEventType.RepairSessionStarted.value,
    )

    assert all(
        pipeline.emit(
            {
                "type": event_type,
                "data": {"summary": "safe projection", "result": "accepted"},
            }
        )
        for event_type in event_types
    )

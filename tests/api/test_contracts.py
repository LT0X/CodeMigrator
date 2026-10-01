from __future__ import annotations

from uuid import uuid4

import pytest

from codemigrator.api.dto import MigrationEvent, SpecView
from codemigrator.api.events import RunEventType
from codemigrator.core import SecretRegistry

from .conftest import event


def test_spec_view_does_not_expose_command_fields() -> None:
    fields = set(SpecView.model_fields)
    assert {"program", "argv", "prompt", "write_scope"}.isdisjoint(fields)


def test_run_event_type_contains_judgement_and_repair_lifecycle() -> None:
    assert RunEventType.RunCreated.value == "run.created"
    assert RunEventType.PlanAccepted.value == "run.plan.accepted"
    assert RunEventType.AdviceProposed.value == "advice.proposed"
    assert RunEventType.RepairSessionCompleted.value == "repair.session.completed"
    assert RunEventType.SliceSegmentContinued.value == "slice.segment_continued"


def test_event_envelope_has_six_fields_and_sequence_identity() -> None:
    value = MigrationEvent.from_record(event(uuid4(), 7))
    assert set(value.model_dump(mode="json", by_alias=True)) == {
        "schema",
        "version",
        "type",
        "data",
        "sequence",
        "timestamp_utc",
    }
    assert value.schema == "migration.event"
    assert value.sequence == 7
    assert value.sse_id == "7"


def test_event_data_rejects_secrets_and_full_text() -> None:
    with pytest.raises(ValueError, match="redacted"):
        MigrationEvent.from_record(
            event(uuid4(), 1),
            data={"token": "secret"},
        )


def test_event_data_rejects_credential_aliases_through_the_shared_redaction_boundary() -> None:
    with pytest.raises(ValueError, match="redacted"):
        MigrationEvent.from_record(
            event(uuid4(), 1),
            data={"credential": "secret"},
        )


def test_event_record_can_use_the_runtime_secret_registry() -> None:
    registry = SecretRegistry()
    registry.register("runtime-secret")

    with pytest.raises(ValueError, match="redacted"):
        MigrationEvent.from_record(
            event(uuid4(), 1),
            data={"summary": "runtime-secret"},
            secret_registry=registry,
        )


def test_agent_run_events_project_only_low_sensitivity_summary_fields() -> None:
    run_id = uuid4()
    value = MigrationEvent.from_record(
        event(run_id, 9, "agent_run.terminal"),
        data={
            "agent_run_id": str(uuid4()),
            "phase": "EXECUTE",
            "session_kind": "IMPLEMENTATION",
            "slice_id": str(uuid4()),
            "generation": 2,
            "exit": "COMPLETED",
            "receipt_category": "session.terminal",
            "thread_id": str(uuid4()),
            "checkpoint_uri": "cas://private/thread/checkpoint",
            "prompt": "source code and credentials",
            "provider_error": "private endpoint and response body",
        },
    )

    assert value.data == {
        "agent_run_id": value.data["agent_run_id"],
        "phase": "EXECUTE",
        "session_kind": "IMPLEMENTATION",
        "slice_id": value.data["slice_id"],
        "generation": 2,
        "exit": "COMPLETED",
        "receipt_category": "session.terminal",
    }


def test_agent_run_event_summary_rejects_unbounded_or_invalid_public_fields() -> None:
    with pytest.raises(ValueError, match="AgentRun event summary"):
        MigrationEvent.from_record(
            event(uuid4(), 1, "agent_run.started"),
            data={
                "agent_run_id": str(uuid4()),
                "phase": "prompt text must not be projected",
                "session_kind": "IMPLEMENTATION",
            },
        )

    with pytest.raises(ValueError, match="AgentRun event summary"):
        MigrationEvent.from_record(
            event(uuid4(), 1, "agent_run.started"),
            data={
                "agent_run_id": str(uuid4()),
                "phase": "EXECUTE",
                "session_kind": "IMPLEMENTATION",
                "generation": 1,
            },
        )


def test_run_lifecycle_events_project_only_safe_receipt_fields() -> None:
    run_id = uuid4()
    agent_run_id = uuid4()
    created = MigrationEvent.from_record(
        event(run_id, 1, "run.created"),
        data={
            "status": "PLANNING",
            "state_version": 1,
            "receipt_key": f"run.created:{run_id}",
            "debug_metadata": "private",
        },
    )
    accepted = MigrationEvent.from_record(
        event(run_id, 5, "run.plan.accepted"),
        data={
            "agent_run_id": str(agent_run_id),
            "plan_sha256": "a" * 64,
            "receipt_key": f"run.plan.accepted:{run_id}",
            "prompt": "private",
        },
    )

    assert created.data == {"status": "PLANNING", "state_version": 1}
    assert accepted.data == {"agent_run_id": str(agent_run_id), "plan_sha256": "a" * 64}


@pytest.mark.parametrize(
    ("event_type", "data"),
    [
        ("run.created", {"status": "NOT_A_STATUS", "state_version": 1}),
        ("run.created", {"status": "PLANNING", "state_version": 0}),
        ("run.plan.accepted", {"agent_run_id": "invalid", "plan_sha256": "a" * 64}),
        ("run.plan.accepted", {"agent_run_id": str(uuid4()), "plan_sha256": "g" * 64}),
    ],
)
def test_run_lifecycle_event_projection_rejects_invalid_safe_fields(
    event_type: str, data: dict[str, object]
) -> None:
    with pytest.raises(ValueError, match="run event summary"):
        MigrationEvent.from_record(event(uuid4(), 1, event_type), data=data)

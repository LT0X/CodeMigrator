from __future__ import annotations

from uuid import uuid4

import pytest

from codemigrator.api.dto import MigrationEvent, SpecView
from codemigrator.api.events import RunEventType

from .conftest import event


def test_spec_view_does_not_expose_command_fields() -> None:
    fields = set(SpecView.model_fields)
    assert {"program", "argv", "prompt", "write_scope"}.isdisjoint(fields)


def test_run_event_type_contains_judgement_and_repair_lifecycle() -> None:
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

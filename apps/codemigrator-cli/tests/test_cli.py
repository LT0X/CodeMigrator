from __future__ import annotations

import json

from codemigrator_cli.__main__ import run_command
from codemigrator_cli.cancel import CancelAction, CancelController
from codemigrator_cli.exit_codes import ExitCode
from codemigrator_cli.http import _event_from_lines
from codemigrator_cli.models import RunEvent
from codemigrator_cli.projector import project_events
from codemigrator_cli.renderer import render_human, render_json, render_jsonl


def event(sequence: int, event_type: str, data: dict[str, object]) -> RunEvent:
    return RunEvent(
        sequence=sequence,
        type=event_type,
        data=data,
        timestamp_utc="2026-08-30T00:00:00Z",
    )


def test_projector_reduces_facts_without_sensitive_payload() -> None:
    projection = project_events(
        [
            event(1, "dispatch.started", {"slice_id": "slice-a", "prompt": "secret"}),
            event(2, "slice.status_changed", {"slice_id": "slice-a", "status": "LOCAL_VERIFYING"}),
        ]
    )
    assert projection.cursor == 2
    assert projection.slices["slice-a"].action == "run"
    assert "secret" not in render_json(projection)


def test_renderers_share_projected_facts_and_jsonl_is_stable() -> None:
    events = [
        event(1, "dispatch.started", {"slice_id": "slice-a", "generation": 0}),
        event(2, "integration.queued", {"slice_id": "slice-a", "integration_rank": 3}),
    ]
    projection = project_events(events)
    assert "slice-a" in render_human(projection)
    assert json.loads(render_json(projection))["cursor"] == 2
    lines = list(render_jsonl(events))
    assert [json.loads(line)["sequence"] for line in lines] == [1, 2]
    assert json.loads(lines[1])["action"] == "wait"


def test_projector_marks_gap_and_caps_visible_activity() -> None:
    events = [
        event(index, "dispatch.started", {"slice_id": f"slice-{index}"})
        for index in range(1, 7)
    ]
    projection = project_events(events, memory_gib=16, cpu_cores=8)
    assert len(projection.active_slices) == 4
    gap = project_events([events[0], events[2]])
    assert gap.connection == "catching-up"
    assert gap.cursor == 1


def test_exit_codes_are_stable() -> None:
    assert ExitCode.COMPLETED == 0
    assert ExitCode.PARTIALLY_COMPLETED == 2
    assert ExitCode.UNKNOWN == 5
    assert ExitCode.LOCAL_CANCEL_CONFIRMED == 130


def test_no_follow_returns_only_creation_projection() -> None:
    code, output = run_command(
        ["migrate", "start", "demo.spec", "--no-follow", "--output", "json"]
    )
    assert code == 0
    created = json.loads(output)
    assert created == {
        "run_id": "mock-run-001",
        "status": "CREATED",
        "web_url": "/runs/mock-run-001",
    }


def test_cancel_controller_waits_for_persisted_confirmation() -> None:
    controller = CancelController()
    assert controller.interrupt() is CancelAction.REQUEST
    assert controller.interrupt() is CancelAction.EXIT
    assert controller.confirmed is False
    controller.observe("CANCELLED")
    assert controller.confirmed is True


def test_run_cancel_uses_if_match_and_returns_cancel_exit_code() -> None:
    code, output = run_command(["run", "cancel", "run-1", "--if-match", "8", "--output", "json"])
    assert code == 4
    assert json.loads(output) == {"run_id": "run-1", "status": "CANCELLED", "version": 9}


def test_sse_transport_accepts_only_the_versioned_event_shape() -> None:
    parsed = _event_from_lines(['{"type":"dispatch.started","sequence":3,"data":{"slice_id":"a"}}'])
    assert parsed is not None
    assert parsed.sequence == 3
    assert parsed.type == "dispatch.started"

from __future__ import annotations

import json

import pytest
from codemigrator_cli.__main__ import (
    _CancelConfirmed,
    _request_interrupt_cancel,
    run_command,
)
from codemigrator_cli.cancel import CancelAction, CancelController
from codemigrator_cli.client import HttpRunControl, StaleVersionError
from codemigrator_cli.exit_codes import ExitCode
from codemigrator_cli.http import HttpEventSource, _event_from_lines
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


def test_projector_requires_matching_generation_and_verified_pair() -> None:
    events = [
        event(1, "candidate.generation_started", {"slice_id": "slice-a", "generation": 1}),
        event(2, "verified.advanced", {"slice_id": "slice-a", "generation": 0}),
        event(3, "integration.completed", {"slice_id": "slice-a", "generation": 0}),
    ]
    projection = project_events(events)
    assert projection.slices["slice-a"].generation == 1
    assert projection.slices["slice-a"].status == "REGENERATING"
    assert not projection.celebrations


def test_safe_jsonl_projection_is_whitelist_and_secret_resistant() -> None:
    projection = project_events(
        [
            event(
                1,
                "dispatch.started",
                {
                    "slice_id": "slice-a",
                    "api_key": "hidden",
                    "authorization": "Bearer hidden",
                    "summary": "private_key=hidden",
                },
            )
        ]
    )
    rendered = render_json(projection)
    assert "hidden" not in rendered


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


def test_run_watch_no_follow_projects_current_events() -> None:
    code, output = run_command(["run", "watch", "run-1", "--no-follow", "--output", "json"])
    assert code == 0
    assert json.loads(output)["cursor"] == 13


def test_transport_failure_returns_bounded_unknown_result() -> None:
    class BrokenSource:
        def events(self):
            raise ValueError("private path /home/secret")

    code, output = run_command(
        ["run", "watch", "run-1", "--follow", "--output", "json"],
        source=BrokenSource(),
    )
    assert code == int(ExitCode.UNKNOWN)
    assert json.loads(output) == {"run_id": "run-1", "status": "UNKNOWN"}
    assert "secret" not in output


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


def test_run_cancel_does_not_retry_after_stale_if_match() -> None:
    class StaleControl:
        def __init__(self) -> None:
            self.show_calls = 0
            self.cancel_calls: list[int] = []

        def show(self, run_id: str) -> dict[str, object]:
            del run_id
            self.show_calls += 1
            return {"run_id": "run-1", "status": "EXECUTING", "version": 9}

        def cancel(self, run_id: str, expected_version: int) -> dict[str, object]:
            del run_id
            self.cancel_calls.append(expected_version)
            raise StaleVersionError("stale")

    control = StaleControl()
    code, output = run_command(
        ["run", "cancel", "run-1", "--if-match", "8", "--output", "json"],
        control=control,
    )

    assert code == int(ExitCode.UNKNOWN)
    assert json.loads(output) == {"run_id": "run-1", "status": "STALE_VERSION"}
    assert control.cancel_calls == [8]
    assert control.show_calls == 0


def test_follow_calls_back_before_the_event_source_finishes() -> None:
    class FiniteSource:
        def events(self):
            yield event(1, "run.status_changed", {"run_status": "EXECUTING"})
            yield event(2, "run.status_changed", {"run_status": "COMPLETED"})

    observed: list[int] = []
    code, output = run_command(
        ["run", "watch", "run-1", "--follow", "--output", "human"],
        source=FiniteSource(),
        on_event=lambda item: observed.append(item.sequence),
    )

    assert code == 0
    assert observed == [1, 2]
    assert "sequence 2" in output


def test_interrupt_cancel_retries_once_after_stale_version() -> None:
    class RefreshingControl:
        def __init__(self) -> None:
            self.show_calls = 0
            self.cancel_calls: list[int] = []

        def show(self, run_id: str) -> dict[str, object]:
            del run_id
            self.show_calls += 1
            return {"run_id": "run-1", "status": "EXECUTING", "version": 9}

        def cancel(self, run_id: str, expected_version: int) -> dict[str, object]:
            del run_id
            self.cancel_calls.append(expected_version)
            if len(self.cancel_calls) == 1:
                raise StaleVersionError("stale")
            return {"run_id": "run-1", "status": "CANCELLED", "version": 10}

    control = RefreshingControl()
    with pytest.raises(_CancelConfirmed):
        _request_interrupt_cancel(control, "run-1", CancelController(), "json")

    assert control.show_calls == 2
    assert control.cancel_calls == [9, 9]


def test_sse_transport_accepts_only_the_versioned_event_shape() -> None:
    parsed = _event_from_lines(
        ['{"schema":"migration.event","version":1,"type":"dispatch.started","sequence":3,"data":{"slice_id":"a"}}']
    )
    assert parsed is not None
    assert parsed.sequence == 3
    assert parsed.type == "dispatch.started"


def test_sse_transport_rejects_wrong_schema_and_id() -> None:
    with pytest.raises(ValueError, match="envelope"):
        _event_from_lines(
            ['{"schema":"other.event","version":1,"type":"x","sequence":3,"data":{}}']
        )
    with pytest.raises(ValueError, match="id"):
        _event_from_lines(
            ['{"schema":"migration.event","version":1,"type":"x","sequence":3,"data":{}}'],
            event_id="4",
        )


def test_http_event_source_sends_auth_and_replays_from_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[object] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def __iter__(self):
            return iter(
                [
                    b"id: 3\n",
                    b'data: {"schema":"migration.event","version":1,"type":"x",'
                    b'"sequence":3,"data":{}}\n',
                    b"\n",
                ]
            )

    def open_url(request: object, *, timeout: int) -> Response:
        del timeout
        requests.append(request)
        return Response()

    monkeypatch.setattr("codemigrator_cli.http.urlopen", open_url)
    source = HttpEventSource(
        "https://api.example.test/api/v1",
        "run/1",
        after_sequence=2,
        token="secret",
    )
    events = list(source.events())
    request = requests[0]
    assert events[0].sequence == 3
    assert request.full_url == "https://api.example.test/api/v1/migrations/run%2F1/events"
    assert request.get_header("Authorization") == "Bearer secret"
    assert request.get_header("Last-event-id") == "2"


def test_http_run_control_sends_quoted_if_match(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[object] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"run_id":"run-1","status":"CANCELLED","version":9}'

    def open_url(request: object, *, timeout: int) -> Response:
        del timeout
        requests.append(request)
        return Response()

    monkeypatch.setattr("codemigrator_cli.client.urlopen", open_url)
    payload = HttpRunControl("https://api.example.test/api/v1", token="secret").cancel(
        "run-1", 8
    )
    assert payload["status"] == "CANCELLED"
    assert requests[0].get_header("If-match") == '"8"'

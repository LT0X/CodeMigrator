from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from codemigrator.api.deps import EventRecord
from codemigrator.api.dto import MigrationEvent, SessionEvent
from codemigrator.api.sse import (
    ConnectionLimitError,
    SseConnectionManager,
    SseQueueOverflowError,
    sse_events,
)
from codemigrator.core import SecretRegistry
from codemigrator.runtime.contracts import DraftSessionEvent, RuntimeEvent

from .conftest import FakeBackend, event


def test_sse_connection_limit_is_explicit() -> None:
    manager = SseConnectionManager(limit=1)
    first = manager.acquire()
    with pytest.raises(ConnectionLimitError):
        manager.acquire()
    first.close()
    second = manager.acquire()
    second.close()


@pytest.mark.asyncio
async def test_sse_replays_after_cursor_before_heartbeat() -> None:
    backend = FakeBackend()
    run_id = uuid4()
    backend.events = [event(run_id, 1), event(run_id, 2)]
    stream = sse_events(backend, run_id, after_sequence=1, heartbeat_seconds=0.001)
    first = await anext(stream)
    assert '"sequence":2' in first.encode().decode()
    await asyncio.sleep(0)
    heartbeat = await anext(stream)
    assert ": heartbeat" in heartbeat.encode().decode()
    await stream.aclose()


@pytest.mark.asyncio
async def test_sse_closes_when_pending_queue_is_full() -> None:
    backend = FakeBackend()
    run_id = uuid4()
    backend.events = [event(run_id, 1), event(run_id, 2)]
    stream = sse_events(backend, run_id, after_sequence=0, queue_size=1)
    with pytest.raises(SseQueueOverflowError):
        await anext(stream)
    await stream.aclose()


@pytest.mark.asyncio
async def test_sse_closes_after_terminal_run_event() -> None:
    backend = FakeBackend()
    run_id = uuid4()
    backend.events = [
        EventRecord(
            run_id=run_id,
            sequence=1,
            event_type="run.status_changed",
            data={"run_status": "COMPLETED"},
            timestamp_utc=datetime.now(UTC),
        )
    ]
    stream = sse_events(backend, run_id, after_sequence=0)
    await anext(stream)
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


@pytest.mark.asyncio
async def test_sse_rechecks_ledger_before_emitting_heartbeat() -> None:
    class EventArrivesDuringWait(FakeBackend):
        inserted = False

        async def wait_for_events(self, run_id: UUID, after_sequence: int) -> None:
            if not self.inserted:
                self.events.append(event(run_id, after_sequence + 1))
                self.inserted = True
            await asyncio.sleep(0.1)

    backend = EventArrivesDuringWait()
    run_id = uuid4()
    stream = sse_events(backend, run_id, after_sequence=0, heartbeat_seconds=0.001)
    first = await anext(stream)
    assert '"sequence":1' in first.encode().decode()
    await stream.aclose()


@pytest.mark.asyncio
async def test_sse_reconnect_after_terminal_cursor_closes_immediately() -> None:
    backend = FakeBackend()
    run_id = uuid4()
    backend.events = [
        EventRecord(
            run_id=run_id,
            sequence=1,
            event_type="run.status_changed",
            data={"run_status": "FAILED"},
            timestamp_utc=datetime.now(UTC),
        )
    ]
    stream = sse_events(backend, run_id, after_sequence=1)
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


@pytest.mark.asyncio
async def test_sse_replays_agent_run_lifecycle_without_treating_it_as_run_terminal() -> None:
    backend = FakeBackend()
    run_id = uuid4()
    agent_run_id = uuid4()
    backend.events = [
        EventRecord(
            run_id=run_id,
            sequence=1,
            event_type="agent_run.started",
            data={
                "agent_run_id": str(agent_run_id),
                "phase": "EXECUTE",
                "session_kind": "IMPLEMENTATION",
            },
            timestamp_utc=datetime.now(UTC),
        ),
        EventRecord(
            run_id=run_id,
            sequence=2,
            event_type="agent_run.terminal",
            data={
                "agent_run_id": str(agent_run_id),
                "phase": "EXECUTE",
                "session_kind": "IMPLEMENTATION",
                "exit": "COMPLETED",
                "receipt_category": "session.terminal",
            },
            timestamp_utc=datetime.now(UTC),
        ),
        EventRecord(
            run_id=run_id,
            sequence=3,
            event_type="run.status_changed",
            data={"run_status": "COMPLETED"},
            timestamp_utc=datetime.now(UTC),
        ),
    ]

    stream = sse_events(backend, run_id, after_sequence=0)
    first = await anext(stream)
    second = await anext(stream)
    terminal = await anext(stream)
    assert '"sequence":1' in first.encode().decode()
    assert '"type":"agent_run.started"' in first.encode().decode()
    assert '"sequence":2' in second.encode().decode()
    assert '"type":"agent_run.terminal"' in second.encode().decode()
    assert '"sequence":3' in terminal.encode().decode()
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    await stream.aclose()

    resumed = sse_events(backend, run_id, after_sequence=1)
    replayed = [await anext(resumed), await anext(resumed)]
    assert '"sequence":2' in replayed[0].encode().decode()
    assert '"sequence":3' in replayed[1].encode().decode()
    with pytest.raises(StopAsyncIteration):
        await anext(resumed)
    await resumed.aclose()


@pytest.mark.asyncio
async def test_session_sse_uses_session_event_envelope() -> None:
    backend = FakeBackend()
    session_id = uuid4()
    backend.session_events = [event(session_id, 1, "assistant.message.completed")]
    stream = sse_events(
        backend,
        session_id,
        after_sequence=0,
        heartbeat_seconds=0.001,
        event_name="migration.session.event",
        envelope_type=SessionEvent,
    )
    first = await anext(stream)
    encoded = first.encode().decode()
    assert "event: migration.session.event" in encoded
    assert '"schema":"migration.session.event"' in encoded
    assert "session.read" in backend.stream_calls
    assert not any(call.startswith("run.") for call in backend.stream_calls)
    await stream.aclose()


@pytest.mark.asyncio
async def test_session_sse_uses_session_port_and_stops_at_attached_cursor() -> None:
    backend = FakeBackend()
    session_id = uuid4()
    backend.events = [
        EventRecord(
            run_id=session_id,
            sequence=2,
            event_type="run.status_changed",
            data={"status": "COMPLETED"},
            timestamp_utc=datetime.now(UTC),
        )
    ]
    backend.session_events = [
        event(session_id, 1, "session.question.asked"),
        event(session_id, 2, "session.attached_to_run"),
        event(session_id, 3, "assistant.message.completed"),
    ]

    stream = sse_events(
        backend,
        session_id,
        after_sequence=1,
        event_name="migration.session.event",
        envelope_type=SessionEvent,
    )
    attached = await anext(stream)
    assert '"sequence":2' in attached.encode().decode()
    assert '"type":"session.attached_to_run"' in attached.encode().decode()
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert all(call.startswith("session.") for call in backend.stream_calls)
    await stream.aclose()

    resumed = sse_events(
        backend,
        session_id,
        after_sequence=2,
        event_name="migration.session.event",
        envelope_type=SessionEvent,
    )
    with pytest.raises(StopAsyncIteration):
        await anext(resumed)
    await resumed.aclose()


@pytest.mark.asyncio
async def test_session_status_event_does_not_close_stream():
    backend = FakeBackend()
    session_id = uuid4()
    backend.session_events = [
        EventRecord(
            run_id=session_id,
            sequence=1,
            event_type="session.status_changed",
            data={"status": "CLOSED"},
            timestamp_utc=datetime.now(UTC),
        )
    ]
    stream = sse_events(
        backend,
        session_id,
        after_sequence=1,
        heartbeat_seconds=0.001,
        event_name="migration.session.event",
        envelope_type=SessionEvent,
    )
    heartbeat = await anext(stream)
    assert ": heartbeat" in heartbeat.encode().decode()
    await stream.aclose()


@pytest.mark.asyncio
async def test_session_sse_rechecks_after_commit_between_read_and_wait() -> None:
    class CommitBeforeWait(FakeBackend):
        async def wait_for_session_events(self, session_id: UUID, after_sequence: int) -> None:
            self.stream_calls.append("session.wait")
            self.session_events.append(event(session_id, after_sequence + 1, "session.closed"))

    backend = CommitBeforeWait()
    session_id = uuid4()
    stream = sse_events(
        backend,
        session_id,
        after_sequence=0,
        heartbeat_seconds=1,
        event_name="migration.session.event",
        envelope_type=SessionEvent,
    )
    closed = await anext(stream)
    assert '"sequence":1' in closed.encode().decode()
    assert '"type":"session.closed"' in closed.encode().decode()
    assert backend.stream_calls[:3] == [
        "session.terminal",
        "session.read",
        "session.wait",
    ]
    await stream.aclose()


def test_persisted_run_and_draft_events_adapt_without_changing_fields() -> None:
    stream_id = uuid4()
    timestamp = datetime(2026, 9, 30, 10, 11, 12, tzinfo=UTC)
    run_event = RuntimeEvent(4, "slice.status_changed", {"status": "RUNNING"}, timestamp)
    draft_event = DraftSessionEvent(
        stream_id, 5, "session.question.asked", {"question_id": "q-1"}, timestamp
    )

    adapted_run = EventRecord.from_persisted(stream_id, run_event)
    adapted_draft = EventRecord.from_persisted(stream_id, draft_event)

    assert (adapted_run.sequence, adapted_run.event_type, adapted_run.data) == (
        4,
        "slice.status_changed",
        {"status": "RUNNING"},
    )
    assert (adapted_draft.sequence, adapted_draft.event_type, adapted_draft.data) == (
        5,
        "session.question.asked",
        {"question_id": "q-1"},
    )
    assert adapted_run.timestamp_utc == adapted_draft.timestamp_utc == timestamp


def test_migration_events_project_only_safe_integration_fields() -> None:
    timestamp = datetime(2026, 10, 2, 10, 11, 12, tzinfo=UTC)
    slice_id = str(uuid4())
    completed = MigrationEvent(
        type="integration.completed",
        data={
            "receipt_key": "integration.completed:private-key",
            "slice_id": slice_id,
            "generation": 1,
            "verified_commit_oid": "a" * 40,
            "provider_response": "private integration details",
        },
        sequence=1,
        timestamp_utc=timestamp,
    )
    advanced = MigrationEvent(
        type="verified.advanced",
        data={
            "slice_id": slice_id,
            "generation": 1,
            "commit_oid": "a" * 40,
            "source_diff": "private source data",
        },
        sequence=2,
        timestamp_utc=timestamp,
    )

    assert completed.data == {
        "slice_id": slice_id,
        "generation": 1,
        "verified_commit_oid": "a" * 40,
    }
    assert advanced.data == {"slice_id": slice_id, "generation": 1, "commit_oid": "a" * 40}


@pytest.mark.asyncio
async def test_sse_applies_the_runtime_secret_registry_before_projection() -> None:
    backend = FakeBackend()
    run_id = uuid4()
    backend.events = [event(run_id, 1)]
    registry = SecretRegistry()
    registry.register("runtime-secret")
    backend.events[0] = EventRecord(
        run_id=run_id,
        sequence=1,
        event_type="tool.call.post",
        data={"summary": "runtime-secret"},
        timestamp_utc=datetime.now(UTC),
    )
    stream = sse_events(
        backend,
        run_id,
        after_sequence=0,
        secret_registry=registry,
    )

    with pytest.raises(ValueError, match="redacted"):
        await anext(stream)
    await stream.aclose()

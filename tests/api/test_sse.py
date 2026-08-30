from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from codemigrator.api.deps import EventRecord
from codemigrator.api.sse import (
    ConnectionLimitError,
    SseConnectionManager,
    SseQueueOverflowError,
    sse_events,
)

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

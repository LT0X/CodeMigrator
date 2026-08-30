"""Strict event-ledger replay and SSE connection admission."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

from sse_starlette import ServerSentEvent

from codemigrator.core import RunStatus

from .deps import ApiBackend
from .dto import MigrationEvent, SessionEvent

EventEnvelope = type[MigrationEvent] | type[SessionEvent]


class ConnectionLimitError(RuntimeError):
    """The process-wide SSE connection limit has been reached."""


class SseQueueOverflowError(RuntimeError):
    """The connection's bounded pending-event queue cannot accept more data."""


@dataclass(slots=True)
class ConnectionLease:
    _manager: SseConnectionManager
    _closed: bool = False

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._manager.release()


class SseConnectionManager:
    def __init__(self, *, limit: int = 100, queue_size: int = 64) -> None:
        if type(limit) is not int or limit < 1:
            raise ValueError("SSE connection limit must be positive")
        if type(queue_size) is not int or queue_size < 1:
            raise ValueError("SSE queue size must be positive")
        self.limit = limit
        self.queue_size = queue_size
        self._active = 0

    def acquire(self) -> ConnectionLease:
        if self._active >= self.limit:
            raise ConnectionLimitError("SSE connection limit reached")
        self._active += 1
        return ConnectionLease(self)

    def release(self) -> None:
        self._active = max(0, self._active - 1)

    @property
    def active(self) -> int:
        return self._active


async def sse_events(
    backend: ApiBackend,
    run_id: UUID,
    *,
    after_sequence: int,
    heartbeat_seconds: float = 15.0,
    queue_size: int = 64,
    event_name: str = "migration.event",
    envelope_type: EventEnvelope = MigrationEvent,
) -> AsyncIterator[ServerSentEvent]:
    """Replay committed events through a bounded queue with heartbeat fallback."""

    if type(queue_size) is not int or queue_size < 1:
        raise ValueError("SSE queue size must be positive")
    if heartbeat_seconds <= 0:
        raise ValueError("heartbeat_seconds must be positive")

    queue: asyncio.Queue[ServerSentEvent] = asyncio.Queue(maxsize=queue_size)
    producer = asyncio.create_task(
        _produce_events(
            backend,
            run_id,
            after_sequence=after_sequence,
            heartbeat_seconds=heartbeat_seconds,
            queue=queue,
            event_name=event_name,
            envelope_type=envelope_type,
        )
    )
    try:
        while True:
            item = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait(
                {item, producer}, return_when=asyncio.FIRST_COMPLETED
            )
            if producer in done:
                producer.result()
                if item not in done:
                    item.cancel()
                    await asyncio.gather(item, return_exceptions=True)
                    return
            if item in done:
                yield item.result()
    finally:
        producer.cancel()
        await asyncio.gather(producer, return_exceptions=True)


async def _produce_events(
    backend: ApiBackend,
    run_id: UUID,
    *,
    after_sequence: int,
    heartbeat_seconds: float,
    queue: asyncio.Queue[ServerSentEvent],
    event_name: str,
    envelope_type: EventEnvelope,
) -> None:
    cursor = after_sequence
    while True:
        records = await backend.read_events(run_id, cursor)
        delivered = False
        for record in sorted(records, key=lambda item: item.sequence):
            if record.sequence <= cursor:
                continue
            envelope = envelope_type.from_record(record)
            _enqueue(
                queue,
                ServerSentEvent(
                    data=json.dumps(
                        envelope.model_dump(mode="json", by_alias=True),
                        separators=(",", ":"),
                    ),
                    event=event_name,
                    id=envelope.sse_id,
                ),
            )
            cursor = envelope.sequence
            delivered = True
            if _is_terminal(envelope):
                return
        if delivered:
            continue
        if await backend.is_stream_terminal(run_id, cursor):
            return
        try:
            await asyncio.wait_for(
                backend.wait_for_events(run_id, cursor), timeout=heartbeat_seconds
            )
        except TimeoutError:
            if await backend.read_events(run_id, cursor):
                continue
            if await backend.is_stream_terminal(run_id, cursor):
                return
            _enqueue(queue, ServerSentEvent(comment="heartbeat"))


def _enqueue(queue: asyncio.Queue[ServerSentEvent], event: ServerSentEvent) -> None:
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull as exc:
        raise SseQueueOverflowError("SSE pending-event queue is full") from exc


def _is_terminal(event: MigrationEvent | SessionEvent) -> bool:
    expected_type = (
        {"session.status_changed", "session.closed"}
        if event.schema == "migration.session.event"
        else {"run.status_changed"}
    )
    if event.type not in expected_type:
        return False
    status = event.data.get("run_status", event.data.get("status"))
    terminal_statuses = {
        RunStatus.Completed.value,
        RunStatus.PartiallyCompleted.value,
        RunStatus.Failed.value,
        RunStatus.Cancelled.value,
    }
    if event.schema == "migration.session.event":
        terminal_statuses.add("CLOSED")
    return getattr(status, "value", status) in terminal_statuses


def parse_sequence_header(value: str | None, *, name: str) -> int:
    if value is None:
        return 0
    if not value.isascii() or not value.isdecimal():
        raise ValueError(f"{name} must be a non-negative decimal integer")
    result = int(value)
    if result < 0:
        raise ValueError(f"{name} must be non-negative")
    return result


__all__ = [
    "ConnectionLease",
    "ConnectionLimitError",
    "SseConnectionManager",
    "SseQueueOverflowError",
    "parse_sequence_header",
    "sse_events",
]

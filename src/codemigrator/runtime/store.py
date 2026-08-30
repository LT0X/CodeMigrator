"""Runtime persistence ports and a deterministic transactional test adapter."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from typing import Any, Protocol, cast
from uuid import UUID

from pydantic import BaseModel

from codemigrator.core import ActiveDispatch, FailureReason, RunId, RunStatus

from .budget import BudgetUsage
from .contracts import EventSpec, RunState, RuntimeEvent, RuntimeSnapshot
from .schema import RUNTIME_SCHEMA_SQL


class RuntimeStore(Protocol):
    async def load(self, run_id: RunId) -> RuntimeSnapshot | None:
        """Load one Run and its append-only events."""

    async def create(self, state: RunState, events: Sequence[EventSpec]) -> RuntimeSnapshot:
        """Insert a new Run atomically with its first events."""

    async def commit(self, state: RunState, events: Sequence[EventSpec]) -> RuntimeSnapshot:
        """Commit state and events in one transaction."""


class StoreCommitError(RuntimeError):
    """Raised when the persistence transaction cannot be committed."""


class InMemoryRuntimeStore:
    """A transactionally behaving store double for actor and contract tests."""

    def __init__(self) -> None:
        self._snapshots: dict[RunId, RuntimeSnapshot] = {}
        self.commit_count = 0
        self._fail_next = False

    async def load(self, run_id: RunId) -> RuntimeSnapshot | None:
        return self._snapshots.get(run_id)

    async def create(self, state: RunState, events: Sequence[EventSpec]) -> RuntimeSnapshot:
        if state.run_id in self._snapshots:
            raise StoreCommitError("run already exists")
        return await self._write(state, events)

    async def commit(self, state: RunState, events: Sequence[EventSpec]) -> RuntimeSnapshot:
        if state.run_id not in self._snapshots:
            raise StoreCommitError("run does not exist")
        return await self._write(state, events)

    async def snapshot(self, run_id: RunId) -> RuntimeSnapshot:
        snapshot = await self.load(run_id)
        if snapshot is None:
            raise KeyError(run_id)
        return snapshot

    def fail_next_commit(self) -> None:
        self._fail_next = True

    async def _write(self, state: RunState, events: Sequence[EventSpec]) -> RuntimeSnapshot:
        if self._fail_next:
            self._fail_next = False
            raise StoreCommitError("injected commit failure")
        previous = self._snapshots.get(state.run_id)
        first_sequence = len(previous.events) + 1 if previous is not None else 1
        materialized = tuple(
            RuntimeEvent(
                sequence=first_sequence + index,
                event_type=event.event_type,
                data=event.data,
            )
            for index, event in enumerate(events)
        )
        snapshot = RuntimeSnapshot(
            state=state,
            events=(*previous.events, *materialized) if previous else materialized,
        )
        self._snapshots[state.run_id] = snapshot
        self.commit_count += 1
        return snapshot


class PostgreSQLRuntimeStore:
    """Durable runtime store using one transaction for state and run events.

    The pool/connection object is deliberately accepted at the adapter boundary;
    runtime logic never imports or manages a database connection directly.
    """

    def __init__(self, pool: Any) -> None:
        self.pool = pool

    async def initialize(self) -> None:
        async with self.pool.acquire() as connection:
            await connection.execute(RUNTIME_SCHEMA_SQL)

    async def load(self, run_id: RunId) -> RuntimeSnapshot | None:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT state FROM runtime_runs WHERE run_id = $1",
                run_id,
            )
            if row is None:
                return None
            event_rows = await connection.fetch(
                """
                SELECT sequence, event_type, data
                FROM runtime_events
                WHERE run_id = $1
                ORDER BY sequence
                """,
                run_id,
            )
        return RuntimeSnapshot(
            state=_decode_state(_row_value(row, "state")),
            events=tuple(
                RuntimeEvent(
                    sequence=int(_row_value(event, "sequence")),
                    event_type=str(_row_value(event, "event_type")),
                    data=dict(_row_value(event, "data")),
                )
                for event in event_rows
            ),
        )

    async def create(self, state: RunState, events: Sequence[EventSpec]) -> RuntimeSnapshot:
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                try:
                    await connection.execute(
                        "INSERT INTO runtime_runs(run_id, state) VALUES ($1, $2::jsonb)",
                        state.run_id,
                        _dump_json(state),
                    )
                except Exception as exc:
                    raise StoreCommitError("unable to create runtime run") from exc
                await _insert_events(connection, state.run_id, events, first_sequence=1)
        snapshot = await self.load(state.run_id)
        if snapshot is None:
            raise StoreCommitError("created runtime run disappeared")
        return snapshot

    async def commit(self, state: RunState, events: Sequence[EventSpec]) -> RuntimeSnapshot:
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    SELECT COALESCE(MAX(sequence), 0) AS last_sequence
                    FROM runtime_events
                    WHERE run_id = $1
                    """,
                    state.run_id,
                )
                if row is None:
                    raise StoreCommitError("runtime run does not exist")
                last_sequence = int(_row_value(row, "last_sequence"))
                updated = await connection.execute(
                    "UPDATE runtime_runs SET state = $2::jsonb WHERE run_id = $1",
                    state.run_id,
                    _dump_json(state),
                )
                if updated.split()[-1] != "1":
                    raise StoreCommitError("runtime run does not exist")
                await _insert_events(
                    connection, state.run_id, events, first_sequence=last_sequence + 1
                )
        snapshot = await self.load(state.run_id)
        if snapshot is None:
            raise StoreCommitError("committed runtime run disappeared")
        return snapshot


async def _insert_events(
    connection: Any, run_id: RunId, events: Sequence[EventSpec], *, first_sequence: int
) -> None:
    for index, event in enumerate(events, start=first_sequence):
        await connection.execute(
            """
            INSERT INTO runtime_events(run_id, sequence, event_type, data)
            VALUES ($1, $2, $3, $4::jsonb)
            """,
            run_id,
            index,
            event.event_type,
            json.dumps(event.data, sort_keys=True, separators=(",", ":")),
        )


def _row_value(row: Any, key: str) -> Any:
    if isinstance(row, Mapping):
        return row[key]
    return row[key]


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump(mode="json", by_alias=True))
    if is_dataclass(value):
        return _json_value(asdict(cast(Any, value)))
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value.value if hasattr(value, "value") else value


def _dump_json(state: RunState) -> str:
    return json.dumps(_json_value(state), sort_keys=True, separators=(",", ":"))


def _decode_state(value: Any) -> RunState:
    payload = json.loads(value) if isinstance(value, str) else dict(value)
    return RunState(
        run_id=RunId(UUID(payload["run_id"])),
        status=RunStatus(payload["status"]),
        version=int(payload["version"]),
        cancel_requested=bool(payload["cancel_requested"]),
        failure_reason=(
            FailureReason(payload["failure_reason"])
            if payload.get("failure_reason") is not None
            else None
        ),
        new_calls_enabled=bool(payload["new_calls_enabled"]),
        budget_usage=BudgetUsage(**payload["budget_usage"]),
        budget_warning_emitted=bool(payload["budget_warning_emitted"]),
        active_dispatches=tuple(
            ActiveDispatch.model_validate(item) for item in payload["active_dispatches"]
        ),
        continuation_counts=tuple(
            (int(item[0]), int(item[1])) for item in payload["continuation_counts"]
        ),
        terminal_slice_failures=tuple(payload["terminal_slice_failures"]),
        adopted_advice_ids=tuple(payload.get("adopted_advice_ids", ())),
        pending_advice_ids=tuple(payload.get("pending_advice_ids", ())),
    )

__all__ = [
    "InMemoryRuntimeStore",
    "PostgreSQLRuntimeStore",
    "RuntimeStore",
    "StoreCommitError",
]

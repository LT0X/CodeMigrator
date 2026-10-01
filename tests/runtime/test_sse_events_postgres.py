"""Run and Draft SSE ledgers on isolated PostgreSQL schemas."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

import asyncpg
import pytest

from codemigrator.core import RunId
from codemigrator.runtime.contracts import DraftSessionEventSpec, EventSpec, RunState
from codemigrator.runtime.store import PostgreSQLRuntimeStore

from .conftest import draft_question_event


@asynccontextmanager
async def isolated_store(*, legacy_run_id: UUID | None = None):
    dsn = os.environ.get("CODEMIGRATOR_TEST_PG_DSN")
    if not dsn:
        pytest.skip("CODEMIGRATOR_TEST_PG_DSN is not configured")
    schema = f"runtime_sse_test_{uuid4().hex}"
    admin = await asyncpg.connect(dsn)
    pool = None
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        pool = await asyncpg.create_pool(dsn, server_settings={"search_path": schema})
        store = PostgreSQLRuntimeStore(pool)
        if legacy_run_id is not None:
            async with pool.acquire() as connection:
                await connection.execute(
                    """CREATE TABLE runtime_runs (
                        run_id uuid PRIMARY KEY, state jsonb NOT NULL
                    )"""
                )
                await connection.execute(
                    """CREATE TABLE runtime_events (
                        run_id uuid NOT NULL REFERENCES runtime_runs(run_id),
                        sequence bigint NOT NULL,
                        event_type text NOT NULL,
                        data jsonb NOT NULL,
                        PRIMARY KEY (run_id, sequence)
                    )"""
                )
                await connection.execute(
                    "INSERT INTO runtime_runs(run_id, state) VALUES ($1, '{}'::jsonb)",
                    legacy_run_id,
                )
                await connection.execute(
                    """INSERT INTO runtime_events(run_id, sequence, event_type, data)
                    VALUES ($1, 1, 'legacy.event', '{}'::jsonb)""",
                    legacy_run_id,
                )
        await store.initialize()
        yield store
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


@pytest.mark.asyncio
async def test_postgres_run_event_timestamp_and_order_survive_new_store_instance():
    async with isolated_store() as store:
        run_id = RunId(uuid4())
        await store.create(
            RunState(run_id=run_id),
            (
                EventSpec("agent_run.terminal", {"status": "FAILED"}),
                EventSpec("slice.status_changed", {"status": "RUNNING"}),
            ),
        )
        first = await store.read_run_events(run_id, 0)
        restarted = PostgreSQLRuntimeStore(store.pool)
        replayed = await restarted.read_run_events(run_id, 0)

        assert [item.sequence for item in replayed] == [1, 2]
        assert replayed == first
        assert all(item.timestamp_utc.tzinfo == UTC for item in replayed)
        assert all(item.timestamp_utc > datetime(1970, 1, 1, tzinfo=UTC) for item in replayed)
        assert await restarted.is_run_stream_terminal(run_id, 2) is False

        await restarted.commit(
            RunState(run_id=run_id, version=1),
            (EventSpec("run.status_changed", {"run_status": "FAILED"}),),
        )
        assert await restarted.is_run_stream_terminal(run_id, 2) is False
        assert await restarted.is_run_stream_terminal(run_id, 3) is True


@pytest.mark.asyncio
async def test_postgres_old_run_event_rows_receive_deterministic_utc_timestamp():
    run_id = uuid4()
    async with isolated_store(legacy_run_id=run_id) as store:
        events = await store.read_run_events(RunId(run_id), 0)
        assert len(events) == 1
        assert events[0].timestamp_utc == datetime(1970, 1, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_postgres_draft_event_order_and_timestamp_survive_new_store_instance():
    async with isolated_store() as store:
        draft_id = uuid4()
        question_id = str(uuid4())
        await store.commit_draft_owner_fact(
            draft_id,
            "question:asked",
            "draft.ask_user.question",
            {},
            events=(draft_question_event(question_id),),
        )
        await store.commit_draft_owner_fact(
            draft_id,
            "question:answered",
            "draft.ask_user.answer",
            {},
            events=(
                DraftSessionEventSpec("session.question.answered", {"question_id": question_id}),
            ),
        )
        first = await store.read_draft_session_events(draft_id, 0)
        restarted = PostgreSQLRuntimeStore(store.pool)
        replayed = await restarted.read_draft_session_events(draft_id, 0)

        assert [item.sequence for item in replayed] == [1, 2]
        assert [item.event_type for item in replayed] == [
            "session.question.asked",
            "session.question.answered",
        ]
        assert replayed == first
        assert all(item.timestamp_utc.tzinfo == UTC for item in replayed)


@pytest.mark.asyncio
async def test_postgres_run_wait_rechecks_commit_between_read_and_listen():
    async with isolated_store() as store:
        run_id = RunId(uuid4())
        await store.create(RunState(run_id=run_id), ())
        assert await store.read_run_events(run_id, 0) == ()
        await store.commit(
            RunState(run_id=run_id, version=1),
            (EventSpec("slice.status_changed", {"status": "RUNNING"}),),
        )

        await asyncio.wait_for(store.wait_for_run_events(run_id, 0), timeout=2)


class _ObservedConnection:
    def __init__(self, connection, listener_ready: asyncio.Event, rechecked: asyncio.Event):
        self._connection = connection
        self._listener_ready = listener_ready
        self._rechecked = rechecked

    def __getattr__(self, name: str):
        return getattr(self._connection, name)

    async def add_listener(self, *args, **kwargs):
        await self._connection.add_listener(*args, **kwargs)
        self._listener_ready.set()

    async def fetchrow(self, query: str, *args, **kwargs):
        row = await self._connection.fetchrow(query, *args, **kwargs)
        if "runtime_events" in query:
            self._rechecked.set()
        return row


class _ObservedAcquire:
    def __init__(self, acquire, listener_ready: asyncio.Event, rechecked: asyncio.Event):
        self._acquire = acquire
        self._listener_ready = listener_ready
        self._rechecked = rechecked

    async def __aenter__(self):
        self._context = self._acquire
        connection = await self._context.__aenter__()
        return _ObservedConnection(connection, self._listener_ready, self._rechecked)

    async def __aexit__(self, *args):
        return await self._context.__aexit__(*args)


class _ObservedPool:
    def __init__(self, pool, listener_ready: asyncio.Event, rechecked: asyncio.Event):
        self._pool = pool
        self._listener_ready = listener_ready
        self._rechecked = rechecked

    def acquire(self):
        return _ObservedAcquire(self._pool.acquire(), self._listener_ready, self._rechecked)


@pytest.mark.asyncio
async def test_postgres_run_wait_is_woken_by_transactional_notification():
    async with isolated_store() as store:
        run_id = RunId(uuid4())
        await store.create(RunState(run_id=run_id), ())
        listener_ready = asyncio.Event()
        rechecked = asyncio.Event()
        waiter_store = PostgreSQLRuntimeStore(_ObservedPool(store.pool, listener_ready, rechecked))
        waiter = asyncio.create_task(waiter_store.wait_for_run_events(run_id, 0))
        await asyncio.wait_for(listener_ready.wait(), timeout=2)
        await asyncio.wait_for(rechecked.wait(), timeout=2)
        assert not waiter.done()
        await store.commit(
            RunState(run_id=run_id, version=1),
            (EventSpec("slice.status_changed", {"status": "RUNNING"}),),
        )

        await asyncio.wait_for(waiter, timeout=2)
        events = await store.read_run_events(run_id, 0)
        assert [(item.sequence, item.event_type) for item in events] == [
            (1, "slice.status_changed")
        ]


@pytest.mark.asyncio
async def test_postgres_draft_terminal_cursor_uses_persisted_terminal_event_sequence():
    async with isolated_store() as store:
        draft_id = uuid4()
        await store.commit_draft_owner_fact(
            draft_id,
            "closed-category-without-terminal-event",
            "draft.closed",
            {},
            events=(
                draft_question_event(),
            ),
        )
        assert await store.is_draft_session_terminal(draft_id, 1) is False

        await store.commit_draft_owner_fact(
            draft_id,
            "attached",
            "draft.attached_to_run",
            {},
            events=(DraftSessionEventSpec("session.attached_to_run", {"run_id": str(uuid4())}),),
        )
        assert await store.is_draft_session_terminal(draft_id, 1) is False
        assert await store.is_draft_session_terminal(draft_id, 2) is True

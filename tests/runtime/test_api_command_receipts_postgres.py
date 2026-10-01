"""API command receipts commit with their Run owner facts on PostgreSQL."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import uuid4

import asyncpg
import pytest

from codemigrator.core import RunId, canonical_json_bytes
from codemigrator.runtime.actor import RunActor
from codemigrator.runtime.contracts import RuntimeStoreTransaction
from codemigrator.runtime.store import (
    PostgreSQLRuntimeStore,
)

from .conftest import create_run as create_run_request


@dataclass(frozen=True)
class CreatedRun:
    run_id: RunId
    receipt_key: str
    response: dict[str, object]


@asynccontextmanager
async def isolated_store():
    dsn = os.environ.get("CODEMIGRATOR_TEST_PG_DSN")
    if not dsn:
        pytest.skip("CODEMIGRATOR_TEST_PG_DSN is not configured")
    schema = f"api_commands_test_{uuid4().hex}"
    admin = await asyncpg.connect(dsn)
    pool = None
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        pool = await asyncpg.create_pool(dsn, server_settings={"search_path": schema})
        store = PostgreSQLRuntimeStore(pool)
        await store.initialize()
        yield store
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def table_count(store: PostgreSQLRuntimeStore, table: str) -> int:
    assert table in {"api_command_receipts", "run_graph_start_handoffs"}
    async with store.pool.acquire() as connection:
        return int(await connection.fetchval(f"SELECT count(*) FROM {table}"))


@pytest.mark.asyncio
async def test_run_facts_and_api_response_replay_share_one_owner_transaction():
    async with isolated_store() as store:
        run_id = RunId(uuid4())
        calls = 0

        async def commit_create_run(transaction: RuntimeStoreTransaction) -> CreatedRun:
            nonlocal calls
            calls += 1
            actor = RunActor(run_id, store)
            await actor.start()
            try:
                receipt = await actor.create(create_run_request(), transaction=transaction)
            finally:
                await actor.stop()
            assert receipt is not None
            return CreatedRun(
                run_id=run_id,
                receipt_key=receipt.receipt_key,
                response={"run_id": str(run_id), "status": "PLANNING", "version": 1},
            )

        first = await store.execute_api_command(
            principal_id="local",
            route="/api/v1/migrations",
            key="create-1",
            canonical_body=canonical_json_bytes({"request": "same"}),
            status_code=201,
            command=commit_create_run,
            project_response=lambda value: value.response,
            owner_receipt=lambda value: ("run", value.run_id, value.receipt_key),
        )
        replay = await store.execute_api_command(
            principal_id="local",
            route="/api/v1/migrations",
            key="create-1",
            canonical_body=canonical_json_bytes({"request": "same"}),
            status_code=201,
            command=commit_create_run,
            project_response=lambda value: value.response,
            owner_receipt=lambda value: ("run", value.run_id, value.receipt_key),
        )

        assert first["response"] == {
            "run_id": str(run_id),
            "status": "PLANNING",
            "version": 1,
        }
        assert replay["response"] == first["response"]
        assert replay["replayed"] is True
        assert calls == 1
        assert [
            event.event_type for event in (await store.load(run_id)).events
        ] == ["run.created", "run.status_changed"]
        assert await table_count(store, "api_command_receipts") == 1
        assert await table_count(store, "run_graph_start_handoffs") == 1


@pytest.mark.asyncio
async def test_same_key_different_canonical_body_conflicts_without_owner_write():
    async with isolated_store() as store:
        run_id = RunId(uuid4())

        async def commit_create_run(transaction: RuntimeStoreTransaction) -> CreatedRun:
            actor = RunActor(run_id, store)
            await actor.start()
            try:
                receipt = await actor.create(create_run_request(), transaction=transaction)
            finally:
                await actor.stop()
            assert receipt is not None
            return CreatedRun(
                run_id=run_id,
                receipt_key=receipt.receipt_key,
                response={"run_id": str(run_id), "status": "PLANNING", "version": 1},
            )

        await store.execute_api_command(
            principal_id="local",
            route="/api/v1/migrations",
            key="create-1",
            canonical_body=b'{"request":"same"}',
            status_code=201,
            command=commit_create_run,
            project_response=lambda value: value.response,
            owner_receipt=lambda value: ("run", value.run_id, value.receipt_key),
        )
        conflict = await store.execute_api_command(
            principal_id="local",
            route="/api/v1/migrations",
            key="create-1",
            canonical_body=b'{"request":"different"}',
            status_code=201,
            command=commit_create_run,
            project_response=lambda value: value.response,
            owner_receipt=lambda value: ("run", value.run_id, value.receipt_key),
        )

        assert conflict["conflict"] is True
        assert await table_count(store, "api_command_receipts") == 1
        assert await table_count(store, "run_graph_start_handoffs") == 1
        assert [
            event.event_type for event in (await store.load(run_id)).events
        ] == ["run.created", "run.status_changed"]


@pytest.mark.asyncio
async def test_outer_failure_rolls_back_owner_event_api_receipt_and_graph_handoff():
    async with isolated_store() as store:
        run_id = RunId(uuid4())
        actors: list[RunActor] = []

        async def fail_after_owner_write(transaction: RuntimeStoreTransaction) -> object:
            actor = RunActor(run_id, store)
            actors.append(actor)
            await actor.start()
            try:
                receipt = await actor.create(
                    create_run_request(), transaction=transaction
                )
                assert receipt is not None
            finally:
                await actor.stop()
            raise RuntimeError("synthetic transaction failure")

        with pytest.raises(RuntimeError, match="synthetic transaction failure"):
            await store.execute_api_command(
                principal_id="local",
                route="/api/v1/migrations",
                key="create-rollback",
                canonical_body=b"{}",
                status_code=201,
                command=fail_after_owner_write,
                project_response=lambda value: value,
                owner_receipt=lambda value: None,
            )

        assert await store.load(run_id) is None
        assert actors[0].state is None
        assert await table_count(store, "api_command_receipts") == 0
        assert await table_count(store, "run_graph_start_handoffs") == 0


@pytest.mark.asyncio
async def test_advisory_lock_connection_owns_and_aborts_inflight_store_write():
    async with isolated_store() as store:
        writer_connection = None
        blocker_connection = None
        owner_lock_key = int(uuid4().int & ((1 << 63) - 1))
        blocker_lock_key = int(uuid4().int & ((1 << 63) - 1))
        blocked_key = "blocked-write"
        operation: asyncio.Task[object] | None = None
        try:
            writer_connection = await store.pool.acquire()
            blocker_connection = await store.pool.acquire()
            write_store = PostgreSQLRuntimeStore(store.pool, write_connection=writer_connection)
            assert await writer_connection.fetchval(
                "SELECT pg_try_advisory_lock($1::bigint)", owner_lock_key
            )
            await write_store.initialize()
            await blocker_connection.fetchval(
                "SELECT pg_advisory_lock($1::bigint)", blocker_lock_key
            )
            async with store.pool.acquire() as connection:
                await connection.execute(
                    f"""CREATE FUNCTION pause_selected_api_receipt()
                    RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN
                        IF NEW.idempotency_key = '{blocked_key}' THEN
                            PERFORM pg_advisory_xact_lock({blocker_lock_key}::bigint);
                        END IF;
                        RETURN NEW;
                    END;
                    $$;
                    CREATE TRIGGER pause_selected_api_receipt
                    BEFORE INSERT ON api_command_receipts
                    FOR EACH ROW EXECUTE FUNCTION pause_selected_api_receipt();"""
                )

            async def command(_transaction: RuntimeStoreTransaction) -> object:
                return {"ok": True}

            operation = asyncio.create_task(
                write_store.execute_api_command(
                    principal_id="local",
                    route="/api/v1/test-write",
                    key=blocked_key,
                    canonical_body=b"{}",
                    status_code=200,
                    command=command,
                    project_response=lambda value: value,
                    owner_receipt=lambda _value: None,
                )
            )

            for _ in range(200):
                async with store.pool.acquire() as connection:
                    blocked = await connection.fetchval(
                        """SELECT EXISTS (
                            SELECT 1 FROM pg_stat_activity
                            WHERE state='active' AND wait_event_type='Lock'
                              AND query LIKE 'INSERT INTO api_command_receipts%'
                              AND pid <> pg_backend_pid()
                        )"""
                    )
                if blocked:
                    break
                await asyncio.sleep(0.01)
            assert blocked, "API receipt write did not reach the database lock gate"

            writer_connection.terminate()
            with pytest.raises((asyncpg.PostgresError, asyncpg.InterfaceError)):
                await asyncio.wait_for(operation, timeout=3)
            operation = None
            await blocker_connection.fetchval(
                "SELECT pg_advisory_unlock($1::bigint)", blocker_lock_key
            )
            assert await table_count(store, "api_command_receipts") == 0

            async with store.pool.acquire() as replacement_owner:
                assert await replacement_owner.fetchval(
                    "SELECT pg_try_advisory_lock($1::bigint)", owner_lock_key
                )
                await replacement_owner.fetchval(
                    "SELECT pg_advisory_unlock($1::bigint)", owner_lock_key
                )
        finally:
            if operation is not None and not operation.done():
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
            if blocker_connection is not None:
                try:
                    if not blocker_connection.is_closed():
                        await blocker_connection.fetchval(
                            "SELECT pg_advisory_unlock($1::bigint)", blocker_lock_key
                        )
                        await store.pool.release(blocker_connection)
                except asyncpg.InterfaceError:
                    pass
            if writer_connection is not None:
                try:
                    if not writer_connection.is_closed():
                        await writer_connection.fetchval(
                            "SELECT pg_advisory_unlock($1::bigint)", owner_lock_key
                        )
                        await store.pool.release(writer_connection)
                except asyncpg.InterfaceError:
                    pass


@pytest.mark.asyncio
async def test_lock_bound_store_serializes_concurrent_transactions():
    async with isolated_store() as store:
        writer_connection = await store.pool.acquire()
        owner_lock_key = int(uuid4().int & ((1 << 63) - 1))
        try:
            assert await writer_connection.fetchval(
                "SELECT pg_try_advisory_lock($1::bigint)", owner_lock_key
            )
            write_store = PostgreSQLRuntimeStore(store.pool, write_connection=writer_connection)

            async def write_receipt(key: str) -> object:
                async def command(_transaction: RuntimeStoreTransaction) -> object:
                    return {"key": key}

                return await write_store.execute_api_command(
                    principal_id="local",
                    route="/api/v1/test-write",
                    key=key,
                    canonical_body=key.encode(),
                    status_code=200,
                    command=command,
                    project_response=lambda value: value,
                    owner_receipt=lambda _value: None,
                )

            first, second = await asyncio.gather(write_receipt("first"), write_receipt("second"))

            assert first["response"] == {"key": "first"}
            assert second["response"] == {"key": "second"}
            assert await table_count(store, "api_command_receipts") == 2
        finally:
            try:
                if not writer_connection.is_closed():
                    await writer_connection.fetchval(
                        "SELECT pg_advisory_unlock($1::bigint)", owner_lock_key
                    )
                    await store.pool.release(writer_connection)
            except asyncpg.InterfaceError:
                pass

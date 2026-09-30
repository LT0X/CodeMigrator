"""API command receipts commit with their Run owner facts on PostgreSQL."""

from __future__ import annotations

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
        assert len((await store.load(run_id)).events) == 1
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
        assert len((await store.load(run_id)).events) == 1


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

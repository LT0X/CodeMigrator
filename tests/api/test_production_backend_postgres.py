"""Route-level PostgreSQL coverage for the concrete API backend."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest

from codemigrator.api import ApiConfig, create_app
from codemigrator.api.backend import ProductionApiBackend
from codemigrator.api.sse import sse_events
from codemigrator.asgi import create_production_app
from codemigrator.core import CreateRun, RunId, canonical_json_bytes
from codemigrator.runtime.actor import RunActor
from codemigrator.runtime.contracts import RunCreatedReceipt, RuntimeStoreTransaction
from codemigrator.runtime.create_run import CreateRunRejected, RunCreationOwner
from codemigrator.runtime.store import PostgreSQLRuntimeStore

from .conftest import create_run_payload


@asynccontextmanager
async def isolated_store():
    dsn = os.environ.get("CODEMIGRATOR_TEST_PG_DSN")
    if not dsn:
        pytest.skip("CODEMIGRATOR_TEST_PG_DSN is not configured")
    schema = f"api_backend_test_{uuid4().hex}"
    admin = await asyncpg.connect(dsn)
    pool = None
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        pool = await asyncpg.create_pool(dsn, server_settings={"search_path": schema})
        store = PostgreSQLRuntimeStore(pool)
        await store.initialize()
        yield store, schema
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


class PassingPreflight:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def verify_descriptor_lock(self, request) -> None:  # type: ignore[no-untyped-def]
        del request
        self.calls.append("descriptor")

    async def verify_preindex(self, request) -> None:  # type: ignore[no-untyped-def]
        del request
        self.calls.append("preindex")

    async def verify_dossier_consistency(self, request) -> None:  # type: ignore[no-untyped-def]
        del request
        self.calls.append("dossier")


class RejectingPreflight(PassingPreflight):
    async def verify_preindex(self, request) -> None:  # type: ignore[no-untyped-def]
        await super().verify_preindex(request)
        raise CreateRunRejected("synthetic gate rejection with sensitive context")


class RecordingGraphStarter:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.receipts: list[RunCreatedReceipt] = []

    async def start(self, run_id: RunId, receipt: RunCreatedReceipt) -> None:
        assert UUID(str(run_id)) == receipt.run_id
        if self.fail:
            raise RuntimeError("synthetic private graph failure")
        self.receipts.append(receipt)


class BlockingGraphStarter(RecordingGraphStarter):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def start(self, run_id: RunId, receipt: RunCreatedReceipt) -> None:
        self.receipts.append(receipt)
        self.entered.set()
        await self.release.wait()


@pytest.mark.asyncio
async def test_create_receipt_replays_and_graph_handoff_recovers_after_restart():
    async with isolated_store() as (store, _schema):
        preflight = PassingPreflight()
        failing_graph = RecordingGraphStarter(fail=True)
        backend = ProductionApiBackend(
            store,
            run_owner=RunCreationOwner(
                store=store, preflight=preflight, graph_starter=failing_graph
            ),
        )
        app = create_app(backend, config=ApiConfig(token="synthetic-token"))
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        )
        headers = {
            "Authorization": "Bearer synthetic-token",
            "Idempotency-Key": "create-run-1",
        }
        async with client:
            first = await client.post(
                "/api/v1/migrations", json=create_run_payload(), headers=headers
            )
        assert first.status_code == 201
        response_body = first.json()
        run_id = RunId(UUID(response_body["run_id"]))
        assert response_body["status"] == "PLANNING"
        assert preflight.calls == ["descriptor", "preindex", "dossier"]
        assert await store.read_run_events(run_id, 0)
        assert await store.list_pending_graph_starts() == (
            (run_id, f"run.created:{run_id}"),
        )

        recovered_graph = RecordingGraphStarter()
        restarted_backend = ProductionApiBackend(
            store,
            run_owner=RunCreationOwner(
                store=store, preflight=preflight, graph_starter=recovered_graph
            ),
        )
        await restarted_backend.recover_pending_graph_starts()

        assert len(recovered_graph.receipts) == 1
        assert recovered_graph.receipts[0].run_id == run_id
        assert await store.list_pending_graph_starts() == ()
        assert len(await store.read_run_events(run_id, 0)) == 1

        replay_backend = ProductionApiBackend(
            store,
            run_owner=RunCreationOwner(
                store=store, preflight=preflight, graph_starter=recovered_graph
            ),
        )
        app = create_app(replay_backend, config=ApiConfig(token="synthetic-token"))
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        )
        async with client:
            replay = await client.post(
                "/api/v1/migrations", json=create_run_payload(), headers=headers
            )
            conflict = await client.post(
                "/api/v1/migrations",
                json={**create_run_payload(), "branch_prefix": "team/other"},
                headers=headers,
            )
        assert replay.status_code == 201
        assert replay.json() == response_body
        assert conflict.status_code == 409
        assert conflict.json()["type"].endswith("/idempotency_conflict")
        assert "synthetic private graph failure" not in first.text
        assert preflight.calls == ["descriptor", "preindex", "dossier"]


@pytest.mark.asyncio
async def test_preflight_failure_has_zero_persistent_run_side_effects():
    async with isolated_store() as (store, _schema):
        backend = ProductionApiBackend(
            store,
            run_owner=RunCreationOwner(
                store=store,
                preflight=RejectingPreflight(),
                graph_starter=RecordingGraphStarter(),
            ),
        )
        app = create_app(backend, config=ApiConfig(token="synthetic-token"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            response = await client.post(
                "/api/v1/migrations",
                json=create_run_payload(),
                headers={
                    "Authorization": "Bearer synthetic-token",
                    "Idempotency-Key": "rejected-run",
                },
            )

        assert response.status_code == 503
        assert response.json()["type"].endswith("/dependency_unavailable")
        assert "sensitive context" not in response.text
        async with store.pool.acquire() as connection:
            assert await connection.fetchval("SELECT count(*) FROM runtime_runs") == 0
            assert await connection.fetchval("SELECT count(*) FROM runtime_events") == 0
            assert await connection.fetchval("SELECT count(*) FROM api_command_receipts") == 0
            assert await connection.fetchval("SELECT count(*) FROM run_graph_start_handoffs") == 0


@pytest.mark.asyncio
async def test_concurrent_same_key_replay_starts_graph_once():
    async with isolated_store() as (store, _schema):
        graph = BlockingGraphStarter()
        backend = ProductionApiBackend(
            store,
            run_owner=RunCreationOwner(
                store=store, preflight=PassingPreflight(), graph_starter=graph
            ),
        )
        app = create_app(backend, config=ApiConfig(token="synthetic-token"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            headers = {
                "Authorization": "Bearer synthetic-token",
                "Idempotency-Key": "parallel-create",
            }
            first_task = asyncio.create_task(
                client.post("/api/v1/migrations", json=create_run_payload(), headers=headers)
            )
            await asyncio.wait_for(graph.entered.wait(), timeout=2)
            replay_task = asyncio.create_task(
                client.post("/api/v1/migrations", json=create_run_payload(), headers=headers)
            )
            await asyncio.sleep(0)
            graph.release.set()
            first, replay = await asyncio.gather(first_task, replay_task)

        assert first.status_code == replay.status_code == 201
        assert first.json() == replay.json()
        assert len(graph.receipts) == 1
        run_id = RunId(UUID(first.json()["run_id"]))
        assert len(await store.read_run_events(run_id, 0)) == 1


@pytest.mark.asyncio
async def test_backend_fails_closed_for_unsupported_projection_and_replays_real_sse():
    async with isolated_store() as (store, _schema):
        backend = ProductionApiBackend(
            store,
            run_owner=RunCreationOwner(
                store=store,
                preflight=PassingPreflight(),
                graph_starter=RecordingGraphStarter(fail=True),
            ),
        )
        app = create_app(backend, config=ApiConfig(token="synthetic-token"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            headers = {
                "Authorization": "Bearer synthetic-token",
                "Idempotency-Key": "stream-run",
            }
            created = await client.post(
                "/api/v1/migrations", json=create_run_payload(), headers=headers
            )
            run_id = created.json()["run_id"]
            unsupported = await client.get("/api/v1/migrations", headers=headers)
            draft = await client.post(
                "/api/v1/sessions",
                json={"kind": "DISCOVERY", "payload": {"synthetic": "input"}},
                headers={
                    "Authorization": "Bearer synthetic-token",
                    "Idempotency-Key": "draft-not-supported",
                },
            )
            event_stream = sse_events(backend, UUID(run_id), after_sequence=0)
            event = await asyncio.wait_for(anext(event_stream), timeout=2)
            await event_stream.aclose()

        assert created.status_code == 201
        assert unsupported.status_code == 503
        assert draft.status_code == 503
        assert draft.json()["type"].endswith("/dependency_unavailable")
        assert "synthetic" not in draft.text
        async with store.pool.acquire() as connection:
            assert await connection.fetchval("SELECT count(*) FROM draft_owner_facts") == 0
            assert await connection.fetchval("SELECT count(*) FROM draft_session_events") == 0
        assert event.event == "migration.event"
        assert '"schema":"migration.event"' in event.data
        assert '"type":"run.created"' in event.data


@pytest.mark.asyncio
async def test_production_asgi_startup_recovers_before_ready_and_closes_owned_resources():
    dsn = os.environ.get("CODEMIGRATOR_TEST_PG_DSN")
    if not dsn:
        pytest.skip("CODEMIGRATOR_TEST_PG_DSN is not configured")
    async with isolated_store() as (store, schema):
        run_id = RunId(uuid4())

        async def create_owner(transaction: RuntimeStoreTransaction) -> RunCreatedReceipt:
            actor = RunActor(run_id, store)
            await actor.start()
            try:
                receipt = await actor.create(
                    CreateRun.model_validate(create_run_payload()), transaction=transaction
                )
            finally:
                await actor.stop()
            assert receipt is not None
            return receipt

        await store.execute_api_command(
            principal_id="local",
            route="/api/v1/migrations",
            key="startup-recovery",
            canonical_body=canonical_json_bytes({"request": "seed"}),
            status_code=201,
            command=create_owner,
            project_response=lambda receipt: {"run_id": str(receipt.run_id)},
            owner_receipt=lambda receipt: ("run", UUID(str(receipt.run_id)), receipt.receipt_key),
        )

        graph = RecordingGraphStarter()
        shutdown_calls: list[str] = []

        async def close_graph_resources() -> None:
            shutdown_calls.append("closed")

        app = create_production_app(
            dsn,
            config=ApiConfig(token="synthetic-token"),
            preflight=PassingPreflight(),
            graph_starter=graph,
            shutdown=close_graph_resources,
            pool_server_settings={"search_path": schema},
        )
        async with app.router.lifespan_context(app):
            assert app.state.runtime_ready is True
            owned_pool = app.state.runtime_pool
            async with owned_pool.acquire() as connection:
                pending_count = await connection.fetchval(
                    "SELECT count(*) FROM run_graph_start_handoffs WHERE status='PENDING'"
                )
                started_count = await connection.fetchval(
                    "SELECT count(*) FROM run_graph_start_handoffs WHERE status='STARTED'"
                )
            assert pending_count == 0
            assert started_count == 1
            assert len(graph.receipts) == 1

        assert app.state.runtime_ready is False
        assert owned_pool.is_closing()
        assert shutdown_calls == ["closed"]

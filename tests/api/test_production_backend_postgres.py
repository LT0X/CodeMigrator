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
from codemigrator.api.deps import ApiRequest
from codemigrator.api.sse import sse_events
from codemigrator.asgi import create_production_app
from codemigrator.core import CreateRun, FailureReason, RunId, StableErrorCode, canonical_json_bytes
from codemigrator.runtime.actor import RunActor
from codemigrator.runtime.contracts import RunCreatedReceipt, RuntimeStoreTransaction
from codemigrator.runtime.create_run import CreateRunRejected, RunCreationOwner
from codemigrator.runtime.store import PostgreSQLRuntimeStore

from .conftest import create_run_payload


async def noop_server_stop() -> None:
    return None


@asynccontextmanager
async def isolated_store(*, max_size: int = 10):
    dsn = os.environ.get("CODEMIGRATOR_TEST_PG_DSN")
    if not dsn:
        pytest.skip("CODEMIGRATOR_TEST_PG_DSN is not configured")
    schema = f"api_backend_test_{uuid4().hex}"
    admin = await asyncpg.connect(dsn)
    pool = None
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        pool = await asyncpg.create_pool(
            dsn,
            min_size=0,
            max_size=max_size,
            server_settings={"search_path": schema},
        )
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


class TwoRequestBarrierPreflight(PassingPreflight):
    def __init__(self) -> None:
        super().__init__()
        self._arrivals = 0
        self._both_arrived = asyncio.Event()

    async def verify_descriptor_lock(self, request) -> None:  # type: ignore[no-untyped-def]
        del request
        self._arrivals += 1
        if self._arrivals == 2:
            self._both_arrived.set()
        await self._both_arrived.wait()
        self.calls.append("descriptor")


class RejectingPreflight(PassingPreflight):
    async def verify_preindex(self, request) -> None:  # type: ignore[no-untyped-def]
        await super().verify_preindex(request)
        raise CreateRunRejected("synthetic gate rejection with sensitive context")


class TypedRejectingPreflight(PassingPreflight):
    def __init__(self, gate: str, code: StableErrorCode | FailureReason) -> None:
        super().__init__()
        self.gate = gate
        self.code = code

    async def _reject(self, gate: str, request) -> None:  # type: ignore[no-untyped-def]
        self.calls.append(gate)
        if gate == self.gate:
            raise CreateRunRejected("synthetic private gate detail", code=self.code)

    async def verify_descriptor_lock(self, request) -> None:  # type: ignore[no-untyped-def]
        await self._reject("descriptor", request)

    async def verify_preindex(self, request) -> None:  # type: ignore[no-untyped-def]
        await self._reject("preindex", request)

    async def verify_dossier_consistency(self, request) -> None:  # type: ignore[no-untyped-def]
        await self._reject("dossier", request)


class RecordingGraphStarter:
    receipt_idempotent = True

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


class ReceiptEffectGraphStarter(RecordingGraphStarter):
    def __init__(self) -> None:
        super().__init__()
        self.effects: set[str] = set()
        self.domain_work_count = 0

    async def start(self, run_id: RunId, receipt: RunCreatedReceipt) -> None:
        self.receipts.append(receipt)
        if receipt.receipt_key not in self.effects:
            self.effects.add(receipt.receipt_key)
            self.domain_work_count += 1


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
        await backend.close()
        await restarted_backend.close()
        await replay_backend.close()


@pytest.mark.asyncio
async def test_graph_effect_replay_after_handoff_mark_failure_is_receipt_idempotent():
    async with isolated_store() as (store, _schema):
        graph = ReceiptEffectGraphStarter()
        preflight = PassingPreflight()
        backend = ProductionApiBackend(
            store,
            run_owner=RunCreationOwner(
                store=store, preflight=preflight, graph_starter=graph
            ),
        )
        mark_started = store.mark_graph_start_started
        fail_first_mark = True

        async def mark_with_one_fault(run_id: RunId, receipt_key: str) -> None:
            nonlocal fail_first_mark
            if fail_first_mark:
                fail_first_mark = False
                raise RuntimeError("synthetic handoff mark fault")
            await mark_started(run_id, receipt_key)

        store.mark_graph_start_started = mark_with_one_fault  # type: ignore[method-assign]
        payload = CreateRun.model_validate(create_run_payload())
        request = ApiRequest(
            operation="create_run", principal_id="local", payload=payload
        )
        outcome = await backend.execute_idempotent(
            request,
            route="/api/v1/migrations",
            key="graph-started-before-handoff-mark",
            canonical_body=canonical_json_bytes(payload.model_dump(mode="json")),
            status_code=201,
        )

        assert outcome["run_id"]
        assert len(graph.receipts) == 1
        assert graph.domain_work_count == 1
        assert len(await store.list_pending_graph_starts()) == 1
        await backend.recover_pending_graph_starts()

        assert len(graph.receipts) == 2
        assert graph.domain_work_count == 1
        assert await store.list_pending_graph_starts() == ()
        assert preflight.calls == ["descriptor", "preindex", "dossier"]
        await backend.close()


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
        await backend.close()


@pytest.mark.asyncio
async def test_outer_command_rollback_removes_run_facts_receipt_and_uncommitted_actor():
    async with isolated_store() as (store, _schema):
        owner = RunCreationOwner(
            store=store,
            preflight=PassingPreflight(),
            graph_starter=RecordingGraphStarter(),
        )
        request = CreateRun.model_validate(create_run_payload())

        async def command(transaction: RuntimeStoreTransaction) -> RunCreatedReceipt:
            return await owner.create_run(request, transaction)

        def fail_projection(_receipt: RunCreatedReceipt) -> object:
            raise RuntimeError("synthetic outer projection failure")

        with pytest.raises(RuntimeError, match="synthetic outer projection failure"):
            await store.execute_api_command(
                principal_id="local",
                route="/api/v1/migrations",
                key="outer-rollback",
                canonical_body=canonical_json_bytes(request.model_dump(mode="json")),
                status_code=201,
                command=command,
                project_response=fail_projection,
                owner_receipt=lambda receipt: (
                    "run",
                    UUID(str(receipt.run_id)),
                    receipt.receipt_key,
                ),
            )
        await asyncio.sleep(0)

        assert owner.active_actor_count == 0
        async with store.pool.acquire() as connection:
            assert await connection.fetchval("SELECT count(*) FROM runtime_runs") == 0
            assert await connection.fetchval("SELECT count(*) FROM runtime_events") == 0
            assert await connection.fetchval("SELECT count(*) FROM api_command_receipts") == 0
            assert await connection.fetchval("SELECT count(*) FROM run_graph_start_handoffs") == 0
        await owner.close()


@pytest.mark.parametrize(
    ("gate", "code", "called_gates"),
    [
        ("descriptor", StableErrorCode.DESCRIPTOR_DIGEST_MISMATCH, ["descriptor"]),
        (
            "preindex",
            StableErrorCode.ANALYSIS_INFRA_ERROR,
            ["descriptor", "preindex"],
        ),
        (
            "dossier",
            FailureReason.DossierInconsistent,
            ["descriptor", "preindex", "dossier"],
        ),
    ],
)
@pytest.mark.asyncio
async def test_typed_create_run_gate_rejections_keep_public_codes(gate, code, called_gates):
    async with isolated_store() as (store, _schema):
        preflight = TypedRejectingPreflight(gate, code)
        backend = ProductionApiBackend(
            store,
            run_owner=RunCreationOwner(
                store=store,
                preflight=preflight,
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
                    "Idempotency-Key": f"reject-{gate}",
                },
            )

        assert response.status_code == 422
        assert response.json()["type"].endswith(f"/{code.value.lower()}")
        assert response.json()["retryable"] is False
        assert "synthetic private gate detail" not in response.text
        assert preflight.calls == called_gates
        async with store.pool.acquire() as connection:
            assert await connection.fetchval("SELECT count(*) FROM runtime_runs") == 0
            assert await connection.fetchval("SELECT count(*) FROM runtime_events") == 0
            assert await connection.fetchval("SELECT count(*) FROM api_command_receipts") == 0
            assert await connection.fetchval("SELECT count(*) FROM run_graph_start_handoffs") == 0
        await backend.close()


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
        await backend.close()


@pytest.mark.asyncio
async def test_two_unique_creates_do_not_need_extra_pool_connections():
    async with isolated_store(max_size=2) as (store, _schema):
        preflight = TwoRequestBarrierPreflight()
        graph = RecordingGraphStarter()
        backend = ProductionApiBackend(
            store,
            run_owner=RunCreationOwner(
                store=store, preflight=preflight, graph_starter=graph
            ),
        )
        app = create_app(backend, config=ApiConfig(token="synthetic-token"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            request_body = create_run_payload()
            first = asyncio.create_task(
                client.post(
                    "/api/v1/migrations",
                    json=request_body,
                    headers={
                        "Authorization": "Bearer synthetic-token",
                        "Idempotency-Key": "unique-create-one",
                    },
                )
            )
            second = asyncio.create_task(
                client.post(
                    "/api/v1/migrations",
                    json=request_body,
                    headers={
                        "Authorization": "Bearer synthetic-token",
                        "Idempotency-Key": "unique-create-two",
                    },
                )
            )
            responses = await asyncio.wait_for(asyncio.gather(first, second), timeout=2)

        assert [response.status_code for response in responses] == [201, 201]
        run_ids = {response.json()["run_id"] for response in responses}
        assert len(run_ids) == 2
        assert len(graph.receipts) == 2
        assert preflight.calls.count("descriptor") == 2
        await backend.close()


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
        await backend.close()
        await backend.close()


@pytest.mark.asyncio
async def test_production_asgi_startup_recovers_before_ready_and_closes_owned_resources():
    dsn = os.environ.get("CODEMIGRATOR_TEST_PG_DSN")
    if not dsn:
        pytest.skip("CODEMIGRATOR_TEST_PG_DSN is not configured")
    async with isolated_store() as (store, schema):
        run_id = RunId(uuid4())

        async def create_owner(transaction: RuntimeStoreTransaction) -> RunCreatedReceipt:
            actor = RunActor(run_id, store)
            await actor.start_new()
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
            stop_server=noop_server_stop,
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


@pytest.mark.asyncio
async def test_lock_connection_loss_revokes_api_and_stops_server_immediately():
    dsn = os.environ.get("CODEMIGRATOR_TEST_PG_DSN")
    if not dsn:
        pytest.skip("CODEMIGRATOR_TEST_PG_DSN is not configured")
    async with isolated_store() as (_store, schema):
        stop_called = asyncio.Event()
        shutdown_calls: list[str] = []

        async def stop_server() -> None:
            stop_called.set()

        async def shutdown_owner() -> None:
            shutdown_calls.append("closed")

        app = create_production_app(
            dsn,
            config=ApiConfig(token="synthetic-token"),
            preflight=PassingPreflight(),
            graph_starter=RecordingGraphStarter(),
            shutdown=shutdown_owner,
            stop_server=stop_server,
            pool_server_settings={"search_path": schema},
        )
        async with app.router.lifespan_context(app):
            assert app.state.runtime_ready is True
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
            ) as client:
                accepted = await client.post(
                    "/api/v1/migrations",
                    json=create_run_payload(),
                    headers={
                        "Authorization": "Bearer synthetic-token",
                        "Idempotency-Key": "before-lock-loss",
                    },
                )
                assert accepted.status_code == 201

                app.state.runtime_lock.connection.terminate()
                await asyncio.wait_for(app.state.runtime_lock_loss_event.wait(), timeout=2)
                assert app.state.runtime_ready is False
                denied = await client.post(
                    "/api/v1/migrations",
                    json=create_run_payload(),
                    headers={
                        "Authorization": "Bearer synthetic-token",
                        "Idempotency-Key": "after-lock-loss",
                    },
                )
                assert denied.status_code == 503
                assert denied.json()["type"].endswith("/dependency_unavailable")
            await asyncio.wait_for(stop_called.wait(), timeout=2)
            assert shutdown_calls == ["closed"]

        assert shutdown_calls == ["closed"]
        assert app.state.runtime_ready is False


@pytest.mark.asyncio
async def test_health_uses_live_readiness_and_postgresql_checks():
    dsn = os.environ.get("CODEMIGRATOR_TEST_PG_DSN")
    if not dsn:
        pytest.skip("CODEMIGRATOR_TEST_PG_DSN is not configured")
    async with isolated_store() as (_store, schema):
        app = create_production_app(
            dsn,
            config=ApiConfig(token="synthetic-token"),
            stop_server=noop_server_stop,
            pool_server_settings={"search_path": schema},
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
            ) as client:
                headers = {"Authorization": "Bearer synthetic-token"}
                healthy = await client.get("/api/v1/system/health", headers=headers)
                assert healthy.status_code == 200
                assert healthy.json() == {
                    "app": "READY",
                    "postgres": "READY",
                    "sandbox": "NOT_CONFIGURED",
                    "optional_profiles": {},
                }

                app.state.runtime_pool.terminate()
                unavailable = await client.get("/api/v1/system/health", headers=headers)
                assert unavailable.status_code == 503
                assert unavailable.json()["type"].endswith("/dependency_unavailable")


@pytest.mark.asyncio
async def test_startup_refuses_graph_starter_without_receipt_recovery_contract():
    dsn = os.environ.get("CODEMIGRATOR_TEST_PG_DSN")
    if not dsn:
        pytest.skip("CODEMIGRATOR_TEST_PG_DSN is not configured")
    async with isolated_store() as (_store, schema):
        app = create_production_app(
            dsn,
            config=ApiConfig(token="synthetic-token"),
            preflight=PassingPreflight(),
            graph_starter=object(),  # type: ignore[arg-type]
            stop_server=noop_server_stop,
            pool_server_settings={"search_path": schema},
        )
        with pytest.raises(RuntimeError, match="production API startup failed") as raised:
            async with app.router.lifespan_context(app):
                pytest.fail("unsafe graph starter must not become ready")

        assert str(raised.value) == "production API startup failed"
        assert app.state.runtime_ready is False
        assert app.state.runtime_pool is None


@pytest.mark.asyncio
async def test_lock_loss_shutdown_errors_still_release_lock_and_pool():
    dsn = os.environ.get("CODEMIGRATOR_TEST_PG_DSN")
    if not dsn:
        pytest.skip("CODEMIGRATOR_TEST_PG_DSN is not configured")
    async with isolated_store() as (_store, schema):
        stop_calls: list[str] = []
        shutdown_calls: list[str] = []

        async def fail_stop() -> None:
            stop_calls.append("called")
            raise RuntimeError("synthetic private server failure")

        async def fail_shutdown() -> None:
            shutdown_calls.append("called")
            raise RuntimeError("synthetic private owner failure")

        app = create_production_app(
            dsn,
            config=ApiConfig(token="synthetic-token"),
            preflight=PassingPreflight(),
            graph_starter=RecordingGraphStarter(),
            shutdown=fail_shutdown,
            stop_server=fail_stop,
            pool_server_settings={"search_path": schema},
        )
        async with app.router.lifespan_context(app):
            lock_connection = app.state.runtime_lock.connection
            owned_pool = app.state.runtime_pool
            lock_connection.terminate()
            await asyncio.wait_for(app.state.runtime_lock_loss_event.wait(), timeout=2)
            lock_loss_task = app.state.lock_loss_task
            assert lock_loss_task is not None
            await asyncio.wait_for(lock_loss_task, timeout=2)
            assert app.state.runtime_shutdown_error == "ServerStopFailed"

        assert app.state.runtime_ready is False
        assert lock_connection.is_closed()
        assert owned_pool.is_closing()
        assert shutdown_calls == ["called"]
        assert stop_calls == ["called"]


@pytest.mark.asyncio
async def test_cancel_routes_to_one_durable_run_actor_with_expected_version():
    async with isolated_store() as (store, _schema):
        owner = RunCreationOwner(
            store=store,
            preflight=PassingPreflight(),
            graph_starter=RecordingGraphStarter(),
        )
        backend = ProductionApiBackend(store, run_owner=owner)
        app = create_app(backend, config=ApiConfig(token="synthetic-token"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            created = await client.post(
                "/api/v1/migrations",
                json=create_run_payload(),
                headers={
                    "Authorization": "Bearer synthetic-token",
                    "Idempotency-Key": "cancel-run",
                },
            )
            run_id = UUID(created.json()["run_id"])
            headers = {"Authorization": "Bearer synthetic-token", "If-Match": '"1"'}
            cancelled, stale = await asyncio.gather(
                client.delete(f"/api/v1/migrations/{run_id}", headers=headers),
                client.delete(f"/api/v1/migrations/{run_id}", headers=headers),
            )

        responses = (cancelled, stale)
        accepted = [response for response in responses if response.status_code == 200]
        rejected = [response for response in responses if response.status_code == 409]
        assert len(accepted) == len(rejected) == 1
        assert accepted[0].json()["status"] == "CANCELLED"
        assert accepted[0].json()["version"] == 2
        assert rejected[0].json()["type"].endswith("/stale_version")
        assert rejected[0].json()["retryable"] is False
        assert owner.active_actor_count == 1
        run_events = await store.read_run_events(RunId(run_id), 0)
        assert [event.event_type for event in run_events] == ["run.created", "run.cancelled"]
        await backend.close()


@pytest.mark.asyncio
async def test_cancel_during_graph_start_uses_the_registered_create_actor():
    async with isolated_store() as (store, _schema):
        graph = BlockingGraphStarter()
        owner = RunCreationOwner(
            store=store,
            preflight=PassingPreflight(),
            graph_starter=graph,
        )
        backend = ProductionApiBackend(store, run_owner=owner)
        app = create_app(backend, config=ApiConfig(token="synthetic-token"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            create_task = asyncio.create_task(
                client.post(
                    "/api/v1/migrations",
                    json=create_run_payload(),
                    headers={
                        "Authorization": "Bearer synthetic-token",
                        "Idempotency-Key": "cancel-during-graph-start",
                    },
                )
            )
            await asyncio.wait_for(graph.entered.wait(), timeout=2)
            run_id = graph.receipts[0].run_id
            selected_actor = owner._actors._actors[run_id]
            cancelled = await client.delete(
                f"/api/v1/migrations/{run_id}",
                headers={"Authorization": "Bearer synthetic-token", "If-Match": '"1"'},
            )
            assert owner._actors._actors[run_id] is selected_actor
            graph.release.set()
            created = await create_task

        assert created.status_code == 201
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "CANCELLED"
        assert owner.active_actor_count == 1
        run_events = await store.read_run_events(run_id, 0)
        assert [event.event_type for event in run_events] == ["run.created", "run.cancelled"]
        await backend.close()

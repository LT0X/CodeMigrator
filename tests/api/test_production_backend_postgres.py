"""Route-level PostgreSQL coverage for the concrete API backend."""

from __future__ import annotations

import asyncio
import hashlib
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
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
from codemigrator.runtime.create_run import (
    CreateRunRejected,
    RunCreationOwner,
    RunWorkflowGraphStarter,
)
from codemigrator.runtime.store import PostgreSQLRuntimeStore

from .conftest import build_plan_agent_inputs, create_run_payload


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

    async def verify_descriptor_lock(
        self, request, transaction: RuntimeStoreTransaction | None = None
    ) -> None:  # type: ignore[no-untyped-def]
        del request, transaction
        self.calls.append("descriptor")

    async def verify_preindex(
        self, request, transaction: RuntimeStoreTransaction | None = None
    ) -> None:  # type: ignore[no-untyped-def]
        del request, transaction
        self.calls.append("preindex")

    async def verify_dossier_consistency(
        self, request, transaction: RuntimeStoreTransaction | None = None
    ) -> None:  # type: ignore[no-untyped-def]
        del request, transaction
        self.calls.append("dossier")


class TwoRequestBarrierPreflight(PassingPreflight):
    def __init__(self) -> None:
        super().__init__()
        self._arrivals = 0
        self._both_arrived = asyncio.Event()

    async def verify_descriptor_lock(
        self, request, transaction: RuntimeStoreTransaction | None = None
    ) -> None:  # type: ignore[no-untyped-def]
        del request, transaction
        self._arrivals += 1
        if self._arrivals == 2:
            self._both_arrived.set()
        await self._both_arrived.wait()
        self.calls.append("descriptor")


class BlockingPreflight(PassingPreflight):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def verify_descriptor_lock(
        self, request, transaction: RuntimeStoreTransaction | None = None
    ) -> None:  # type: ignore[no-untyped-def]
        del request, transaction
        self.entered.set()
        await self.release.wait()
        self.calls.append("descriptor")


class TransactionBackedRejectingPreflight:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.connection_ids: list[int] = []

    async def _read(self, gate: str, request, transaction) -> None:  # type: ignore[no-untyped-def]
        del request
        assert transaction is not None
        self.calls.append(gate)
        self.connection_ids.append(id(transaction.connection))
        assert await transaction.connection.fetchval("SELECT 1") == 1

    async def verify_descriptor_lock(self, request, transaction) -> None:  # type: ignore[no-untyped-def]
        await self._read("descriptor", request, transaction)

    async def verify_preindex(self, request, transaction) -> None:  # type: ignore[no-untyped-def]
        await self._read("preindex", request, transaction)

    async def verify_dossier_consistency(self, request, transaction) -> None:  # type: ignore[no-untyped-def]
        await self._read("dossier", request, transaction)
        raise CreateRunRejected(
            "synthetic transaction-backed gate rejection",
            code=StableErrorCode.DESCRIPTOR_DIGEST_MISMATCH,
        )


class RejectingPreflight(PassingPreflight):
    async def verify_preindex(
        self, request, transaction: RuntimeStoreTransaction | None = None
    ) -> None:  # type: ignore[no-untyped-def]
        await super().verify_preindex(request, transaction)
        raise CreateRunRejected("synthetic gate rejection with sensitive context")


class TypedRejectingPreflight(PassingPreflight):
    def __init__(self, gate: str, code: StableErrorCode | FailureReason) -> None:
        super().__init__()
        self.gate = gate
        self.code = code

    async def _reject(
        self, gate: str, request, transaction: RuntimeStoreTransaction | None = None
    ) -> None:  # type: ignore[no-untyped-def]
        del transaction
        self.calls.append(gate)
        if gate == self.gate:
            raise CreateRunRejected("synthetic private gate detail", code=self.code)

    async def verify_descriptor_lock(
        self, request, transaction: RuntimeStoreTransaction | None = None
    ) -> None:  # type: ignore[no-untyped-def]
        await self._reject("descriptor", request, transaction)

    async def verify_preindex(
        self, request, transaction: RuntimeStoreTransaction | None = None
    ) -> None:  # type: ignore[no-untyped-def]
        await self._reject("preindex", request, transaction)

    async def verify_dossier_consistency(
        self, request, transaction: RuntimeStoreTransaction | None = None
    ) -> None:  # type: ignore[no-untyped-def]
        await self._reject("dossier", request, transaction)


class RecordingGraphStarter:
    receipt_idempotent = True

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.receipts: list[RunCreatedReceipt] = []
        self.completed = asyncio.Event()

    async def start(self, run_id: RunId, receipt: RunCreatedReceipt) -> None:
        assert UUID(str(run_id)) == receipt.run_id
        try:
            if self.fail:
                raise RuntimeError("synthetic private graph failure")
            self.receipts.append(receipt)
        finally:
            self.completed.set()


class BlockingGraphStarter(RecordingGraphStarter):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def start(self, run_id: RunId, receipt: RunCreatedReceipt) -> None:
        self.receipts.append(receipt)
        self.entered.set()
        try:
            await self.release.wait()
        finally:
            self.completed.set()


class ReceiptEffectGraphStarter(RecordingGraphStarter):
    def __init__(self) -> None:
        super().__init__()
        self.effects: set[str] = set()
        self.domain_work_count = 0

    async def start(self, run_id: RunId, receipt: RunCreatedReceipt) -> None:
        try:
            self.receipts.append(receipt)
            if receipt.receipt_key not in self.effects:
                self.effects.add(receipt.receipt_key)
                self.domain_work_count += 1
        finally:
            self.completed.set()


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
        await asyncio.wait_for(failing_graph.completed.wait(), timeout=2)
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
        await asyncio.wait_for(recovered_graph.completed.wait(), timeout=2)
        for _ in range(100):
            if await store.list_pending_graph_starts() == ():
                break
            await asyncio.sleep(0.01)

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
        await asyncio.wait_for(graph.completed.wait(), timeout=2)
        assert len(graph.receipts) == 1
        assert graph.domain_work_count == 1
        assert len(await store.list_pending_graph_starts()) == 1
        graph.completed.clear()
        await backend.recover_pending_graph_starts()
        await asyncio.wait_for(graph.completed.wait(), timeout=2)
        for _ in range(100):
            if await store.list_pending_graph_starts() == ():
                break
            await asyncio.sleep(0.01)

        assert len(graph.receipts) == 2
        assert graph.domain_work_count == 1
        assert await store.list_pending_graph_starts() == ()
        assert preflight.calls == ["descriptor", "preindex", "dossier"]
        await backend.close()


@pytest.mark.asyncio
async def test_graph_restart_uses_fresh_actor_and_pg_cas_checkpoint(tmp_path: Path):
    from types import SimpleNamespace

    from codemigrator.core import Phase, RunStatus, SessionKind
    from codemigrator.runtime.agent_runs import AgentRun, AgentRunId, AgentRunReceipt
    from codemigrator.runtime.cas import CasObject, FileHostCAS
    from codemigrator.runtime.checkpointer import CasCheckpointSaver
    from codemigrator.runtime.contracts import (
        ExecutionRoundDecision,
        ReportSummary,
        VerificationSummary,
    )
    from codemigrator.runtime.create_run import RunCreationOwner
    from codemigrator.runtime.loop_contracts import SessionExit, SessionState
    from codemigrator.runtime.run_graph import PlanAgentCompletion, RunWorkflowGraph

    class SyntheticPlanStage:
        def __init__(self, store, actor, effects) -> None:  # type: ignore[no-untyped-def]
            self.store = store
            self.actor = actor
            self.effects = effects

        async def run(self, run_id):  # type: ignore[no-untyped-def]
            self.effects["PLAN"] += 1
            created = await self.store.create_or_get_agent_run(
                AgentRun(
                    agent_run_id=AgentRunId(uuid4()),
                    owner_kind="run",
                    owner_id=run_id,
                    logical_task_key=f"plan:{run_id}",
                    phase=Phase.Plan,
                    session_kind=SessionKind.PlanAuxiliary,
                    thread_id=str(uuid4()),
                    model_binding_sha256="a" * 64,
                    context_sha256="b" * 64,
                    toolset_sha256="c" * 64,
                    template_sha256="d" * 64,
                )
            )
            await self.actor.record_agent_run_started(run_id, created.agent_run_id)
            plan_body = b'{"plan":"synthetic"}'
            result_body = b"synthetic-plan-result"
            plan_object = CasObject(hashlib.sha256(plan_body).hexdigest(), len(plan_body))
            result_object = CasObject(hashlib.sha256(result_body).hexdigest(), len(result_body))
            completed = replace(
                created,
                state=SessionState.Closed,
                exit=SessionExit.Completed,
                result_sha256=result_object.digest,
            )
            frozen_plan = SimpleNamespace(
                plan_hash=plan_object.digest,
                validation=SimpleNamespace(accepted=True),
                canonical_payload=lambda: plan_body,
            )
            return await self.actor.accept_plan(
                run_id,
                PlanAgentCompletion(
                    record=completed,
                    receipt=AgentRunReceipt(
                        uuid4(), created.agent_run_id, "plan.accepted"
                    ),
                    result_object=result_object,
                    plan_object=plan_object,
                ),
                frozen_plan,
            )

    class SyntheticExecutionScheduler:
        def __init__(self, effects) -> None:  # type: ignore[no-untyped-def]
            self.effects = effects

        async def advance_one_round(self, run_id, logical_key, *, on_agent_run_started):  # type: ignore[no-untyped-def]
            del run_id, logical_key, on_agent_run_started
            self.effects["EXECUTE"] += 1
            return ExecutionRoundDecision(complete=True, dispatch_count=0)

    class CountedStage:
        def __init__(self, name, result, effects) -> None:  # type: ignore[no-untyped-def]
            self.name = name
            self.result = result
            self.effects = effects

        async def run(self, run_id):  # type: ignore[no-untyped-def]
            del run_id
            self.effects[self.name] += 1
            return self.result

    class ReportStage:
        def __init__(self, effects, *, block: bool, entered, release) -> None:  # type: ignore[no-untyped-def]
            self.effects = effects
            self.block = block
            self.entered = entered
            self.release = release

        async def run(self, run_id):  # type: ignore[no-untyped-def]
            del run_id
            self.effects["REPORT_ATTEMPTS"] += 1
            if self.block:
                self.entered.set()
                await self.release.wait()
            self.effects["REPORT_EFFECTS"] += 1
            return ReportSummary("f" * 64, RunStatus.Completed)

    class TrackingStarter:
        receipt_idempotent = True

        def __init__(
            self, store, cas_root: Path, effects, *, block_report: bool = False
        ) -> None:  # type: ignore[no-untyped-def]
            self.store = store
            self.cas_root = cas_root
            self.effects = effects
            self.block_report = block_report
            self.completed = asyncio.Event()
            self.report_entered = asyncio.Event()
            self.report_release = asyncio.Event()
            self.actors = []
            self.graphs = []
            self.savers = []
            self._delegate = RunWorkflowGraphStarter(
                self._graph_factory, durable_checkpointer=True
            )

        def _graph_factory(self, actor):  # type: ignore[no-untyped-def]
            self.actors.append(actor)
            actor.execution_scheduler = SyntheticExecutionScheduler(self.effects)
            saver = CasCheckpointSaver(
                FileHostCAS(self.cas_root),
                self.store,
                graph_family="run",
                owner_kind="run",
                owner_id=UUID(str(actor.run_id)),
            )
            self.savers.append(saver)
            graph = RunWorkflowGraph(
                actor=actor,
                planner=SyntheticPlanStage(self.store, actor, self.effects),
                verifier=CountedStage(
                    "VERIFY", VerificationSummary(True, "e" * 64), self.effects
                ),
                reporter=ReportStage(
                    self.effects,
                    block=self.block_report,
                    entered=self.report_entered,
                    release=self.report_release,
                ),
                checkpointer=saver,
            )
            self.graphs.append(graph)
            return graph

        async def start(self, run_id, receipt):  # type: ignore[no-untyped-def]
            await self._delegate.start(run_id, receipt)

        async def start_for_actor(self, run_id, receipt, actor):  # type: ignore[no-untyped-def]
            try:
                await self._delegate.start_for_actor(run_id, receipt, actor)
            finally:
                self.completed.set()

    async with isolated_store() as (store, _schema):
        effects = {
            "PLAN": 0,
            "EXECUTE": 0,
            "VERIFY": 0,
            "REPORT_ATTEMPTS": 0,
            "REPORT_EFFECTS": 0,
        }
        cas_root = tmp_path / "shared-run-cas"
        preflight = PassingPreflight()
        starter1 = TrackingStarter(store, cas_root, effects, block_report=True)
        backend1 = ProductionApiBackend(
            store,
            run_owner=RunCreationOwner(
                store=store, preflight=preflight, graph_starter=starter1
            ),
        )
        headers = {
            "Authorization": "Bearer synthetic-token",
            "Idempotency-Key": "pg-cas-restart",
        }
        app1 = create_app(backend1, config=ApiConfig(token="synthetic-token"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app1), base_url="http://127.0.0.1"
        ) as client:
            first = await client.post(
                "/api/v1/migrations", json=create_run_payload(), headers=headers
            )
        assert first.status_code == 201
        response_body = first.json()
        run_id = RunId(UUID(response_body["run_id"]))
        await asyncio.wait_for(starter1.report_entered.wait(), timeout=5)
        assert effects == {
            "PLAN": 1,
            "EXECUTE": 1,
            "VERIFY": 1,
            "REPORT_ATTEMPTS": 1,
            "REPORT_EFFECTS": 0,
        }
        assert len(await store.list_pending_graph_starts()) == 1
        assert await store.list_checkpoint_indexes(str(run_id))
        first_events = await store.read_run_events(run_id, 0)
        first_event_types = [event.event_type for event in first_events]
        for event_type in (
            "run.created",
            "run.plan.accepted",
            "run.execute.round",
            "run.verify.completed",
        ):
            assert first_event_types.count(event_type) == 1

        await backend1.close()
        assert starter1.completed.is_set()
        assert len(await store.list_pending_graph_starts()) == 1

        store2 = PostgreSQLRuntimeStore(store.pool)
        starter2 = TrackingStarter(store2, cas_root, effects)
        original_mark_started = store2.mark_graph_start_started
        mark_failure = asyncio.Event()
        failed_once = False

        async def fail_first_handoff_mark(run_id, receipt_key):  # type: ignore[no-untyped-def]
            nonlocal failed_once
            if not failed_once:
                failed_once = True
                mark_failure.set()
                raise RuntimeError("synthetic durable handoff mark failure")
            await original_mark_started(run_id, receipt_key)

        store2.mark_graph_start_started = fail_first_handoff_mark  # type: ignore[method-assign]
        owner2 = RunCreationOwner(
            store=store2, preflight=preflight, graph_starter=starter2
        )
        backend2 = ProductionApiBackend(
            store2, run_owner=owner2
        )
        await backend2.recover_pending_graph_starts()
        recovery_tasks = tuple(backend2._graph_start_tasks.values())
        assert len(recovery_tasks) == 1
        await asyncio.wait_for(starter2.completed.wait(), timeout=5)
        await asyncio.wait_for(mark_failure.wait(), timeout=5)
        for _ in range(100):
            if await store2.list_pending_graph_starts() != ():
                break
            await asyncio.sleep(0.01)

        assert len(await store2.list_pending_graph_starts()) == 1
        assert effects == {
            "PLAN": 1,
            "EXECUTE": 1,
            "VERIFY": 1,
            "REPORT_ATTEMPTS": 2,
            "REPORT_EFFECTS": 1,
        }
        assert len(starter1.graphs) == len(starter2.graphs) == 1
        assert starter1.graphs[0] is not starter2.graphs[0]
        assert starter1.savers[0] is not starter2.savers[0]
        assert starter1.actors[0] is not starter2.actors[0]
        final_events = await store2.read_run_events(run_id, 0)
        final_event_types = [event.event_type for event in final_events]
        assert final_event_types.count("run.report.completed") == 1
        assert final_event_types[: len(first_event_types)] == first_event_types
        assert preflight.calls == ["descriptor", "preindex", "dossier"]

        await backend2.close()
        store3 = PostgreSQLRuntimeStore(store.pool)
        starter3 = TrackingStarter(store3, cas_root, effects)
        backend3 = ProductionApiBackend(
            store3,
            run_owner=RunCreationOwner(
                store=store3, preflight=preflight, graph_starter=starter3
            ),
        )
        await backend3.recover_pending_graph_starts()
        recovery_tasks = tuple(backend3._graph_start_tasks.values())
        assert len(recovery_tasks) == 1
        await asyncio.gather(*recovery_tasks)
        assert await store3.list_pending_graph_starts() == ()
        assert starter3.graphs == []
        assert effects == {
            "PLAN": 1,
            "EXECUTE": 1,
            "VERIFY": 1,
            "REPORT_ATTEMPTS": 2,
            "REPORT_EFFECTS": 1,
        }

        app3 = create_app(backend3, config=ApiConfig(token="synthetic-token"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app3), base_url="http://127.0.0.1"
        ) as client:
            replay = await client.post(
                "/api/v1/migrations", json=create_run_payload(), headers=headers
            )
        assert replay.status_code == 201
        assert replay.json() == response_body
        assert [
            event.event_type for event in await store3.read_run_events(run_id, 0)
        ] == final_event_types
        await backend3.close()


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
async def test_preflight_gates_share_the_api_transaction_connection_at_pool_size_one():
    async with isolated_store(max_size=1) as (store, _schema):
        preflight = TransactionBackedRejectingPreflight()
        backend = ProductionApiBackend(
            store,
            run_owner=RunCreationOwner(
                store=store,
                preflight=preflight,  # type: ignore[arg-type]
                graph_starter=RecordingGraphStarter(),
            ),
        )
        app = create_app(backend, config=ApiConfig(token="synthetic-token"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            response = await asyncio.wait_for(
                client.post(
                    "/api/v1/migrations",
                    json=create_run_payload(),
                    headers={
                        "Authorization": "Bearer synthetic-token",
                        "Idempotency-Key": "transaction-backed-preflight",
                    },
                ),
                timeout=2,
            )

        assert response.status_code == 422
        assert response.json()["retryable"] is False
        assert preflight.calls == ["descriptor", "preindex", "dossier"]
        assert len(set(preflight.connection_ids)) == 1
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


@pytest.mark.asyncio
async def test_cancelled_outer_command_finishes_rollback_callbacks_and_removes_run_facts():
    async with isolated_store() as (store, _schema):
        owner = RunCreationOwner(
            store=store,
            preflight=PassingPreflight(),
            graph_starter=RecordingGraphStarter(),
        )
        request = CreateRun.model_validate(create_run_payload())
        created = asyncio.Event()
        transactions: list[RuntimeStoreTransaction] = []

        async def command(transaction: RuntimeStoreTransaction) -> RunCreatedReceipt:
            transactions.append(transaction)
            receipt = await owner.create_run(request, transaction)
            created.set()
            await asyncio.Event().wait()
            return receipt

        command_task = asyncio.create_task(
            store.execute_api_command(
                principal_id="local",
                route="/api/v1/migrations",
                key="cancelled-outer-command",
                canonical_body=canonical_json_bytes(request.model_dump(mode="json")),
                status_code=201,
                command=command,
                project_response=lambda receipt: {"run_id": str(receipt.run_id)},
                owner_receipt=lambda receipt: (
                    "run",
                    UUID(str(receipt.run_id)),
                    receipt.receipt_key,
                ),
            )
        )
        await asyncio.wait_for(created.wait(), timeout=2)
        command_task.cancel()
        await asyncio.gather(command_task, return_exceptions=True)

        try:
            assert transactions and transactions[0].active is False
            await asyncio.sleep(0)
            assert owner.active_actor_count == 0
            async with store.pool.acquire() as connection:
                assert await connection.fetchval("SELECT count(*) FROM runtime_runs") == 0
                assert await connection.fetchval("SELECT count(*) FROM runtime_events") == 0
                assert await connection.fetchval("SELECT count(*) FROM api_command_receipts") == 0
                assert (
                    await connection.fetchval("SELECT count(*) FROM run_graph_start_handoffs")
                    == 0
                )
        finally:
            if transactions and transactions[0].active:
                transactions[0].finish(committed=False)
            await owner.close()


@pytest.mark.asyncio
async def test_cancelled_create_run_stops_actor_before_create_returns(monkeypatch):
    from codemigrator.api.problems import ApiError

    async with isolated_store() as (store, _schema):
        owner = RunCreationOwner(
            store=store,
            preflight=PassingPreflight(),
            graph_starter=RecordingGraphStarter(),
        )
        backend = ProductionApiBackend(store, run_owner=owner)
        request = CreateRun.model_validate(create_run_payload())
        create_processed = asyncio.Event()
        release_join = asyncio.Event()
        actors: list[RunActor] = []
        original_join = RunActor.join

        async def pause_after_create_mailbox(self: RunActor) -> None:
            await original_join(self)
            if self._create_receipt is not None:
                actors.append(self)
                create_processed.set()
                await release_join.wait()

        monkeypatch.setattr(RunActor, "join", pause_after_create_mailbox)
        command_task = asyncio.create_task(
            backend.execute_idempotent(
                ApiRequest(operation="create_run", principal_id="local", payload=request),
                route="/api/v1/migrations",
                key="cancel-while-create-receipt-awaits",
                canonical_body=canonical_json_bytes(request.model_dump(mode="json")),
                status_code=201,
            )
        )
        actor: RunActor | None = None
        try:
            await asyncio.wait_for(create_processed.wait(), timeout=2)
            assert len(actors) == 1
            actor = actors[0]
            assert actor.state is not None
            assert owner.active_actor_count == 0

            backend.close_admission()
            await backend.close()
            with pytest.raises(ApiError) as raised:
                await command_task
            assert raised.value.status_code == 503
            assert actor.state is None
            assert actor._stopping is True
            assert actor._task is None
            assert owner.active_actor_count == 0
            async with store.pool.acquire() as connection:
                assert await connection.fetchval("SELECT count(*) FROM runtime_runs") == 0
                assert await connection.fetchval("SELECT count(*) FROM runtime_events") == 0
                assert await connection.fetchval("SELECT count(*) FROM api_command_receipts") == 0
                assert (
                    await connection.fetchval("SELECT count(*) FROM run_graph_start_handoffs")
                    == 0
                )
        finally:
            release_join.set()
            if not command_task.done():
                command_task.cancel()
                await asyncio.gather(command_task, return_exceptions=True)
            if actor is not None and actor._task is not None:
                await actor.stop()
            await backend.close()


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
        await asyncio.wait_for(graph.completed.wait(), timeout=2)
        assert len(graph.receipts) == 1
        run_id = RunId(UUID(first.json()["run_id"]))
        assert len(await store.read_run_events(run_id, 0)) == 1
        for _ in range(100):
            if await store.list_pending_graph_starts() == ():
                break
            await asyncio.sleep(0.01)
        assert await store.list_pending_graph_starts() == ()
        await backend.close()


@pytest.mark.asyncio
async def test_create_returns_while_receipt_graph_handoff_is_still_running():
    async with isolated_store() as (store, _schema):
        graph = BlockingGraphStarter()
        backend = ProductionApiBackend(
            store,
            run_owner=RunCreationOwner(
                store=store, preflight=PassingPreflight(), graph_starter=graph
            ),
        )
        app = create_app(backend, config=ApiConfig(token="synthetic-token"))
        headers = {
            "Authorization": "Bearer synthetic-token",
            "Idempotency-Key": "create-returns-after-handoff-schedule",
        }
        create_task: asyncio.Task[httpx.Response] | None = None
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
            ) as client:
                create_task = asyncio.create_task(
                    client.post("/api/v1/migrations", json=create_run_payload(), headers=headers)
                )
                await asyncio.wait_for(graph.entered.wait(), timeout=2)
                created = await asyncio.wait_for(create_task, timeout=0.5)
                assert created.status_code == 201
                run_id = RunId(UUID(created.json()["run_id"]))
                pending = await store.list_pending_graph_starts()
                assert pending == ((run_id, f"run.created:{run_id}"),)

                replay = await client.post(
                    "/api/v1/migrations", json=create_run_payload(), headers=headers
                )
                assert replay.status_code == 201
                assert replay.json() == created.json()
                assert len(graph.receipts) == 1
                assert await store.list_pending_graph_starts() == pending
                graph.release.set()

            await asyncio.wait_for(graph.completed.wait(), timeout=2)
            for _ in range(100):
                if await store.list_pending_graph_starts() == ():
                    break
                await asyncio.sleep(0.01)
            assert await store.list_pending_graph_starts() == ()
        finally:
            graph.release.set()
            if create_task is not None and not create_task.done():
                await asyncio.gather(create_task, return_exceptions=True)
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
        for _ in range(100):
            if len(graph.receipts) == 2:
                break
            await asyncio.sleep(0.01)
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

        graph = BlockingGraphStarter()
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
        try:
            async with app.router.lifespan_context(app):
                assert app.state.runtime_ready is True
                await asyncio.wait_for(graph.entered.wait(), timeout=2)
                assert not graph.release.is_set()
                owned_pool = app.state.runtime_pool
                async with owned_pool.acquire() as connection:
                    pending_count = await connection.fetchval(
                        "SELECT count(*) FROM run_graph_start_handoffs WHERE status='PENDING'"
                    )
                    started_count = await connection.fetchval(
                        "SELECT count(*) FROM run_graph_start_handoffs WHERE status='STARTED'"
                    )
                assert pending_count == 1
                assert started_count == 0
                graph.release.set()
                await asyncio.wait_for(graph.completed.wait(), timeout=2)
                for _ in range(100):
                    if await store.list_pending_graph_starts() == ():
                        break
                    await asyncio.sleep(0.01)
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
        finally:
            graph.release.set()

        assert app.state.runtime_ready is False
        assert owned_pool.is_closing()
        assert shutdown_calls == ["closed"]


@pytest.mark.asyncio
async def test_production_asgi_composes_run_graph_from_locked_store_and_actor_factory(
    tmp_path: Path,
):
    dsn = os.environ.get("CODEMIGRATOR_TEST_PG_DSN")
    if not dsn:
        pytest.skip("CODEMIGRATOR_TEST_PG_DSN is not configured")
    async with isolated_store() as (_store, schema):
        from codemigrator.runtime.cas import FileHostCAS
        from codemigrator.runtime.checkpointer import CasCheckpointSaver
        from codemigrator.runtime.graph_composition import (
            AgentGraphInfrastructure,
            RuntimeGraphAssembly,
        )
        from codemigrator.runtime.memory import ContextManager
        from codemigrator.runtime.provider import ProviderRegistry

        entered_plan = asyncio.Event()
        hold_plan = asyncio.Event()
        scheduler = object()
        factory_calls = []
        created_actors = []
        stage_actors = []

        class WaitingPlanStage:
            def __init__(self, received_actor):
                self.actor = received_actor

            async def run(self, run_id):
                assert self.actor.run_id == run_id
                assert self.actor.execution_scheduler is scheduler
                stage_actors.append(self.actor)
                entered_plan.set()
                await hold_plan.wait()

        def components_factory(store, pool, lock_connection):
            assert isinstance(store, PostgreSQLRuntimeStore)
            assert store.pool is pool
            assert store._write_connection is lock_connection
            app_owner_id = uuid4()
            cas = FileHostCAS(tmp_path / "cas")
            infrastructure = AgentGraphInfrastructure(
                provider_registry=ProviderRegistry({}),
                context_manager=ContextManager(),
                tool_gateway=object(),
                runtime_store=store,
                host_cas=cas,
                cas_references=store,
                usage_sink=object(),
                run_checkpointer=CasCheckpointSaver(
                    cas,
                    store,
                    graph_family="run",
                    owner_kind="run",
                    owner_id=app_owner_id,
                ),
                draft_graph_checkpointer=CasCheckpointSaver(
                    cas,
                    store,
                    graph_family="draft",
                    owner_kind="draft",
                    owner_id=app_owner_id,
                ),
                agent_run_checkpointer=CasCheckpointSaver(
                    cas,
                    store,
                    graph_family="agent",
                    owner_kind="run",
                    owner_id=app_owner_id,
                ),
            )

            def actor_factory(run_id, actor_store):
                factory_calls.append((run_id, actor_store))
                actor = RunActor(run_id, actor_store, execution_scheduler=scheduler)
                created_actors.append(actor)
                return actor

            assembly = RuntimeGraphAssembly(
                infrastructure,
                plan_stage_factory=lambda _infra, received_actor: WaitingPlanStage(
                    received_actor
                ),
                verifier_factory=lambda _infra, _actor: object(),
                reporter_factory=lambda _infra, _actor: object(),
                draft_agent_runner_factory=lambda _infra, _owner: object(),
                create_run_service_factory=lambda _infra, _owner: object(),
            )
            from codemigrator.asgi import ProductionRunComponents

            return ProductionRunComponents(
                preflight=PassingPreflight(),
                graph_assembly=assembly,
                actor_factory=actor_factory,
                durable_checkpointer=True,
            )

        app = create_production_app(
            dsn,
            config=ApiConfig(token="synthetic-token"),
            run_components_factory=components_factory,
            stop_server=noop_server_stop,
            pool_server_settings={"search_path": schema},
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
            ) as client:
                response = await client.post(
                    "/api/v1/migrations",
                    json=create_run_payload(),
                    headers={
                        "Authorization": "Bearer synthetic-token",
                        "Idempotency-Key": "runtime-components-factory",
                    },
                )
            assert response.status_code == 201
            await asyncio.wait_for(entered_plan.wait(), timeout=2)
            assert len(factory_calls) == 1
            assert factory_calls[0][1] is stage_actors[0].store
            assert stage_actors == created_actors


@pytest.mark.asyncio
async def test_production_asgi_runs_plan_agent_to_actor_acceptance_before_execute(
    tmp_path: Path,
):
    dsn = os.environ.get("CODEMIGRATOR_TEST_PG_DSN")
    if not dsn:
        pytest.skip("CODEMIGRATOR_TEST_PG_DSN is not configured")
    import json

    from codemigrator.asgi import ProductionRunComponents
    from codemigrator.core import ModelProfile, Phase, SessionKind, load_resource
    from codemigrator.core.enums import SliceKind
    from codemigrator.core.models.plan import PlanProposal, PlanSliceProposal
    from codemigrator.runtime.binding import LockedModelBinding
    from codemigrator.runtime.cas import FileHostCAS
    from codemigrator.runtime.checkpointer import CasCheckpointSaver
    from codemigrator.runtime.context import ContextEnvelope, ContextSegment
    from codemigrator.runtime.graph_composition import (
        AgentGraphInfrastructure,
        RuntimeGraphAssembly,
    )
    from codemigrator.runtime.memory import ContextManager, FormulaNetInputCap
    from codemigrator.runtime.plan_agent import (
        PersistentPlanStageFactory,
        PlanSessionMaterial,
    )
    from codemigrator.runtime.provider import (
        OpenAICompatibleProvider,
        ProviderRegistry,
        ProviderResponse,
        ProviderToolCall,
        TokenUsage,
        provider_adapter_id_for_label,
        select_unique_provider_config,
    )
    from codemigrator.workspace import GatewayContext
    async with isolated_store() as (_store, schema):
        planning_inputs = build_plan_agent_inputs()
        source_modules = planning_inputs.analysis.modules
        proposal = PlanProposal(
            slices=[
                PlanSliceProposal(
                    local_ref="A",
                    kind=SliceKind.Implementation,
                    source_modules=[source_modules[0].module_id],
                    write_paths=["target/a.py"],
                    create_roots=["target/a"],
                ),
                PlanSliceProposal(
                    local_ref="B",
                    kind=SliceKind.Implementation,
                    source_modules=[source_modules[1].module_id],
                    write_paths=["target/b.py"],
                    create_roots=["target/b"],
                ),
            ],
            edges=[],
            integration_ranks={"A": 0, "B": 1},
            planner_rationale=[],
        )
        entered_after_plan = asyncio.Event()
        plan_result_ready = asyncio.Event()
        release_plan = asyncio.Event()
        actors = []
        requests = []
        stage_error_types = []

        class StructuredProvider:
            async def complete(self, request):
                return ProviderResponse(
                    content="",
                    tool_calls=(
                        ProviderToolCall(
                            "PlanProposal",
                            json.dumps(proposal.model_dump(mode="json", by_alias=True)),
                            f"production-plan-{len(requests)}",
                        ),
                    ),
                    finish_reason="tool_calls",
                    usage=TokenUsage(12, 9),
                    model=request.binding.model_id,
                    provider_receipt_id=f"production-receipt-{len(requests)}",
                )

        class ExactCounter:
            def count(self, messages):
                return sum(len(message.content) for message in messages)

            def count_tool_schemas(self, tools):
                return sum(len(json.dumps(dict(tool.parameters))) for tool in tools)

        class UsageSink:
            def __init__(self):
                self.calls = {}

            async def reserve_round(self, agent_run_id, call_id, *, max_rounds):
                calls = self.calls.setdefault(agent_run_id, {})
                if call_id in calls:
                    return calls[call_id]
                if calls:
                    return None
                calls[call_id] = 1
                return 1

            async def record(self, agent_run_id, usage, receipt):
                return None

        class Gateway:
            def __init__(self, context):
                self.context = context

            def dispatch(self, raw_call, *, cancellation_token=None):
                raise AssertionError("the synthetic PLAN proposal requires no exploration")

        class MaterialLoader:
            async def load(self, run_id):
                return PlanSessionMaterial(
                    planning_inputs,
                    binding,
                    envelope,
                )

        class ProviderSpy:
            def __init__(self, delegate):
                self.delegate = delegate

            async def complete(self, request):
                requests.append(request)
                return await self.delegate.complete(request)

        real_opencode = os.environ.get("CODEMIGRATOR_REAL_OPENCODE", "").casefold() in {
            "1",
            "true",
        }
        if real_opencode:
            configured_path = os.environ.get("CODEMIGRATOR_OPENCODE_CONFIG")
            candidates = (
                [Path(configured_path)]
                if configured_path
                else [
                    Path(__file__).resolve().parents[2] / "my_space/model_api_key.json",
                    Path("/home/xtc/project/CodeM/CodeMigrator/my_space/model_api_key.json"),
                ]
            )
            config_path = next((path for path in candidates if path.is_file()), None)
            if config_path is None:
                pytest.fail("local OpenCode provider config is unavailable")
            opencode = select_unique_provider_config(
                config_path.read_text(encoding="utf-8"), "OpenCode"
            )
            provider_id = provider_adapter_id_for_label(str(opencode["Provider"]))
            model_id = str(opencode["模型"])
            config_revision = hashlib.sha256(
                json.dumps(
                    {key: value for key, value in opencode.items() if key != "API Key"},
                    sort_keys=True,
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
            binding = LockedModelBinding(
                provider_id=provider_id,
                model_id=model_id,
                profile=ModelProfile.Reasoning,
                config_revision=config_revision,
                context_window=int(opencode["Context Window"]),
                output_cap=min(2_048, int(opencode["模型输出上限"])),
            )
            delegate = OpenAICompatibleProvider(
                endpoint=str(opencode["Base URL"]), api_key=str(opencode["API Key"])
            )
            class ConservativeCounter:
                def count(self, messages):
                    return sum(max(1, len(message.content.encode("utf-8"))) for message in messages)

                def count_tool_schemas(self, tools):
                    return sum(
                        len(json.dumps(dict(tool.parameters), ensure_ascii=False).encode("utf-8"))
                        for tool in tools
                    )

            token_counter = ConservativeCounter()
            envelope = ContextEnvelope(
                stable=(
                    ContextSegment(
                        "stable",
                        "All planning data is synthetic. Create exactly two IMPLEMENTATION "
                        "slices: A uses source module "
                        f"{source_modules[0].module_id}, writes target/a.py, and owns target/a; "
                        "B uses source module "
                        f"{source_modules[1].module_id}, writes target/b.py, and owns target/b. "
                        "Use no edges, rank A as 0 and B as 1, and do not call tools.",
                        required=True,
                    ),
                )
            )
        else:
            provider_id = "synthetic"
            binding = LockedModelBinding(
                provider_id=provider_id,
                model_id="fixed-plan-test",
                profile=ModelProfile.Reasoning,
                config_revision="production-plan-test-v1",
                context_window=64_000,
                output_cap=2_048,
            )
            delegate = StructuredProvider()
            token_counter = ExactCounter()
            envelope = ContextEnvelope()
        provider = ProviderSpy(delegate)

        def components_factory(store, pool, lock_connection):
            assert isinstance(store, PostgreSQLRuntimeStore)
            assert store.pool is pool
            assert store._write_connection is lock_connection
            cas = FileHostCAS(tmp_path / "cas")
            prototype_owner = uuid4()
            context_manager = ContextManager(
                token_counter=token_counter,
                net_input_cap=FormulaNetInputCap(),
            )
            infrastructure = AgentGraphInfrastructure(
                provider_registry=ProviderRegistry({provider_id: provider}),
                context_manager=context_manager,
                tool_gateway=object(),
                runtime_store=store,
                host_cas=cas,
                cas_references=store,
                usage_sink=UsageSink(),
                run_checkpointer=CasCheckpointSaver(
                    cas,
                    store,
                    graph_family="run",
                    owner_kind="run",
                    owner_id=prototype_owner,
                ),
                draft_graph_checkpointer=CasCheckpointSaver(
                    cas,
                    store,
                    graph_family="draft",
                    owner_kind="draft",
                    owner_id=prototype_owner,
                ),
                agent_run_checkpointer=CasCheckpointSaver(
                    cas,
                    store,
                    graph_family="agent",
                    owner_kind="run",
                    owner_id=prototype_owner,
                ),
            )
            stage_factory = PersistentPlanStageFactory(
                material_loader=MaterialLoader(),
                gateway_factory=lambda record: Gateway(
                    GatewayContext(
                        run_id=RunId(record.owner_id),
                        agent_run_id=record.agent_run_id,
                        phase_policy_sha256=load_resource(
                            "core://phase-tool-policy/v2"
                        ).sha256,
                        phase=Phase.Plan,
                        session_kind=SessionKind.PlanAuxiliary,
                    )
                ),
            )

            def actor_factory(run_id, actor_store):
                actor = RunActor(run_id, actor_store)
                actors.append(actor)
                return actor

            def plan_factory(infra, actor):
                workflow = stage_factory(infra, actor)

                class PauseAfterAcceptance:
                    async def run(self, run_id):
                        try:
                            receipt = await workflow.run(run_id)
                        except Exception as error:
                            stage_error_types.append(type(error).__name__)
                            plan_result_ready.set()
                            raise
                        plan_result_ready.set()
                        entered_after_plan.set()
                        await release_plan.wait()
                        return receipt

                return PauseAfterAcceptance()

            return ProductionRunComponents(
                preflight=PassingPreflight(),
                graph_assembly=RuntimeGraphAssembly(
                    infrastructure,
                    plan_stage_factory=plan_factory,
                    verifier_factory=lambda _infra, _actor: object(),
                    reporter_factory=lambda _infra, _actor: object(),
                    draft_agent_runner_factory=lambda _infra, _owner: object(),
                    create_run_service_factory=lambda _infra, _owner: object(),
                ),
                actor_factory=actor_factory,
                durable_checkpointer=True,
            )

        request_body = create_run_payload()
        request_body["frozen_artifacts"] = planning_inputs.frozen_artifacts.model_dump(
            mode="json"
        )
        app = create_production_app(
            dsn,
            config=ApiConfig(token="synthetic-token"),
            run_components_factory=components_factory,
            stop_server=noop_server_stop,
            pool_server_settings={"search_path": schema},
        )
        test_failure: Exception | None = None
        try:
            async with app.router.lifespan_context(app):
                try:
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
                    ) as client:
                        response = await client.post(
                            "/api/v1/migrations",
                            json=request_body,
                            headers={
                                "Authorization": "Bearer synthetic-token",
                                "Idempotency-Key": "production-plan-agent-run",
                            },
                        )
                    assert response.status_code == 201
                    run_id = RunId(UUID(response.json()["run_id"]))
                    await asyncio.wait_for(plan_result_ready.wait(), timeout=45)
                    assert not stage_error_types, (
                        "production PLAN stage failed with exception types: "
                        f"{stage_error_types}"
                    )
                    assert entered_after_plan.is_set(), (
                        "production PLAN returned without reaching its acceptance pause"
                    )
                    assert len(requests) == 1
                    assert len(actors) == 1
                    run_events = await actors[0].store.read_run_events(run_id, 0)
                    assert [event.event_type for event in run_events][-3:] == [
                        "agent_run.started",
                        "agent_run.terminal",
                        "run.plan.accepted",
                    ]
                    records = await actors[0].store.list_agent_runs_by_owner("run", run_id)
                    assert len(records) == 1
                    assert records[0].state.value == "CLOSED"
                    assert records[0].checkpoint_sha256 is not None
                    assert (
                        await actors[0].store.load(run_id)
                    ).state.frozen_plan_sha256 is not None
                    assert await actors[0].store.get_cas_reference(
                        "run", run_id, "frozen-plan"
                    ) is not None
                except Exception as error:
                    test_failure = error
        finally:
            release_plan.set()
        if test_failure is not None:
            raise test_failure


def test_production_run_owner_rejects_assembly_for_another_store(tmp_path: Path):
    from langgraph.checkpoint.memory import InMemorySaver

    from codemigrator.asgi import ProductionRunComponents, create_production_run_owner
    from codemigrator.runtime.graph_composition import (
        AgentGraphInfrastructure,
        RuntimeGraphAssembly,
        RuntimeGraphConfigurationError,
    )
    from codemigrator.runtime.provider import ProviderRegistry
    from codemigrator.runtime.store import InMemoryRuntimeStore

    application_store = InMemoryRuntimeStore()
    assembly_store = InMemoryRuntimeStore()
    assembly = RuntimeGraphAssembly(
        AgentGraphInfrastructure(
            provider_registry=ProviderRegistry({}),
            context_manager=object(),
            tool_gateway=object(),
            runtime_store=assembly_store,
            host_cas=object(),
            cas_references=assembly_store,
            usage_sink=object(),
            run_checkpointer=InMemorySaver(),
            draft_graph_checkpointer=InMemorySaver(),
            agent_run_checkpointer=InMemorySaver(),
        ),
        plan_stage_factory=lambda _infra, _actor: object(),
        verifier_factory=lambda _infra, _actor: object(),
        reporter_factory=lambda _infra, _actor: object(),
        draft_agent_runner_factory=lambda _infra, _owner: object(),
        create_run_service_factory=lambda _infra, _owner: object(),
    )
    components = ProductionRunComponents(
        preflight=PassingPreflight(),
        graph_assembly=assembly,
        actor_factory=RunActor,
        durable_checkpointer=True,
    )

    with pytest.raises(RuntimeGraphConfigurationError, match="application RuntimeStore"):
        create_production_run_owner(application_store, components)


@pytest.mark.asyncio
async def test_production_root_rejects_checkpointer_bound_to_unfenced_postgres_store(
    tmp_path: Path,
):
    dsn = os.environ.get("CODEMIGRATOR_TEST_PG_DSN")
    if not dsn:
        pytest.skip("CODEMIGRATOR_TEST_PG_DSN is not configured")
    async with isolated_store() as (_store, schema):
        from codemigrator.runtime.cas import FileHostCAS
        from codemigrator.runtime.checkpointer import CasCheckpointSaver
        from codemigrator.runtime.graph_composition import (
            AgentGraphInfrastructure,
            RuntimeGraphAssembly,
        )
        from codemigrator.runtime.memory import ContextManager
        from codemigrator.runtime.provider import ProviderRegistry

        def components_factory(store, pool, lock_connection):
            assert isinstance(store, PostgreSQLRuntimeStore)
            assert store.pool is pool
            assert store._write_connection is lock_connection
            unfenced_store = PostgreSQLRuntimeStore(pool)
            app_owner_id = uuid4()
            cas = FileHostCAS(tmp_path / "cas")
            infrastructure = AgentGraphInfrastructure(
                provider_registry=ProviderRegistry({}),
                context_manager=ContextManager(),
                tool_gateway=object(),
                runtime_store=store,
                host_cas=cas,
                cas_references=store,
                usage_sink=object(),
                run_checkpointer=CasCheckpointSaver(
                    cas,
                    unfenced_store,
                    graph_family="run",
                    owner_kind="run",
                    owner_id=app_owner_id,
                ),
                draft_graph_checkpointer=CasCheckpointSaver(
                    cas,
                    store,
                    graph_family="draft",
                    owner_kind="draft",
                    owner_id=app_owner_id,
                ),
                agent_run_checkpointer=CasCheckpointSaver(
                    cas,
                    store,
                    graph_family="agent",
                    owner_kind="run",
                    owner_id=app_owner_id,
                ),
            )
            assembly = RuntimeGraphAssembly(
                infrastructure,
                plan_stage_factory=lambda _infra, _actor: object(),
                verifier_factory=lambda _infra, _actor: object(),
                reporter_factory=lambda _infra, _actor: object(),
                draft_agent_runner_factory=lambda _infra, _owner: object(),
                create_run_service_factory=lambda _infra, _owner: object(),
            )
            from codemigrator.asgi import ProductionRunComponents

            return ProductionRunComponents(
                preflight=PassingPreflight(),
                graph_assembly=assembly,
                actor_factory=RunActor,
                durable_checkpointer=True,
            )

        app = create_production_app(
            dsn,
            config=ApiConfig(token="synthetic-token"),
            run_components_factory=components_factory,
            stop_server=noop_server_stop,
            pool_server_settings={"search_path": schema},
        )
        with pytest.raises(RuntimeError, match="production API startup failed"):
            async with app.router.lifespan_context(app):
                pytest.fail("production application accepted an unfenced checkpointer")


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
async def test_lock_loss_drains_inflight_create_before_backend_and_server_shutdown():
    dsn = os.environ.get("CODEMIGRATOR_TEST_PG_DSN")
    if not dsn:
        pytest.skip("CODEMIGRATOR_TEST_PG_DSN is not configured")
    async with isolated_store() as (_store, schema):
        stop_called = asyncio.Event()
        owner_shutdown_called = asyncio.Event()
        preflight = BlockingPreflight()

        async def stop_server() -> None:
            stop_called.set()

        async def shutdown_owner() -> None:
            owner_shutdown_called.set()

        app = create_production_app(
            dsn,
            config=ApiConfig(token="synthetic-token"),
            preflight=preflight,
            graph_starter=RecordingGraphStarter(),
            shutdown=shutdown_owner,
            stop_server=stop_server,
            pool_server_settings={"search_path": schema},
        )
        async with app.router.lifespan_context(app):
            pool = app.state.runtime_pool
            assert pool is not None
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
            ) as client:
                in_flight = asyncio.create_task(
                    client.post(
                        "/api/v1/migrations",
                        json=create_run_payload(),
                        headers={
                            "Authorization": "Bearer synthetic-token",
                            "Idempotency-Key": "in-flight-before-lock-loss",
                        },
                    )
                )
                await asyncio.wait_for(preflight.entered.wait(), timeout=2)

                app.state.runtime_lock.connection.terminate()
                await asyncio.wait_for(app.state.runtime_lock_loss_event.wait(), timeout=2)
                try:
                    completed = await asyncio.wait_for(in_flight, timeout=2)
                finally:
                    preflight.release.set()
                    if not in_flight.done():
                        in_flight.cancel()
                        await asyncio.gather(in_flight, return_exceptions=True)
                assert completed.status_code == 503
                assert preflight.calls == []
                assert owner_shutdown_called.is_set()
                await asyncio.wait_for(owner_shutdown_called.wait(), timeout=2)
                await asyncio.wait_for(stop_called.wait(), timeout=2)

            lock_loss_task = app.state.lock_loss_task
            assert lock_loss_task is not None
            await asyncio.wait_for(lock_loss_task, timeout=2)
            async with pool.acquire() as connection:
                assert await connection.fetchval("SELECT count(*) FROM runtime_runs") == 0
                assert await connection.fetchval("SELECT count(*) FROM runtime_events") == 0
                assert await connection.fetchval("SELECT count(*) FROM api_command_receipts") == 0
                assert (
                    await connection.fetchval("SELECT count(*) FROM run_graph_start_handoffs")
                    == 0
                )


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

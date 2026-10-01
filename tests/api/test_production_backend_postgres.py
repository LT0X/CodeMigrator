"""Route-level PostgreSQL coverage for the concrete API backend."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest

from codemigrator.api import ApiConfig, create_app
from codemigrator.api.backend import DraftCommandResult, ProductionApiBackend
from codemigrator.api.deps import ApiRequest
from codemigrator.api.sse import sse_events
from codemigrator.asgi import create_production_app
from codemigrator.core import (
    CreateRun,
    FailureReason,
    MigrationSessionStatus,
    RunId,
    StableErrorCode,
    canonical_json_bytes,
)
from codemigrator.core.models.plan import PlanProposal
from codemigrator.runtime.actor import RunActor
from codemigrator.runtime.contracts import (
    DraftSessionEventSpec,
    RunCreatedReceipt,
    RuntimeStoreTransaction,
)
from codemigrator.runtime.create_run import (
    CreateRunRejected,
    RunCreationOwner,
    RunWorkflowGraphStarter,
)
from codemigrator.runtime.store import (
    InMemoryRuntimeStore,
    PostgreSQLRuntimeStore,
    RuntimeStore,
)

from .conftest import build_frozen_plan, build_plan_agent_inputs, create_run_payload


def _safe_provider_shape_for_diagnostics(
    request,
    response,
    *,
    content_is_plan_proposal: bool,
    tool_arguments_are_plan_proposal: bool = False,
):
    requested_names = frozenset(tool.name for tool in request.tools)
    response_names = tuple(call.name for call in response.tool_calls)
    finish_reason = response.finish_reason
    truncated_reasons = {"length", "max_tokens", "model_context_window_exceeded"}
    return {
        "tool_choice_required": request.tool_choice == "any",
        "request_tool_count": min(len(requested_names), 64),
        "response_tool_count": min(len(response_names), 64),
        "response_tools_match_request": all(
            isinstance(name, str) and len(name) <= 64 and name in requested_names
            for name in response_names
        ),
        "response_truncated": isinstance(finish_reason, str)
        and len(finish_reason) <= 64
        and finish_reason in truncated_reasons,
        "response_content_length": min(len(response.content), 1_000_000)
        if isinstance(response.content, str)
        else 0,
        "content_is_plan_proposal": bool(content_is_plan_proposal),
        "tool_arguments_are_plan_proposal": bool(tool_arguments_are_plan_proposal),
    }


def _safe_plan_proposal_argument_shape(raw_arguments: object) -> dict[str, object]:
    """Summarize structured output validation without retaining provider data."""

    from codemigrator.runtime.plan_agent_output import parse_plan_proposal_agent_output

    fields = frozenset({"slices", "edges", "integration_ranks", "planner_rationale"})
    if not isinstance(raw_arguments, str) or len(raw_arguments) > 1_000_000:
        return {"root_kind": "unavailable", "known_field_count": 0, "missing_field_count": 4}
    try:
        value = json.loads(raw_arguments)
    except (TypeError, ValueError):
        return {"root_kind": "invalid_json", "known_field_count": 0, "missing_field_count": 4}
    if not isinstance(value, dict):
        return {"root_kind": "non_object", "known_field_count": 0, "missing_field_count": 4}
    value = parse_plan_proposal_agent_output(value) or value

    keys = set(value)
    result: dict[str, object] = {
        "root_kind": "object",
        "known_field_count": min(len(keys & fields), 4),
        "missing_field_count": min(len(fields - keys), 4),
        "unknown_field_count": min(len(keys - fields), 64),
    }
    categories = {
        "missing": "missing",
        "extra_forbidden": "extra",
        "enum": "enum",
        "literal_error": "literal",
        "value_error": "value",
        "string_type": "type",
        "int_type": "type",
        "list_type": "type",
        "dict_type": "type",
    }
    schema_fields: set[str] = set()

    def visit_schema(node: object) -> None:
        if not isinstance(node, dict):
            return
        properties = node.get("properties")
        if isinstance(properties, dict):
            schema_fields.update(str(key) for key in properties)
            for child in properties.values():
                visit_schema(child)
        items = node.get("items")
        visit_schema(items)
        definitions = node.get("$defs")
        if isinstance(definitions, dict):
            for child in definitions.values():
                visit_schema(child)

    visit_schema(PlanProposal.model_json_schema())
    try:
        PlanProposal.model_validate(value)
    except Exception as error:
        errors = (
            error.errors(include_input=False, include_url=False)
            if hasattr(error, "errors")
            else ()
        )
        counts: dict[str, int] = {}
        safe_paths = []
        for item in errors:
            category = categories.get(str(item.get("type")), "other")
            counts[category] = min(counts.get(category, 0) + 1, 64)
            path = []
            for part in item.get("loc", ()):
                if isinstance(part, int):
                    path.append(min(max(part, 0), 1_000_000))
                elif isinstance(part, str):
                    path.append(part if part in schema_fields else "<field>")
            safe_paths.append(tuple(path[:12]))
        result["valid"] = False
        result["validation_category_counts"] = counts
        result["validation_exception_kind"] = (
            type(error).__name__
            if type(error).__name__ in {"ValidationError", "ValueError", "TypeError"}
            else "other"
        )
        result["validation_paths"] = tuple(safe_paths[:16])
    else:
        result["valid"] = True
        result["validation_category_counts"] = {}
    return result


def test_provider_shape_diagnostics_do_not_echo_untrusted_response_fields() -> None:
    request = SimpleNamespace(
        tool_choice="private request choice",
        tools=(SimpleNamespace(name="ReadFile"),),
    )
    response = SimpleNamespace(
        finish_reason="private finish reason marker",
        tool_calls=(SimpleNamespace(name="private tool name marker"),),
        content="private response body marker",
    )

    shape = _safe_provider_shape_for_diagnostics(request, response, content_is_plan_proposal=False)

    assert shape["response_tools_match_request"] is False
    assert shape["response_truncated"] is False
    assert shape["response_content_length"] == len(response.content)
    assert "private" not in repr(shape)


def test_plan_argument_shape_diagnostics_only_report_bounded_schema_metadata() -> None:
    shape = _safe_plan_proposal_argument_shape(
        json.dumps(
            {
                "slices": [],
                "edges": [],
                "integration_ranks": {},
                "planner_rationale": [],
                "private": "response body marker",
            }
        )
    )

    assert shape["root_kind"] == "object"
    assert shape["known_field_count"] == 4
    assert shape["missing_field_count"] == 0
    assert shape["unknown_field_count"] == 1
    assert "response body marker" not in repr(shape)
    assert "private" not in repr(shape)


def test_draft_command_result_requires_a_receipt_for_the_same_session() -> None:
    with pytest.raises(ValueError, match="identify its session owner"):
        DraftCommandResult(
            session_id=uuid4(),
            status=MigrationSessionStatus.Drafting,
            revision=0,
            owner_receipt=SimpleNamespace(draft_id=uuid4(), receipt_key="draft.receipt"),
        )


def test_draft_command_result_rejects_status_outside_m00_contract() -> None:
    session_id = uuid4()
    with pytest.raises(ValueError, match="valid MigrationSessionStatus"):
        DraftCommandResult(
            session_id=session_id,
            status="DRAFT",  # type: ignore[arg-type]
            revision=0,
            owner_receipt=SimpleNamespace(draft_id=session_id, receipt_key="draft.receipt"),
        )


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


@pytest.mark.asyncio
async def test_postgres_run_state_pages_and_state_event_snapshot_are_stable():
    from codemigrator.core import RunStatus
    from codemigrator.runtime.contracts import EventSpec, RunState

    async with isolated_store() as (store, _schema):
        run_ids = tuple(UUID(int=value) for value in range(601, 606))
        for run_id in reversed(run_ids):
            await store.create(
                RunState(run_id=RunId(run_id), status=RunStatus.Created),
                (EventSpec("run.status_changed", {"run_status": RunStatus.Created.value}),),
            )

        first = await store.list_run_states(limit=2)
        second = await store.list_run_states(limit=2, after_run_id=first.next_cursor)
        last = await store.list_run_states(limit=2, after_run_id=second.next_cursor)
        snapshot = await store.load(RunId(run_ids[0]))

        assert tuple(state.run_id for state in first.states) == run_ids[:2]
        assert first.next_cursor == run_ids[1]
        assert tuple(state.run_id for state in second.states) == run_ids[2:4]
        assert second.next_cursor == run_ids[3]
        assert tuple(state.run_id for state in last.states) == run_ids[4:]
        assert last.next_cursor is None
        assert snapshot is not None
        assert snapshot.state.run_id == run_ids[0]
        assert [event.sequence for event in snapshot.events] == [1]
        assert snapshot.events[0].data["run_status"] == RunStatus.Created.value


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


class RecordingDraftSessionCommands:
    def __init__(self, store: RuntimeStore) -> None:
        self.store = store
        self.draft_id = uuid4()
        self.calls: list[tuple[str, RuntimeStoreTransaction]] = []
        self.connection_ids: list[int] = []

    async def _commit(
        self,
        operation: str,
        draft_id: UUID,
        revision: int,
        transaction: RuntimeStoreTransaction,
    ) -> SimpleNamespace:
        assert transaction.store is self.store
        assert transaction.active
        if transaction.connection is not None:
            self.connection_ids.append(id(transaction.connection))
            assert await transaction.connection.fetchval("SELECT 1") == 1
        self.calls.append((operation, transaction))
        receipt = await self.store.commit_draft_owner_fact(
            draft_id,
            f"draft.api-command:{len(self.calls)}",
            "draft.api-command",
            {"operation": operation, "revision": revision},
            transaction=transaction,
        )
        return DraftCommandResult(
            session_id=draft_id,
            status=MigrationSessionStatus.Drafting,
            revision=revision,
            owner_receipt=receipt,
        )

    async def create_session(self, payload, transaction):  # type: ignore[no-untyped-def]
        del payload
        return await self._commit("create_session", self.draft_id, 0, transaction)

    async def send_message(self, session_id, payload, transaction):  # type: ignore[no-untyped-def]
        del payload
        return await self._commit("session_message", session_id, 1, transaction)

    async def answer_question(self, session_id, payload, transaction):  # type: ignore[no-untyped-def]
        del payload
        return await self._commit("session_answer", session_id, 2, transaction)

    async def confirm_session(self, session_id, payload, transaction):  # type: ignore[no-untyped-def]
        del payload
        return await self._commit("session_confirm", session_id, 3, transaction)


class RecordingDraftGraphStarter:
    supported_receipt_categories = frozenset({"draft.api-command"})

    def __init__(self) -> None:
        self.receipts: list[tuple[UUID, str]] = []

    async def start_graph(self, draft_id: UUID, receipt_key: str) -> None:
        self.receipts.append((draft_id, receipt_key))


class FailingDraftSessionCommands(RecordingDraftSessionCommands):
    async def create_session(self, payload, transaction):  # type: ignore[no-untyped-def]
        await super().create_session(payload, transaction)
        raise RuntimeError("synthetic Draft command failure")


class InvalidDraftResultCommands(RecordingDraftSessionCommands):
    async def create_session(self, payload, transaction):  # type: ignore[no-untyped-def]
        await super().create_session(payload, transaction)
        return SimpleNamespace(
            session_id=self.draft_id,
            status=MigrationSessionStatus.Drafting,
            revision=0,
        )


class BlockingDraftSessionCommands(RecordingDraftSessionCommands):
    def __init__(self, store: RuntimeStore) -> None:
        super().__init__(store)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def create_session(self, payload, transaction):  # type: ignore[no-untyped-def]
        result = await super().create_session(payload, transaction)
        self.entered.set()
        await self.release.wait()
        return result


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
        assert await store.list_pending_graph_starts() == ((run_id, f"run.created:{run_id}"),)

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
        assert [event.event_type for event in await store.read_run_events(run_id, 0)] == [
            "run.created",
            "run.status_changed",
        ]

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
            run_owner=RunCreationOwner(store=store, preflight=preflight, graph_starter=graph),
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
        request = ApiRequest(operation="create_run", principal_id="local", payload=payload)
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
                    receipt=AgentRunReceipt(uuid4(), created.agent_run_id, "plan.accepted"),
                    result_object=result_object,
                    plan_object=plan_object,
                ),
                frozen_plan,
            )

    class SyntheticExecutionScheduler:
        def __init__(self, effects) -> None:  # type: ignore[no-untyped-def]
            self.effects = effects

        async def advance_one_round(
            self,
            run_id,
            logical_key,
            *,
            on_agent_run_started,
            on_agent_run_terminal,
        ):  # type: ignore[no-untyped-def]
            del run_id, logical_key, on_agent_run_started, on_agent_run_terminal
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

        def __init__(self, store, cas_root: Path, effects, *, block_report: bool = False) -> None:  # type: ignore[no-untyped-def]
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
            self._delegate = RunWorkflowGraphStarter(self._graph_factory, durable_checkpointer=True)

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
                verifier=CountedStage("VERIFY", VerificationSummary(True, "e" * 64), self.effects),
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
            run_owner=RunCreationOwner(store=store, preflight=preflight, graph_starter=starter1),
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
        owner2 = RunCreationOwner(store=store2, preflight=preflight, graph_starter=starter2)
        backend2 = ProductionApiBackend(store2, run_owner=owner2)
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
            run_owner=RunCreationOwner(store=store3, preflight=preflight, graph_starter=starter3),
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
async def test_draft_api_commands_delegate_through_the_api_transaction_without_run_handoff():
    store = InMemoryRuntimeStore()
    draft_owner = RecordingDraftSessionCommands(store)
    backend = ProductionApiBackend(store, draft_owner=draft_owner)
    app = create_app(backend, config=ApiConfig(token="synthetic-token"))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        auth = {"Authorization": "Bearer synthetic-token"}
        headers = {**auth, "Idempotency-Key": "draft-api-create"}
        created = await client.post(
            "/api/v1/sessions",
            json={"kind": "DRAFT", "payload": {}},
            headers=headers,
        )
        replay = await client.post(
            "/api/v1/sessions",
            json={"kind": "DRAFT", "payload": {}},
            headers=headers,
        )
        conflict = await client.post(
            "/api/v1/sessions",
            json={"kind": "OTHER", "payload": {}},
            headers=headers,
        )
        session_id = str(draft_owner.draft_id)
        message = await client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={"message": "add context", "revision": 0},
            headers={**auth, "Idempotency-Key": "draft-api-message"},
        )
        answer = await client.post(
            f"/api/v1/sessions/{session_id}/answers",
            json={"question_id": str(uuid4()), "answer": "yes", "revision": 1},
            headers={**auth, "Idempotency-Key": "draft-api-answer"},
        )
        confirmed = await client.post(
            f"/api/v1/sessions/{session_id}/confirm",
            json={"revision": 2},
            headers={**auth, "Idempotency-Key": "draft-api-confirm"},
        )

    assert created.status_code == 201
    assert replay.status_code == 201
    assert replay.json() == created.json()
    assert conflict.status_code == 409
    assert conflict.json()["type"].endswith("/idempotency_conflict")
    for response, revision in ((created, 0), (message, 1), (answer, 2), (confirmed, 3)):
        assert response.status_code in {200, 201}
        assert set(response.json()) == {"session_id", "status", "revision"}
        assert response.json() == {
            "session_id": session_id,
            "status": MigrationSessionStatus.Drafting.value,
            "revision": revision,
        }
    assert [operation for operation, _transaction in draft_owner.calls] == [
        "create_session",
        "session_message",
        "session_answer",
        "session_confirm",
    ]
    assert all(transaction.store is store for _, transaction in draft_owner.calls)
    assert len(await store.list_draft_owner_facts(draft_owner.draft_id)) == 4
    assert await store.list_pending_graph_starts() == ()


@pytest.mark.asyncio
async def test_draft_api_write_commands_fail_closed_without_a_draft_owner():
    store = InMemoryRuntimeStore()
    backend = ProductionApiBackend(store)
    app = create_app(backend, config=ApiConfig(token="synthetic-token"))
    session_id = uuid4()
    headers = {"Authorization": "Bearer synthetic-token"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        responses = (
            await client.post(
                "/api/v1/sessions",
                json={"kind": "DRAFT", "payload": {}},
                headers={**headers, "Idempotency-Key": "missing-draft-create"},
            ),
            await client.post(
                f"/api/v1/sessions/{session_id}/messages",
                json={"message": "add context", "revision": 0},
                headers={**headers, "Idempotency-Key": "missing-draft-message"},
            ),
            await client.post(
                f"/api/v1/sessions/{session_id}/answers",
                json={"question_id": str(uuid4()), "answer": "yes", "revision": 0},
                headers={**headers, "Idempotency-Key": "missing-draft-answer"},
            ),
            await client.post(
                f"/api/v1/sessions/{session_id}/confirm",
                json={"revision": 0},
                headers={**headers, "Idempotency-Key": "missing-draft-confirm"},
            ),
        )

    assert all(response.status_code == 503 for response in responses)
    assert all(
        response.json()["type"].endswith("/dependency_unavailable") for response in responses
    )
    assert store._api_commands == {}
    assert await store.list_draft_owner_facts(session_id) == ()
    assert await store.list_pending_graph_starts() == ()


@pytest.mark.asyncio
async def test_failed_draft_owner_command_rolls_back_fact_and_api_receipt():
    store = InMemoryRuntimeStore()
    draft_owner = FailingDraftSessionCommands(store)
    backend = ProductionApiBackend(store, draft_owner=draft_owner)
    app = create_app(backend, config=ApiConfig(token="synthetic-token"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        response = await client.post(
            "/api/v1/sessions",
            json={"kind": "DRAFT", "payload": {}},
            headers={
                "Authorization": "Bearer synthetic-token",
                "Idempotency-Key": "failed-draft-command",
            },
        )

    assert response.status_code == 503
    assert response.json()["type"].endswith("/dependency_unavailable")
    assert await store.list_draft_owner_facts(draft_owner.draft_id) == ()
    assert store._api_commands == {}
    assert await store.list_pending_graph_starts() == ()
    await backend.close()


@pytest.mark.asyncio
async def test_draft_command_result_without_owner_receipt_is_rejected_and_rolled_back():
    store = InMemoryRuntimeStore()
    draft_owner = InvalidDraftResultCommands(store)
    backend = ProductionApiBackend(store, draft_owner=draft_owner)
    app = create_app(backend, config=ApiConfig(token="synthetic-token"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        response = await client.post(
            "/api/v1/sessions",
            json={"kind": "DRAFT", "payload": {}},
            headers={
                "Authorization": "Bearer synthetic-token",
                "Idempotency-Key": "invalid-draft-result",
            },
        )

    assert response.status_code == 503
    assert response.json()["type"].endswith("/dependency_unavailable")
    assert await store.list_draft_owner_facts(draft_owner.draft_id) == ()
    assert store._api_commands == {}
    await backend.close()


@pytest.mark.asyncio
async def test_draft_command_cancellation_rolls_back_owner_fact_and_api_receipt():
    store = InMemoryRuntimeStore()
    draft_owner = BlockingDraftSessionCommands(store)
    backend = ProductionApiBackend(store, draft_owner=draft_owner)
    app = create_app(backend, config=ApiConfig(token="synthetic-token"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        request = asyncio.create_task(
            client.post(
                "/api/v1/sessions",
                json={"kind": "DRAFT", "payload": {}},
                headers={
                    "Authorization": "Bearer synthetic-token",
                    "Idempotency-Key": "cancelled-draft-command",
                },
            )
        )
        await asyncio.wait_for(draft_owner.entered.wait(), timeout=2)
        backend.close_admission()
        response = await asyncio.wait_for(request, timeout=2)

    assert response.status_code == 503
    assert response.json()["type"].endswith("/dependency_unavailable")
    assert await store.list_draft_owner_facts(draft_owner.draft_id) == ()
    assert store._api_commands == {}
    assert await store.list_pending_graph_starts() == ()
    await backend.close()


@pytest.mark.asyncio
async def test_production_asgi_injects_draft_owner_for_all_four_atomic_commands():
    dsn = os.environ.get("CODEMIGRATOR_TEST_PG_DSN")
    if not dsn:
        pytest.skip("CODEMIGRATOR_TEST_PG_DSN is not configured")
    async with isolated_store(max_size=1) as (inspection_store, schema):
        owners: list[RecordingDraftSessionCommands] = []
        graph_starters: list[RecordingDraftGraphStarter] = []

        def draft_owner_factory(store, resources):  # type: ignore[no-untyped-def]
            assert store._write_connection is resources.write_connection
            owner = RecordingDraftSessionCommands(store)
            owners.append(owner)
            return owner

        def draft_graph_starter_factory(store, resources, owner, assembly):  # type: ignore[no-untyped-def]
            assert owner is owners[0]
            assert assembly is None
            starter = RecordingDraftGraphStarter()
            graph_starters.append(starter)
            return starter

        app = create_production_app(
            dsn,
            config=ApiConfig(token="synthetic-token"),
            draft_command_owner_factory=draft_owner_factory,
            draft_graph_starter_factory=draft_graph_starter_factory,
            stop_server=noop_server_stop,
            pool_server_settings={"search_path": schema},
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
            ) as client:
                auth = {"Authorization": "Bearer synthetic-token"}
                create_headers = {**auth, "Idempotency-Key": "root-draft-create"}
                created = await client.post(
                    "/api/v1/sessions",
                    json={"kind": "DRAFT", "payload": {}},
                    headers=create_headers,
                )
                replay = await client.post(
                    "/api/v1/sessions",
                    json={"kind": "DRAFT", "payload": {}},
                    headers=create_headers,
                )
                conflict = await client.post(
                    "/api/v1/sessions",
                    json={"kind": "OTHER", "payload": {}},
                    headers=create_headers,
                )
                session_id = created.json()["session_id"]
                message = await client.post(
                    f"/api/v1/sessions/{session_id}/messages",
                    json={"message": "add context", "revision": 0},
                    headers={**auth, "Idempotency-Key": "root-draft-message"},
                )
                answer = await client.post(
                    f"/api/v1/sessions/{session_id}/answers",
                    json={"question_id": str(uuid4()), "answer": "yes", "revision": 1},
                    headers={**auth, "Idempotency-Key": "root-draft-answer"},
                )
                confirmed = await client.post(
                    f"/api/v1/sessions/{session_id}/confirm",
                    json={"revision": 2},
                    headers={**auth, "Idempotency-Key": "root-draft-confirm"},
                )

        assert len(owners) == 1
        assert len(graph_starters) == 1
        owner = owners[0]
        assert [operation for operation, _transaction in owner.calls] == [
            "create_session",
            "session_message",
            "session_answer",
            "session_confirm",
        ]
        assert len(owner.connection_ids) == 4
        assert len(set(owner.connection_ids)) == 1
        assert [created.status_code, replay.status_code, conflict.status_code] == [201, 201, 409]
        assert replay.json() == created.json()
        assert conflict.json()["type"].endswith("/idempotency_conflict")
        for response, revision in ((created, 0), (message, 1), (answer, 2), (confirmed, 3)):
            assert response.status_code in {200, 201}
            assert set(response.json()) == {"session_id", "status", "revision"}
            assert response.json() == {
                "session_id": session_id,
                "status": MigrationSessionStatus.Drafting.value,
                "revision": revision,
            }
        async with inspection_store.pool.acquire() as connection:
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM draft_owner_facts WHERE draft_id=$1", owner.draft_id
                )
                == 4
            )
            assert (
                await connection.fetchval(
                    """SELECT count(*) FROM api_command_receipts
                    WHERE owner_kind='draft' AND owner_id=$1""",
                    owner.draft_id,
                )
                == 4
            )
            assert await connection.fetchval("SELECT count(*) FROM draft_graph_start_handoffs") == 4
            assert await connection.fetchval("SELECT count(*) FROM run_graph_start_handoffs") == 0


@pytest.mark.asyncio
async def test_production_asgi_hides_draft_owner_without_graph_starter():
    dsn = os.environ.get("CODEMIGRATOR_TEST_PG_DSN")
    if not dsn:
        pytest.skip("CODEMIGRATOR_TEST_PG_DSN is not configured")
    async with isolated_store(max_size=1) as (inspection_store, schema):
        owners: list[RecordingDraftSessionCommands] = []

        def draft_owner_factory(store, resources):  # type: ignore[no-untyped-def]
            owner = RecordingDraftSessionCommands(store)
            owners.append(owner)
            return owner

        app = create_production_app(
            dsn,
            config=ApiConfig(token="synthetic-token"),
            draft_command_owner_factory=draft_owner_factory,
            stop_server=noop_server_stop,
            pool_server_settings={"search_path": schema},
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
            ) as client:
                response = await client.post(
                    "/api/v1/sessions",
                    json={"kind": "DRAFT", "payload": {}},
                    headers={
                        "Authorization": "Bearer synthetic-token",
                        "Idempotency-Key": "draft-needs-graph-starter",
                    },
                )

        assert response.status_code == 503
        assert response.json()["type"].endswith("/dependency_unavailable")
        assert len(owners) == 1
        assert owners[0].calls == []
        async with inspection_store.pool.acquire() as connection:
            assert await connection.fetchval("SELECT count(*) FROM draft_owner_facts") == 0
            assert await connection.fetchval("SELECT count(*) FROM api_command_receipts") == 0


@pytest.mark.asyncio
async def test_draft_owner_api_receipt_commits_and_replays_without_run_handoff():
    async with isolated_store() as (store, _schema):
        draft_id = uuid4()
        event = DraftSessionEventSpec("session.question.asked", {"question_id": str(uuid4())})

        async def command(transaction: RuntimeStoreTransaction):
            return await store.commit_draft_owner_fact(
                draft_id,
                "draft.created",
                "draft.created",
                {"revision": 0},
                events=(event,),
                transaction=transaction,
            )

        def project(receipt):
            return {"session_id": str(receipt.draft_id), "revision": 0}

        def owner(receipt):
            return ("draft", receipt.draft_id, receipt.receipt_key)

        first = await store.execute_api_command(
            principal_id="local",
            route="/api/v1/sessions",
            key="create-draft-atomic",
            canonical_body=b'{"kind":"DRAFT"}',
            status_code=201,
            command=command,
            project_response=project,
            owner_receipt=owner,
        )
        replay = await store.execute_api_command(
            principal_id="local",
            route="/api/v1/sessions",
            key="create-draft-atomic",
            canonical_body=b'{"kind":"DRAFT"}',
            status_code=201,
            command=lambda _transaction: pytest.fail("replayed Draft command ran twice"),
            project_response=project,
            owner_receipt=owner,
        )
        conflict = await store.execute_api_command(
            principal_id="local",
            route="/api/v1/sessions",
            key="create-draft-atomic",
            canonical_body=b'{"kind":"OTHER"}',
            status_code=201,
            command=lambda _transaction: pytest.fail("conflicting Draft command ran"),
            project_response=project,
            owner_receipt=owner,
        )

        assert first["replayed"] is False
        assert replay["replayed"] is True
        assert replay["response"] == {"session_id": str(draft_id), "revision": 0}
        assert conflict == {"conflict": True, "replayed": False}
        async with store.pool.acquire() as connection:
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM draft_owner_facts WHERE draft_id=$1", draft_id
                )
                == 1
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM draft_session_events WHERE draft_id=$1", draft_id
                )
                == 1
            )
            assert (
                await connection.fetchval(
                    """SELECT count(*) FROM api_command_receipts
                WHERE route='/api/v1/sessions' AND idempotency_key='create-draft-atomic'
                AND owner_kind='draft' AND owner_id=$1""",
                    draft_id,
                )
                == 1
            )
            assert await connection.fetchval("SELECT count(*) FROM run_graph_start_handoffs") == 0

            with pytest.raises(asyncpg.CheckViolationError):
                async with connection.transaction():
                    await connection.execute(
                        """INSERT INTO api_command_receipts(
                            principal_id, route, idempotency_key, body_sha256,
                            status_code, response_body, owner_kind, owner_id,
                            owner_receipt_key, expires_at
                        ) VALUES ('local', '/api/v1/sessions', 'invalid-null-owner-kind',
                            $1, 201, '{}'::jsonb, NULL, $2, 'draft.created',
                            now() + interval '1 day')""",
                        "a" * 64,
                        draft_id,
                    )


@pytest.mark.asyncio
async def test_draft_owner_api_receipt_rolls_back_fact_and_event_after_projection_error():
    async with isolated_store() as (store, _schema):
        draft_id = uuid4()
        event = DraftSessionEventSpec("session.question.asked", {"question_id": str(uuid4())})

        async def command(transaction: RuntimeStoreTransaction):
            return await store.commit_draft_owner_fact(
                draft_id,
                "draft.created",
                "draft.created",
                {"revision": 0},
                events=(event,),
                transaction=transaction,
            )

        with pytest.raises(RuntimeError, match="projection failed"):
            await store.execute_api_command(
                principal_id="local",
                route="/api/v1/sessions",
                key="create-draft-rollback",
                canonical_body=b'{"kind":"DRAFT"}',
                status_code=201,
                command=command,
                project_response=lambda _receipt: (_ for _ in ()).throw(
                    RuntimeError("projection failed")
                ),
                owner_receipt=lambda receipt: (
                    "draft",
                    receipt.draft_id,
                    receipt.receipt_key,
                ),
            )

        async with store.pool.acquire() as connection:
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM draft_owner_facts WHERE draft_id=$1", draft_id
                )
                == 0
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM draft_session_events WHERE draft_id=$1", draft_id
                )
                == 0
            )
            assert (
                await connection.fetchval(
                    """SELECT count(*) FROM api_command_receipts
                    WHERE route='/api/v1/sessions' AND idempotency_key='create-draft-rollback'"""
                )
                == 0
            )


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
                    await connection.fetchval("SELECT count(*) FROM run_graph_start_handoffs") == 0
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
                    await connection.fetchval("SELECT count(*) FROM run_graph_start_handoffs") == 0
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
        assert [event.event_type for event in await store.read_run_events(run_id, 0)] == [
            "run.created",
            "run.status_changed",
        ]
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
            run_owner=RunCreationOwner(store=store, preflight=preflight, graph_starter=graph),
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
                plan_stage_factory=lambda _infra, received_actor: WaitingPlanStage(received_actor),
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
async def test_production_asgi_projects_committed_run_views_and_resumes_sse_from_snapshot(
    tmp_path: Path,
):
    dsn = os.environ.get("CODEMIGRATOR_TEST_PG_DSN")
    if not dsn:
        pytest.skip("CODEMIGRATOR_TEST_PG_DSN is not configured")
    from codemigrator.asgi import ProductionRunComponents
    from codemigrator.core import RunStatus, SliceAttemptStatus, SliceKind
    from codemigrator.runtime.cas import FileHostCAS
    from codemigrator.runtime.checkpointer import CasCheckpointSaver
    from codemigrator.runtime.contracts import EventSpec, RunState
    from codemigrator.runtime.graph_composition import (
        AgentGraphInfrastructure,
        RuntimeGraphAssembly,
    )
    from codemigrator.runtime.memory import ContextManager
    from codemigrator.runtime.provider import ProviderRegistry

    async with isolated_store() as (_test_store, schema):
        captured: dict[str, object] = {}

        def components_factory(store, pool, lock_connection):
            del pool, lock_connection
            cas = FileHostCAS(tmp_path / "cas")
            captured["store"] = store
            captured["cas"] = cas
            app_owner_id = uuid4()
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
            assembly = RuntimeGraphAssembly(
                infrastructure,
                plan_stage_factory=lambda _infra, _actor: object(),
                verifier_factory=lambda _infra, _actor: object(),
                reporter_factory=lambda _infra, _actor: object(),
                draft_agent_runner_factory=lambda _infra, _owner: object(),
                create_run_service_factory=lambda _infra, _owner: object(),
            )
            return ProductionRunComponents(
                preflight=PassingPreflight(),
                graph_assembly=assembly,
                actor_factory=lambda run_id, actor_store: RunActor(run_id, actor_store),
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
            store = captured["store"]
            cas = captured["cas"]
            assert isinstance(store, PostgreSQLRuntimeStore)
            assert isinstance(cas, FileHostCAS)
            run_id = uuid4()
            frozen_plan = build_frozen_plan()
            slice_id = frozen_plan.slices[0].id
            plan_ref = cas.put(frozen_plan.canonical_payload())
            await store.create(
                RunState(run_id=RunId(run_id), status=RunStatus.Planning),
                (EventSpec("run.status_changed", {"run_status": RunStatus.Planning.value}),),
            )
            await store.commit(
                RunState(
                    run_id=RunId(run_id),
                    status=RunStatus.Failed,
                    version=1,
                    frozen_plan_sha256=frozen_plan.plan_hash,
                ),
                (
                    EventSpec(
                        "slice.status_changed",
                        {
                            "slice_id": str(slice_id),
                            "status": SliceAttemptStatus.Integrated.value,
                            "generation": 3,
                        },
                    ),
                    EventSpec("run.status_changed", {"run_status": RunStatus.Failed.value}),
                ),
            )
            await store.add_cas_reference(plan_ref, "run", run_id, "frozen-plan")
            persisted_after_cursor = await store.read_run_events(RunId(run_id), 2)
            assert [event.sequence for event in persisted_after_cursor] == [3]
            assert await store.is_run_stream_terminal(RunId(run_id), 3)

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
            ) as client:
                headers = {"Authorization": "Bearer synthetic-token"}
                listing = await client.get("/api/v1/migrations?limit=10", headers=headers)
                detail = await client.get(f"/api/v1/migrations/{run_id}", headers=headers)
                workspace = await client.get(
                    f"/api/v1/migrations/{run_id}/workspace", headers=headers
                )
                replay = await client.get(
                    f"/api/v1/migrations/{run_id}/events",
                    headers={**headers, "Last-Event-ID": "2"},
                )

            assert listing.status_code == detail.status_code == workspace.status_code == 200
            assert listing.json()["items"][0]["run_id"] == str(run_id)
            assert detail.json()["status"] == RunStatus.Failed.value
            assert workspace.json()["latest_sequence"] == 3
            assert workspace.json()["slices"] == [
                {
                    "slice_id": str(slice_id),
                    "kind": SliceKind.Implementation.value,
                    "status": SliceAttemptStatus.Integrated.value,
                    "generation": 3,
                    "write_scope": {
                        "write_paths": ["target/a.py"],
                        "create_roots": ["target/a"],
                    },
                    "integration_rank": 0,
                },
                {
                    "slice_id": str(frozen_plan.slices[1].id),
                    "kind": SliceKind.Implementation.value,
                    "status": SliceAttemptStatus.Ready.value,
                    "generation": 0,
                    "write_scope": {
                        "write_paths": ["target/b.py"],
                        "create_roots": ["target/b"],
                    },
                    "integration_rank": 1,
                },
            ]
            assert replay.status_code == 200
            assert "id: 3" in replay.text
            assert '"run_status":"FAILED"' in replay.text


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
        ProviderError,
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
        provider_shapes = []
        stage_error_types = []

        def safe_failure_code(error):
            pending = [error]
            seen = set()
            while pending:
                current = pending.pop()
                if id(current) in seen:
                    continue
                seen.add(id(current))
                if isinstance(current, ProviderError):
                    return current.failure_code
                for cause in (current.__cause__, current.__context__):
                    if cause is not None:
                        pending.append(cause)
            return "unclassified"

        def safe_failure_frames(error):
            import traceback

            frames = []
            pending = [error]
            seen = set()
            while pending:
                current = pending.pop()
                if id(current) in seen:
                    continue
                seen.add(id(current))
                frames.extend(
                    (Path(frame.filename).name, frame.name, frame.lineno)
                    for frame in traceback.extract_tb(current.__traceback__)
                )
                for cause in (current.__cause__, current.__context__):
                    if cause is not None:
                        pending.append(cause)
            return tuple(frames[-12:])

        class StructuredProvider:
            async def complete(self, request):
                wire_proposal = proposal.model_dump(mode="json", by_alias=True)
                wire_proposal["integration_ranks"] = [
                    {"local_ref": local_ref, "rank": rank}
                    for local_ref, rank in proposal.integration_ranks.items()
                ]
                return ProviderResponse(
                    content="",
                    tool_calls=(
                        ProviderToolCall(
                            "PlanProposal",
                            json.dumps(wire_proposal),
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
                round_limit = 1 if real_opencode else 4
                if len(calls) >= min(max_rounds, round_limit):
                    return None
                round_index = len(calls) + 1
                calls[call_id] = round_index
                return round_index

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
                response = await self.delegate.complete(request)
                try:
                    content_value = json.loads(response.content)
                except Exception:
                    content_is_plan_proposal = False
                else:
                    content_is_plan_proposal = (
                        _safe_plan_proposal_argument_shape(
                            json.dumps(content_value, ensure_ascii=False)
                        ).get("valid")
                        is True
                    )
                matching_calls = [
                    call for call in response.tool_calls if call.name == "PlanProposal"
                ]
                plan_argument_shape = (
                    _safe_plan_proposal_argument_shape(matching_calls[0].arguments)
                    if len(matching_calls) == 1
                    else {"root_kind": "tool_call_count", "known_field_count": 0}
                )
                tool_arguments_are_plan_proposal = plan_argument_shape.get("valid") is True
                provider_shapes.append(
                    {
                        **_safe_provider_shape_for_diagnostics(
                            request,
                            response,
                            content_is_plan_proposal=content_is_plan_proposal,
                            tool_arguments_are_plan_proposal=tool_arguments_are_plan_proposal,
                        ),
                        "plan_content_shape": _safe_plan_proposal_argument_shape(
                            response.content
                        ),
                        "plan_argument_shape": plan_argument_shape,
                    }
                )
                return response

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
                        "Use no edges, rank A as 0 and B as 1. Do not call exploration tools; "
                        "return the required structured PlanProposal.",
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
                        phase_policy_sha256=load_resource("core://phase-tool-policy/v2").sha256,
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
                            stage_error_types.append(
                                (
                                    type(error).__name__,
                                    safe_failure_code(error),
                                    safe_failure_frames(error),
                                )
                            )
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
        request_body["frozen_artifacts"] = planning_inputs.frozen_artifacts.model_dump(mode="json")
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
                        await asyncio.wait_for(
                            plan_result_ready.wait(),
                            timeout=150 if real_opencode else 45,
                        )
                    assert not stage_error_types, (
                        "production PLAN stage failed with exception types: "
                        f"{stage_error_types}; provider_adapter_calls={len(requests)}; "
                        f"provider_shapes={provider_shapes}"
                    )
                    assert entered_after_plan.is_set(), (
                        "production PLAN returned without reaching its acceptance pause"
                    )
                    assert 1 <= len(requests) <= (1 if real_opencode else 4)
                    assert len(actors) == 1
                    run_events = await actors[0].store.read_run_events(run_id, 0)
                    assert [event.event_type for event in run_events][-4:] == [
                        "agent_run.started",
                        "agent_run.terminal",
                        "run.plan.accepted",
                        "run.status_changed",
                    ]
                    records = await actors[0].store.list_agent_runs_by_owner("run", run_id)
                    assert len(records) == 1
                    assert records[0].state.value == "CLOSED"
                    assert records[0].checkpoint_sha256 is not None
                    assert (await actors[0].store.load(run_id)).state.frozen_plan_sha256 is not None
                    assert (
                        await actors[0].store.get_cas_reference("run", run_id, "frozen-plan")
                        is not None
                    )
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
                    await connection.fetchval("SELECT count(*) FROM run_graph_start_handoffs") == 0
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
        assert [event.event_type for event in run_events] == [
            "run.created",
            "run.status_changed",
            "run.cancelled",
            "run.status_changed",
        ]
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
        assert [event.event_type for event in run_events] == [
            "run.created",
            "run.status_changed",
            "run.cancelled",
            "run.status_changed",
        ]
        await backend.close()

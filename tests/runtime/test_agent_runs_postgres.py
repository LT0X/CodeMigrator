"""AgentRun PostgreSQL contract tests against an isolated temporary schema."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from uuid import uuid4

import asyncpg
import pytest

from codemigrator.core import GitOid, Phase, RunStatus, SessionKind, SliceGenerationRef, SliceId
from codemigrator.runtime.actor import RunActor
from codemigrator.runtime.agent_runs import AgentRunId, AgentRunReceipt
from codemigrator.runtime.cas import CasObject
from codemigrator.runtime.contracts import EventSpec, RunState
from codemigrator.runtime.loop_contracts import SessionExit, SessionState
from codemigrator.runtime.store import PostgreSQLRuntimeStore, StoreCommitError

from .test_agent_runs import run_record


@asynccontextmanager
async def isolated_store():
    dsn = os.environ.get("CODEMIGRATOR_TEST_PG_DSN")
    if not dsn:
        pytest.skip("CODEMIGRATOR_TEST_PG_DSN is not configured")
    schema = f"agent_runs_test_{uuid4().hex}"
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


@pytest.mark.asyncio
async def test_postgres_run_owner_gate_has_zero_agent_run_side_effects():
    async with isolated_store() as store:
        record = run_record()
        with pytest.raises(StoreCommitError, match="owner Run does not exist"):
            await store.create_or_get_agent_run(record)
        assert await store.load_agent_run(record.agent_run_id) is None
        draft = replace(record, owner_kind="draft")
        assert await store.create_or_get_agent_run(draft) == draft


@pytest.mark.asyncio
async def test_postgres_concurrent_replay_and_unique_thread():
    async with isolated_store() as store:
        first = run_record()
        await store.create(RunState(run_id=first.owner_id), ())
        replay = replace(first, agent_run_id=AgentRunId(uuid4()), thread_id=str(uuid4()))
        records = await asyncio.gather(
            store.create_or_get_agent_run(first),
            store.create_or_get_agent_run(replay),
        )
        assert records[0] == records[1]
        different_task = replace(
            records[0],
            agent_run_id=AgentRunId(uuid4()),
            logical_task_key="plan:2",
        )
        with pytest.raises(StoreCommitError, match="thread"):
            await store.create_or_get_agent_run(different_task)
        assert await store.load_agent_run(different_task.agent_run_id) is None


@pytest.mark.asyncio
async def test_postgres_receipt_failure_rolls_back_run_agent_and_event():
    async with isolated_store() as store:
        record = run_record()
        await store.create(RunState(run_id=record.owner_id), ())
        await store.create_or_get_agent_run(record)
        terminal = replace(
            record,
            state=SessionState.Closed,
            exit=SessionExit.Completed,
            result_sha256="e" * 64,
        )
        receipt = AgentRunReceipt(uuid4(), record.agent_run_id, "plan.accepted")
        cas_references = ((f"agent-result:{record.agent_run_id}", CasObject("e" * 64, 12)),)
        with pytest.raises(StoreCommitError, match="observation rejected"):
            await store.commit_agent_run_receipt(
                terminal,
                receipt,
                state=RunState(run_id=record.owner_id, version=1),
                events=(EventSpec("plan.accepted", {"content": "raw source"}),),
                cas_references=cas_references,
            )
        assert (await store.load(record.owner_id)).state.version == 0
        assert (await store.load(record.owner_id)).events == ()
        assert await store.load_agent_run(record.agent_run_id) == record
        assert await store.load_agent_run_receipt(record.agent_run_id) is None
        assert await store.get_cas_reference("run", record.owner_id, cas_references[0][0]) is None
        assert (
            await store.commit_agent_run_receipt(
                terminal,
                receipt,
                state=RunState(run_id=record.owner_id, version=1),
                events=(EventSpec("plan.accepted", {"category": "plan"}),),
                cas_references=cas_references,
            )
            == receipt
        )
        assert await store.load_agent_run_receipt(record.agent_run_id) == receipt
        assert (
            await store.get_cas_reference("run", record.owner_id, cas_references[0][0])
            == cas_references[0][1]
        )


@pytest.mark.asyncio
async def test_postgres_actor_commits_execute_agent_terminal_and_result_reference():
    async with isolated_store() as store:
        created = replace(
            run_record(key="execute:slice-1:g1"),
            phase=Phase.Execute,
            session_kind=SessionKind.Implementation,
            slice_ref=SliceGenerationRef(
                slice_id=SliceId(uuid4()),
                generation=1,
                baseline_candidate_oid=GitOid("1" * 40),
            ),
            write_scope_sha256="d" * 64,
        )
        await store.create(
            RunState(
                run_id=created.owner_id,
                status=RunStatus.Executing,
                version=1,
                frozen_plan_sha256="c" * 64,
            ),
            (EventSpec("run.created", {"receipt_key": f"run.created:{created.owner_id}"}),),
        )
        await store.create_or_get_agent_run(created)
        result = CasObject("e" * 64, 16)
        terminal = replace(
            created,
            state=SessionState.Closed,
            exit=SessionExit.SegmentStopped,
            result_sha256=result.digest,
        )
        receipt = AgentRunReceipt(uuid4(), terminal.agent_run_id, "session.terminal")
        actor = RunActor(created.owner_id, store)
        await actor.start()
        try:
            await actor.record_agent_run_started(created.owner_id, created.agent_run_id)
            terminal_receipt = await actor.record_agent_run_terminal(
                created.owner_id, terminal, receipt, result
            )
            with pytest.raises(StoreCommitError, match="replay differs"):
                await actor.record_agent_run_terminal(
                    created.owner_id,
                    terminal,
                    replace(receipt, receipt_id=uuid4()),
                    result,
                )
            snapshot = await store.load(created.owner_id)
            assert snapshot is not None
            assert snapshot.state.version == 3
            assert snapshot.events[-1].event_type == "agent_run.terminal"
            assert await store.load_agent_run(terminal.agent_run_id) == terminal
            assert await store.load_agent_run_receipt(terminal.agent_run_id) == receipt
            assert (
                await store.get_cas_reference(
                    "run", created.owner_id, f"agent-result:{terminal.agent_run_id}"
                )
                == result
            )
            assert terminal_receipt.receipt_key == f"agent_run.terminal:{terminal.agent_run_id}"
        finally:
            await actor.stop()


@pytest.mark.asyncio
async def test_postgres_draft_owner_fact_receipt_is_idempotent():
    async with isolated_store() as store:
        draft_id = uuid4()
        key = "draft.answer:question-1"
        fact = {"question_id": "question-1", "selected_option": "preserve"}
        receipt = await store.commit_draft_owner_fact(draft_id, key, "draft.ask_user.answer", fact)
        assert (
            await store.commit_draft_owner_fact(draft_id, key, "draft.ask_user.answer", fact)
            == receipt
        )
        assert await store.load_draft_owner_fact(draft_id, key) == (receipt, fact)
        assert await store.list_draft_owner_facts(draft_id) == ((receipt, fact),)
        with pytest.raises(StoreCommitError, match="replay mismatch"):
            await store.commit_draft_owner_fact(
                draft_id,
                key,
                "draft.ask_user.answer",
                {"question_id": "question-1", "selected_option": "merge"},
            )

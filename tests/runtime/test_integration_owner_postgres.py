from __future__ import annotations

import hashlib
import os
from contextlib import asynccontextmanager
from uuid import uuid4

import asyncpg
import pytest

from codemigrator.core import (
    GitOid,
    IntegrationIntent,
    RunId,
    RunStatus,
    Sha256,
    SliceId,
    canonical_json_bytes,
)
from codemigrator.runtime.actor import RunActor
from codemigrator.runtime.agent_runs import AgentRunId
from codemigrator.runtime.contracts import CandidateCheckpointFact, RunState
from codemigrator.runtime.store import PostgreSQLRuntimeStore, StoreCommitError


@asynccontextmanager
async def isolated_store():
    dsn = os.environ.get("CODEMIGRATOR_TEST_PG_DSN")
    if not dsn:
        pytest.skip("CODEMIGRATOR_TEST_PG_DSN is not configured")
    schema = f"integration_owner_test_{uuid4().hex}"
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
async def test_postgres_integration_intent_and_receipt_survive_store_restart():
    async with isolated_store() as store:
        run_id = RunId(uuid4())
        slice_id = SliceId(uuid4())
        await store.create(
            RunState(
                run_id=run_id,
                status=RunStatus.Executing,
                version=1,
                frozen_plan_sha256="f" * 64,
                candidate_checkpoints=(
                    CandidateCheckpointFact(
                        agent_run_id=AgentRunId(uuid4()),
                        slice_id=slice_id,
                        generation=0,
                        expected_candidate_oid="1" * 40,
                        candidate_oid="2" * 40,
                        receipt_sha256="3" * 64,
                    ),
                ),
            ),
            (),
        )
        intent = IntegrationIntent(
            run_id=run_id,
            slice_id=slice_id,
            generation=0,
            expected_verified_oid=GitOid("a" * 40),
            prospective_commit_oid=GitOid("b" * 40),
            guard_sha256=Sha256("c" * 64),
            verification_fingerprint=Sha256("d" * 64),
            idempotency_key=Sha256("e" * 64),
        )
        actor = RunActor(run_id, store)
        await actor.start()
        await actor.persist_integration_intent(intent)
        await actor.stop()

        restarted = PostgreSQLRuntimeStore(store.pool)
        assert await restarted.load_integration_intent(
            run_id, str(intent.idempotency_key)
        ) == intent
        assert await restarted.list_pending_integration_intents(run_id) == (intent,)

        resumed_actor = RunActor(run_id, restarted)
        await resumed_actor.start()
        first = await resumed_actor.complete_integration(intent, GitOid("b" * 40))
        replay = await resumed_actor.complete_integration(intent, GitOid("b" * 40))
        await resumed_actor.stop()

        assert first == replay
        durable = PostgreSQLRuntimeStore(store.pool)
        receipt = await durable.load_integration_receipt(run_id, str(intent.idempotency_key))
        assert receipt is not None
        assert receipt.intent == intent
        assert receipt.verified_commit_oid == GitOid("b" * 40)
        assert await durable.list_pending_integration_intents(run_id) == ()
        assert await durable.list_integration_receipts(run_id) == (receipt,)
        snapshot = await durable.load(run_id)
        assert snapshot is not None
        assert snapshot.state.version == 2
        assert [event.event_type for event in snapshot.events] == [
            "integration.completed",
            "verified.advanced",
        ]


@pytest.mark.asyncio
async def test_postgres_integration_records_reject_digest_mismatch():
    async with isolated_store() as store:
        run_id = RunId(uuid4())
        slice_id = SliceId(uuid4())
        await store.create(
            RunState(
                run_id=run_id,
                status=RunStatus.Executing,
                version=1,
                frozen_plan_sha256="f" * 64,
                candidate_checkpoints=(
                    CandidateCheckpointFact(
                        agent_run_id=AgentRunId(uuid4()),
                        slice_id=slice_id,
                        generation=0,
                        expected_candidate_oid="1" * 40,
                        candidate_oid="2" * 40,
                        receipt_sha256="3" * 64,
                    ),
                ),
            ),
            (),
        )
        intent = IntegrationIntent(
            run_id=run_id,
            slice_id=slice_id,
            generation=0,
            expected_verified_oid=GitOid("a" * 40),
            prospective_commit_oid=GitOid("b" * 40),
            guard_sha256=Sha256("c" * 64),
            verification_fingerprint=Sha256("d" * 64),
            idempotency_key=Sha256("e" * 64),
        )
        actor = RunActor(run_id, store)
        await actor.start()
        await actor.persist_integration_intent(intent)
        await actor.complete_integration(intent, GitOid("b" * 40))
        await actor.stop()

        async with store.pool.acquire() as connection:
            await connection.execute(
                """UPDATE run_integration_intents SET intent_sha256=$3
                WHERE run_id=$1 AND idempotency_key=$2""",
                run_id,
                str(intent.idempotency_key),
                "0" * 64,
            )
        with pytest.raises(StoreCommitError, match="intent digest mismatch"):
            await store.load_integration_intent(run_id, str(intent.idempotency_key))

        intent_digest = hashlib.sha256(
            canonical_json_bytes(intent.model_dump(mode="json", by_alias=True))
        ).hexdigest()
        async with store.pool.acquire() as connection:
            await connection.execute(
                """UPDATE run_integration_intents SET intent_sha256=$3
                WHERE run_id=$1 AND idempotency_key=$2""",
                run_id,
                str(intent.idempotency_key),
                intent_digest,
            )
            await connection.execute(
                """UPDATE run_integration_receipts SET receipt_sha256=$3
                WHERE run_id=$1 AND idempotency_key=$2""",
                run_id,
                str(intent.idempotency_key),
                "0" * 64,
            )
        with pytest.raises(StoreCommitError, match="receipt digest mismatch"):
            await store.load_integration_receipt(run_id, str(intent.idempotency_key))

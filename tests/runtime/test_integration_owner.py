from __future__ import annotations

import hashlib
from uuid import uuid4

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
from codemigrator.runtime.contracts import CandidateCheckpointFact, EventSpec, RunState
from codemigrator.runtime.store import InMemoryRuntimeStore, StoreCommitError


def _intent(
    run_id: RunId, slice_id: SliceId, *, prospective_oid: str = "b" * 40
) -> IntegrationIntent:
    return IntegrationIntent(
        run_id=run_id,
        slice_id=slice_id,
        generation=0,
        expected_verified_oid=GitOid("a" * 40),
        prospective_commit_oid=GitOid(prospective_oid),
        guard_sha256=Sha256("c" * 64),
        verification_fingerprint=Sha256("d" * 64),
        idempotency_key=Sha256("e" * 64),
    )


async def _started_actor(*, candidate: bool = True):
    store = InMemoryRuntimeStore()
    run_id = RunId(uuid4())
    slice_id = SliceId(uuid4())
    checkpoints = (
        CandidateCheckpointFact(
            agent_run_id=AgentRunId(uuid4()),
            slice_id=slice_id,
            generation=0,
            expected_candidate_oid="1" * 40,
            candidate_oid="2" * 40,
            receipt_sha256="3" * 64,
        ),
    ) if candidate else ()
    await store.create(
        RunState(
            run_id=run_id,
            status=RunStatus.Executing,
            version=1,
            frozen_plan_sha256="f" * 64,
            candidate_checkpoints=checkpoints,
        ),
        (EventSpec("run.status_changed", {"run_status": RunStatus.Executing.value}),),
    )
    actor = RunActor(run_id, store)
    await actor.start()
    return actor, store, run_id, slice_id


@pytest.mark.asyncio
async def test_integration_intent_is_durable_idempotent_and_conflict_checked():
    actor, store, run_id, slice_id = await _started_actor()
    intent = _intent(run_id, slice_id)
    try:
        first = await actor.persist_integration_intent(intent)
        replay = await actor.persist_integration_intent(intent)

        assert first == replay == intent
        assert await store.load_integration_intent(run_id, str(intent.idempotency_key)) == intent
        assert len((await store.load(run_id)).events) == 1

        conflicting = intent.model_copy(update={"prospective_commit_oid": GitOid("9" * 40)})
        with pytest.raises(StoreCommitError, match="intent replay mismatch"):
            await actor.persist_integration_intent(conflicting)
        assert await store.load_integration_intent(run_id, str(intent.idempotency_key)) == intent
    finally:
        await actor.stop()


@pytest.mark.asyncio
async def test_integration_completion_atomically_adopts_verified_head_once():
    actor, store, run_id, slice_id = await _started_actor()
    intent = _intent(run_id, slice_id)
    try:
        await actor.persist_integration_intent(intent)
        first = await actor.complete_integration(intent, GitOid("b" * 40))
        replay = await actor.complete_integration(intent, GitOid("b" * 40))

        assert first == replay
        receipt = await store.load_integration_receipt(run_id, str(intent.idempotency_key))
        assert receipt is not None
        assert receipt.verified_commit_oid == "b" * 40
        assert receipt.event_sequence == 2
        assert await store.list_pending_integration_intents(run_id) == ()
        assert await store.list_integration_receipts(run_id) == (receipt,)
        snapshot = await store.load(run_id)
        assert snapshot.state.version == 2
        assert [event.event_type for event in snapshot.events] == [
            "run.status_changed",
            "integration.completed",
            "verified.advanced",
        ]
        assert snapshot.events[1].data["receipt_key"] == (
            f"integration.completed:{intent.idempotency_key}"
        )
    finally:
        await actor.stop()


@pytest.mark.asyncio
async def test_pending_integration_intent_is_recoverable_before_git_receipt():
    actor, store, run_id, slice_id = await _started_actor()
    intent = _intent(run_id, slice_id)
    try:
        await actor.persist_integration_intent(intent)
        assert await store.list_pending_integration_intents(run_id) == (intent,)
        assert await store.list_integration_receipts(run_id) == ()
    finally:
        await actor.stop()


@pytest.mark.asyncio
async def test_integration_intent_requires_accepted_candidate_and_matching_verified_head():
    actor, store, run_id, slice_id = await _started_actor(candidate=False)
    intent = _intent(run_id, slice_id)
    try:
        with pytest.raises(StoreCommitError, match="accepted M-08 candidate"):
            await actor.persist_integration_intent(intent)
        assert await store.load_integration_intent(run_id, str(intent.idempotency_key)) is None

        await actor.stop()

        actor, store, run_id, slice_id = await _started_actor()
        intent = _intent(run_id, slice_id)
        await actor.persist_integration_intent(intent)
        with pytest.raises(StoreCommitError, match="does not match the intent"):
            await actor.complete_integration(intent, GitOid("9" * 40))
        assert await store.load_integration_receipt(run_id, str(intent.idempotency_key)) is None
        assert len((await store.load(run_id)).events) == 1
    finally:
        await actor.stop()


@pytest.mark.asyncio
async def test_integration_receipt_digest_covers_committed_intent_and_verified_head():
    actor, store, run_id, slice_id = await _started_actor()
    intent = _intent(run_id, slice_id)
    try:
        await actor.persist_integration_intent(intent)
        await actor.complete_integration(intent, GitOid("b" * 40))
        receipt = await store.load_integration_receipt(run_id, str(intent.idempotency_key))

        expected_payload = {
            "idempotency_key": str(intent.idempotency_key),
            "verified_commit_oid": "b" * 40,
        }
        expected_digest = hashlib.sha256(canonical_json_bytes(expected_payload)).hexdigest()
        assert receipt.receipt_sha256 == expected_digest
    finally:
        await actor.stop()

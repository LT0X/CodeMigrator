from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from codemigrator.core import (
    GitOid,
    IntegrationIntent,
    RunId,
    RunStatus,
    Sha256,
    SliceId,
    StableErrorCode,
)
from codemigrator.runtime.actor import RunActor
from codemigrator.runtime.agent_runs import AgentRunId
from codemigrator.runtime.contracts import CandidateCheckpointFact, EventSpec, RunState
from codemigrator.runtime.integration_driver import (
    IntegrationRecoveryError,
    RunIntegrationDriver,
)
from codemigrator.runtime.store import InMemoryRuntimeStore
from codemigrator.workspace import CandidateRefConflict, GitRunRepository


class RetryOnceRepository(GitRunRepository):
    def __init__(self, path: Path, run_id: RunId) -> None:
        super().__init__(path, run_id)
        self.verified_cas_attempts = 0
        self.track_verified_cas = False

    def update_ref(self, ref: str, new_oid: GitOid, expected_oid: GitOid) -> None:
        if ref == self.refs.verified and self.track_verified_cas:
            self.verified_cas_attempts += 1
            if self.verified_cas_attempts == 1:
                raise CandidateRefConflict("injected verified-ref CAS contention")
        super().update_ref(ref, new_oid, expected_oid)


async def _case(tmp_path: Path, *, retry_once: bool = False):
    run_id = RunId(uuid4())
    slice_id = SliceId(uuid4())
    repo_type = RetryOnceRepository if retry_once else GitRunRepository
    repo = repo_type(tmp_path / "output.git", run_id)
    repo.initialize()
    if retry_once:
        repo.track_verified_cas = True
    expected_oid = repo.resolve(repo.refs.verified)
    prospective_oid = repo.create_commit(
        repo.empty_tree,
        parent=expected_oid,
        message="prospective verified integration",
    )
    intent = IntegrationIntent(
        run_id=run_id,
        slice_id=slice_id,
        generation=0,
        expected_verified_oid=expected_oid,
        prospective_commit_oid=prospective_oid,
        guard_sha256=Sha256("c" * 64),
        verification_fingerprint=Sha256("d" * 64),
        idempotency_key=Sha256("e" * 64),
    )
    store = InMemoryRuntimeStore()
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
        (EventSpec("run.status_changed", {"run_status": RunStatus.Executing.value}),),
    )
    actor = RunActor(run_id, store)
    await actor.start()
    return actor, store, repo, intent


@pytest.mark.asyncio
async def test_driver_retries_an_unapplied_verified_ref_cas(tmp_path: Path):
    actor, store, repo, intent = await _case(tmp_path, retry_once=True)
    try:
        result = await RunIntegrationDriver(actor, repo).integrate(intent)

        assert repo.resolve(repo.refs.verified) == intent.prospective_commit_oid
        assert repo.verified_cas_attempts == 2
        assert result.event_sequence == 2
        assert await store.load_integration_receipt(
            intent.run_id, str(intent.idempotency_key)
        ) is not None
    finally:
        await actor.stop()


@pytest.mark.asyncio
async def test_driver_adopts_verified_head_after_crash_before_receipt(tmp_path: Path, monkeypatch):
    actor, store, repo, intent = await _case(tmp_path)
    driver = RunIntegrationDriver(actor, repo)
    complete_integration = actor.complete_integration

    async def crash_before_receipt(_intent: IntegrationIntent, _verified_oid: GitOid):
        raise RuntimeError("simulated process crash after Git CAS")

    monkeypatch.setattr(actor, "complete_integration", crash_before_receipt)
    try:
        with pytest.raises(RuntimeError, match="simulated process crash"):
            await driver.integrate(intent)

        assert repo.resolve(repo.refs.verified) == intent.prospective_commit_oid
        assert await store.load_integration_receipt(
            intent.run_id, str(intent.idempotency_key)
        ) is None
        assert await store.list_pending_integration_intents(intent.run_id) == (intent,)

        monkeypatch.setattr(actor, "complete_integration", complete_integration)
        recovered = await driver.integrate(intent)
        assert recovered.event_sequence == 2
        assert len((await store.load(intent.run_id)).events) == 3
    finally:
        await actor.stop()


@pytest.mark.asyncio
async def test_driver_replays_existing_receipt_without_rewriting_run_facts(tmp_path: Path):
    actor, store, repo, intent = await _case(tmp_path)
    driver = RunIntegrationDriver(actor, repo)
    try:
        first = await driver.integrate(intent)
        before = await store.load(intent.run_id)
        replay = await driver.integrate(intent)
        after = await store.load(intent.run_id)

        assert replay == first
        assert after == before
        assert [event.event_type for event in after.events].count("integration.completed") == 1
    finally:
        await actor.stop()


@pytest.mark.asyncio
async def test_driver_fails_closed_when_verified_oid_matches_neither_intent_head(
    tmp_path: Path,
):
    actor, store, repo, intent = await _case(tmp_path)
    unrelated_oid = repo.create_commit(
        repo.empty_tree,
        parent=intent.expected_verified_oid,
        message="unrelated verified movement",
    )
    repo.update_ref(repo.refs.verified, unrelated_oid, intent.expected_verified_oid)
    try:
        with pytest.raises(IntegrationRecoveryError) as error:
            await RunIntegrationDriver(actor, repo).integrate(intent)

        assert error.value.code is StableErrorCode.RECOVERY_LEDGER_INCONSISTENT
        assert repo.resolve(repo.refs.verified) == unrelated_oid
        assert await store.load_integration_receipt(
            intent.run_id, str(intent.idempotency_key)
        ) is None
        assert await store.list_pending_integration_intents(intent.run_id) == (intent,)
    finally:
        await actor.stop()

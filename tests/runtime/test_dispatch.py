from __future__ import annotations

import pytest

from codemigrator.core import (
    ActiveDispatch,
    CandidateGeneration,
    CheckId,
    DispatchAttemptId,
    GitOid,
    LocalCandidate,
    SliceId,
)
from codemigrator.runtime.actor import RunActor
from codemigrator.runtime.contracts import ExecutionReceiptMessage
from codemigrator.runtime.store import InMemoryRuntimeStore

from .conftest import create_run, uid


def dispatch() -> ActiveDispatch:
    commit = GitOid("a" * 40)
    subject = LocalCandidate(
        kind="LOCAL_CANDIDATE",
        slice_id=SliceId(uid()),
        generation=CandidateGeneration(0),
        candidate_commit_oid=commit,
    )
    return ActiveDispatch(
        dispatch_attempt_id=DispatchAttemptId(uid()),
        subject=subject,
        check_id=CheckId(uid()),
        tested_commit_oid=commit,
    )


@pytest.mark.asyncio
async def test_dispatch_gate_has_one_active_attempt_and_late_results_are_audit_only(run_id):
    store = InMemoryRuntimeStore()
    actor = RunActor(run_id, store)
    await actor.start()
    await actor.create(create_run())
    active = dispatch()
    assert await actor.dispatch_started(active) is True
    assert await actor.dispatch_started(active) is False

    late = active.model_copy(update={"dispatch_attempt_id": DispatchAttemptId(uid())})
    await actor.submit(ExecutionReceiptMessage(dispatch=late, result_status="PASSED"))
    await actor.join()
    snapshot = await store.snapshot(run_id)
    assert snapshot.state.active_dispatches == (active,)
    assert snapshot.events[-1].event_type == "LATE_DISPATCH_RESULT"
    await actor.stop()


@pytest.mark.asyncio
async def test_matching_receipt_closes_gate_but_does_not_decide_verification(run_id):
    store = InMemoryRuntimeStore()
    actor = RunActor(run_id, store)
    await actor.start()
    await actor.create(create_run())
    active = dispatch()
    assert await actor.dispatch_started(active) is True
    await actor.submit(ExecutionReceiptMessage(dispatch=active, result_status="PASSED"))
    await actor.join()
    snapshot = await store.snapshot(run_id)
    assert snapshot.state.active_dispatches == ()
    assert snapshot.events[-1].event_type == "dispatch.completed"
    await actor.stop()

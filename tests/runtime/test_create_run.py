from __future__ import annotations

import pytest

from codemigrator.runtime.actor import RunActor
from codemigrator.runtime.contracts import RunCreatedReceipt, RuntimeStoreTransaction
from codemigrator.runtime.create_run import (
    CreateRunRejected,
    CreateRunService,
    RunCreationOwner,
)
from codemigrator.runtime.store import InMemoryRuntimeStore

from .conftest import create_run


class RecordingPreflight:
    def __init__(self, *, reject_at: str | None = None) -> None:
        self.calls: list[str] = []
        self.reject_at = reject_at

    async def verify_descriptor_lock(self, request, transaction=None) -> None:
        await self._check("descriptor_lock")

    async def verify_preindex(self, request, transaction=None) -> None:
        await self._check("preindex")

    async def verify_dossier_consistency(self, request, transaction=None) -> None:
        await self._check("dossier_consistency")

    async def _check(self, name: str) -> None:
        self.calls.append(name)
        if self.reject_at == name:
            raise CreateRunRejected(name)


class RecordingActor:
    def __init__(self, receipt: RunCreatedReceipt | None) -> None:
        self.receipt = receipt
        self.calls = 0

    async def create(self, request):
        self.calls += 1
        return self.receipt


class RecordingGraphStarter:
    receipt_idempotent = True

    def __init__(self) -> None:
        self.receipts: list[RunCreatedReceipt] = []

    async def start(self, run_id, receipt: RunCreatedReceipt) -> None:
        self.receipts.append(receipt)


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected_at", ["descriptor_lock", "preindex", "dossier_consistency"])
async def test_preflight_rejection_has_no_run_or_graph_side_effects(run_id, rejected_at) -> None:
    preflight = RecordingPreflight(reject_at=rejected_at)
    actor = RecordingActor(None)
    graph = RecordingGraphStarter()
    service = CreateRunService(preflight=preflight, actor=actor, graph_starter=graph)

    with pytest.raises(CreateRunRejected):
        await service.create(run_id, create_run())

    assert actor.calls == 0
    assert graph.receipts == []
    assert (
        preflight.calls
        == [
            "descriptor_lock",
            "preindex",
            *(["dossier_consistency"] if rejected_at == "dossier_consistency" else []),
        ][: {"descriptor_lock": 1, "preindex": 2, "dossier_consistency": 3}[rejected_at]]
    )


@pytest.mark.asyncio
async def test_run_graph_starts_only_after_persisted_run_created_receipt(run_id) -> None:
    receipt = RunCreatedReceipt(
        run_id=run_id,
        receipt_key=f"run.created:{run_id}",
        event_sequence=1,
        state_version=1,
    )
    preflight = RecordingPreflight()
    actor = RecordingActor(receipt)
    graph = RecordingGraphStarter()
    service = CreateRunService(preflight=preflight, actor=actor, graph_starter=graph)

    result = await service.create(run_id, create_run())

    assert result == receipt
    assert preflight.calls == ["descriptor_lock", "preindex", "dossier_consistency"]
    assert actor.calls == 1
    assert graph.receipts == [receipt]


@pytest.mark.asyncio
async def test_missing_actor_receipt_does_not_start_run_graph(run_id) -> None:
    service = CreateRunService(
        preflight=RecordingPreflight(),
        actor=RecordingActor(None),
        graph_starter=RecordingGraphStarter(),
    )

    with pytest.raises(CreateRunRejected, match="RunCreated receipt"):
        await service.create(run_id, create_run())


@pytest.mark.asyncio
async def test_run_creation_owner_uses_injected_factory_for_new_actor(run_id) -> None:
    store = InMemoryRuntimeStore()
    scheduler = object()
    factory_calls = []

    def actor_factory(received_run_id, received_store):
        factory_calls.append((received_run_id, received_store))
        return RunActor(
            received_run_id,
            received_store,
            execution_scheduler=scheduler,
        )

    owner = RunCreationOwner(
        store=store,
        preflight=RecordingPreflight(),
        graph_starter=RecordingGraphStarter(),
        actor_factory=actor_factory,
    )
    transaction = RuntimeStoreTransaction(store, None)

    receipt = await owner.create_run(create_run(), transaction)
    transaction.finish(committed=True)
    actor = await owner._get_actor(receipt.run_id)

    assert actor is not None
    assert actor.execution_scheduler is scheduler
    assert factory_calls == [(receipt.run_id, store)]
    await owner.close()

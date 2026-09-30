"""Deterministic CreateRun barrier before any Run-owned side effect."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID, uuid4

from codemigrator.core import CreateRun, RunId

from .actor import RunActor
from .contracts import RunCreatedReceipt, RuntimeStoreTransaction
from .store import RuntimeStore, StoreCommitError


class CreateRunRejected(RuntimeError):
    """A safe, low-sensitivity rejection from preflight or owner admission."""


class CreateRunPreflightPort(Protocol):
    async def verify_descriptor_lock(self, request: CreateRun) -> None: ...

    async def verify_preindex(self, request: CreateRun) -> None: ...

    async def verify_dossier_consistency(self, request: CreateRun) -> None: ...


class RunCreationActor(Protocol):
    async def create(self, request: CreateRun) -> RunCreatedReceipt | None: ...


class RunGraphStarter(Protocol):
    async def start(self, run_id: RunId, receipt: RunCreatedReceipt) -> None: ...


class CreateRunService:
    """Run all deterministic gates before asking the actor to create a Run."""

    def __init__(
        self,
        *,
        preflight: CreateRunPreflightPort,
        actor: RunCreationActor,
        graph_starter: RunGraphStarter,
    ) -> None:
        self.preflight = preflight
        self.actor = actor
        self.graph_starter = graph_starter

    async def create(self, run_id: RunId, request: CreateRun) -> RunCreatedReceipt:
        await self.preflight.verify_descriptor_lock(request)
        await self.preflight.verify_preindex(request)
        await self.preflight.verify_dossier_consistency(request)
        receipt = await self.actor.create(request)
        if receipt is None or receipt.run_id != run_id:
            raise CreateRunRejected("missing RunCreated receipt")
        await self.graph_starter.start(run_id, receipt)
        return receipt


class RunCreationOwner:
    """Runtime adapter that applies CreateRun gates and writes through RunActor."""

    def __init__(
        self,
        *,
        store: RuntimeStore,
        preflight: CreateRunPreflightPort,
        graph_starter: RunGraphStarter,
    ) -> None:
        self.store = store
        self.preflight = preflight
        self.graph_starter = graph_starter

    async def create_run(
        self, request: CreateRun, transaction: object
    ) -> RunCreatedReceipt:
        if not isinstance(transaction, RuntimeStoreTransaction):
            raise StoreCommitError("CreateRun requires the shared runtime store transaction")
        await self.preflight.verify_descriptor_lock(request)
        await self.preflight.verify_preindex(request)
        await self.preflight.verify_dossier_consistency(request)

        run_id = RunId(uuid4())
        actor = RunActor(run_id, self.store)
        await actor.start()
        try:
            receipt = await actor.create(request, transaction=transaction)
        finally:
            await actor.stop()
        if receipt is None or receipt.run_id != run_id:
            raise CreateRunRejected("missing RunCreated receipt")
        return receipt

    async def start_graph(self, run_id: UUID, receipt: RunCreatedReceipt) -> None:
        if receipt.run_id != RunId(run_id):
            raise CreateRunRejected("RunCreated receipt owner mismatch")
        await self.graph_starter.start(RunId(run_id), receipt)

    async def load_run_created_receipt(
        self, run_id: UUID, receipt_key: str
    ) -> RunCreatedReceipt:
        typed_run_id = RunId(run_id)
        snapshot = await self.store.load(typed_run_id)
        if snapshot is None:
            raise CreateRunRejected("RunCreated owner facts are missing")
        for event in snapshot.events:
            if event.event_type == "run.created" and event.data.get("receipt_key") == receipt_key:
                version = event.data.get("state_version", 1)
                if type(version) is not int:
                    raise CreateRunRejected("RunCreated receipt version is invalid")
                return RunCreatedReceipt(
                    run_id=typed_run_id,
                    receipt_key=receipt_key,
                    event_sequence=event.sequence,
                    state_version=version,
                )
        raise CreateRunRejected("RunCreated owner receipt is missing")


__all__ = [
    "CreateRunPreflightPort",
    "CreateRunRejected",
    "CreateRunService",
    "RunCreationOwner",
]

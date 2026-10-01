"""Deterministic CreateRun barrier before any Run-owned side effect."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Literal, Protocol, cast
from uuid import UUID, uuid4

from codemigrator.core import CreateRun, FailureReason, RunId, StableErrorCode

from .actor import ActorRegistry, RunActor, RunActorFactory, RunCommandRejected
from .contracts import RunCreatedReceipt, RunState, RuntimeStoreTransaction
from .run_graph import RunWorkflowGraph
from .store import RuntimeStore, StoreCommitError


class CreateRunRejected(RuntimeError):
    """A safe, low-sensitivity rejection from preflight or owner admission."""

    def __init__(
        self,
        detail: str,
        *,
        code: StableErrorCode | FailureReason | None = None,
    ) -> None:
        super().__init__(detail)
        self.create_run_rejection_code = (
            code.value if isinstance(code, (StableErrorCode, FailureReason)) else None
        )


class CreateRunPreflightPort(Protocol):
    """Validate request gates using the API transaction when one is supplied.

    API-owned calls pass their command transaction so database-backed gates use
    its connection. Standalone CreateRunService calls pass ``None``; a gate may
    use a separate read transaction in that standalone context.
    """

    async def verify_descriptor_lock(
        self, request: CreateRun, transaction: RuntimeStoreTransaction | None
    ) -> None: ...

    async def verify_preindex(
        self, request: CreateRun, transaction: RuntimeStoreTransaction | None
    ) -> None: ...

    async def verify_dossier_consistency(
        self, request: CreateRun, transaction: RuntimeStoreTransaction | None
    ) -> None: ...


class RunCreationActor(Protocol):
    async def create(self, request: CreateRun) -> RunCreatedReceipt | None: ...


class RunGraphStarter(Protocol):
    receipt_idempotent: Literal[True]

    async def start(self, run_id: RunId, receipt: RunCreatedReceipt) -> None:
        """Replay a committed receipt safely after process recovery."""


class ActorBoundRunGraphStarter(Protocol):
    receipt_idempotent: Literal[True]

    async def start_for_actor(
        self, run_id: RunId, receipt: RunCreatedReceipt, actor: RunActor
    ) -> None:
        """Start or resume a receipt-keyed graph against this Run's single actor."""


class RunWorkflowGraphStarter:
    """Bind RunWorkflowGraph to the owner's actor and injected durable checkpointer.

    ``durable_checkpointer`` is an explicit host attestation: the graph factory must
    return graphs sharing the same durable checkpoint namespace across restarts.
    """

    receipt_idempotent: Literal[True] = True

    def __init__(
        self,
        graph_factory: Callable[[RunActor], RunWorkflowGraph],
        *,
        durable_checkpointer: Literal[True],
    ) -> None:
        if durable_checkpointer is not True:
            raise ValueError("Run graph recovery requires a durable checkpointer")
        self._graph_factory = graph_factory

    async def start(self, run_id: RunId, receipt: RunCreatedReceipt) -> None:
        raise RuntimeError("RunWorkflowGraphStarter requires its durable RunActor")

    async def start_for_actor(
        self, run_id: RunId, receipt: RunCreatedReceipt, actor: RunActor
    ) -> None:
        if run_id != receipt.run_id or actor.run_id != run_id:
            raise CreateRunRejected("Run graph receipt owner mismatch")
        await self._graph_factory(actor).start(receipt)


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
        await self.preflight.verify_descriptor_lock(request, None)
        await self.preflight.verify_preindex(request, None)
        await self.preflight.verify_dossier_consistency(request, None)
        receipt = await self.actor.create(request)
        if receipt is None or receipt.run_id != run_id:
            raise CreateRunRejected("missing RunCreated receipt")
        await self.graph_starter.start(run_id, receipt)
        return receipt


class RunCreationOwner:
    """Runtime adapter that applies CreateRun gates and writes through RunActor."""

    recovery_safe = True

    def __init__(
        self,
        *,
        store: RuntimeStore,
        preflight: CreateRunPreflightPort,
        graph_starter: RunGraphStarter | ActorBoundRunGraphStarter,
        actor_factory: RunActorFactory | None = None,
    ) -> None:
        if getattr(graph_starter, "receipt_idempotent", None) is not True:
            raise ValueError("Run graph starter must guarantee receipt-idempotent recovery")
        self.store = store
        self.preflight = preflight
        self.graph_starter = graph_starter
        self._actors = ActorRegistry(store, actor_factory=actor_factory)
        self._pending_committed_actors: dict[RunId, RunActor] = {}
        self._actor_selection_lock = asyncio.Lock()
        self._admission_open = True

    @property
    def active_actor_count(self) -> int:
        return self._actors.active_actor_count

    async def close(self) -> None:
        self.close_admission()
        async with self._actor_selection_lock:
            pending = tuple(self._pending_committed_actors.values())
            self._pending_committed_actors.clear()
            await self._actors.close()
        await asyncio.gather(*(actor.stop() for actor in pending), return_exceptions=True)

    def close_admission(self) -> None:
        self._admission_open = False
        self._actors.close_admission()
        for actor in tuple(self._pending_committed_actors.values()):
            actor.close_admission()

    async def create_run(
        self, request: CreateRun, transaction: object
    ) -> RunCreatedReceipt:
        if not self._admission_open:
            raise StoreCommitError("Run creation owner is closed")
        if not isinstance(transaction, RuntimeStoreTransaction):
            raise StoreCommitError("CreateRun requires the shared runtime store transaction")
        await self.preflight.verify_descriptor_lock(request, transaction)
        await self.preflight.verify_preindex(request, transaction)
        await self.preflight.verify_dossier_consistency(request, transaction)
        if not self._admission_open:
            raise StoreCommitError("Run creation owner is closed")

        run_id = RunId(uuid4())
        actor = self._actors.create_actor(run_id)
        transaction.after_rollback(lambda: self._actors.stop_after_rollback(actor))
        await actor.start_new()
        try:
            receipt = await actor.create(request, transaction=transaction)
        except Exception:
            await actor.stop()
            raise
        if receipt is None or receipt.run_id != run_id:
            await actor.stop()
            raise CreateRunRejected("missing RunCreated receipt")
        transaction.after_commit(
            lambda: self._pending_committed_actors.__setitem__(run_id, actor)
        )
        return receipt

    async def cancel_run(self, run_id: UUID, expected_version: int) -> RunState:
        if not self._admission_open:
            raise StoreCommitError("Run creation owner is closed")
        typed_run_id = RunId(run_id)
        actor = await self._get_actor(typed_run_id)
        if actor is None:
            snapshot = await self.store.load(typed_run_id)
            if snapshot is None:
                raise KeyError(str(run_id))
            raise RunCommandRejected(StableErrorCode.PHASE_STATUS_MISMATCH.value)
        return await actor.cancel(expected_version)

    async def start_graph(self, run_id: UUID, receipt: RunCreatedReceipt) -> None:
        if not self._admission_open:
            raise StoreCommitError("Run creation owner is closed")
        typed_run_id = RunId(run_id)
        if receipt.run_id != typed_run_id:
            raise CreateRunRejected("RunCreated receipt owner mismatch")
        actor = await self._get_actor(typed_run_id)
        if actor is None:
            return
        start_for_actor = getattr(self.graph_starter, "start_for_actor", None)
        if callable(start_for_actor):
            await start_for_actor(typed_run_id, receipt, actor)
        else:
            await cast(RunGraphStarter, self.graph_starter).start(typed_run_id, receipt)

    async def _get_actor(self, run_id: RunId) -> RunActor | None:
        if not self._admission_open:
            raise StoreCommitError("Run creation owner is closed")
        async with self._actor_selection_lock:
            if not self._admission_open:
                raise StoreCommitError("Run creation owner is closed")
            pending = self._pending_committed_actors.pop(run_id, None)
            if pending is not None:
                return await self._actors.register_committed(pending)
            return await self._actors.get_or_create(run_id)

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
    "ActorBoundRunGraphStarter",
    "CreateRunPreflightPort",
    "CreateRunRejected",
    "CreateRunService",
    "RunCreationOwner",
    "RunGraphStarter",
    "RunWorkflowGraphStarter",
]

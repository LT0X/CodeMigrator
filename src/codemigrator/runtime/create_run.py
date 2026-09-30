"""Deterministic CreateRun barrier before any Run-owned side effect."""

from __future__ import annotations

from typing import Protocol

from codemigrator.core import CreateRun, RunId

from .contracts import RunCreatedReceipt


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


__all__ = ["CreateRunPreflightPort", "CreateRunRejected", "CreateRunService"]

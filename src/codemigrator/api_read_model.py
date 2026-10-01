"""Read-only API projections over committed Run facts and frozen-plan CAS."""

from __future__ import annotations

import json
from uuid import UUID

from pydantic import ValidationError

from codemigrator.api.dto import MigrationListView, MigrationView, SliceView, WorkspaceView
from codemigrator.core import MigrationSlice, RunId, SliceAttemptStatus, SliceId

from .runtime.cas import CasIntegrityError, FileHostCAS
from .runtime.contracts import RunState, RuntimeSnapshot
from .runtime.store import RuntimeStore, StoreCommitError


class RuntimeRunReadModel:
    """Project only the existing public Run views from owner-committed facts."""

    def __init__(self, store: RuntimeStore, host_cas: FileHostCAS) -> None:
        self._store = store
        self._host_cas = host_cas

    async def list_migrations(self, *, limit: int, cursor: UUID | None) -> MigrationListView:
        page = await self._store.list_run_states(
            limit=limit,
            after_run_id=RunId(cursor) if cursor is not None else None,
        )
        return MigrationListView(
            items=[self._migration_view(state) for state in page.states],
            next_cursor=str(page.next_cursor) if page.next_cursor is not None else None,
        )

    async def get_migration(self, run_id: UUID) -> MigrationView:
        snapshot = await self._load(run_id)
        return self._migration_view(snapshot.state)

    async def get_workspace(self, run_id: UUID) -> WorkspaceView:
        snapshot = await self._load(run_id)
        slices, integration_order = await self._load_frozen_plan(snapshot)
        by_id = self._project_slice_states(snapshot, slices)
        return WorkspaceView(
            run_id=run_id,
            slices=[by_id[slice_id] for slice_id in integration_order],
            integration_queue=[
                {
                    "slice_id": str(slice_id),
                    "integration_rank": by_id[slice_id].integration_rank,
                }
                for slice_id in integration_order
            ],
            latest_sequence=snapshot.events[-1].sequence if snapshot.events else 0,
        )

    async def _load(self, run_id: UUID) -> RuntimeSnapshot:
        snapshot = await self._store.load(RunId(run_id))
        if snapshot is None:
            raise KeyError(run_id)
        return snapshot

    @staticmethod
    def _migration_view(state: RunState) -> MigrationView:
        return MigrationView(run_id=state.run_id, status=state.status, version=state.version)

    async def _load_frozen_plan(
        self, snapshot: RuntimeSnapshot
    ) -> tuple[tuple[MigrationSlice, ...], tuple[SliceId, ...]]:
        expected_digest = snapshot.state.frozen_plan_sha256
        if expected_digest is None:
            return (), ()
        run_id = snapshot.state.run_id
        plan_ref = await self._store.get_cas_reference("run", run_id, "frozen-plan")
        if plan_ref is None or plan_ref.digest != expected_digest:
            raise StoreCommitError("Run frozen-plan reference does not match committed state")
        try:
            body = self._host_cas.read(plan_ref)
            payload = json.loads(body)
        except (CasIntegrityError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StoreCommitError("Run frozen-plan object is unavailable") from exc
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("slices"), list)
            or not isinstance(payload.get("integration_order"), list)
        ):
            raise StoreCommitError("Run frozen-plan projection is invalid")
        try:
            slices = tuple(MigrationSlice.model_validate(item) for item in payload["slices"])
        except (TypeError, ValueError, ValidationError) as exc:
            raise StoreCommitError("Run frozen-plan slices are invalid") from exc
        identifiers = tuple(slice_.id for slice_ in slices)
        if len(set(identifiers)) != len(identifiers):
            raise StoreCommitError("Run frozen-plan contains duplicate Slice identifiers")
        try:
            integration_order = tuple(
                SliceId(UUID(str(value))) for value in payload["integration_order"]
            )
        except (TypeError, ValueError) as exc:
            raise StoreCommitError("Run frozen integration order is invalid") from exc
        expected_order = tuple(
            slice_.id
            for slice_ in sorted(
                slices, key=lambda item: (item.integration_rank, item.id.bytes)
            )
        )
        if (
            len(set(integration_order)) != len(integration_order)
            or integration_order != expected_order
        ):
            raise StoreCommitError("Run frozen integration order does not match its slices")
        return slices, integration_order

    @staticmethod
    def _project_slice_states(
        snapshot: RuntimeSnapshot, slices: tuple[MigrationSlice, ...]
    ) -> dict[SliceId, SliceView]:
        status_by_id = {slice_.id: SliceAttemptStatus.Ready for slice_ in slices}
        generation_by_id = {slice_.id: 0 for slice_ in slices}
        for event in snapshot.events:
            slice_value = event.data.get("slice_id")
            try:
                slice_id = SliceId(UUID(str(slice_value)))
            except (TypeError, ValueError):
                continue
            if slice_id not in status_by_id:
                continue
            raw_generation = event.data.get("generation")
            stale_generation = False
            if type(raw_generation) is int and raw_generation >= 0:
                if raw_generation < generation_by_id[slice_id]:
                    stale_generation = True
                else:
                    generation_by_id[slice_id] = raw_generation
            if stale_generation:
                continue
            status_value = event.data.get("status")
            if event.event_type == "slice.status_changed" and isinstance(status_value, str):
                try:
                    status_by_id[slice_id] = SliceAttemptStatus(status_value)
                except ValueError:
                    continue
            elif event.event_type == "dispatch.started":
                status_by_id[slice_id] = SliceAttemptStatus.Running
            elif event.event_type == "verification.completed":
                outcome = str(event.data.get("outcome", event.data.get("status", ""))).upper()
                if outcome in {"PASSED", "PASS"} or event.data.get("local") is True:
                    status_by_id[slice_id] = SliceAttemptStatus.LocallyVerified
            elif event.event_type == "integration.queued":
                status_by_id[slice_id] = SliceAttemptStatus.IntegrationQueued
            elif event.event_type == "integration.started":
                status_by_id[slice_id] = SliceAttemptStatus.Integrating
            elif event.event_type in {
                "candidate.generation_started",
                "candidate.generation_invalidated",
            }:
                status_by_id[slice_id] = SliceAttemptStatus.Regenerating
            elif event.event_type == "verified.advanced":
                status_by_id[slice_id] = SliceAttemptStatus.Integrated

        result: dict[SliceId, SliceView] = {}
        for slice_ in slices:
            slice_id = slice_.id
            result[slice_id] = SliceView(
                slice_id=slice_id,
                kind=slice_.kind,
                status=status_by_id[slice_id],
                generation=generation_by_id[slice_id],
                write_scope={
                    "write_paths": [
                        str(path) for path in slice_.write_scope.out.write_paths
                    ],
                    "create_roots": [
                        str(path) for path in slice_.write_scope.out.create_roots
                    ],
                },
                integration_rank=slice_.integration_rank,
            )
        return result


__all__ = ["RuntimeRunReadModel"]

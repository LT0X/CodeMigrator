from __future__ import annotations

from uuid import UUID

import httpx
import pytest

from codemigrator.api import ApiConfig, create_app
from codemigrator.api.backend import ProductionApiBackend
from codemigrator.api_read_model import RuntimeRunReadModel
from codemigrator.core import (
    RunId,
    RunStatus,
    SliceAttemptStatus,
    canonical_json_bytes,
)
from codemigrator.runtime.cas import FileHostCAS
from codemigrator.runtime.contracts import EventSpec, RunState
from codemigrator.runtime.store import InMemoryRuntimeStore

from .conftest import build_frozen_plan


async def _create_planned_run(store, cas, run_id: UUID):  # type: ignore[no-untyped-def]
    frozen_plan = build_frozen_plan()
    slices = frozen_plan.slices
    plan_ref = cas.put(frozen_plan.canonical_payload())
    assert plan_ref.digest != frozen_plan.plan_hash
    await store.create(
        RunState(
            run_id=RunId(run_id),
            status=RunStatus.Executing,
            version=7,
            frozen_plan_sha256=frozen_plan.plan_hash,
        ),
        (
            EventSpec("dispatch.started", {"slice_id": str(slices[0].id), "generation": 2}),
            EventSpec(
                "slice.status_changed",
                {
                    "slice_id": str(slices[1].id),
                    "status": SliceAttemptStatus.Integrated.value,
                    "generation": 1,
                },
            ),
            EventSpec(
                "slice.status_changed",
                {
                    "slice_id": str(slices[0].id),
                    "status": SliceAttemptStatus.Ready.value,
                    "generation": 1,
                },
            ),
            EventSpec(
                "test.failure_attributed",
                {"slice_id": str(slices[0].id), "generation": 2},
            ),
            EventSpec(
                "integration.completed",
                {"slice_id": str(slices[1].id), "generation": 1},
            ),
            EventSpec(
                "verified.advanced",
                {"slice_id": str(slices[1].id), "generation": 1},
            ),
        ),
    )
    await store.add_cas_reference(plan_ref, "run", run_id, "frozen-plan")
    return slices


@pytest.mark.asyncio
async def test_workspace_projection_uses_frozen_plan_allowlist_and_committed_event_cursor(tmp_path):
    store = InMemoryRuntimeStore()
    cas = FileHostCAS(tmp_path / "cas")
    run_id = UUID(int=100)
    slices = await _create_planned_run(store, cas, run_id)
    read_model = RuntimeRunReadModel(store, cas)

    workspace = await read_model.get_workspace(run_id)

    assert workspace.run_id == run_id
    assert [item.slice_id for item in workspace.slices] == [slice_.id for slice_ in slices]
    assert [item.status for item in workspace.slices] == [
        SliceAttemptStatus.Regenerating,
        SliceAttemptStatus.IntegrationQueued,
    ]
    assert [item.generation for item in workspace.slices] == [2, 1]
    assert [item["slice_id"] for item in workspace.integration_queue] == [
        str(slice_.id) for slice_ in slices
    ]
    assert workspace.latest_sequence == 6
    assert "plan_hash" not in workspace.model_dump()
    assert "planner_rationale" not in workspace.model_dump()


@pytest.mark.asyncio
async def test_run_list_detail_and_workspace_are_read_only_views(tmp_path):
    store = InMemoryRuntimeStore()
    cas = FileHostCAS(tmp_path / "cas")
    run_ids = (UUID(int=101), UUID(int=102), UUID(int=103))
    for run_id in run_ids:
        await store.create(RunState(run_id=RunId(run_id), version=4), ())
    await _create_planned_run(store, cas, UUID(int=104))
    read_model = RuntimeRunReadModel(store, cas)

    first = await read_model.list_migrations(limit=2, cursor=None)
    second = await read_model.list_migrations(limit=2, cursor=UUID(first.next_cursor))
    detail = await read_model.get_migration(UUID(int=104))

    assert [item.run_id for item in first.items] == list(run_ids[:2])
    assert first.next_cursor == str(run_ids[1])
    assert [item.run_id for item in second.items] == [run_ids[2], UUID(int=104)]
    assert second.next_cursor is None
    assert detail.status is RunStatus.Executing
    assert detail.version == 7


@pytest.mark.asyncio
async def test_read_model_returns_not_found_and_fails_closed_for_broken_frozen_plan(tmp_path):
    store = InMemoryRuntimeStore()
    cas = FileHostCAS(tmp_path / "cas")
    read_model = RuntimeRunReadModel(store, cas)
    with pytest.raises(KeyError):
        await read_model.get_migration(UUID(int=200))

    run_id = RunId(UUID(int=201))
    body = canonical_json_bytes({"slices": [], "integration_order": []})
    plan_ref = cas.put(body)
    await store.create(
        RunState(run_id=run_id, frozen_plan_sha256=plan_ref.digest), ()
    )
    await store.add_cas_reference(plan_ref, "run", run_id, "frozen-plan")
    cas.path_for(plan_ref.digest).unlink()

    with pytest.raises(RuntimeError):
        await read_model.get_workspace(run_id)


@pytest.mark.asyncio
async def test_workspace_uses_state_digest_as_plan_adoption_boundary(tmp_path):
    store = InMemoryRuntimeStore()
    cas = FileHostCAS(tmp_path / "cas")
    run_id = RunId(UUID(int=250))
    await store.create(RunState(run_id=run_id, status=RunStatus.Planning), ())
    unadopted_plan = cas.put(canonical_json_bytes({"slices": [], "integration_order": []}))
    await store.add_cas_reference(unadopted_plan, "run", run_id, "frozen-plan")

    workspace = await RuntimeRunReadModel(store, cas).get_workspace(run_id)

    assert workspace.slices == []
    assert workspace.integration_queue == []
    assert workspace.latest_sequence == 0


@pytest.mark.asyncio
async def test_existing_read_routes_delegate_to_projection_and_missing_capability_fails_closed(
    tmp_path,
):
    store = InMemoryRuntimeStore()
    cas = FileHostCAS(tmp_path / "cas")
    run_id = UUID(int=301)
    await _create_planned_run(store, cas, run_id)
    projection = RuntimeRunReadModel(store, cas)
    backend = ProductionApiBackend(store, run_read_projection=projection)
    app = create_app(backend, config=ApiConfig(token="read-token"))
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    )
    headers = {"Authorization": "Bearer read-token"}
    async with client:
        listing = await client.get("/api/v1/migrations?limit=1", headers=headers)
        detail = await client.get(f"/api/v1/migrations/{run_id}", headers=headers)
        workspace = await client.get(
            f"/api/v1/migrations/{run_id}/workspace", headers=headers
        )
        missing = await client.get(
            f"/api/v1/migrations/{UUID(int=999)}", headers=headers
        )

    assert listing.status_code == detail.status_code == workspace.status_code == 200
    assert listing.json()["items"][0]["run_id"] == str(run_id)
    assert detail.json()["status"] == RunStatus.Executing.value
    assert workspace.json()["latest_sequence"] == 6
    assert len(workspace.json()["slices"]) == 2
    assert missing.status_code == 404

    unavailable_app = create_app(
        ProductionApiBackend(store), config=ApiConfig(token="read-token")
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=unavailable_app),
        base_url="http://127.0.0.1",
    ) as unavailable_client:
        response = await unavailable_client.get(
            "/api/v1/migrations", headers=headers
        )
    assert response.status_code == 503
    assert response.json()["title"] == "Dependency Unavailable"

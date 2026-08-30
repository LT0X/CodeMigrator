from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from codemigrator.api import ApiConfig, create_app, route_surface

from .conftest import FakeBackend, create_run_payload


def test_m02_route_surface_is_declared() -> None:
    assert len(route_surface()) == 22
    assert ("POST", "/api/v1/specs") in route_surface()
    assert ("GET", "/api/v1/migrations/{run_id}/events") in route_surface()
    assert (
        "POST",
        "/api/v1/sessions/{session_id}/corrections/{correction_id}/confirm",
    ) in route_surface()


def test_declared_route_surface_matches_fastapi_routes() -> None:
    app = create_app(FakeBackend(), config=ApiConfig(token="secret"))
    actual = {
        (method, route.path)
        for route in app.routes
        if hasattr(route, "methods")
        for method in route.methods
        if route.path.startswith("/api/")
    }
    assert actual == set(route_surface())


@pytest.mark.asyncio
async def test_create_run_is_idempotent_for_same_canonical_body(backend) -> None:  # type: ignore[no-untyped-def]
    app = create_app(backend, config=ApiConfig(token="secret"))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1")
    headers = {"Authorization": "Bearer secret", "Idempotency-Key": "run-1"}
    async with client:
        first = await client.post("/api/v1/migrations", json=create_run_payload(), headers=headers)
        second = await client.post("/api/v1/migrations", json=create_run_payload(), headers=headers)
        conflict = await client.post(
            "/api/v1/migrations",
            json={**create_run_payload(), "branch_prefix": "team/other"},
            headers=headers,
        )
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert conflict.status_code == 409
    assert [request.operation for request in backend.requests].count("create_run") == 1


@pytest.mark.asyncio
async def test_cancel_forwards_if_match_as_expected_version(backend) -> None:  # type: ignore[no-untyped-def]
    run_id = uuid4()
    app = create_app(backend, config=ApiConfig(token="secret"))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1")
    async with client:
        response = await client.delete(
            f"/api/v1/migrations/{run_id}",
            headers={"Authorization": "Bearer secret", "If-Match": '"4"'},
        )
    assert response.status_code == 200
    request = backend.requests[-1]
    assert request.operation == "cancel_run"
    assert request.expected_version == 4

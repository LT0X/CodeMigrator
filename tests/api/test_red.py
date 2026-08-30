from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from codemigrator.api import ApiConfig, create_app

from .conftest import create_run_payload, spec_payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "repository_url",
    [
        "http://github.com/example/source.git",
        "file:///etc/passwd",
        "https://user:password@github.com/example/source.git",
        "https://127.0.0.1/source.git",
    ],
)
async def test_create_run_rejects_unsafe_repository_url(backend, repository_url: str) -> None:  # type: ignore[no-untyped-def]
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(backend, config=ApiConfig(token="secret"))),
        base_url="http://127.0.0.1",
    )
    async with client:
        response = await client.post(
            "/api/v1/migrations",
            json={
                **create_run_payload(),
                "source": {
                    **create_run_payload()["source"],
                    "repository_url": repository_url,
                },
            },
            headers={
                "Authorization": "Bearer secret",
                "Idempotency-Key": "unsafe-url",
            },
        )
    assert response.status_code == 422
    assert response.json()["type"].endswith("/invalid_repository_url")
    assert backend.requests == []


@pytest.mark.asyncio
async def test_missing_token_returns_rfc9457_problem(backend) -> None:  # type: ignore[no-untyped-def]
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(backend, config=ApiConfig(token="secret"))),
        base_url="http://127.0.0.1",
    )
    async with client:
        response = await client.get("/api/v1/system/health")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["status"] == 401


@pytest.mark.asyncio
async def test_forbidden_spec_field_is_rejected_before_backend_call(backend) -> None:  # type: ignore[no-untyped-def]
    payload = {**spec_payload(), "program": "python"}
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(backend, config=ApiConfig(token="secret"))),
        base_url="http://127.0.0.1",
    )
    async with client:
        response = await client.post(
            "/api/v1/specs",
            json=payload,
            headers={"Authorization": "Bearer secret", "Idempotency-Key": "spec-1"},
        )
    assert response.status_code == 422
    assert backend.requests == []


@pytest.mark.asyncio
async def test_create_run_requires_idempotency_key(backend) -> None:  # type: ignore[no-untyped-def]
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(backend, config=ApiConfig(token="secret"))),
        base_url="http://127.0.0.1",
    )
    async with client:
        response = await client.post(
            "/api/v1/migrations",
            json=create_run_payload(),
            headers={"Authorization": "Bearer secret"},
        )
    assert response.status_code == 422
    assert backend.requests == []


@pytest.mark.asyncio
async def test_session_message_rejects_undocumented_fields(backend) -> None:  # type: ignore[no-untyped-def]
    session_id = uuid4()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(backend, config=ApiConfig(token="secret"))),
        base_url="http://127.0.0.1",
    )
    async with client:
        response = await client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={"message": "hello", "undocumented": True},
            headers={
                "Authorization": "Bearer secret",
                "Idempotency-Key": "message-1",
            },
        )
    assert response.status_code == 422
    assert backend.requests == []


@pytest.mark.asyncio
async def test_http_body_limit_is_checked_before_route_parsing(backend) -> None:  # type: ignore[no-untyped-def]
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=create_app(backend, config=ApiConfig(token="secret"))
        ),
        base_url="http://127.0.0.1",
    )
    async with client:
        response = await client.post(
            "/api/v1/migrations",
            content=b"{}",
            headers={
                "Authorization": "Bearer secret",
                "Content-Length": str(1_048_577),
            },
        )
    assert response.status_code == 413
    assert response.headers["content-type"].startswith("application/problem+json")
    assert backend.requests == []


@pytest.mark.asyncio
async def test_chunked_http_body_limit_is_enforced_before_parsing(backend) -> None:  # type: ignore[no-untyped-def]
    class OversizedBody(httpx.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[no-untyped-def]
            yield b"{" + b"x" * 1_048_576
            yield b"}"

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(backend, config=ApiConfig(token="secret"))),
        base_url="http://127.0.0.1",
    )
    async with client:
        response = await client.post(
            "/api/v1/migrations",
            content=OversizedBody(),
            headers={"Authorization": "Bearer secret"},
        )
    assert response.status_code == 413
    assert backend.requests == []


@pytest.mark.asyncio
async def test_spec_raw_body_limit_does_not_allow_trailing_whitespace_bypass(backend) -> None:  # type: ignore[no-untyped-def]
    import json

    body = json.dumps(spec_payload(), separators=(",", ":")).encode()
    body += b" " * (262_145 - len(body))
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(backend, config=ApiConfig(token="secret"))),
        base_url="http://127.0.0.1",
    )
    async with client:
        response = await client.post(
            "/api/v1/specs",
            content=body,
            headers={
                "Authorization": "Bearer secret",
                "Content-Type": "application/json",
                "Idempotency-Key": "spec-whitespace",
            },
        )
    assert response.status_code == 422
    assert response.json()["type"].endswith("/spec_too_large")
    assert backend.requests == []


@pytest.mark.asyncio
async def test_unexpected_backend_failure_is_safe_problem_response(backend) -> None:  # type: ignore[no-untyped-def]
    async def fail(request):  # type: ignore[no-untyped-def]
        del request
        raise RuntimeError("private backend detail")

    backend.execute = fail
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=create_app(backend, config=ApiConfig(token="secret")),
            raise_app_exceptions=False,
        ),
        base_url="http://127.0.0.1",
    )
    async with client:
        response = await client.get(
            "/api/v1/system/health",
            headers={"Authorization": "Bearer secret"},
        )
    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["detail"] == "internal server error"
    assert "private backend detail" not in response.text

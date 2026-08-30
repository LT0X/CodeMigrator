"""FastAPI application factory and resource routes."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from secrets import compare_digest
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sse_starlette import EventSourceResponse, ServerSentEvent

from codemigrator.core import CreateRun, MigrationSpec, canonical_json_bytes

from .deps import ApiBackend, ApiConfig, ApiRequest
from .dto import (
    CorrectionConfirmRequest,
    SessionAnswerRequest,
    SessionConfirmRequest,
    SessionCreateRequest,
    SessionMessageRequest,
)
from .idempotency import IdempotencyStore
from .problems import ApiError, problem_response
from .sse import ConnectionLimitError, SseConnectionManager, parse_sequence_header, sse_events

_ROUTE_SURFACE = (
    ("POST", "/api/v1/specs"),
    ("POST", "/api/v1/migrations"),
    ("DELETE", "/api/v1/migrations/{run_id}"),
    ("GET", "/api/v1/migrations"),
    ("GET", "/api/v1/migrations/{run_id}"),
    ("GET", "/api/v1/migrations/{run_id}/workspace"),
    ("GET", "/api/v1/migrations/{run_id}/events"),
    ("GET", "/api/v1/migrations/{run_id}/report"),
    ("GET", "/api/v1/migrations/{run_id}/evidence/{receipt_id}"),
    ("GET", "/api/v1/descriptors"),
    ("GET", "/api/v1/system/health"),
    ("POST", "/api/v1/projects/register"),
    ("GET", "/api/v1/projects"),
    ("POST", "/api/v1/sessions"),
    ("POST", "/api/v1/sessions/{session_id}/messages"),
    ("POST", "/api/v1/sessions/{session_id}/answers"),
    ("POST", "/api/v1/sessions/{session_id}/confirm"),
    ("POST", "/api/v1/sessions/{session_id}/corrections/{correction_id}/confirm"),
    ("GET", "/api/v1/sessions/{session_id}/events"),
    ("GET", "/api/v1/migrations/{run_id}/changes"),
    ("GET", "/api/v1/migrations/{run_id}/output"),
    ("GET", "/api/v1/skills"),
)


def route_surface() -> tuple[tuple[str, str], ...]:
    return _ROUTE_SURFACE


def create_app(
    backend: ApiBackend,
    *,
    config: ApiConfig,
    idempotency: IdempotencyStore | None = None,
    connections: SseConnectionManager | None = None,
) -> FastAPI:
    """Build an API app from runtime-owned ports and deployment configuration."""

    store = idempotency or IdempotencyStore()
    connection_manager = connections or SseConnectionManager(
        limit=config.max_sse_connections, queue_size=config.sse_queue_size
    )
    app = FastAPI(title="CodeMigrator API", version="1")
    app.state.backend = backend
    app.state.config = config

    @app.middleware("http")
    async def enforce_body_limit(request: Request, call_next):  # type: ignore[no-untyped-def]
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                size = int(content_length)
            except ValueError:
                size = config.max_body_bytes + 1
            if size > config.max_body_bytes:
                return problem_response(
                    ApiError(413, "request body is too large", "BODY_TOO_LARGE"),
                    _request_id(request),
                )
        return await call_next(request)

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, error: ApiError) -> JSONResponse:
        return problem_response(error, _request_id(request))

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        del error
        return problem_response(
            ApiError(422, "request validation failed", "INVALID_REQUEST"), _request_id(request)
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, error: Exception) -> JSONResponse:
        del error
        return problem_response(
            ApiError(500, "internal server error", "INTERNAL_ERROR"), _request_id(request)
        )

    async def authenticate(request: Request) -> str:
        authorization = request.headers.get("authorization")
        expected = f"Bearer {config.token}"
        if authorization is None or not compare_digest(authorization, expected):
            raise ApiError(401, "authentication required", "UNAUTHENTICATED", retryable=False)
        return config.principal_id

    @app.post("/api/v1/specs", status_code=201)
    async def create_spec(
        payload: MigrationSpec, request: Request, principal_id: str = Depends(authenticate)
    ) -> object:
        body = _canonical_body(payload)
        if len(body) > config.max_spec_bytes:
            raise ApiError(422, "spec exceeds the 256 KiB limit", "SPEC_TOO_LARGE")
        return await _write(
            backend,
            store,
            request,
            principal_id,
            operation="create_spec",
            payload=payload,
            body=body,
            status_code=201,
        )

    @app.post("/api/v1/migrations", status_code=201)
    async def create_migration(
        payload: CreateRun, request: Request, principal_id: str = Depends(authenticate)
    ) -> object:
        return await _write(
            backend,
            store,
            request,
            principal_id,
            operation="create_run",
            payload=payload,
            body=_canonical_body(payload),
            status_code=201,
        )

    @app.delete("/api/v1/migrations/{run_id}")
    async def cancel_migration(
        run_id: UUID, request: Request, principal_id: str = Depends(authenticate)
    ) -> object:
        expected_version = _parse_if_match(request.headers.get("if-match"))
        return await _write(
            backend,
            store,
            request,
            principal_id,
            operation="cancel_run",
            payload=None,
            body=canonical_json_bytes(
                {"run_id": str(run_id), "expected_version": expected_version}
            ),
            status_code=200,
            resource_id=run_id,
            expected_version=expected_version,
            require_key=False,
        )

    @app.get("/api/v1/migrations")
    async def list_migrations(
        request: Request,
        principal_id: str = Depends(authenticate),
        limit: int = 100,
        cursor: str | None = None,
    ) -> object:
        if not 1 <= limit <= 100:
            raise ApiError(422, "limit must be between 1 and 100", "INVALID_REQUEST")
        return await _read(
            backend,
            "list_migrations",
            principal_id,
            query={"limit": str(limit), "cursor": cursor or ""},
        )

    @app.get("/api/v1/migrations/{run_id}")
    async def get_migration(run_id: UUID, principal_id: str = Depends(authenticate)) -> object:
        return await _read(backend, "get_migration", principal_id, resource_id=run_id)

    @app.get("/api/v1/migrations/{run_id}/workspace")
    async def get_workspace(run_id: UUID, principal_id: str = Depends(authenticate)) -> object:
        return await _read(backend, "get_workspace", principal_id, resource_id=run_id)

    @app.get("/api/v1/migrations/{run_id}/events")
    async def migration_events(
        run_id: UUID, request: Request, principal_id: str = Depends(authenticate)
    ) -> EventSourceResponse:
        del principal_id
        try:
            after_sequence = parse_sequence_header(
                request.headers.get("last-event-id"), name="Last-Event-ID"
            )
            lease = connection_manager.acquire()
        except ValueError as exc:
            raise ApiError(422, str(exc), "INVALID_EVENT_CURSOR") from exc
        except ConnectionLimitError as exc:
            raise ApiError(429, str(exc), "SSE_CONNECTION_LIMIT", retryable=True) from exc

        async def stream() -> AsyncIterator[ServerSentEvent]:
            try:
                async for item in sse_events(
                    backend,
                    run_id,
                    after_sequence=after_sequence,
                    heartbeat_seconds=config.heartbeat_seconds,
                    queue_size=connection_manager.queue_size,
                ):
                    yield item
            finally:
                lease.close()

        return EventSourceResponse(stream(), ping=None)

    @app.get("/api/v1/migrations/{run_id}/report")
    async def get_report(run_id: UUID, principal_id: str = Depends(authenticate)) -> object:
        return await _read(backend, "get_report", principal_id, resource_id=run_id)

    @app.get("/api/v1/migrations/{run_id}/evidence/{receipt_id}")
    async def get_evidence(
        run_id: UUID, receipt_id: UUID, principal_id: str = Depends(authenticate)
    ) -> object:
        return await _read(
            backend,
            "get_evidence",
            principal_id,
            resource_id=run_id,
            query={"receipt_id": str(receipt_id)},
        )

    @app.get("/api/v1/descriptors")
    async def get_descriptors(principal_id: str = Depends(authenticate)) -> object:
        return await _read(backend, "list_descriptors", principal_id)

    @app.get("/api/v1/system/health")
    async def get_health(principal_id: str = Depends(authenticate)) -> object:
        return await _read(backend, "health", principal_id)

    @app.post("/api/v1/projects/register", status_code=201)
    async def register_project(
        payload: dict[str, object],
        request: Request,
        principal_id: str = Depends(authenticate),
    ) -> object:
        return await _write(
            backend,
            store,
            request,
            principal_id,
            operation="register_project",
            payload=payload,
            body=_canonical_body(payload),
            status_code=201,
        )

    @app.get("/api/v1/projects")
    async def list_projects(principal_id: str = Depends(authenticate)) -> object:
        return await _read(backend, "list_projects", principal_id)

    @app.post("/api/v1/sessions", status_code=201)
    async def create_session(
        payload: SessionCreateRequest,
        request: Request,
        principal_id: str = Depends(authenticate),
    ) -> object:
        return await _write(
            backend,
            store,
            request,
            principal_id,
            operation="create_session",
            payload=payload,
            body=_canonical_body(payload),
            status_code=201,
        )

    @app.post("/api/v1/sessions/{session_id}/messages")
    async def send_message(
        session_id: UUID,
        payload: SessionMessageRequest,
        request: Request,
        principal_id: str = Depends(authenticate),
    ) -> object:
        return await _write(
            backend,
            store,
            request,
            principal_id,
            operation="session_message",
            payload=payload,
            body=_canonical_body(payload),
            status_code=200,
            resource_id=session_id,
        )

    @app.post("/api/v1/sessions/{session_id}/answers")
    async def answer_question(
        session_id: UUID,
        payload: SessionAnswerRequest,
        request: Request,
        principal_id: str = Depends(authenticate),
    ) -> object:
        return await _write(
            backend,
            store,
            request,
            principal_id,
            operation="session_answer",
            payload=payload,
            body=_canonical_body(payload),
            status_code=200,
            resource_id=session_id,
        )

    @app.post("/api/v1/sessions/{session_id}/confirm")
    async def confirm_session(
        session_id: UUID,
        payload: SessionConfirmRequest,
        request: Request,
        principal_id: str = Depends(authenticate),
    ) -> object:
        return await _write(
            backend,
            store,
            request,
            principal_id,
            operation="session_confirm",
            payload=payload,
            body=_canonical_body(payload),
            status_code=200,
            resource_id=session_id,
        )

    @app.post("/api/v1/sessions/{session_id}/corrections/{correction_id}/confirm")
    async def confirm_correction(
        session_id: UUID,
        correction_id: UUID,
        payload: CorrectionConfirmRequest,
        request: Request,
        principal_id: str = Depends(authenticate),
    ) -> object:
        return await _write(
            backend,
            store,
            request,
            principal_id,
            operation="correction_confirm",
            payload=payload,
            body=_canonical_body(payload),
            status_code=200,
            resource_id=session_id,
            query={"correction_id": str(correction_id)},
        )

    @app.get("/api/v1/sessions/{session_id}/events")
    async def session_events(
        session_id: UUID, request: Request, principal_id: str = Depends(authenticate)
    ) -> EventSourceResponse:
        del principal_id
        try:
            after_sequence = parse_sequence_header(
                request.headers.get("last-event-id"), name="Last-Event-ID"
            )
            lease = connection_manager.acquire()
        except ValueError as exc:
            raise ApiError(422, str(exc), "INVALID_EVENT_CURSOR") from exc
        except ConnectionLimitError as exc:
            raise ApiError(429, str(exc), "SSE_CONNECTION_LIMIT", retryable=True) from exc

        async def stream() -> AsyncIterator[ServerSentEvent]:
            try:
                async for item in sse_events(
                    backend,
                    session_id,
                    after_sequence=after_sequence,
                    heartbeat_seconds=config.heartbeat_seconds,
                    queue_size=connection_manager.queue_size,
                ):
                    yield item
            finally:
                lease.close()

        return EventSourceResponse(stream(), ping=None)

    @app.get("/api/v1/migrations/{run_id}/changes")
    async def get_changes(run_id: UUID, principal_id: str = Depends(authenticate)) -> object:
        return await _read(backend, "get_changes", principal_id, resource_id=run_id)

    @app.get("/api/v1/migrations/{run_id}/output")
    async def get_output(run_id: UUID, principal_id: str = Depends(authenticate)) -> object:
        return await _read(backend, "get_output", principal_id, resource_id=run_id)

    @app.get("/api/v1/skills")
    async def get_skills(principal_id: str = Depends(authenticate)) -> object:
        return await _read(backend, "list_skills", principal_id)

    return app


async def _read(
    backend: ApiBackend,
    operation: str,
    principal_id: str,
    *,
    resource_id: UUID | None = None,
    query: Mapping[str, str] | None = None,
) -> object:
    try:
        return await backend.execute(
            ApiRequest(
                operation=operation,
                principal_id=principal_id,
                resource_id=resource_id,
                query=query or {},
            )
        )
    except ApiError:
        raise
    except KeyError as exc:
        raise ApiError(404, "resource not found", "NOT_FOUND") from exc


async def _write(
    backend: ApiBackend,
    store: IdempotencyStore,
    request: Request,
    principal_id: str,
    *,
    operation: str,
    payload: object | None,
    body: bytes,
    status_code: int,
    resource_id: UUID | None = None,
    expected_version: int | None = None,
    query: Mapping[str, str] | None = None,
    require_key: bool = True,
) -> JSONResponse:
    route = request.url.path
    key = request.headers.get("idempotency-key")
    if require_key and (key is None or not key.strip()):
        raise ApiError(422, "Idempotency-Key is required", "IDEMPOTENCY_KEY_REQUIRED")
    if key is not None:
        cached = store.lookup(principal_id, route, key, body)
        if cached is not None:
            if cached.conflict:
                raise ApiError(
                    409,
                    "idempotency key was reused with a different body",
                    "IDEMPOTENCY_CONFLICT",
                )
            return JSONResponse(status_code=cached.status_code, content=cached.body)
    try:
        result = await backend.execute(
            ApiRequest(
                operation=operation,
                principal_id=principal_id,
                resource_id=resource_id,
                payload=payload,
                query=query or {},
                expected_version=expected_version,
            )
        )
    except ApiError:
        raise
    except KeyError as exc:
        raise ApiError(404, "resource not found", "NOT_FOUND") from exc
    serialized = _jsonable(result)
    if key is not None:
        store.remember(principal_id, route, key, body, status_code, serialized)
    return JSONResponse(status_code=status_code, content=serialized)


def _canonical_body(value: object) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", by_alias=True)
    return canonical_json_bytes(value)


def _jsonable(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(nested) for key, nested in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(nested) for nested in value]
    return value


def _parse_if_match(value: str | None) -> int:
    if value is None:
        raise ApiError(422, "If-Match is required", "IF_MATCH_REQUIRED")
    if len(value) < 3 or not (value.startswith('"') and value.endswith('"')):
        raise ApiError(422, "If-Match must contain a quoted version", "INVALID_IF_MATCH")
    raw = value[1:-1]
    if not raw.isascii() or not raw.isdecimal():
        raise ApiError(422, "If-Match must contain a non-negative version", "INVALID_IF_MATCH")
    return int(raw)


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or str(uuid4())


__all__ = ["create_app", "route_surface"]

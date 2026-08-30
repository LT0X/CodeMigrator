"""FastAPI application factory and resource routes."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from ipaddress import ip_address
from secrets import compare_digest
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError
from sse_starlette import EventSourceResponse, ServerSentEvent

from codemigrator.core import (
    CreateRun,
    MigrationSpec,
    RemoteRepository,
    SecretRegistry,
    canonical_json_bytes,
)

from .deps import ApiBackend, ApiConfig, ApiRequest
from .dto import (
    ChangesView,
    CorrectionConfirmRequest,
    DescriptorListView,
    EvidenceView,
    HealthView,
    MigrationListView,
    MigrationView,
    OutputView,
    ProjectListView,
    ProjectView,
    ReportView,
    SessionAnswerRequest,
    SessionConfirmRequest,
    SessionCreateRequest,
    SessionEvent,
    SessionMessageRequest,
    SessionView,
    SkillListView,
    SpecView,
    WorkspaceView,
)
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

_PROJECTION_MODELS: dict[str, type[BaseModel]] = {
    "create_spec": SpecView,
    "create_run": MigrationView,
    "cancel_run": MigrationView,
    "list_migrations": MigrationListView,
    "get_migration": MigrationView,
    "get_workspace": WorkspaceView,
    "get_report": ReportView,
    "get_evidence": EvidenceView,
    "list_descriptors": DescriptorListView,
    "health": HealthView,
    "register_project": ProjectView,
    "list_projects": ProjectListView,
    "create_session": SessionView,
    "session_message": SessionView,
    "session_answer": SessionView,
    "session_confirm": SessionView,
    "correction_confirm": SessionView,
    "get_changes": ChangesView,
    "get_output": OutputView,
    "list_skills": SkillListView,
}


def route_surface() -> tuple[tuple[str, str], ...]:
    return _ROUTE_SURFACE


def create_app(
    backend: ApiBackend,
    *,
    config: ApiConfig,
    connections: SseConnectionManager | None = None,
    secret_registry: SecretRegistry | None = None,
) -> FastAPI:
    """Build an API app from runtime-owned ports and deployment configuration."""

    connection_manager = connections or SseConnectionManager(
        limit=config.max_sse_connections, queue_size=config.sse_queue_size
    )
    redaction_registry = secret_registry or SecretRegistry()
    # Authentication material is itself untrusted projection input. Register it
    # even when callers provide a shared registry, so accidental backend echoing
    # cannot turn a valid bearer token into public response data.
    redaction_registry.register(config.token)
    app = FastAPI(title="CodeMigrator API", version="1")
    app.state.backend = backend
    app.state.config = config
    app.state.secret_registry = redaction_registry

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

        receive = request._receive
        messages = []
        raw_size = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] != "http.request":
                break
            raw_size += len(message.get("body", b""))
            if raw_size > config.max_body_bytes:
                return problem_response(
                    ApiError(413, "request body is too large", "BODY_TOO_LARGE"),
                    _request_id(request),
                )
            if not message.get("more_body", False):
                break

        async def replay_receive():  # type: ignore[no-untyped-def]
            if messages:
                return messages.pop(0)
            return {"type": "http.disconnect"}

        request._receive = replay_receive
        request.state.raw_body_size = raw_size
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
        raw_size = getattr(request.state, "raw_body_size", len(body))
        if raw_size > config.max_spec_bytes:
            raise ApiError(422, "spec exceeds the 256 KiB limit", "SPEC_TOO_LARGE")
        return await _write(
            backend,
            request,
            principal_id,
            operation="create_spec",
            payload=payload,
            body=body,
            status_code=201,
            secret_registry=redaction_registry,
        )

    @app.post("/api/v1/migrations", status_code=201)
    async def create_migration(
        payload: CreateRun, request: Request, principal_id: str = Depends(authenticate)
    ) -> object:
        _validate_create_run_source(payload)
        return await _write(
            backend,
            request,
            principal_id,
            operation="create_run",
            payload=payload,
            body=_canonical_body(payload),
            status_code=201,
            secret_registry=redaction_registry,
        )

    @app.delete("/api/v1/migrations/{run_id}")
    async def cancel_migration(
        run_id: UUID, request: Request, principal_id: str = Depends(authenticate)
    ) -> object:
        expected_version = _parse_if_match(request.headers.get("if-match"))
        return await _write(
            backend,
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
            secret_registry=redaction_registry,
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
            secret_registry=redaction_registry,
        )

    @app.get("/api/v1/migrations/{run_id}")
    async def get_migration(run_id: UUID, principal_id: str = Depends(authenticate)) -> object:
        return await _read(
            backend,
            "get_migration",
            principal_id,
            resource_id=run_id,
            secret_registry=redaction_registry,
        )

    @app.get("/api/v1/migrations/{run_id}/workspace")
    async def get_workspace(run_id: UUID, principal_id: str = Depends(authenticate)) -> object:
        return await _read(
            backend,
            "get_workspace",
            principal_id,
            resource_id=run_id,
            secret_registry=redaction_registry,
        )

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
                    secret_registry=redaction_registry,
                ):
                    yield item
            finally:
                lease.close()

        return EventSourceResponse(stream(), ping=None)

    @app.get("/api/v1/migrations/{run_id}/report")
    async def get_report(run_id: UUID, principal_id: str = Depends(authenticate)) -> object:
        return await _read(
            backend,
            "get_report",
            principal_id,
            resource_id=run_id,
            secret_registry=redaction_registry,
        )

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
            secret_registry=redaction_registry,
        )

    @app.get("/api/v1/descriptors")
    async def get_descriptors(principal_id: str = Depends(authenticate)) -> object:
        return await _read(
            backend,
            "list_descriptors",
            principal_id,
            secret_registry=redaction_registry,
        )

    @app.get("/api/v1/system/health")
    async def get_health(principal_id: str = Depends(authenticate)) -> object:
        return await _read(
            backend,
            "health",
            principal_id,
            secret_registry=redaction_registry,
        )

    @app.post("/api/v1/projects/register", status_code=201)
    async def register_project(
        payload: dict[str, object],
        request: Request,
        principal_id: str = Depends(authenticate),
    ) -> object:
        return await _write(
            backend,
            request,
            principal_id,
            operation="register_project",
            payload=payload,
            body=_canonical_body(payload),
            status_code=201,
            secret_registry=redaction_registry,
        )

    @app.get("/api/v1/projects")
    async def list_projects(principal_id: str = Depends(authenticate)) -> object:
        return await _read(
            backend,
            "list_projects",
            principal_id,
            secret_registry=redaction_registry,
        )

    @app.post("/api/v1/sessions", status_code=201)
    async def create_session(
        payload: SessionCreateRequest,
        request: Request,
        principal_id: str = Depends(authenticate),
    ) -> object:
        return await _write(
            backend,
            request,
            principal_id,
            operation="create_session",
            payload=payload,
            body=_canonical_body(payload),
            status_code=201,
            secret_registry=redaction_registry,
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
            request,
            principal_id,
            operation="session_message",
            payload=payload,
            body=_canonical_body(payload),
            status_code=200,
            resource_id=session_id,
            secret_registry=redaction_registry,
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
            request,
            principal_id,
            operation="session_answer",
            payload=payload,
            body=_canonical_body(payload),
            status_code=200,
            resource_id=session_id,
            secret_registry=redaction_registry,
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
            request,
            principal_id,
            operation="session_confirm",
            payload=payload,
            body=_canonical_body(payload),
            status_code=200,
            resource_id=session_id,
            secret_registry=redaction_registry,
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
            request,
            principal_id,
            operation="correction_confirm",
            payload=payload,
            body=_canonical_body(payload),
            status_code=200,
            resource_id=session_id,
            query={"correction_id": str(correction_id)},
            secret_registry=redaction_registry,
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
                    event_name="migration.session.event",
                    envelope_type=SessionEvent,
                    secret_registry=redaction_registry,
                ):
                    yield item
            finally:
                lease.close()

        return EventSourceResponse(stream(), ping=None)

    @app.get("/api/v1/migrations/{run_id}/changes")
    async def get_changes(run_id: UUID, principal_id: str = Depends(authenticate)) -> object:
        return await _read(
            backend,
            "get_changes",
            principal_id,
            resource_id=run_id,
            secret_registry=redaction_registry,
        )

    @app.get("/api/v1/migrations/{run_id}/output")
    async def get_output(run_id: UUID, principal_id: str = Depends(authenticate)) -> object:
        return await _read(
            backend,
            "get_output",
            principal_id,
            resource_id=run_id,
            secret_registry=redaction_registry,
        )

    @app.get("/api/v1/skills")
    async def get_skills(principal_id: str = Depends(authenticate)) -> object:
        return await _read(
            backend,
            "list_skills",
            principal_id,
            secret_registry=redaction_registry,
        )

    return app


async def _read(
    backend: ApiBackend,
    operation: str,
    principal_id: str,
    *,
    resource_id: UUID | None = None,
    query: Mapping[str, str] | None = None,
    secret_registry: SecretRegistry,
) -> object:
    try:
        result = await backend.execute(
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
    return _project(operation, result, secret_registry=secret_registry)


async def _write(
    backend: ApiBackend,
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
    secret_registry: SecretRegistry,
) -> JSONResponse:
    route = request.url.path
    key = request.headers.get("idempotency-key")
    if require_key and (key is None or not key.strip()):
        raise ApiError(422, "Idempotency-Key is required", "IDEMPOTENCY_KEY_REQUIRED")
    try:
        api_request = ApiRequest(
            operation=operation,
            principal_id=principal_id,
            resource_id=resource_id,
            payload=payload,
            query=query or {},
            expected_version=expected_version,
        )
        if key is not None:
            result = await backend.execute_idempotent(
                api_request,
                route=route,
                key=key,
                canonical_body=body,
                status_code=status_code,
            )
        else:
            result = await backend.execute(api_request)
    except ApiError:
        raise
    except KeyError as exc:
        raise ApiError(404, "resource not found", "NOT_FOUND") from exc
    serialized = _project(operation, result, secret_registry=secret_registry)
    return JSONResponse(status_code=status_code, content=serialized)


def _canonical_body(value: object) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", by_alias=True)
    return canonical_json_bytes(value)


def _project(
    operation: str,
    value: object,
    *,
    secret_registry: SecretRegistry,
) -> object:
    model = _PROJECTION_MODELS.get(operation)
    if model is None:
        raise ApiError(500, "backend returned an unsupported projection", "INVALID_PROJECTION")
    if operation in {
        "list_descriptors",
        "list_migrations",
        "list_projects",
        "list_skills",
    } and isinstance(value, list):
        value = {"items": value}
    try:
        projection = model.model_validate(value)
    except ValidationError as exc:
        raise ApiError(500, "backend returned an invalid projection", "INVALID_PROJECTION") from exc
    serialized = projection.model_dump(mode="json", by_alias=True)
    if not secret_registry.redact(serialized).accepted:
        raise ApiError(500, "backend returned an unsafe projection", "INVALID_PROJECTION")
    return serialized


def _parse_if_match(value: str | None) -> int:
    if value is None:
        raise ApiError(422, "If-Match is required", "IF_MATCH_REQUIRED")
    if len(value) < 3 or not (value.startswith('"') and value.endswith('"')):
        raise ApiError(422, "If-Match must contain a quoted version", "INVALID_IF_MATCH")
    raw = value[1:-1]
    if not raw.isascii() or not raw.isdecimal():
        raise ApiError(422, "If-Match must contain a non-negative version", "INVALID_IF_MATCH")
    return int(raw)


def _validate_create_run_source(payload: CreateRun) -> None:
    source = payload.source
    if not isinstance(source, RemoteRepository):
        return
    try:
        parsed = urlsplit(str(source.repository_url))
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ApiError(
            422,
            "repository_url is not a valid HTTPS URL",
            "INVALID_REPOSITORY_URL",
        ) from exc
    if (
        parsed.scheme != "https"
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or port is None and ":" in parsed.netloc.rsplit("@", 1)[-1]
    ):
        raise ApiError(
            422,
            "repository_url must be an HTTPS URL without userinfo",
            "INVALID_REPOSITORY_URL",
        )
    try:
        ip_address(hostname)
    except ValueError:
        return
    raise ApiError(422, "repository_url must not use an IP literal", "INVALID_REPOSITORY_URL")


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or str(uuid4())


__all__ = ["create_app", "route_surface"]

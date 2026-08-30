from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from codemigrator.api.deps import ApiRequest, EventRecord
from codemigrator.api.idempotency import IdempotencyStore
from codemigrator.api.problems import ApiError


class FakeBackend:
    def __init__(self) -> None:
        self.requests: list[ApiRequest] = []
        self.events: list[EventRecord] = []
        self.idempotency = IdempotencyStore()
        self.idempotency_lock = asyncio.Lock()

    async def execute(self, request: ApiRequest) -> object:
        self.requests.append(request)
        if request.operation == "create_spec":
            payload = request.payload
            return {
                "spec_id": str(uuid4()),
                "canonical_sha256": "f" * 64,
                "source_language_id": payload.source_language_id,
                "target_language_id": payload.target_language_id,
                "descriptor_lock": payload.descriptor_lock,
                "required_checks": payload.required_checks,
            }
        if request.operation in {"create_run", "cancel_run"}:
            return {
                "run_id": str(request.resource_id or uuid4()),
                "status": "PLANNING",
                "version": 1,
            }
        if request.operation in {
            "list_migrations",
            "list_descriptors",
            "list_projects",
            "list_skills",
        }:
            return {"items": []}
        if request.operation == "get_workspace":
            return {"run_id": str(request.resource_id), "slices": []}
        if request.operation == "get_report":
            return {"run_id": str(request.resource_id), "status": "READY"}
        if request.operation == "get_evidence":
            return {
                "run_id": str(request.resource_id),
                "receipt_id": request.query["receipt_id"],
                "status": "READY",
            }
        if request.operation == "health":
            return {"app": "READY", "postgres": "READY", "sandbox": "READY"}
        if request.operation == "register_project":
            return {"project_id": str(uuid4())}
        if request.operation == "create_session":
            return {"session_id": str(uuid4()), "status": "DRAFTING"}
        if request.operation in {
            "session_message",
            "session_answer",
            "session_confirm",
            "correction_confirm",
        }:
            return {"session_id": str(request.resource_id), "status": "DRAFTING"}
        if request.operation == "get_changes":
            return {"run_id": str(request.resource_id), "changes": []}
        if request.operation == "get_output":
            return {"run_id": str(request.resource_id), "status": "READY", "files": []}
        return {"operation": request.operation}

    async def execute_idempotent(
        self,
        request: ApiRequest,
        *,
        route: str,
        key: str,
        canonical_body: bytes,
        status_code: int,
    ) -> object:
        async with self.idempotency_lock:
            cached = self.idempotency.lookup(
                request.principal_id, route, key, canonical_body
            )
            if cached is not None:
                if cached.conflict:
                    raise ApiError(
                        409,
                        "idempotency key was reused with a different body",
                        "IDEMPOTENCY_CONFLICT",
                    )
                return cached.body
            result = await self.execute(request)
            self.idempotency.remember(
                request.principal_id,
                route,
                key,
                canonical_body,
                status_code,
                result,
            )
            return result

    async def read_events(self, run_id: UUID, after_sequence: int) -> tuple[EventRecord, ...]:
        return tuple(
            event
            for event in self.events
            if event.run_id == run_id and event.sequence > after_sequence
        )

    async def wait_for_events(self, run_id: UUID, after_sequence: int) -> None:
        del run_id, after_sequence
        await asyncio.sleep(60)

    async def is_stream_terminal(self, run_id: UUID, after_sequence: int) -> bool:
        terminal = {"COMPLETED", "PARTIALLY_COMPLETED", "FAILED", "CANCELLED", "CLOSED"}
        return any(
            item.run_id == run_id
            and item.sequence <= after_sequence
            and item.data.get("run_status", item.data.get("status")) in terminal
            for item in self.events
        )


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


def artifact() -> dict[str, object]:
    return {"sha256": "a" * 64, "size": 1, "media_type": "application/json"}


def spec_payload() -> dict[str, object]:
    return {
        "schema": "codemigrator.spec",
        "version": 3,
        "name": "typescript-to-python",
        "source_language_id": "typescript",
        "target_language_id": "python",
        "descriptor_lock": {
            "descriptor_version": "1.0.0",
            "source_descriptor_sha256": "a" * 64,
            "target_descriptor_sha256": "b" * 64,
            "toolchain_image_digest": "c" * 64,
        },
        "scope": {"include": ["src/"]},
        "required_checks": [
            {"action": "COMPILE", "template_sha256": "d" * 64},
            {"action": "TEST", "template_sha256": "e" * 64},
        ],
    }


def create_run_payload() -> dict[str, object]:
    return {
        "source": {
            "repository_url": "https://github.com/example/source.git",
            "base_ref": "main",
        },
        "branch_prefix": "team/port-py",
        "frozen_artifacts": {
            "spec": artifact(),
            "understanding_dossier": artifact(),
            "target_project_blueprint": artifact(),
            "migration_rulebook": artifact(),
        },
    }


def event(run_id: UUID, sequence: int, event_type: str = "run.status_changed") -> EventRecord:
    return EventRecord(
        run_id=run_id,
        sequence=sequence,
        event_type=event_type,
        data={"status": "PLANNING"},
        timestamp_utc=datetime.now(UTC),
    )

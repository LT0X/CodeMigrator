from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from codemigrator.api.deps import ApiRequest, EventRecord


class FakeBackend:
    def __init__(self) -> None:
        self.requests: list[ApiRequest] = []
        self.events: list[EventRecord] = []

    async def execute(self, request: ApiRequest) -> object:
        self.requests.append(request)
        if request.operation == "create_spec":
            return {"spec_id": str(uuid4()), "accepted": True}
        if request.operation in {"create_run", "cancel_run"}:
            return {"run_id": str(request.resource_id or uuid4()), "status": "PLANNING"}
        return {"operation": request.operation}

    async def read_events(self, run_id: UUID, after_sequence: int) -> tuple[EventRecord, ...]:
        return tuple(
            event
            for event in self.events
            if event.run_id == run_id and event.sequence > after_sequence
        )

    async def wait_for_events(self, run_id: UUID, after_sequence: int) -> None:
        del run_id, after_sequence
        await asyncio.sleep(60)


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

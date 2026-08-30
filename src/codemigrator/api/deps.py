"""Dependency-injection ports for the API and event ledger."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ApiConfig:
    token: str
    principal_id: str = "local"
    max_body_bytes: int = 1_048_576
    max_spec_bytes: int = 262_144
    max_sse_connections: int = 100
    sse_queue_size: int = 64
    heartbeat_seconds: float = 15.0

    def __post_init__(self) -> None:
        if not self.token:
            raise ValueError("API token must not be empty")
        if not self.principal_id:
            raise ValueError("principal_id must not be empty")
        if type(self.max_body_bytes) is not int or self.max_body_bytes < 1:
            raise ValueError("max_body_bytes must be positive")
        if type(self.max_spec_bytes) is not int or self.max_spec_bytes < 1:
            raise ValueError("max_spec_bytes must be positive")
        if type(self.max_sse_connections) is not int or self.max_sse_connections < 1:
            raise ValueError("max_sse_connections must be positive")
        if type(self.sse_queue_size) is not int or self.sse_queue_size < 1:
            raise ValueError("sse_queue_size must be positive")
        if self.heartbeat_seconds <= 0:
            raise ValueError("heartbeat_seconds must be positive")


@dataclass(frozen=True, slots=True)
class ApiRequest:
    operation: str
    principal_id: str
    resource_id: UUID | None = None
    payload: object | None = None
    query: Mapping[str, str] = field(default_factory=dict)
    expected_version: int | None = None


@dataclass(frozen=True, slots=True)
class EventRecord:
    run_id: UUID
    sequence: int
    event_type: str
    data: dict[str, object]
    timestamp_utc: datetime

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise ValueError("event sequence must be positive")
        if self.timestamp_utc.tzinfo is None or self.timestamp_utc.utcoffset() is None:
            raise ValueError("event timestamp must be timezone-aware")
        object.__setattr__(self, "timestamp_utc", self.timestamp_utc.astimezone(UTC))


class ApiBackend(Protocol):
    """Runtime-owned command/query boundary; API never imports its implementation."""

    async def execute(self, request: ApiRequest) -> object:
        """Execute a command or read projection and return committed facts."""

    async def read_events(self, run_id: UUID, after_sequence: int) -> Sequence[EventRecord]:
        """Read committed events strictly after a sequence cursor."""

    async def wait_for_events(self, run_id: UUID, after_sequence: int) -> None:
        """Wait for a NOTIFY wake-up; the ledger remains the source of event data."""


__all__ = ["ApiBackend", "ApiConfig", "ApiRequest", "EventRecord"]

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class RunEvent:
    sequence: int
    type: str
    data: dict[str, Any]
    timestamp_utc: str


@dataclass(frozen=True, slots=True)
class SliceProjection:
    slice_id: str
    status: str
    generation: int
    action: str
    zone: str
    integration_rank: int | None = None


@dataclass(frozen=True, slots=True)
class AgentRunProjection:
    agent_run_id: str
    phase: str
    session_kind: str
    state: str
    slice_id: str | None = None
    generation: int | None = None
    exit: str | None = None
    receipt_category: str | None = None
    last_sequence: int = 0


@dataclass(slots=True)
class Projection:
    cursor: int = 0
    connection: str = "disconnected"
    run_status: str = "UNKNOWN"
    slices: dict[str, SliceProjection] = field(default_factory=dict)
    agent_runs: dict[str, AgentRunProjection] = field(default_factory=dict)
    timeline: list[dict[str, Any]] = field(default_factory=list)
    notices: list[dict[str, Any]] = field(default_factory=list)
    completed_integrations: set[str] = field(default_factory=set)
    advanced_verifications: set[str] = field(default_factory=set)
    celebrations: set[str] = field(default_factory=set)
    active_slices: list[SliceProjection] = field(default_factory=list)
    overflow_active: int = 0

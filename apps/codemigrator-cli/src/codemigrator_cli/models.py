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


@dataclass(slots=True)
class Projection:
    cursor: int = 0
    connection: str = "disconnected"
    run_status: str = "UNKNOWN"
    slices: dict[str, SliceProjection] = field(default_factory=dict)
    timeline: list[dict[str, Any]] = field(default_factory=list)
    notices: list[dict[str, Any]] = field(default_factory=list)
    active_slices: list[SliceProjection] = field(default_factory=list)
    overflow_active: int = 0

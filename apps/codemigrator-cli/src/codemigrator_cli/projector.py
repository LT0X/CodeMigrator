from __future__ import annotations

from collections.abc import Iterable
from typing import Any, cast

from .models import Projection, RunEvent, SliceProjection
from .sequence import SequenceCursor

_SENSITIVE_KEYS = frozenset(
    {
        "prompt",
        "source",
        "source_body",
        "body",
        "log",
        "logs",
        "stdout",
        "stderr",
        "token",
        "tokens",
        "credential",
        "credentials",
        "password",
        "artifact_ref",
        "artifact_refs",
        "host_path",
        "path",
    }
)

_PLACEMENT: dict[str, tuple[str, str]] = {
    "RUNNING": ("work", "run"),
    "LOCAL_VERIFYING": ("work", "run"),
    "LOCALLY_VERIFIED": ("waiting", "wait"),
    "INTEGRATION_QUEUED": ("waiting", "wait"),
    "INTEGRATING": ("waiting", "wait"),
    "REGENERATING": ("regeneration", "error"),
    "INTEGRATED": ("confluence", "verified"),
    "TERMINAL_FAILED": ("regeneration", "error"),
}


def _safe_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _safe_value(item)
            for key, item in value.items()
            if str(key).lower() not in _SENSITIVE_KEYS
        }
    if isinstance(value, list):
        return [_safe_value(item) for item in value[:20]]
    if isinstance(value, str):
        return value[:240]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:240]


def safe_data(data: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], _safe_value(data))


def _slice_id(data: dict[str, Any]) -> str | None:
    value = data.get("slice_id", data.get("sliceId"))
    return value.strip() if isinstance(value, str) and value.strip() else None


def _generation(data: dict[str, Any], fallback: int = 0) -> int:
    value = data.get("generation", fallback)
    return value if type(value) is int and value >= 0 else fallback


def _slice_for(
    projection: Projection, event: RunEvent, status: str | None = None
) -> SliceProjection | None:
    slice_id = _slice_id(event.data)
    if slice_id is None:
        return None
    current = projection.slices.get(slice_id)
    next_status = status or (
        str(event.data.get("status"))
        if event.data.get("status")
        else current.status
        if current
        else "RUNNING"
    )
    generation = _generation(event.data, current.generation if current else 0)
    zone, action = _PLACEMENT.get(
        next_status,
        (current.zone if current else "work", current.action if current else "run"),
    )
    rank_value = event.data.get("integration_rank", current.integration_rank if current else None)
    rank = rank_value if type(rank_value) is int and rank_value >= 0 else None
    result = SliceProjection(slice_id, next_status, generation, action, zone, rank)
    projection.slices[slice_id] = result
    return result


def _timeline(projection: Projection, event: RunEvent, label: str, slice_id: str | None) -> None:
    projection.timeline.append(
        {"sequence": event.sequence, "type": event.type, "label": label, "slice_id": slice_id}
    )
    del projection.timeline[:-200]


def _apply(projection: Projection, event: RunEvent) -> None:
    data = safe_data(event.data)
    event = RunEvent(event.sequence, event.type, data, event.timestamp_utc)
    slice_id = _slice_id(data)
    labels = {
        "dispatch.started": "persona 上场",
        "slice.status_changed": "Slice 状态变化",
        "verification.completed": "验证完成",
        "integration.queued": "排队集成",
        "integration.started": "集成开始",
        "integration.completed": "集成完成",
        "verified.advanced": "verified 主线推进",
        "test.failure_attributed": "失败归因",
        "candidate.generation_started": "重生成代次开始",
    }
    if event.type == "run.status_changed":
        projection.run_status = str(
            data.get("run_status", data.get("status", projection.run_status))
        )
    elif event.type == "slice.status_changed":
        _slice_for(projection, event, str(data.get("status", "UNKNOWN")))
    elif event.type == "dispatch.started":
        _slice_for(projection, event, "RUNNING")
    elif event.type == "verification.completed":
        if str(data.get("outcome", "")).upper() in {"PASS", "PASSED"}:
            _slice_for(projection, event, "LOCALLY_VERIFIED")
    elif event.type == "integration.queued":
        _slice_for(projection, event, "INTEGRATION_QUEUED")
    elif event.type == "integration.started":
        _slice_for(projection, event, "INTEGRATING")
    elif event.type in {
        "test.failure_attributed",
        "candidate.generation_started",
        "candidate.generation_invalidated",
    }:
        _slice_for(projection, event, "REGENERATING")
    elif event.type == "integration.completed":
        _slice_for(projection, event, "INTEGRATION_QUEUED")
    elif event.type == "verified.advanced" and slice_id in projection.slices:
        _slice_for(projection, event, "INTEGRATED")
    elif event.type.startswith(("advice.", "repair.")):
        projection.notices.append(
            {
                "sequence": event.sequence,
                "type": event.type,
                "summary": str(data.get("summary", event.type))[:240],
            }
        )
        del projection.notices[:-40]
    _timeline(projection, event, labels.get(event.type, "事件事实"), slice_id)


def _refresh_activity(projection: Projection, memory_gib: int, cpu_cores: int) -> None:
    limit = max(1, min(4, memory_gib // 4, cpu_cores // 2))
    active = [
        item
        for item in projection.slices.values()
        if item.action in {"run", "error"} and item.zone in {"work", "regeneration"}
    ]
    active.sort(key=lambda item: item.slice_id)
    projection.active_slices = active[:limit]
    projection.overflow_active = max(0, len(active) - limit)


def project_events(
    events: Iterable[RunEvent], *, memory_gib: int = 8, cpu_cores: int = 4
) -> Projection:
    projection = Projection()
    cursor = SequenceCursor()
    for event in events:
        result = cursor.accept(event.sequence)
        if result == "gap":
            projection.connection = "catching-up"
            break
        if result == "duplicate":
            continue
        _apply(projection, event)
    projection.cursor = cursor.cursor
    projection.connection = cursor.connection
    _refresh_activity(projection, memory_gib, cpu_cores)
    return projection

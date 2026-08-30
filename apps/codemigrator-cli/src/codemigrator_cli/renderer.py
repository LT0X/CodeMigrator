from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from io import StringIO
from typing import Any

from rich.console import Console
from rich.table import Table

from .models import Projection, RunEvent
from .projector import project_events, safe_data


def _jsonable(projection: Projection) -> dict[str, Any]:
    return {
        "cursor": projection.cursor,
        "connection": projection.connection,
        "run_status": projection.run_status,
        "active_slices": [
            {
                "slice_id": item.slice_id,
                "status": item.status,
                "generation": item.generation,
                "action": item.action,
            }
            for item in projection.active_slices
        ],
        "overflow_active": projection.overflow_active,
        "timeline": projection.timeline,
        "notices": projection.notices,
    }


def render_json(projection: Projection) -> str:
    return json.dumps(
        _jsonable(projection), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def render_human(projection: Projection) -> str:
    console = Console(
        record=True,
        file=StringIO(),
        force_terminal=False,
        color_system=None,
        width=120,
    )
    console.print(f"CodeMigrator · Run 状态 {projection.run_status} · sequence {projection.cursor}")
    table = Table(show_header=True, header_style="bold")
    table.add_column("Slice")
    table.add_column("动作")
    table.add_column("状态")
    table.add_column("代次")
    for item in projection.active_slices:
        table.add_row(item.slice_id, item.action, item.status, f"g{item.generation}")
    console.print(table)
    if projection.overflow_active:
        console.print(f"另有 {projection.overflow_active} 个活动对象已聚合")
    for timeline_item in projection.timeline[-8:]:
        target = f" · {timeline_item['slice_id']}" if timeline_item.get("slice_id") else ""
        console.print(f"#{timeline_item['sequence']} {timeline_item['label']}{target}")
    return console.export_text(clear=False)


def render_jsonl(events: Iterable[RunEvent]) -> Iterator[str]:
    history: list[RunEvent] = []
    for event in events:
        history.append(event)
        projection = project_events(history, memory_gib=8, cpu_cores=4)
        slice_id = event.data.get("slice_id", event.data.get("sliceId"))
        slice_projection = projection.slices.get(slice_id) if isinstance(slice_id, str) else None
        yield json.dumps(
            {
                "sequence": event.sequence,
                "type": event.type,
                "connection": projection.connection,
                "action": slice_projection.action if slice_projection else None,
                "status": slice_projection.status if slice_projection else None,
                "generation": slice_projection.generation if slice_projection else None,
                "data": safe_data(event.data),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

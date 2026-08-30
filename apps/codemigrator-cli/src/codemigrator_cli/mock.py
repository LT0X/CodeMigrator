from __future__ import annotations

from .models import RunEvent


def mock_events() -> tuple[RunEvent, ...]:
    def event(sequence: int, event_type: str, data: dict[str, object]) -> RunEvent:
        return RunEvent(sequence, event_type, data, "2026-08-30T00:00:00Z")

    return (
        event(1, "run.status_changed", {"run_status": "EXECUTING"}),
        event(2, "dispatch.started", {"slice_id": "slice-a", "generation": 0}),
        event(3, "dispatch.started", {"slice_id": "slice-b", "generation": 0}),
        event(4, "slice.status_changed", {"slice_id": "slice-a", "status": "LOCAL_VERIFYING"}),
        event(
            5,
            "verification.completed",
            {"slice_id": "slice-a", "outcome": "PASSED", "local": True},
        ),
        event(6, "integration.queued", {"slice_id": "slice-a", "integration_rank": 1}),
        event(7, "integration.completed", {"slice_id": "slice-a", "generation": 0}),
        event(
            8,
            "verified.advanced",
            {"slice_id": "slice-a", "generation": 0, "commit_oid": "7f2a91c"},
        ),
        event(9, "test.failure_attributed", {"slice_id": "slice-b", "generation": 0}),
        event(10, "candidate.generation_started", {"slice_id": "slice-b", "generation": 1}),
        event(11, "repair.session.started", {"summary": "联合域修复会话已启动"}),
        event(12, "advice.adopted", {"summary": "已收养修复路由结论"}),
        event(13, "run.status_changed", {"run_status": "COMPLETED"}),
    )

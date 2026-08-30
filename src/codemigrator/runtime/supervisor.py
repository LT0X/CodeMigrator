"""The two event-triggered Supervisor hand-off conditions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SupervisorAdviceKind(str, Enum):
    RepairDecision = "REPAIR_DECISION"
    RouteSuggestion = "ROUTE_SUGGESTION"


@dataclass(frozen=True, slots=True)
class SupervisorTrigger:
    kind: SupervisorAdviceKind
    reason: str


def supervisor_triggers(
    *, candidate_slice_ids: frozenset[str], session_failed_and_stopped: bool
) -> tuple[SupervisorTrigger, ...]:
    """Return only the two mechanically defined trigger conditions."""

    triggers: list[SupervisorTrigger] = []
    if len(candidate_slice_ids) > 1:
        triggers.append(
            SupervisorTrigger(
                SupervisorAdviceKind.RepairDecision,
                "AMBIGUOUS_ATTRIBUTION_CANDIDATES",
            )
        )
    if session_failed_and_stopped:
        triggers.append(
            SupervisorTrigger(SupervisorAdviceKind.RouteSuggestion, "SLICE_SESSION_STOPPED")
        )
    return tuple(triggers)


__all__ = ["SupervisorAdviceKind", "SupervisorTrigger", "supervisor_triggers"]

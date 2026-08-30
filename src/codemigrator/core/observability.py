"""Stable metric descriptors owned by the core contract layer."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from math import prod

from .enums import CheckAction, CheckStatus, Phase, RunStatus, SliceKind


class MetricKind(str, Enum):
    Counter = "counter"
    Histogram = "histogram"
    Gauge = "gauge"


@dataclass(frozen=True, slots=True)
class MetricLabel:
    name: str
    values: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.name or not self.values or len(set(self.values)) != len(self.values):
            raise ValueError("metric labels must have a unique name and non-empty values")

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "values": list(self.values)}


@dataclass(frozen=True, slots=True)
class MetricDescriptor:
    name: str
    kind: MetricKind
    labels: tuple[MetricLabel, ...] = ()
    buckets: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        names = tuple(label.name for label in self.labels)
        if not self.name.startswith("codemigrator_"):
            raise ValueError("metric names must use the codemigrator_ prefix")
        if len(set(names)) != len(names):
            raise ValueError("metric label names must be unique")
        if self.kind is MetricKind.Histogram:
            if not self.buckets or tuple(sorted(self.buckets)) != self.buckets:
                raise ValueError("histogram buckets must be non-empty and sorted")
        elif self.buckets:
            raise ValueError("only histograms may define buckets")

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind.value,
            "labels": [label.to_dict() for label in self.labels],
            "buckets": list(self.buckets),
        }

    def validate_labels(self, labels: Mapping[str, str]) -> str | None:
        expected = {label.name for label in self.labels}
        actual = set(labels)
        if actual != expected:
            return "invalid_label_keys"
        allowed = {label.name: set(label.values) for label in self.labels}
        if any(
            not isinstance(value, str) or value not in allowed[key]
            for key, value in labels.items()
        ):
            return "invalid_label_value"
        return None


_TERMINAL_STATUSES = tuple(
    status.value
    for status in (
        RunStatus.Completed,
        RunStatus.PartiallyCompleted,
        RunStatus.Failed,
        RunStatus.Cancelled,
    )
)
_PHASES = tuple(phase.value for phase in Phase)
_SLICE_KINDS = tuple(kind.value for kind in SliceKind)
_CHECK_ACTIONS = tuple(action.value for action in CheckAction)
_CHECK_STATUSES = tuple(status.value for status in CheckStatus)


def _label(name: str, *values: str) -> MetricLabel:
    return MetricLabel(name, tuple(values))


CORE_METRIC_DESCRIPTORS: tuple[MetricDescriptor, ...] = (
    MetricDescriptor(
        "codemigrator_run_total",
        MetricKind.Counter,
        (_label("terminal_status", *_TERMINAL_STATUSES),),
    ),
    MetricDescriptor(
        "codemigrator_run_duration_seconds",
        MetricKind.Histogram,
        (_label("terminal_status", *_TERMINAL_STATUSES),),
        (0.1, 1, 5, 15, 30, 60, 120, 300, 600, 1800),
    ),
    MetricDescriptor(
        "codemigrator_phase_duration_seconds",
        MetricKind.Histogram,
        (_label("phase", *_PHASES), _label("result", "completed", "failed", "cancelled")),
        (0.1, 1, 5, 15, 30, 60, 120, 300, 600),
    ),
    MetricDescriptor(
        "codemigrator_slice_first_pass_total",
        MetricKind.Counter,
        (_label("kind", *_SLICE_KINDS), _label("result", "first_pass", "after_regeneration")),
    ),
    MetricDescriptor(
        "codemigrator_check_total",
        MetricKind.Counter,
        (_label("action", *_CHECK_ACTIONS), _label("result", *_CHECK_STATUSES)),
    ),
    MetricDescriptor(
        "codemigrator_sandbox_termination_total",
        MetricKind.Counter,
        (
            _label(
                "reason",
                "timeout",
                "resource_limit",
                "cancelled",
                "signal",
                "policy_denied",
                "unknown",
            ),
        ),
    ),
    MetricDescriptor(
        "codemigrator_budget_ratio",
        MetricKind.Gauge,
        (_label("kind", "input", "output", "cost"),),
    ),
    MetricDescriptor(
        "codemigrator_observation_dropped_total",
        MetricKind.Counter,
        (
            _label(
                "sink",
                "stdout",
                "jsonl",
                "run_events",
                "sse",
                "problem_details",
                "tool_output",
                "sandbox_output",
                "report_delivery",
                "metric_exemplar",
            ),
        ),
    ),
)


DIAGNOSTIC_METRIC_DESCRIPTORS: tuple[MetricDescriptor, ...] = (
    MetricDescriptor(
        "codemigrator_integration_queue_depth",
        MetricKind.Gauge,
        (_label("state", "ready", "blocked_by_predecessor", "regenerating"),),
    ),
    MetricDescriptor(
        "codemigrator_checkpoint_commit_total",
        MetricKind.Counter,
        (_label("result", "committed", "subset_violation"),),
    ),
    MetricDescriptor(
        "codemigrator_test_outcome_total",
        MetricKind.Counter,
        (_label("result", "passed", "failed", "flaky"),),
    ),
    MetricDescriptor(
        "codemigrator_attribution_regen_total",
        MetricKind.Counter,
        (_label("outcome", "repaired", "exhausted"),),
    ),
    MetricDescriptor(
        "codemigrator_contract_drift_total",
        MetricKind.Counter,
        (_label("stage", "preview", "confirmed", "downstream_invalidated", "downstream_rebuilt"),),
    ),
)


def canonical_metric_descriptors(descriptors: Sequence[MetricDescriptor]) -> bytes:
    return json.dumps(
        [descriptor.to_dict() for descriptor in descriptors],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def logical_labelset_count(descriptor: MetricDescriptor) -> int:
    return prod(len(label.values) for label in descriptor.labels)


def exporter_series_ceiling(descriptor: MetricDescriptor) -> int:
    labelsets = logical_labelset_count(descriptor)
    if descriptor.kind is MetricKind.Histogram:
        return labelsets * (len(descriptor.buckets) + 3)
    return labelsets


def descriptor_for(name: str, *, include_diagnostics: bool = True) -> MetricDescriptor:
    descriptors = CORE_METRIC_DESCRIPTORS
    if include_diagnostics:
        descriptors += DIAGNOSTIC_METRIC_DESCRIPTORS
    for descriptor in descriptors:
        if descriptor.name == name:
            return descriptor
    raise KeyError(name)


CORE_METRIC_DESCRIPTOR_HASH = hashlib.sha256(
    canonical_metric_descriptors(CORE_METRIC_DESCRIPTORS)
).hexdigest()
CORE_METRIC_DESCRIPTOR_SET = CORE_METRIC_DESCRIPTORS


__all__ = [
    "CORE_METRIC_DESCRIPTOR_HASH",
    "CORE_METRIC_DESCRIPTOR_SET",
    "CORE_METRIC_DESCRIPTORS",
    "DIAGNOSTIC_METRIC_DESCRIPTORS",
    "MetricDescriptor",
    "MetricKind",
    "MetricLabel",
    "canonical_metric_descriptors",
    "descriptor_for",
    "exporter_series_ceiling",
    "logical_labelset_count",
]

from __future__ import annotations

import hashlib
import json

from codemigrator.core.observability import (
    CORE_METRIC_DESCRIPTOR_HASH,
    CORE_METRIC_DESCRIPTORS,
    DIAGNOSTIC_METRIC_DESCRIPTORS,
    MetricKind,
    canonical_metric_descriptors,
    exporter_series_ceiling,
    logical_labelset_count,
)


def test_core_metric_descriptor_set_is_exactly_the_eight_metric_contract() -> None:
    names = tuple(descriptor.name for descriptor in CORE_METRIC_DESCRIPTORS)

    assert names == (
        "codemigrator_run_total",
        "codemigrator_run_duration_seconds",
        "codemigrator_phase_duration_seconds",
        "codemigrator_slice_first_pass_total",
        "codemigrator_check_total",
        "codemigrator_sandbox_termination_total",
        "codemigrator_budget_ratio",
        "codemigrator_observation_dropped_total",
    )
    assert len(DIAGNOSTIC_METRIC_DESCRIPTORS) == 5


def test_core_descriptor_hash_and_capacity_are_stable() -> None:
    canonical = canonical_metric_descriptors(CORE_METRIC_DESCRIPTORS)
    expected_hash = hashlib.sha256(canonical).hexdigest()

    assert len(CORE_METRIC_DESCRIPTOR_HASH) == 64
    assert CORE_METRIC_DESCRIPTOR_HASH == expected_hash
    assert sum(logical_labelset_count(item) for item in CORE_METRIC_DESCRIPTORS) == 71
    assert sum(exporter_series_ceiling(item) for item in CORE_METRIC_DESCRIPTORS) == 251


def test_descriptor_json_is_canonical_and_phase_has_v6_four_values() -> None:
    phase = next(
        item
        for item in CORE_METRIC_DESCRIPTORS
        if item.name.endswith("phase_duration_seconds")
    )
    encoded = canonical_metric_descriptors((phase,))

    assert encoded == json.dumps(
        [phase.to_dict()], sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    assert phase.kind is MetricKind.Histogram
    assert phase.labels[0].name == "phase"
    assert phase.labels[0].values == ("PLAN", "EXECUTE", "VERIFY", "REPORT")


def test_descriptor_label_validation_rejects_high_cardinality_or_unknown_values() -> None:
    run_total = CORE_METRIC_DESCRIPTORS[0]

    assert run_total.validate_labels({"terminal_status": "COMPLETED"}) is None
    assert run_total.validate_labels({"terminal_status": "other"}) == "invalid_label_value"
    assert run_total.validate_labels({"terminal_status": "COMPLETED", "run_id": "x"}) == (
        "invalid_label_keys"
    )

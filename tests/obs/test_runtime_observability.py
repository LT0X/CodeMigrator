from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from codemigrator.core import SecretRegistry
from codemigrator.runtime.observability import (
    BoundedExporter,
    BudgetAlertTracker,
    JsonlSegmentWriter,
    MetricRegistry,
    MetricsSnapshotCache,
    ObservationPipeline,
    ObservationSerializationError,
    ObservationTracer,
    RetentionCleaner,
    RetentionRecord,
    SentinelSuite,
    serialize_observation,
)


def test_metric_registry_accepts_frozen_labels_and_rejects_dynamic_dimensions() -> None:
    registry = MetricRegistry()

    registry.observe(
        "codemigrator_run_total", {"terminal_status": "COMPLETED"}
    )
    with pytest.raises(ValueError, match="invalid_label_keys"):
        registry.observe(
            "codemigrator_run_total",
            {"terminal_status": "COMPLETED", "run_id": "high-cardinality"},
        )

    snapshot = registry.snapshot()
    assert snapshot["descriptor_hash"]
    assert snapshot["metrics"]["codemigrator_run_total"][0]["value"] == 1.0
    assert registry.dropped_count("metrics") == 1


def test_histogram_snapshot_preserves_count_sum_and_bucket_counts() -> None:
    registry = MetricRegistry()

    registry.observe(
        "codemigrator_run_duration_seconds",
        {"terminal_status": "COMPLETED"},
        2,
    )
    registry.observe(
        "codemigrator_run_duration_seconds",
        {"terminal_status": "COMPLETED"},
        20,
    )

    row = registry.snapshot()["metrics"]["codemigrator_run_duration_seconds"][0]
    assert row["count"] == 2
    assert row["sum"] == 22.0
    assert row["buckets"]["5.0"] == 1
    assert row["buckets"]["+Inf"] == 2


def test_diagnostic_metrics_are_opt_in_and_do_not_change_core_hash() -> None:
    registry = MetricRegistry(enable_diagnostics=True)

    registry.set(
        "codemigrator_integration_queue_depth",
        {"state": "ready"},
        3,
    )
    registry.observe(
        "codemigrator_test_outcome_total",
        {"result": "passed"},
    )

    assert registry.snapshot()["descriptor_hash"] == MetricRegistry().snapshot()["descriptor_hash"]
    assert registry.snapshot()["metrics"]["codemigrator_integration_queue_depth"][0]["value"] == 3.0


def test_snapshot_cache_reuses_a_snapshot_for_sixty_seconds() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    registry = MetricRegistry()

    def clock() -> datetime:
        return now

    cache = MetricsSnapshotCache(registry, clock=clock)

    first = cache.get()
    registry.observe("codemigrator_run_total", {"terminal_status": "FAILED"})
    second = cache.get()

    assert second is first
    clock_now = [now]

    def mutable_clock() -> datetime:
        return clock_now[0]

    cache = MetricsSnapshotCache(registry, clock=mutable_clock)
    before = cache.get()
    clock_now[0] = now + timedelta(seconds=60)
    after = cache.get()
    assert after is not before


def test_jsonl_writer_rotates_segments_and_writes_sha256_sidecars(tmp_path: Path) -> None:
    writer = JsonlSegmentWriter(
        tmp_path,
        secret_registry=SecretRegistry(),
        max_segment_bytes=80,
    )

    assert writer.write({"type": "event", "data": {"value": "one"}}) is True
    assert writer.write({"type": "event", "data": {"value": "two"}}) is True
    writer.close()

    segments = sorted(tmp_path.glob("segment-*.jsonl"))
    assert len(segments) == 2
    for segment in segments:
        digest = segment.with_suffix(segment.suffix + ".sha256")
        assert digest.read_text(encoding="utf-8").strip()


def test_oversized_event_uses_only_a_controlled_artifact_reference() -> None:
    registry = SecretRegistry()
    event = {"type": "large", "data": {"summary": "x" * 70_000}}

    with pytest.raises(ObservationSerializationError, match="event exceeds"):
        serialize_observation(event, registry)
    encoded = serialize_observation(event, registry, artifact_ref=lambda payload: "cas://large")

    assert len(encoded) <= 64 * 1024
    assert b"cas://large" in encoded
    assert b"x" * 100 not in encoded


def test_artifact_reference_failure_is_a_dropped_observation() -> None:
    dropped: list[str] = []

    def unavailable(payload: bytes) -> str:
        del payload
        raise RuntimeError("private storage detail")

    pipeline = ObservationPipeline(
        SecretRegistry(),
        on_drop=dropped.append,
        artifact_ref=unavailable,
    )

    assert pipeline.emit({"type": "large", "data": {"summary": "x" * 70_000}}) is False
    assert dropped == ["run_events"]


def test_pipeline_can_externalize_an_oversized_event_before_fanning_out(tmp_path: Path) -> None:
    output: list[str] = []
    exporter = BoundedExporter()
    writer = JsonlSegmentWriter(
        tmp_path / "log",
        secret_registry=SecretRegistry(),
        stdout_write=output.append,
        artifact_ref=lambda payload: "cas://large" if payload else "cas://empty",
    )
    pipeline = ObservationPipeline(
        SecretRegistry(),
        jsonl=writer,
        exporters=(exporter,),
        artifact_ref=lambda payload: "cas://large" if payload else "cas://empty",
    )

    assert pipeline.emit({"type": "large", "data": {"summary": "x" * 70_000}}) is True
    assert exporter.drain()[0].find(b"cas://large") >= 0
    writer.close()


def test_pipeline_rechecks_enabled_sinks_at_the_sentinel_interval() -> None:
    dropped: list[str] = []
    registry = SecretRegistry()
    registry.register("runtime-secret")
    sentinel = SentinelSuite(registry, sinks=("run_events",))
    pipeline = ObservationPipeline(
        registry,
        run_events=lambda payload: True,
        on_drop=dropped.append,
        sentinel=sentinel,
        sentinel_interval_events=2,
        sentinel_outputs=lambda payload: {"run_events": {"summary": "runtime-secret"}},
    )

    assert pipeline.emit({"type": "safe", "data": {"summary": "one"}}) is True
    assert pipeline.emit({"type": "safe", "data": {"summary": "two"}}) is False
    assert dropped == ["run_events"]


def test_bounded_exporter_drops_oldest_item_and_reports_the_drop() -> None:
    dropped: list[str] = []
    exporter = BoundedExporter(capacity=2, on_drop=dropped.append)

    exporter.publish(b"one")
    exporter.publish(b"two")
    exporter.publish(b"three")

    assert exporter.drain() == [b"two", b"three"]
    assert dropped == ["metric_exemplar"]


def test_exporter_drop_can_update_the_core_dropped_metric() -> None:
    registry = MetricRegistry()
    exporter = BoundedExporter(capacity=1, on_drop=registry.record_drop)

    exporter.publish(b"one")
    exporter.publish(b"two")

    snapshot = registry.snapshot()["metrics"]["codemigrator_observation_dropped_total"]
    assert snapshot[0]["labels"] == {"sink": "metric_exemplar"}
    assert snapshot[0]["value"] == 1.0


def test_sentinel_suite_fails_closed_for_every_registered_output() -> None:
    registry = SecretRegistry()
    registry.register("sentinel-secret")
    suite = SentinelSuite(registry)
    outputs = {sink: {"summary": "clean"} for sink in suite.sinks}

    assert suite.run(outputs).passed is True
    outputs["stdout"] = {"summary": "sentinel-secret"}
    report = suite.run(outputs)
    assert report.passed is False
    assert report.failed_sinks == ("stdout",)


def test_tracer_exposes_only_the_three_fixed_span_names() -> None:
    tracer = ObservationTracer()

    with tracer.span("run") as span:
        assert span.name == "migration.run"
    with tracer.span("phase") as span:
        assert span.name == "migration.phase"
    with tracer.span("slice") as span:
        assert span.name == "migration.slice"
    with pytest.raises(ValueError, match="fixed span"):
        with tracer.span("migration.run/secret"):
            pass


def test_budget_alerts_are_emitted_once_per_run_kind_threshold() -> None:
    alerts = BudgetAlertTracker()

    assert [alert.level for alert in alerts.observe("run-1", "input", 0.8)] == ["Warning"]
    assert alerts.observe("run-1", "input", 0.9) == ()
    assert [alert.level for alert in alerts.observe("run-1", "input", 1.0)] == ["Critical"]
    assert alerts.redaction_failed("run-1").level == "Critical"


def test_retention_cleaner_respects_reference_and_time_boundaries() -> None:
    now = datetime(2026, 1, 31, tzinfo=UTC)
    cleaner = RetentionCleaner()
    records = (
        RetentionRecord("artifact", "execution_artifact", now - timedelta(days=30)),
        RetentionRecord("ast", "ast_index", now - timedelta(days=7)),
        RetentionRecord("orphan", "orphan", now - timedelta(hours=24)),
        RetentionRecord("held", "execution_artifact", now - timedelta(days=31), referenced=True),
    )

    eligible = cleaner.eligible(records, now=now)

    assert tuple(record.identifier for record in eligible) == ("artifact", "ast", "orphan")


def test_retention_cleaner_deletes_at_most_one_thousand_selected_records() -> None:
    now = datetime(2026, 1, 31, tzinfo=UTC)
    cleaner = RetentionCleaner()
    records = tuple(
        RetentionRecord(str(index), "orphan", now - timedelta(days=2))
        for index in range(1_005)
    )
    deleted: list[str] = []

    result = cleaner.clean(records, deleted.append, now=now)

    assert len(result) == 1_000
    assert len(deleted) == 1_000


def test_retention_cleaner_keeps_non_terminal_run_artifacts() -> None:
    now = datetime(2026, 1, 31, tzinfo=UTC)
    cleaner = RetentionCleaner()
    records = (
        RetentionRecord(
            "active-artifact",
            "execution_artifact",
            now - timedelta(days=31),
            terminal=False,
        ),
    )

    assert cleaner.eligible(records, now=now) == ()

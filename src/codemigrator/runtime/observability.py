"""Runtime observability sinks with one fail-closed redaction boundary."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import structlog
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

from codemigrator.core import (
    CORE_METRIC_DESCRIPTOR_HASH,
    CORE_METRIC_DESCRIPTORS,
    DIAGNOSTIC_METRIC_DESCRIPTORS,
    MetricDescriptor,
    MetricKind,
    SecretRegistry,
)

MAX_EVENT_BYTES = 64 * 1024
JSONL_SEGMENT_BYTES = 64 * 1024 * 1024
SNAPSHOT_INTERVAL_SECONDS = 60
EXPORTER_QUEUE_CAPACITY = 4096
SENTINEL_INTERVAL_EVENTS = 10_000
JSONL_SEGMENT_PREFIX = "segment-"

DEFAULT_SENTINEL_SINKS: tuple[str, ...] = (
    "stdout",
    "jsonl",
    "run_events",
    "sse",
    "problem_details",
    "tool_output",
    "sandbox_output",
    "report_delivery",
    "metric_exemplar",
    "cli_renderer",
)


class ObservationSerializationError(ValueError):
    """Raised when an event cannot be safely serialized."""


@dataclass(frozen=True, slots=True)
class ObservationEvent:
    """A small event envelope accepted by the observation pipeline."""

    event_type: str
    data: Mapping[str, object] = field(default_factory=dict)
    sequence: int = 1
    timestamp_utc: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.event_type.strip():
            raise ValueError("event_type must not be empty")
        if type(self.sequence) is not int or self.sequence < 1:
            raise ValueError("sequence must be positive")
        if self.timestamp_utc.tzinfo is None or self.timestamp_utc.utcoffset() is None:
            raise ValueError("timestamp_utc must be timezone-aware")
        object.__setattr__(self, "timestamp_utc", self.timestamp_utc.astimezone(UTC))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "codemigrator.observation.event",
            "version": 1,
            "type": self.event_type,
            "data": dict(self.data),
            "sequence": self.sequence,
            "timestamp_utc": self.timestamp_utc.isoformat().replace("+00:00", "Z"),
        }


def serialize_observation(
    event: ObservationEvent | Mapping[str, object],
    secret_registry: SecretRegistry,
    artifact_ref: Callable[[bytes], str] | None = None,
) -> bytes:
    """Redact and serialize an event, externalizing oversized safe payloads."""

    payload = event.to_dict() if isinstance(event, ObservationEvent) else dict(event)
    redacted = secret_registry.redact(payload)
    if not redacted.accepted:
        raise ObservationSerializationError(f"observation rejected: {redacted.reason}")
    encoded = _render_json(cast(Mapping[str, object], redacted.value))
    if len(encoded) <= MAX_EVENT_BYTES:
        return encoded
    if artifact_ref is None:
        raise ObservationSerializationError("event exceeds 64 KiB")
    digest = hashlib.sha256(encoded).hexdigest()
    try:
        locator = artifact_ref(encoded)
    except Exception as exc:
        raise ObservationSerializationError("artifact reference unavailable") from exc
    if not isinstance(locator, str) or not locator:
        raise ObservationSerializationError("artifact reference unavailable")
    reference = {
        "schema": "codemigrator.observation.event",
        "version": 1,
        "type": str(payload.get("type", "observation")),
        "sequence": payload.get("sequence", 1),
        "timestamp_utc": payload.get("timestamp_utc", ""),
        "payload_ref": {
            "locator": locator,
            "sha256": digest,
            "size": len(encoded),
        },
    }
    reference_result = secret_registry.redact(reference)
    if not reference_result.accepted:
        raise ObservationSerializationError("oversized event reference was rejected")
    compact = _render_json(cast(Mapping[str, object], reference_result.value))
    if len(compact) > MAX_EVENT_BYTES:
        raise ObservationSerializationError("event reference exceeds 64 KiB")
    return compact


def _render_json(value: Mapping[str, object]) -> bytes:
    renderer = structlog.processors.JSONRenderer(serializer=json.dumps, sort_keys=True)
    rendered = renderer(None, "observation", dict(value))
    return str(rendered).encode("utf-8")


class JsonlSegmentWriter:
    """Write redacted JSONL segments and degrade to stdout on I/O failure."""

    def __init__(
        self,
        directory: Path,
        *,
        secret_registry: SecretRegistry,
        stdout_write: Callable[[str], object] | None = None,
        on_drop: Callable[[str], object] | None = None,
        max_segment_bytes: int = JSONL_SEGMENT_BYTES,
        artifact_ref: Callable[[bytes], str] | None = None,
    ) -> None:
        if max_segment_bytes < 1:
            raise ValueError("max_segment_bytes must be positive")
        self.directory = directory
        self.secret_registry = secret_registry
        self.stdout_write = stdout_write or sys.stdout.write
        self.on_drop = on_drop or (lambda sink: None)
        self.max_segment_bytes = max_segment_bytes
        self.artifact_ref = artifact_ref
        self._file: Any | None = None
        self._digest = hashlib.sha256()
        self._offset = 0
        self._segment_number = 0
        self._unavailable = False

    @property
    def offset(self) -> int:
        return self._offset

    @property
    def segment_number(self) -> int:
        return self._segment_number

    def write(self, event: ObservationEvent | Mapping[str, object]) -> bool:
        try:
            payload = serialize_observation(event, self.secret_registry, self.artifact_ref)
        except ObservationSerializationError:
            self._drop("jsonl")
            return False
        return self._write_serialized(payload)

    def _write_serialized(self, payload: bytes) -> bool:
        """Write already checked bytes without re-serializing the event."""

        if len(payload) > MAX_EVENT_BYTES:
            self._drop("jsonl")
            return False

        line = payload + b"\n"
        if self._unavailable or not self._ensure_open():
            return self._write_stdout(line)
        if self._offset and self._offset + len(line) > self.max_segment_bytes:
            self._finalize_segment()
            if not self._ensure_open():
                return self._write_stdout(line)
        output_file = self._file
        if output_file is None:
            return self._write_stdout(line)
        try:
            output_file.write(line)
            output_file.flush()
            self._digest.update(line)
            self._offset += len(line)
            return True
        except (OSError, ValueError):
            self._unavailable = True
            self._close_file()
            return self._write_stdout(line)

    def close(self) -> None:
        if self._file is not None:
            self._finalize_segment()

    def __enter__(self) -> JsonlSegmentWriter:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _ensure_open(self) -> bool:
        if self._file is not None:
            return True
        if self._unavailable:
            return False
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._segment_number += 1
            path = self.directory / f"{JSONL_SEGMENT_PREFIX}{self._segment_number:06d}.jsonl"
            self._file = path.open("wb")
            self._digest = hashlib.sha256()
            self._offset = 0
            return True
        except OSError:
            self._unavailable = True
            return False

    def _finalize_segment(self) -> None:
        if self._file is None:
            return
        path = Path(self._file.name)
        digest = self._digest.hexdigest()
        self._close_file()
        try:
            path.with_suffix(path.suffix + ".sha256").write_text(
                f"{digest}  {path.name}\n", encoding="utf-8"
            )
        except OSError:
            self._drop("jsonl")
        self._offset = 0

    def _close_file(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            finally:
                self._file = None

    def _write_stdout(self, line: bytes) -> bool:
        try:
            self.stdout_write(line.decode("utf-8"))
            return True
        except (OSError, UnicodeError, ValueError):
            self._drop("stdout")
            return False

    def _drop(self, sink: str) -> None:
        try:
            self.on_drop(sink)
        except Exception:
            pass


class MetricRegistry:
    """A private Prometheus registry guarded by static descriptor allowlists."""

    def __init__(
        self,
        *,
        enable_diagnostics: bool = False,
        registry: CollectorRegistry | None = None,
        on_drop: Callable[[str], object] | None = None,
    ) -> None:
        self.collector_registry = registry or CollectorRegistry(auto_describe=True)
        descriptors = CORE_METRIC_DESCRIPTORS
        if enable_diagnostics:
            descriptors += DIAGNOSTIC_METRIC_DESCRIPTORS
        self.descriptors: tuple[MetricDescriptor, ...] = descriptors
        self._descriptors = {descriptor.name: descriptor for descriptor in descriptors}
        self._metrics: dict[str, Any] = {}
        self._values: dict[str, dict[tuple[str, ...], float]] = {
            descriptor.name: {} for descriptor in descriptors
        }
        self._histogram_stats: dict[
            str, dict[tuple[str, ...], dict[str, object]]
        ] = {descriptor.name: {} for descriptor in descriptors}
        self._dropped: dict[str, int] = {}
        self.on_drop = on_drop or (lambda sink: None)
        for descriptor in descriptors:
            label_names = tuple(label.name for label in descriptor.labels)
            if descriptor.kind is MetricKind.Counter:
                metric: Any = Counter(
                    descriptor.name,
                    descriptor.name,
                    labelnames=label_names,
                    registry=self.collector_registry,
                )
            elif descriptor.kind is MetricKind.Gauge:
                metric = Gauge(
                    descriptor.name,
                    descriptor.name,
                    labelnames=label_names,
                    registry=self.collector_registry,
                )
            else:
                metric = Histogram(
                    descriptor.name,
                    descriptor.name,
                    labelnames=label_names,
                    buckets=descriptor.buckets,
                    registry=self.collector_registry,
                )
            self._metrics[descriptor.name] = metric

    def observe(self, name: str, labels: Mapping[str, str], value: float = 1.0) -> None:
        descriptor = self._descriptor(name)
        self._validate(descriptor, labels)
        if descriptor.kind is MetricKind.Gauge:
            raise ValueError("gauge metrics require set")
        if not isinstance(value, (int, float)) or value < 0:
            raise ValueError("metric value must be non-negative")
        metric = self._metrics[name].labels(**dict(labels))
        if descriptor.kind is MetricKind.Counter:
            metric.inc(value)
        else:
            metric.observe(value)
        key = self._label_key(descriptor, labels)
        self._values[name][key] = self._values[name].get(key, 0.0) + float(value)
        if descriptor.kind is MetricKind.Histogram:
            stats = self._histogram_stats[name].setdefault(
                key,
                {
                    "count": 0,
                    "sum": 0.0,
                    "buckets": {
                        **{str(float(bucket)): 0 for bucket in descriptor.buckets},
                        "+Inf": 0,
                    },
                },
            )
            stats["count"] = cast(int, stats["count"]) + 1
            stats["sum"] = cast(float, stats["sum"]) + float(value)
            buckets = cast(dict[str, int], stats["buckets"])
            for bucket in descriptor.buckets:
                if value <= bucket:
                    bucket_key = str(float(bucket))
                    buckets[bucket_key] += 1
            buckets["+Inf"] += 1

    def set(self, name: str, labels: Mapping[str, str], value: float) -> None:
        descriptor = self._descriptor(name)
        self._validate(descriptor, labels)
        if descriptor.kind is not MetricKind.Gauge:
            raise ValueError("only gauge metrics support set")
        if not isinstance(value, (int, float)):
            raise ValueError("metric value must be numeric")
        self._metrics[name].labels(**dict(labels)).set(value)
        self._values[name][self._label_key(descriptor, labels)] = float(value)

    def snapshot(self, *, captured_at: datetime | None = None) -> dict[str, object]:
        timestamp = (captured_at or datetime.now(UTC)).astimezone(UTC)
        metrics: dict[str, list[dict[str, object]]] = {}
        for descriptor in self.descriptors:
            rows: list[dict[str, object]] = []
            for key, value in self._values[descriptor.name].items():
                row: dict[str, object] = {
                    "labels": {
                        label.name: key[index]
                        for index, label in enumerate(descriptor.labels)
                    },
                    "value": value,
                }
                if descriptor.kind is MetricKind.Histogram:
                    row.update(self._histogram_stats[descriptor.name][key])
                rows.append(row)
            metrics[descriptor.name] = rows
        return {
            "descriptor_hash": CORE_METRIC_DESCRIPTOR_HASH,
            "captured_at": timestamp.isoformat().replace("+00:00", "Z"),
            "metrics": metrics,
        }

    def prometheus_bytes(self) -> bytes:
        return generate_latest(self.collector_registry)

    def dropped_count(self, sink: str) -> int:
        return self._dropped.get(sink, 0)

    def record_drop(self, sink: str) -> None:
        """Record a failed projection for an injected sink callback."""

        self._record_drop(sink)

    def _descriptor(self, name: str) -> MetricDescriptor:
        try:
            return self._descriptors[name]
        except KeyError as exc:
            self._record_drop("metrics")
            raise ValueError("unknown_metric") from exc

    def _validate(self, descriptor: MetricDescriptor, labels: Mapping[str, str]) -> None:
        reason = descriptor.validate_labels(labels)
        if reason is not None:
            self._record_drop("metrics")
            raise ValueError(reason)

    @staticmethod
    def _label_key(descriptor: MetricDescriptor, labels: Mapping[str, str]) -> tuple[str, ...]:
        return tuple(labels[label.name] for label in descriptor.labels)

    def _record_drop(self, sink: str) -> None:
        self._dropped[sink] = self._dropped.get(sink, 0) + 1
        try:
            self.on_drop(sink)
        except Exception:
            pass
        dropped = self._metrics.get("codemigrator_observation_dropped_total")
        if dropped is not None:
            sink_value = (
                "metric_exemplar"
                if sink in {"metrics", "exporter"}
                else sink
            )
            allowed = next(
                label.values
                for descriptor in CORE_METRIC_DESCRIPTORS
                if descriptor.name == "codemigrator_observation_dropped_total"
                for label in descriptor.labels
            )
            if sink_value in allowed:
                dropped.labels(sink=sink_value).inc()
                dropped_descriptor = self._descriptors[
                    "codemigrator_observation_dropped_total"
                ]
                dropped_key = self._label_key(dropped_descriptor, {"sink": sink_value})
                self._values[dropped_descriptor.name][dropped_key] = (
                    self._values[dropped_descriptor.name].get(dropped_key, 0.0) + 1.0
                )


class MetricsSnapshotCache:
    """Serve one in-process JSON metric snapshot for a fixed sixty-second window."""

    def __init__(
        self,
        registry: MetricRegistry,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        interval_seconds: int = SNAPSHOT_INTERVAL_SECONDS,
    ) -> None:
        if interval_seconds < 1:
            raise ValueError("interval_seconds must be positive")
        self.registry = registry
        self.clock = clock
        self.interval = timedelta(seconds=interval_seconds)
        self._captured_at: datetime | None = None
        self._snapshot: dict[str, object] | None = None

    def get(self) -> dict[str, object]:
        now = self.clock().astimezone(UTC)
        if (
            self._snapshot is None
            or self._captured_at is None
            or now - self._captured_at >= self.interval
        ):
            self._captured_at = now
            self._snapshot = self.registry.snapshot(captured_at=now)
        return self._snapshot

    def write_json(self, path: Path) -> None:
        payload = json.dumps(self.get(), sort_keys=True, separators=(",", ":"))
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as temporary:
            temporary.write(payload)
            temporary.flush()
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)


class BoundedExporter:
    """A drop-oldest exporter queue that cannot block the domain path."""

    def __init__(
        self,
        *,
        capacity: int = EXPORTER_QUEUE_CAPACITY,
        on_drop: Callable[[str], object] | None = None,
        send: Callable[[bytes], object] | None = None,
        drop_sink: str = "metric_exemplar",
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self.on_drop = on_drop or (lambda sink: None)
        self.send = send
        allowed_sinks = next(
            label.values
            for descriptor in CORE_METRIC_DESCRIPTORS
            if descriptor.name == "codemigrator_observation_dropped_total"
            for label in descriptor.labels
        )
        if drop_sink not in allowed_sinks:
            raise ValueError("drop_sink must use the observation dropped sink allowlist")
        self.drop_sink = drop_sink
        self._queue: deque[bytes] = deque(maxlen=capacity)

    @property
    def size(self) -> int:
        return len(self._queue)

    def publish(self, payload: bytes) -> None:
        if len(self._queue) == self.capacity:
            self._queue.popleft()
            self._drop()
        self._queue.append(payload)

    def drain(self) -> list[bytes]:
        payloads = list(self._queue)
        self._queue.clear()
        if self.send is not None:
            for payload in payloads:
                try:
                    self.send(payload)
                except Exception:
                    self._drop()
        return payloads

    def _drop(self) -> None:
        try:
            self.on_drop(self.drop_sink)
        except Exception:
            pass


class ObservationPipeline:
    """Serialize once and fan out only safe bytes to independent sinks."""

    def __init__(
        self,
        secret_registry: SecretRegistry,
        *,
        jsonl: JsonlSegmentWriter | None = None,
        exporters: Sequence[BoundedExporter] = (),
        on_drop: Callable[[str], object] | None = None,
        artifact_ref: Callable[[bytes], str] | None = None,
        sentinel: SentinelSuite | None = None,
        enabled_sinks: Sequence[str] = ("run_events",),
        sentinel_interval_events: int = SENTINEL_INTERVAL_EVENTS,
        sentinel_outputs: Callable[[bytes], Mapping[str, object]] | None = None,
        run_events: Callable[[bytes], object] | None = None,
    ) -> None:
        if sentinel_interval_events < 1:
            raise ValueError("sentinel_interval_events must be positive")
        self.secret_registry = secret_registry
        self.jsonl = jsonl
        self.exporters = tuple(exporters)
        self.on_drop = on_drop or (lambda sink: None)
        self.artifact_ref = artifact_ref
        self.sentinel = sentinel
        self.enabled_sinks = tuple(enabled_sinks)
        self.sentinel_interval_events = sentinel_interval_events
        self.sentinel_outputs = sentinel_outputs
        self.run_events = run_events
        self._event_count = 0

    def emit(self, event: ObservationEvent | Mapping[str, object]) -> bool:
        try:
            payload = serialize_observation(event, self.secret_registry, self.artifact_ref)
        except ObservationSerializationError:
            self._drop("run_events")
            return False
        self._event_count += 1
        try:
            decoded = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._drop("run_events")
            return False
        for sink in self.enabled_sinks:
            if not self.secret_registry.redact(decoded).accepted:
                self._drop(sink)
                return False
        if self.sentinel is not None and self._event_count % self.sentinel_interval_events == 0:
            outputs = (
                self.sentinel_outputs(payload)
                if self.sentinel_outputs is not None
                else {sink: decoded for sink in self.sentinel.sinks}
            )
            if not self.sentinel.run(outputs).passed:
                self._drop("run_events")
                return False
        delivered = False
        if self.run_events is not None:
            try:
                result = self.run_events(payload)
            except Exception:
                self._drop("run_events")
                return False
            if result is False:
                self._drop("run_events")
                return False
            delivered = True
        if self.jsonl is not None:
            delivered = self.jsonl._write_serialized(payload) or delivered
        for exporter in self.exporters:
            exporter.publish(payload)
        delivered = bool(self.exporters) or delivered
        return delivered

    def _drop(self, sink: str) -> None:
        try:
            self.on_drop(sink)
        except Exception:
            pass


@dataclass(frozen=True, slots=True)
class SentinelReport:
    passed: bool
    failed_sinks: tuple[str, ...] = ()
    missing_sinks: tuple[str, ...] = ()


class SentinelSuite:
    """Run the startup/periodic four-encoding safety check over every sink."""

    def __init__(
        self,
        secret_registry: SecretRegistry,
        *,
        sinks: Sequence[str] = DEFAULT_SENTINEL_SINKS,
    ) -> None:
        self.secret_registry = secret_registry
        self.sinks = tuple(sinks)

    def run(self, outputs: Mapping[str, object]) -> SentinelReport:
        failed: list[str] = []
        missing: list[str] = []
        for sink in self.sinks:
            if sink not in outputs:
                missing.append(sink)
                continue
            if not self.secret_registry.redact(outputs[sink]).accepted:
                failed.append(sink)
        return SentinelReport(not failed and not missing, tuple(failed), tuple(missing))

    def assert_clean(self, outputs: Mapping[str, object]) -> None:
        report = self.run(outputs)
        if not report.passed:
            raise RuntimeError("observation sentinel failed")


class ObservationTracer:
    """OpenTelemetry tracer with the fixed M-13 span vocabulary."""

    _SPAN_NAMES = {"run": "migration.run", "phase": "migration.phase", "slice": "migration.slice"}

    def __init__(self, provider: TracerProvider | None = None) -> None:
        self.provider = provider or TracerProvider()
        self.tracer = self.provider.get_tracer("codemigrator.observability", "0.1.0")

    @contextmanager
    def span(self, kind: str) -> Iterator[trace.Span]:
        try:
            name = self._SPAN_NAMES[kind]
        except KeyError as exc:
            raise ValueError("span name must use the fixed span vocabulary") from exc
        with self.tracer.start_as_current_span(name) as span:
            yield span


@dataclass(frozen=True, slots=True)
class BudgetAlert:
    run_id: str
    kind: str
    level: str
    ratio: float


class BudgetAlertTracker:
    """Deduplicate threshold alerts without taking budget decisions."""

    def __init__(self) -> None:
        self._emitted: set[tuple[str, str, str]] = set()

    def observe(self, run_id: object, kind: str, ratio: float) -> tuple[BudgetAlert, ...]:
        if ratio < 0:
            raise ValueError("budget ratio must be non-negative")
        alerts: list[BudgetAlert] = []
        run = str(run_id)
        if ratio >= 0.8 and (run, kind, "Warning") not in self._emitted:
            self._emitted.add((run, kind, "Warning"))
            alerts.append(BudgetAlert(run, kind, "Warning", ratio))
        if ratio >= 1.0 and (run, kind, "Critical") not in self._emitted:
            self._emitted.add((run, kind, "Critical"))
            alerts.append(BudgetAlert(run, kind, "Critical", ratio))
        return tuple(alerts)

    def redaction_failed(self, run_id: object) -> BudgetAlert:
        return BudgetAlert(str(run_id), "redaction", "Critical", 1.0)


@dataclass(frozen=True, slots=True)
class RetentionRecord:
    identifier: str
    kind: str
    created_at: datetime
    referenced: bool = False
    terminal: bool = True

    def __post_init__(self) -> None:
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")


class RetentionCleaner:
    """Select unreferenced records using M-00's retention boundaries."""

    RETENTION = {
        "execution_artifact": timedelta(days=30),
        "ast_index": timedelta(days=7),
        "orphan": timedelta(hours=24),
    }
    MAX_BATCH = 1000
    TRANSACTION_DEADLINE_SECONDS = 5

    def eligible(
        self,
        records: Sequence[RetentionRecord],
        *,
        now: datetime | None = None,
    ) -> tuple[RetentionRecord, ...]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        selected: list[RetentionRecord] = []
        for record in records:
            if record.referenced or not record.terminal:
                continue
            retention = self.RETENTION.get(record.kind)
            if retention is None:
                continue
            created = record.created_at.astimezone(UTC)
            if current - created >= retention:
                selected.append(record)
            if len(selected) == self.MAX_BATCH:
                break
        return tuple(selected)

    def clean(
        self,
        records: Sequence[RetentionRecord],
        delete: Callable[[str], object],
        *,
        now: datetime | None = None,
    ) -> tuple[RetentionRecord, ...]:
        """Delete one bounded batch through an injected persistence adapter."""

        selected = self.eligible(records, now=now)
        for record in selected:
            delete(record.identifier)
        return selected


__all__ = [
    "BoundedExporter",
    "BudgetAlert",
    "BudgetAlertTracker",
    "DEFAULT_SENTINEL_SINKS",
    "EXPORTER_QUEUE_CAPACITY",
    "JSONL_SEGMENT_BYTES",
    "JsonlSegmentWriter",
    "MAX_EVENT_BYTES",
    "MetricRegistry",
    "MetricsSnapshotCache",
    "ObservationEvent",
    "ObservationPipeline",
    "ObservationSerializationError",
    "ObservationTracer",
    "RetentionCleaner",
    "RetentionRecord",
    "SNAPSHOT_INTERVAL_SECONDS",
    "SENTINEL_INTERVAL_EVENTS",
    "SentinelReport",
    "SentinelSuite",
    "serialize_observation",
]

"""Side-effect boundaries for source analysis and projection persistence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from codemigrator.core import StableErrorCode

from .errors import AnalysisFailure
from .models import AnalysisCapability, AnalysisResult


class SnapshotSource(Protocol):
    snapshot_oid: str

    @property
    def paths(self) -> tuple[str, ...]: ...

    def read(self, path: str) -> bytes: ...


class InMemorySnapshotSource:
    """Deterministic read-only snapshot source used by tests and callers."""

    def __init__(self, snapshot_oid: str, files: Mapping[str, bytes]) -> None:
        normalized = sorted(set(files), key=lambda path: path.encode("utf-8"))
        if any(
            not path
            or path.startswith(("/", "~"))
            or "\\" in path
            or "\x00" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            for path in normalized
        ):
            raise ValueError("snapshot paths must be relative POSIX paths")
        self.snapshot_oid = snapshot_oid
        self._files = {path: bytes(files[path]) for path in normalized}
        self.read_count = 0

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(self._files)

    def read(self, path: str) -> bytes:
        self.read_count += 1
        try:
            return self._files[path]
        except KeyError as exc:
            raise KeyError(path) from exc


@dataclass(frozen=True)
class ProjectionKey:
    snapshot_oid: str
    descriptor_sha256: str


@dataclass(frozen=True)
class AnalysisArtifact:
    key: ProjectionKey
    result: AnalysisResult


class ProjectionStore(Protocol):
    def write(self, artifact: AnalysisArtifact) -> None: ...

    def read(self, key: ProjectionKey) -> AnalysisArtifact | None: ...

    def cleanup(self, *, now: datetime | None = None) -> list[ProjectionKey]: ...


class InMemoryProjectionStore:
    """Wave 1 projection stub; runtime owns SQLite/FTS5 implementation."""

    def __init__(self, *, fail_writes: int = 0) -> None:
        self.fail_writes = fail_writes
        self.write_attempts = 0
        self._records: dict[ProjectionKey, tuple[AnalysisArtifact, datetime]] = {}

    def write(self, artifact: AnalysisArtifact) -> None:
        for _ in range(2):
            self.write_attempts += 1
            if self.fail_writes:
                self.fail_writes -= 1
                continue
            self._records[artifact.key] = (artifact, datetime.now(UTC))
            return
        raise AnalysisFailure(
            StableErrorCode.ANALYSIS_INFRA_ERROR,
            "projection write failed after one retry",
        )

    def read(self, key: ProjectionKey) -> AnalysisArtifact | None:
        record = self._records.get(key)
        return None if record is None else record[0]

    def seed(self, key: ProjectionKey, created_at: datetime) -> None:
        result = AnalysisResult(
            snapshot_oid=key.snapshot_oid,
            descriptor_sha256=key.descriptor_sha256,
            capability=AnalysisCapability.TextFallback,
            modules=[],
            imports=[],
            coverage=[],
            coverage_status=[],
            conservation=[],
            manifests=[],
            artifacts=[],
            symbol_bindings=[],
            reference_sites=[],
            symbol_coverage=[],
        )
        artifact = AnalysisArtifact(key=key, result=result)
        self._records[key] = (artifact, created_at)

    def cleanup(self, *, now: datetime | None = None) -> list[ProjectionKey]:
        current = now or datetime.now(UTC)
        cutoff = current - timedelta(days=7)
        expired = sorted(
            (key for key, (_, created_at) in self._records.items() if created_at < cutoff),
            key=lambda key: (key.snapshot_oid, key.descriptor_sha256),
        )
        for key in expired:
            del self._records[key]
        return expired


__all__ = [
    "AnalysisArtifact",
    "InMemoryProjectionStore",
    "InMemorySnapshotSource",
    "ProjectionKey",
    "ProjectionStore",
    "SnapshotSource",
]

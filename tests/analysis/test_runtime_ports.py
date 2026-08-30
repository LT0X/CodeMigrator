from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from codemigrator.analysis import (
    AnalysisArtifact,
    AnalysisCapability,
    AnalysisFailure,
    GrammarCache,
    GrammarCircuitBreaker,
    GrammarFailure,
    InMemoryProjectionStore,
    ProjectionKey,
)
from codemigrator.core import StableErrorCode

from .test_pipeline import descriptor, snapshot


def test_projection_store_retries_once_and_never_exposes_partial_writes() -> None:
    store = InMemoryProjectionStore(fail_writes=1)
    key = ProjectionKey(snapshot_oid="a" * 40, descriptor_sha256="b" * 64)
    artifact = AnalysisArtifact(
        key=key,
        result=__import__("codemigrator.analysis", fromlist=["analyze_snapshot"]).analyze_snapshot(
            snapshot(), descriptor()
        ),
    )

    store.write(artifact)

    assert store.read(key) == artifact
    assert store.write_attempts == 2


def test_projection_store_gives_up_after_one_retry_without_partial_data() -> None:
    store = InMemoryProjectionStore(fail_writes=2)
    key = ProjectionKey(snapshot_oid="a" * 40, descriptor_sha256="b" * 64)
    artifact = AnalysisArtifact(
        key=key,
        result=__import__("codemigrator.analysis", fromlist=["AnalysisResult"]).AnalysisResult(
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
        ),
    )

    with pytest.raises(AnalysisFailure, match=StableErrorCode.ANALYSIS_INFRA_ERROR.value):
        store.write(artifact)
    assert store.read(key) is None


def test_grammar_circuit_breaker_is_per_grammar_and_opens_after_two_failures() -> None:
    breaker = GrammarCircuitBreaker()

    def broken() -> None:
        raise RuntimeError("parser crashed")

    for _ in range(2):
        with pytest.raises(GrammarFailure):
            breaker.run("grammar-a", broken)

    with pytest.raises(GrammarFailure, match=StableErrorCode.ANALYSIS_INFRA_ERROR.value):
        breaker.run("grammar-a", lambda: None)
    assert breaker.run("grammar-b", lambda: "ok") == "ok"


def test_grammar_cache_is_lru_and_scoped_to_snapshot_file_and_grammar() -> None:
    cache = GrammarCache[str](max_entries=1)
    loads = 0

    def load() -> str:
        nonlocal loads
        loads += 1
        return f"tree-{loads}"

    assert cache.get_or_load("snapshot-a", "src/a.py", "grammar-a", load) == "tree-1"
    assert cache.get_or_load("snapshot-a", "src/a.py", "grammar-a", load) == "tree-1"
    assert cache.get_or_load("snapshot-a", "src/b.py", "grammar-a", load) == "tree-2"
    assert cache.get_or_load("snapshot-a", "src/a.py", "grammar-a", load) == "tree-3"


def test_projection_cleanup_uses_seven_day_retention() -> None:
    store = InMemoryProjectionStore()
    old = ProjectionKey(snapshot_oid="a" * 40, descriptor_sha256="b" * 64)
    fresh = ProjectionKey(snapshot_oid="c" * 40, descriptor_sha256="d" * 64)
    now = datetime.now(UTC)
    store.seed(old, now - timedelta(days=8))
    store.seed(fresh, now - timedelta(days=1))

    assert store.cleanup(now=now) == [old]
    assert store.read(old) is None
    assert store.read(fresh) is not None

from __future__ import annotations

import pytest

from codemigrator.analysis import (
    ExtractSubtree,
    FindReferences,
    FindSymbol,
    InMemorySnapshotSource,
    QuerySourceAst,
    SearchContext,
    analyze_snapshot,
)
from codemigrator.core import StableErrorCode

from .test_pipeline import descriptor, snapshot


def query_service() -> QuerySourceAst:
    source = snapshot()
    result = analyze_snapshot(source, descriptor())
    return QuerySourceAst(result=result, snapshot=source)


def test_query_source_ast_supports_symbol_and_context_queries_with_anchors() -> None:
    service = query_service()

    symbols = service.query(FindSymbol(kind="FIND_SYMBOL", symbol="run"))
    context = service.query(SearchContext(kind="SEARCH_CONTEXT", query="helper"))

    assert symbols.hits
    assert context.hits
    assert all(hit.range.file_path for hit in symbols.hits + context.hits)


def test_query_source_ast_uses_psf_index_for_references() -> None:
    service = query_service()

    result = service.query(FindReferences(kind="FIND_REFERENCES", symbol="helper"))

    assert result.hits
    assert all(hit.range.file_path == "src/main/main.py" for hit in result.hits)


def test_query_source_ast_rejects_snapshot_external_ranges() -> None:
    service = query_service()
    query = FindReferences(kind="FIND_REFERENCES", symbol="../secret")

    with pytest.raises(ValueError, match=StableErrorCode.PATH_OUTSIDE_SNAPSHOT.value):
        service.query(query)


def test_query_source_ast_limits_hits_and_text_without_partial_success() -> None:
    source = InMemorySnapshotSource(
        snapshot_oid="5" * 40,
        files={"src/main.py": b"\n".join(b"helper" for _ in range(250))},
    )
    result = analyze_snapshot(source, descriptor())
    service = QuerySourceAst(result=result, snapshot=source, max_hits=200, max_text_bytes=20)

    query = SearchContext(kind="SEARCH_CONTEXT", query="helper")
    response = service.query(query)

    assert response.truncated is True
    assert len(response.hits) <= 200
    assert response.error is None


def test_query_source_ast_rejects_metadata_paths_and_reports_timeout(monkeypatch) -> None:
    service = query_service()

    with pytest.raises(ValueError, match=StableErrorCode.PATH_OUTSIDE_SNAPSHOT.value):
        service.query(
            ExtractSubtree(
                kind="EXTRACT_SUBTREE",
                range={
                    "file_path": "candidate/out.py",
                    "start": {"line": 1, "column": 0},
                    "end": {"line": 1, "column": 1},
                },
            )
        )

    ticks = iter((0.0, 2.0))
    monkeypatch.setattr("codemigrator.analysis.query.time.monotonic", lambda: next(ticks))
    timed = QuerySourceAst(result=service.result, snapshot=service.snapshot, timeout_seconds=1.0)
    with pytest.raises(ValueError, match="QUERY_TIMEOUT"):
        timed.query(SearchContext(kind="SEARCH_CONTEXT", query="helper"))

from __future__ import annotations

import pytest

from codemigrator.analysis import (
    AnalysisArtifact,
    AnalysisResult,
    ExtractSubtree,
    FindCallees,
    FindCallers,
    FindReferences,
    FindSymbol,
    GotoDefinition,
    InMemoryProjectionStore,
    InMemorySnapshotSource,
    ProjectionKey,
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


def test_query_source_ast_reads_the_psf_projection_through_the_store_port() -> None:
    source = snapshot()
    projected = analyze_snapshot(source, descriptor())
    store = InMemoryProjectionStore()
    key = ProjectionKey(projected.snapshot_oid, projected.descriptor_sha256)
    store.write(AnalysisArtifact(key=key, result=projected))
    empty = AnalysisResult(
        snapshot_oid=projected.snapshot_oid,
        descriptor_sha256=projected.descriptor_sha256,
        capability=projected.capability,
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

    service = QuerySourceAst(
        result=empty,
        snapshot=source,
        projection_store=store,
        projection_key=key,
    )

    assert service.query(FindSymbol(kind="FIND_SYMBOL", symbol="run")).hits


def test_query_source_ast_uses_psf_index_for_references() -> None:
    service = query_service()

    result = service.query(FindReferences(kind="FIND_REFERENCES", symbol="helper"))

    assert result.hits
    assert all(hit.range.file_path == "src/main/main.py" for hit in result.hits)


def test_query_source_ast_resolves_definition_and_symbol_call_graph_ranges() -> None:
    service = query_service()
    references = service.query(FindReferences(kind="FIND_REFERENCES", symbol="helper"))
    use_site = references.hits[0].range

    definition = service.query(GotoDefinition(kind="GOTO_DEFINITION", use_site=use_site))
    callers = service.query(FindCallers(kind="FIND_CALLERS", symbol_or_range="helper"))
    callees = service.query(FindCallees(kind="FIND_CALLEES", symbol_or_range="helper"))

    assert definition.hits[0].range.file_path == "src/util/util.py"
    assert callers.hits[0].range == use_site
    assert callees.hits[0].range.file_path == "src/util/util.py"


def test_query_source_ast_applies_module_ids_without_path_substring_matching() -> None:
    service = query_service()
    main_module = next(
        module for module in service.result.modules if "src/main/main.py" in module.file_paths
    )

    result = service.query(
        FindSymbol(kind="FIND_SYMBOL", symbol="run", module=main_module.module_id)
    )

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
    assert response.error is StableErrorCode.TRUNCATED


def test_query_source_ast_rejects_limits_above_contract_caps() -> None:
    source = snapshot()
    result = analyze_snapshot(source, descriptor())

    with pytest.raises(ValueError, match="max_hits"):
        QuerySourceAst(result=result, snapshot=source, max_hits=201)
    with pytest.raises(ValueError, match="max_text_bytes"):
        QuerySourceAst(result=result, snapshot=source, max_text_bytes=256 * 1024 + 1)
    with pytest.raises(ValueError, match="timeout_seconds"):
        QuerySourceAst(result=result, snapshot=source, timeout_seconds=60.1)


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

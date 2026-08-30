from __future__ import annotations

from dataclasses import replace

from codemigrator.analysis import (
    AnalysisCapability,
    ArtifactRule,
    ExternalTarget,
    ImportRule,
    InMemorySnapshotSource,
    ManifestRule,
    SourceAnalysisDescriptor,
    SyntaxNode,
    TextRule,
    analyze_snapshot,
)
from codemigrator.core import ArtifactKind, ModuleBoundaryStrategy, StableErrorCode


def descriptor() -> SourceAnalysisDescriptor:
    return SourceAnalysisDescriptor(
        language_id="python",
        extensions=(".py",),
        module_boundary_strategy=ModuleBoundaryStrategy.DirectoryConvention,
        test_patterns=("tests/", "*_test.py"),
        import_rules=(
            ImportRule(
                pattern=(
                    r"^\s*from\s+(?P<target>[A-Za-z_][\w./]*)\s+import\s+"
                    r"(?P<symbol>[A-Za-z_]\w*|\*)"
                ),
                target_group="target",
                symbol_group="symbol",
            ),
            ImportRule(
                pattern=r"^\s*import\s+(?P<target>[A-Za-z_][\w.]*)",
                target_group="target",
            ),
        ),
        export_rules=(
            TextRule(pattern=r"^\s*def\s+(?P<symbol>[A-Za-z_]\w*)\s*\(", kind="FUNCTION"),
            TextRule(pattern=r"^\s*class\s+(?P<symbol>[A-Za-z_]\w*)\b", kind="CLASS"),
        ),
        test_function_rules=(TextRule(pattern=r"^\s*def\s+(?P<symbol>test_\w+)", kind="FUNCTION"),),
        assertion_rules=(TextRule(pattern=r"\bassert\b", kind="ASSERTION"),),
        manifest_rules=(ManifestRule(pattern="pyproject.toml", manifest_kind="pyproject.toml"),),
        artifact_rules=(ArtifactRule(pattern="*.sql", artifact_kind=ArtifactKind.ResourceFile),),
    )


def snapshot() -> InMemorySnapshotSource:
    return InMemorySnapshotSource(
        snapshot_oid="1" * 40,
        files={
            "src/main/main.py": b"from src.util import helper\n\ndef run():\n    return helper()\n",
            "src/util/util.py": b"def helper():\n    assert True\n    return 1\n",
            "tests/test_main.py": (
                b"from src.main import run\n\ndef test_run():\n    assert run() == 1\n"
            ),
            "pyproject.toml": b"[project]\nname = 'demo'\n",
            "schema.sql": b"create table demo (id integer);\n",
            ".git/config": b"[core]\nrepositoryformatversion = 0\n",
        },
    )


def test_pipeline_is_read_only_deterministic_and_produces_f1_to_f4() -> None:
    source = snapshot()
    before = source.read_count

    first = analyze_snapshot(source, descriptor())
    second = analyze_snapshot(source, descriptor())

    assert first.capability is AnalysisCapability.Full
    assert [module.role.value for module in first.modules].count("SOURCE") == 2
    assert [module.role.value for module in first.modules].count("TEST") == 1
    assert first.manifests[0].manifest_path == "pyproject.toml"
    assert first.artifacts[0].path == "schema.sql"
    assert first.canonical_bytes == second.canonical_bytes
    assert first.canonical_sha256 == second.canonical_sha256
    assert source.read_count - before == len(source.paths) * 2
    assert ".git/config" not in {path for module in first.modules for path in module.file_paths}


def test_pipeline_marks_unresolved_imports_unknown_with_evidence() -> None:
    source = InMemorySnapshotSource(
        snapshot_oid="2" * 40,
        files={"src/main.py": b"import missing_package\n"},
    )
    result = analyze_snapshot(source, descriptor())

    assert len(result.imports) == 1
    assert result.imports[0].confidence.value == "UNKNOWN"
    assert result.imports[0].reason.value == "UNRESOLVED_PATH"
    assert result.imports[0].evidence.file_path == "src/main.py"


def test_pipeline_resolves_manifest_dependencies_as_external_targets() -> None:
    source = InMemorySnapshotSource(
        snapshot_oid="7" * 40,
        files={
            "src/main.py": b"import requests\n",
            "pyproject.toml": b"[project]\ndependencies = ['requests>=2']\n",
        },
    )

    result = analyze_snapshot(source, descriptor())

    assert isinstance(result.imports[0].to, ExternalTarget)
    assert result.imports[0].to.package == "requests"


def test_text_fallback_is_explicit_and_does_not_build_symbol_index() -> None:
    fallback = replace(descriptor(), text_fallback=True, grammar_id=None, grammar_sha256=None)

    result = analyze_snapshot(snapshot(), fallback)

    assert result.capability is AnalysisCapability.TextFallback
    assert result.symbol_bindings == []
    assert result.reference_sites == []


def test_unsupported_snapshot_paths_and_large_files_are_not_analyzed() -> None:
    source = InMemorySnapshotSource(
        snapshot_oid="3" * 40,
        files={".git/config": b"bad", "src/large.py": b"x" * 20},
    )
    tiny = replace(descriptor(), max_file_bytes=10)

    result = analyze_snapshot(source, tiny)

    assert result.modules == []
    assert result.skipped_files == ["src/large.py"]
    assert result.errors[0].code.value == "SOURCE_FILE_TOO_LARGE"


def test_manifest_parse_errors_are_recorded_without_blocking_other_facts() -> None:
    source = InMemorySnapshotSource(
        snapshot_oid="4" * 40,
        files={"src/main.py": b"def main():\n    pass\n", "pyproject.toml": b"not = [valid"},
    )

    result = analyze_snapshot(source, descriptor())

    assert result.modules
    assert result.manifests == []
    assert result.errors[0].code.value == StableErrorCode.ANALYSIS_INFRA_ERROR.value


def test_directory_convention_fallback_derives_coverage_without_import_edges() -> None:
    source = InMemorySnapshotSource(
        snapshot_oid="6" * 40,
        files={
            "src/main/main.py": b"def main():\n    return 1\n",
            "tests/test_main.py": b"def test_main():\n    assert True\n",
        },
    )

    result = analyze_snapshot(source, descriptor())

    assert len(result.coverage) == 1
    assert result.coverage[0].derivation.value == "DIRECTORY_CONVENTION"
    assert result.coverage[0].tested_modules


def test_relation_graph_contains_and_orients_module_edges() -> None:
    result = analyze_snapshot(snapshot(), descriptor())
    test_module = next(module for module in result.modules if module.role.value == "TEST")
    main_module = next(
        module for module in result.modules if "src/main/main.py" in module.file_paths
    )

    contains = [edge for edge in result.relation_edges if edge.kind == "CONTAINS"]
    coverage = [edge for edge in result.relation_edges if edge.kind == "COVERAGE"]

    assert {edge.file_path for edge in contains} == {
        "src/main/main.py",
        "src/util/util.py",
        "tests/test_main.py",
    }
    assert any(
        edge.from_module == test_module.module_id and edge.to_module == main_module.module_id
        for edge in coverage
    )


def test_parser_failures_are_isolated_by_grammar_circuit_breaker() -> None:
    calls = 0

    def broken_parser(_: bytes) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("parser crashed")

    result = analyze_snapshot(snapshot(), descriptor(), parser=broken_parser)

    assert calls == 2
    assert len(result.errors) == 3
    assert result.modules


def test_ast_nodes_are_retained_as_the_source_of_exports_and_degraded_files() -> None:
    def parser(_: bytes) -> SyntaxNode:
        return SyntaxNode(
            kind="module",
            start_byte=0,
            end_byte=4,
            children=(
                SyntaxNode(kind="function_definition", name="run", start_byte=0, end_byte=4),
                SyntaxNode(kind="ERROR", start_byte=4, end_byte=4),
            ),
        )

    source = InMemorySnapshotSource(snapshot_oid="8" * 40, files={"src/main.py": b"????"})
    result = analyze_snapshot(source, descriptor(), parser=parser)

    assert [export.symbol for module in result.modules for export in module.exported_symbols] == [
        "run"
    ]
    assert result.modules[0].degraded_files == ["src/main.py"]


def test_reference_and_call_queries_use_actual_symbol_sites() -> None:
    source = snapshot()
    result = analyze_snapshot(source, descriptor())
    service = __import__("codemigrator.analysis", fromlist=["QuerySourceAst"]).QuerySourceAst(
        result=result, snapshot=source
    )

    references = service.query(
        __import__("codemigrator.analysis", fromlist=["FindReferences"]).FindReferences(
            kind="FIND_REFERENCES", symbol="helper"
        )
    )
    callers = service.query(
        __import__("codemigrator.analysis", fromlist=["FindCallers"]).FindCallers(
            kind="FIND_CALLERS", symbol_or_range="helper"
        )
    )

    assert references.hits
    assert all(hit.range.start.line == 4 for hit in references.hits)
    assert callers.hits
    assert all(hit.range.file_path == "src/main/main.py" for hit in callers.hits)


def test_alias_imports_bind_reference_sites_to_the_exported_symbol() -> None:
    aliased = replace(
        descriptor(),
        import_rules=(
            ImportRule(
                pattern=(
                    r"^\s*from\s+(?P<target>[A-Za-z_][\w./]*)\s+import\s+"
                    r"(?P<symbol>[A-Za-z_]\w*)\s+as\s+(?P<alias>[A-Za-z_]\w*)"
                ),
                symbol_group="symbol",
            ),
        ),
    )
    source = InMemorySnapshotSource(
        snapshot_oid="9" * 40,
        files={
            "src/main.py": b"from src.util import helper as h\n\nh()\n",
            "src/util.py": b"def helper():\n    return 1\n",
        },
    )

    result = analyze_snapshot(source, aliased)

    assert [
        (reference.symbol, reference.site.start.line) for reference in result.reference_sites
    ] == [("helper", 3)]

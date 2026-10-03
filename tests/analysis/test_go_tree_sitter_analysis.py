from __future__ import annotations

import posixpath

import pytest

from codemigrator.analysis import (
    AnalysisFailure,
    EdgeConfidence,
    ExternalTarget,
    InMemorySnapshotSource,
    ModuleTarget,
    UnknownReason,
    analyze_snapshot,
)
from codemigrator.runtime.project_migration import _go_analysis_descriptor, _go_parser


def _module_by_directory(result):
    return {
        posixpath.dirname(str(module.file_paths[0])) or ".": module.module_id
        for module in result.modules
        if module.role.value == "SOURCE"
    }


def test_go_tree_sitter_derives_manifest_imports_alias_references_and_unknowns() -> None:
    parser = _go_parser()
    assert parser is not None, "the checked-in tree-sitter Go grammar must be loadable"
    source = InMemorySnapshotSource(
        snapshot_oid="a" * 40,
        files={
            "go.mod": (
                b"module douyin\n\nrequire (\n"
                b" example.com/shared v1.2.3\n"
                b" example.com/extra v0.4.0 // indirect\n"
                b")\n"
            ),
            "cmd/server/main.go": (
                b'package main\n\nimport (\n'
                b' svc "douyin/internal/service"\n'
                b' "example.com/shared/dep"\n'
                b')\n\n'
                b'func Main() { svc.Handle(); dep.Do(); plugin.Open(path) }\n'
            ),
            "internal/service/handle.go": (
                b'package service\nimport "douyin"\n\n'
                b"func Handle() { douyin.Root() }\n"
            ),
            "root.go": b"package douyin\n\nfunc Root() {}\n",
        },
    )

    result = analyze_snapshot(source, _go_analysis_descriptor(), parser=parser)
    modules = _module_by_directory(result)

    assert result.capability.value == "FULL"
    assert result.manifests[0].module_path == "douyin"
    assert [(item.name, item.version) for item in result.manifests[0].dependencies] == [
        ("example.com/extra", "v0.4.0"),
        ("example.com/shared", "v1.2.3"),
    ]

    static_edges = [edge for edge in result.imports if edge.confidence is EdgeConfidence.Static]
    internal = [edge for edge in static_edges if edge.from_module == modules["cmd/server"]]
    assert len(internal) == 2
    internal_edge = next(edge for edge in internal if isinstance(edge.to, ModuleTarget))
    external_edge = next(edge for edge in internal if isinstance(edge.to, ExternalTarget))
    assert internal_edge.to.module_id == modules["internal/service"]
    assert external_edge.to.package == "example.com/shared"
    root_import = next(
        edge
        for edge in result.imports
        if edge.evidence.file_path == "internal/service/handle.go"
    )
    assert isinstance(root_import.to, ModuleTarget)
    assert root_import.to.module_id == modules["."]

    dynamic = [edge for edge in result.imports if edge.confidence is EdgeConfidence.Unknown]
    assert len(dynamic) == 1
    assert dynamic[0].reason is UnknownReason.DynamicImport
    assert dynamic[0].evidence.file_path == "cmd/server/main.go"

    handle_binding = [
        binding
        for binding in result.symbol_bindings
        if binding.symbol == "Handle"
    ]
    assert len(handle_binding) == 1
    assert handle_binding[0].definition.file_path == "internal/service/handle.go"

    references = [reference for reference in result.reference_sites if reference.symbol == "Handle"]
    assert len(references) == 1
    assert references[0].site.file_path == "cmd/server/main.go"
    assert references[0].site.start.line == 8
    assert references[0].binding == handle_binding[0].definition

    calls = [call for call in result.call_edges if call.symbol == "Handle"]
    assert len(calls) == 1
    assert calls[0].caller == references[0].site
    assert calls[0].callee == handle_binding[0].definition


def test_go_full_analysis_without_a_parser_fails_closed() -> None:
    source = InMemorySnapshotSource(
        snapshot_oid="b" * 40,
        files={"main.go": b"package main\nfunc Main() {}\n"},
    )

    with pytest.raises(AnalysisFailure, match="required tree-sitter parser is unavailable"):
        analyze_snapshot(source, _go_analysis_descriptor())

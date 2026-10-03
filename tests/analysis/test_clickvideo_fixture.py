from __future__ import annotations

import json
from pathlib import Path

from codemigrator.analysis import (
    EdgeConfidence,
    InMemorySnapshotSource,
    ModuleTarget,
    analyze_snapshot,
)
from codemigrator.runtime.project_migration import _go_analysis_descriptor, _go_parser


def test_clickvideo_analysis_fixture_contains_golden_candidate_sets() -> None:
    fixture_root = Path(__file__).parents[2] / "test_fixtures" / "clickvideo-analysis"
    fixture = json.loads((fixture_root / "golden.json").read_text(encoding="utf-8"))
    snapshot_data = json.loads((fixture_root / "snapshot.json").read_text(encoding="utf-8"))
    source = InMemorySnapshotSource(
        snapshot_oid=snapshot_data["snapshot_oid"],
        files={path: content.encode("utf-8") for path, content in snapshot_data["files"].items()},
    )
    parser = _go_parser()
    assert parser is not None
    result = analyze_snapshot(source, _go_analysis_descriptor(), parser=parser)

    assert fixture["fixture"] == "click-video"
    actual_source_files = sorted(
        str(path)
        for module in result.modules
        if module.role.value == "SOURCE"
        for path in module.file_paths
    )
    assert actual_source_files == fixture["expected_source_files"]
    module_keys = {
        module.module_id: (
            str(module.file_paths[0]).rsplit("/", 1)[0]
            + ("#test" if module.role.value == "TEST" else "")
        )
        for module in result.modules
    }
    actual_edges = {
        (module_keys[edge.from_module], module_keys[edge.to.module_id])
        for edge in result.imports
        if edge.confidence is EdgeConfidence.Static and isinstance(edge.to, ModuleTarget)
    }
    assert actual_edges == {tuple(edge) for edge in fixture["static_import_edges"]}
    assert fixture["candidate_modules"]
    assert fixture["static_false_positive_edges"] == []
    actual_bindings = sorted(
        (str(binding.definition.file_path), binding.symbol, binding.definition.start.line)
        for binding in result.symbol_bindings
    )
    actual_references = sorted(
        (str(reference.site.file_path), reference.symbol, reference.site.start.line)
        for reference in result.reference_sites
    )
    actual_calls = sorted(
        (
            str(call.caller.file_path),
            call.symbol,
            call.caller.start.line,
            str(call.callee.file_path),
        )
        for call in result.call_edges
    )
    assert actual_bindings == [tuple(item) for item in fixture["expected_symbol_bindings"]]
    assert actual_references == [tuple(item) for item in fixture["expected_reference_sites"]]
    assert actual_calls == [tuple(item) for item in fixture["expected_call_edges"]]
    assert actual_edges
    assert actual_references

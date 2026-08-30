from __future__ import annotations

import json
from pathlib import Path

from codemigrator.analysis import (
    EdgeConfidence,
    ImportRule,
    InMemorySnapshotSource,
    ModuleTarget,
    SourceAnalysisDescriptor,
    TextRule,
    analyze_snapshot,
)
from codemigrator.core import ModuleBoundaryStrategy


def test_clickvideo_analysis_fixture_contains_golden_candidate_sets() -> None:
    fixture_root = Path(__file__).parents[2] / "test_fixtures" / "clickvideo-analysis"
    fixture = json.loads((fixture_root / "golden.json").read_text(encoding="utf-8"))
    snapshot_data = json.loads((fixture_root / "snapshot.json").read_text(encoding="utf-8"))
    source = InMemorySnapshotSource(
        snapshot_oid=snapshot_data["snapshot_oid"],
        files={path: content.encode("utf-8") for path, content in snapshot_data["files"].items()},
    )
    descriptor = SourceAnalysisDescriptor(
        language_id="go",
        extensions=(".go",),
        module_boundary_strategy=ModuleBoundaryStrategy.DirectoryConvention,
        test_patterns=("*_test.go",),
        import_rules=(
            ImportRule(
                pattern=r'^\s*import\s+"(?P<target>[A-Za-z_][\w/]*)"',
            ),
        ),
        export_rules=(
            TextRule(pattern=r"^\s*func\s+(?P<symbol>[A-Za-z_]\w*)\s*\(", kind="FUNCTION"),
        ),
    )
    result = analyze_snapshot(source, descriptor)

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

from uuid import uuid4

import pytest
from pydantic import ValidationError

from codemigrator.analysis import (
    AnalysisCapability,
    AnalysisResult,
    ArtifactFact,
    CallEdge,
    EdgeConfidence,
    ExportSummary,
    ImportEdge,
    ModuleBoundary,
    ModuleFact,
    ModuleRole,
    ModuleTarget,
    SourcePosition,
    SourceRange,
    SymbolBinding,
    SymbolKind,
    UnknownReason,
)
from codemigrator.analysis.canonical import canonical_bytes as analysis_canonical_bytes
from codemigrator.core import ArtifactKind, ProjectModuleId, RepoRelativePath


def source_range(path: str = "src/main.py") -> SourceRange:
    return SourceRange(
        file_path=RepoRelativePath(path),
        start=SourcePosition(line=1, column=0),
        end=SourcePosition(line=1, column=4),
    )


def test_analysis_models_are_closed_and_ranges_are_one_based() -> None:
    with pytest.raises(ValidationError):
        SourcePosition(line=0, column=0)
    with pytest.raises(ValidationError):
        SourceRange(**{**source_range().model_dump(), "write_scope": ["src"]})


def test_fact_models_retain_public_enums_and_json_pointer_safe_values() -> None:
    module_id = ProjectModuleId(uuid4())
    module = ModuleFact(
        module_id=module_id,
        file_paths=[RepoRelativePath("src/main.py")],
        role=ModuleRole.Source,
        boundary=ModuleBoundary.Directory,
        exported_symbols=[
            ExportSummary(
                symbol="run",
                kind=SymbolKind.Function,
                signature_text="def run()",
            )
        ],
        capability=AnalysisCapability.Full,
        degraded_files=[],
    )
    edge = ImportEdge(
        from_module=module_id,
        to=ModuleTarget(module_id=module_id),
        confidence=EdgeConfidence.Unknown,
        reason=UnknownReason.UnresolvedPath,
        evidence=source_range(),
    )
    artifact = ArtifactFact(
        path=RepoRelativePath("schema.sql"),
        artifact_kind=ArtifactKind.ResourceFile,
        source_path=None,
    )

    assert module.model_dump(mode="json")["role"] == "SOURCE"
    assert edge.model_dump(mode="json")["reason"] == "UNRESOLVED_PATH"
    assert artifact.model_dump(mode="json")["artifact_kind"] == "RESOURCE_FILE"


def test_symbol_binding_cannot_be_ambiguous_without_an_explicit_flag() -> None:
    binding = SymbolBinding(
        symbol="run",
        kind=SymbolKind.Function,
        definition=source_range(),
        signature_text="def run()",
    )

    assert binding.ambiguous is False


def test_empty_analysis_result_has_stable_canonical_bytes() -> None:
    result = AnalysisResult(
        snapshot_oid="a" * 40,
        descriptor_sha256="b" * 64,
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

    assert result.canonical_bytes == result.canonical_bytes
    assert len(result.canonical_sha256) == 64


def test_call_edge_keeps_symbol_level_caller_and_callee_ranges() -> None:
    caller = source_range("src/main.py")
    callee = source_range("src/util.py")

    edge = CallEdge(symbol="helper", caller=caller, callee=callee)

    assert edge.symbol == "helper"
    assert edge.caller.file_path == "src/main.py"
    assert edge.callee.file_path == "src/util.py"


def test_analysis_canonicalization_owns_projection_array_order() -> None:
    assert analysis_canonical_bytes({"items": [2, 1]}) == b'{"items":[1,2]}'

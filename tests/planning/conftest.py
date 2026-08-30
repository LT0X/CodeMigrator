from __future__ import annotations

import json
import uuid

import pytest

from codemigrator.analysis import (
    AnalysisCapability,
    AnalysisResult,
    ModuleBoundary,
    ModuleFact,
    ModuleRole,
)
from codemigrator.core import (
    ArtifactRef,
    CheckAction,
    DescriptorLock,
    DescriptorResolution,
    DossierBudgetTier,
    DossierEntry,
    FrozenArtifactBundle,
    InMemoryDescriptorRegistry,
    MigrationRulebook,
    MigrationSpec,
    RequiredCheckSelection,
    SpecScope,
    TargetProjectBlueprint,
    UnderstandingDossier,
    validate_spec_bytes,
)
from codemigrator.core.spec import SpecArtifact


def module_id(number: int) -> uuid.UUID:
    return uuid.UUID(f"00000000-0000-7000-8000-{number:012d}")


def artifact_ref(char: str) -> ArtifactRef:
    return ArtifactRef(sha256=char * 64, size=1, media_type="application/json")


def spec() -> MigrationSpec:
    return MigrationSpec(
        schema="codemigrator.migration-spec",
        version=3,
        name="planning-test",
        source_language_id="typescript",
        target_language_id="python",
        descriptor_lock=DescriptorLock(
            descriptor_version="1.0.0",
            source_descriptor_sha256="a" * 64,
            target_descriptor_sha256="b" * 64,
            toolchain_image_digest="c" * 64,
        ),
        scope=SpecScope(include=["src/", "tests/"]),
        required_checks=[
            RequiredCheckSelection(action=CheckAction.Compile, template_sha256="d" * 64)
        ],
    )


def spec_artifact() -> SpecArtifact:
    descriptor = DescriptorResolution(
        source_language_id="typescript",
        target_language_id="python",
        descriptor_version="1.0.0",
        source_descriptor_sha256="a" * 64,
        target_descriptor_sha256="b" * 64,
        toolchain_image_digest="c" * 64,
        checks=(
            RequiredCheckSelection(action=CheckAction.Compile, template_sha256="d" * 64),
        ),
        grammar_available=True,
        image_available=True,
    )
    result = validate_spec_bytes(
        json.dumps(spec().model_dump(mode="json", by_alias=True)).encode(),
        registry=InMemoryDescriptorRegistry({("typescript", "python"): descriptor}),
    )
    assert result.accepted
    return SpecArtifact.from_result(result)


def dossier() -> UnderstandingDossier:
    entry = DossierEntry(
        kind="architecture", content="module facts", anchors=[], advisory=True
    )
    return UnderstandingDossier(
        architecture_narrative=[entry],
        semantic_modules=[],
        dependency_resolutions=[],
        test_map=[],
        risk_hotspots=[],
        strategy_advice=[],
        coverage_self_report={},
        budget_tier=DossierBudgetTier.Shallow,
    )


def analysis_result() -> AnalysisResult:
    modules = [
        ModuleFact(
            module_id=module_id(1),
            file_paths=["src/a.ts"],
            role=ModuleRole.Source,
            boundary=ModuleBoundary.File,
            exported_symbols=[],
            capability=AnalysisCapability.Full,
            degraded_files=[],
        ),
        ModuleFact(
            module_id=module_id(2),
            file_paths=["src/b.ts"],
            role=ModuleRole.Source,
            boundary=ModuleBoundary.File,
            exported_symbols=[],
            capability=AnalysisCapability.Full,
            degraded_files=[],
        ),
    ]
    return AnalysisResult(
        snapshot_oid="snapshot-1",
        descriptor_sha256="e" * 64,
        capability=AnalysisCapability.Full,
        modules=modules,
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


@pytest.fixture
def planning_inputs() -> object:
    from codemigrator.planning import PlanningInputs

    return PlanningInputs(
        frozen_artifacts=FrozenArtifactBundle(
            spec=artifact_ref("1"),
            understanding_dossier=artifact_ref("2"),
            target_project_blueprint=artifact_ref("3"),
            migration_rulebook=artifact_ref("4"),
        ),
        spec=spec(),
        understanding_dossier=dossier(),
        target_project_blueprint=TargetProjectBlueprint(
            module_boundaries=[],
            granularity_principles=["preserve module boundaries"],
            target_layout_principles=["use src layout"],
            parallelism_rules=["independent modules may run in parallel"],
            generated_artifact_policy="regenerate generated code",
            version=1,
        ),
        migration_rulebook=MigrationRulebook(entries=[], version=1),
        analysis=analysis_result(),
        snapshot_oid="snapshot-1",
    )

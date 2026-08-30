import json
from collections.abc import Sequence

import pytest

from codemigrator.core import (
    CheckAction,
    Decomposition,
    DescriptorLock,
    DescriptorResolution,
    DossierBudgetTier,
    DossierEntry,
    InMemoryDescriptorRegistry,
    MigrationRulebook,
    MigrationSpec,
    RequiredCheckSelection,
    SpecArtifact,
    SpecScope,
    TargetProjectBlueprint,
    UnderstandingDossier,
    validate_spec_bytes,
)
from codemigrator.runtime.draft_models import DraftArtifacts


def make_dossier(paths: Sequence[str] = ("src/a.py",)) -> UnderstandingDossier:
    entries = [
        DossierEntry(
            kind="semantic-module",
            content="source module",
            anchors=[
                {"file": path, "start_line": 1, "end_line": 2}
                for path in paths
            ],
            advisory=False,
        )
    ]
    return UnderstandingDossier(
        architecture_narrative=entries,
        semantic_modules=entries,
        dependency_resolutions=[],
        test_map=[],
        risk_hotspots=[],
        strategy_advice=[],
        coverage_self_report={},
        budget_tier=DossierBudgetTier.Deep,
    )


def make_spec(include: Sequence[str] = ("src/",)) -> MigrationSpec:
    return MigrationSpec(
        schema="codemigrator.migration-spec",
        version=3,
        name="draft-test",
        description="draft test fixture",
        source_language_id="python",
        target_language_id="rust",
        descriptor_lock=DescriptorLock(
            descriptor_version="1.0.0",
            source_descriptor_sha256="a" * 64,
            target_descriptor_sha256="b" * 64,
            toolchain_image_digest="c" * 64,
        ),
        scope=SpecScope(include=list(include)),
        required_checks=[
            RequiredCheckSelection(action=CheckAction.Compile, template_sha256="d" * 64),
            RequiredCheckSelection(action=CheckAction.Test, template_sha256="e" * 64),
        ],
        decomposition=Decomposition(module_granularity="module"),
    )


def make_spec_artifact() -> SpecArtifact:
    registry = InMemoryDescriptorRegistry(
        {
            ("python", "rust"): DescriptorResolution(
                source_language_id="python",
                target_language_id="rust",
                descriptor_version="1.0.0",
                source_descriptor_sha256="a" * 64,
                target_descriptor_sha256="b" * 64,
                toolchain_image_digest="c" * 64,
                checks=(
                    RequiredCheckSelection(
                        action=CheckAction.Compile,
                        template_sha256="d" * 64,
                    ),
                    RequiredCheckSelection(
                        action=CheckAction.Test,
                        template_sha256="e" * 64,
                    ),
                ),
                grammar_available=True,
                image_available=True,
            )
        }
    )
    payload = make_spec().model_dump(mode="json", by_alias=True)
    result = validate_spec_bytes(json.dumps(payload).encode(), registry=registry)
    assert result.accepted
    return SpecArtifact.from_result(result)


def make_artifacts() -> DraftArtifacts:
    return DraftArtifacts(
        spec=make_spec_artifact(),
        understanding_dossier=make_dossier(),
        target_project_blueprint=TargetProjectBlueprint(
            module_boundaries=[],
            granularity_principles=["preserve module boundaries"],
            target_layout_principles=["mirror source layout"],
            parallelism_rules=["independent modules may run in parallel"],
            generated_artifact_policy="review before delivery",
            version=1,
        ),
        migration_rulebook=MigrationRulebook(entries=[], version=1),
    )


@pytest.fixture
def artifacts() -> DraftArtifacts:
    return make_artifacts()

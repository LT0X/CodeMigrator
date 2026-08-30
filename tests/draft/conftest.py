from collections.abc import Sequence

import pytest

from codemigrator.core import (
    Decomposition,
    DescriptorLock,
    DossierBudgetTier,
    DossierEntry,
    MigrationRulebook,
    MigrationSpec,
    SpecScope,
    TargetProjectBlueprint,
    UnderstandingDossier,
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
        schema="migration-spec-v3",
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
        required_checks=[],
        decomposition=Decomposition(module_granularity="module"),
    )


def make_artifacts() -> DraftArtifacts:
    return DraftArtifacts(
        spec=make_spec(),
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

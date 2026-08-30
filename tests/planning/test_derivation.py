from __future__ import annotations

from codemigrator.analysis import (
    AnalysisCapability,
    ArtifactFact,
    ArtifactKind,
    ModuleCoverage,
    ModuleCoverageStatus,
    ReferenceSite,
    SourcePosition,
    SourceRange,
)
from codemigrator.core import ProjectModuleId, SliceKind
from codemigrator.planning import (
    ArtifactAction,
    derive_artifact_tasks,
    derive_plan_proposal,
    normalize_group_names,
    resolve_test_generation_anchor,
)

from .conftest import module_id


def test_slice_derivation_keeps_test_translation_and_generation_tracks_disjoint() -> None:
    from .conftest import analysis_result

    analysis = analysis_result().model_copy(
        update={
            "coverage_status": [
                ModuleCoverageStatus(
                    module=ProjectModuleId(module_id(1)), status=ModuleCoverage.EmptyTestSuite
                ),
                ModuleCoverageStatus(
                    module=ProjectModuleId(module_id(2)), status=ModuleCoverage.Undetermined
                ),
            ]
        }
    )
    from codemigrator.core import FrozenArtifactBundle, MigrationRulebook, TargetProjectBlueprint
    from codemigrator.planning import PlanningInputs

    from .conftest import artifact_ref, dossier, spec

    inputs = PlanningInputs(
        frozen_artifacts=FrozenArtifactBundle(
            spec=artifact_ref("1"), understanding_dossier=artifact_ref("2"),
            target_project_blueprint=artifact_ref("3"), migration_rulebook=artifact_ref("4"),
        ),
        spec=spec(), understanding_dossier=dossier(),
        target_project_blueprint=TargetProjectBlueprint(
            module_boundaries=[], granularity_principles=[], target_layout_principles=[],
            parallelism_rules=[], generated_artifact_policy="regenerate", version=1,
        ),
        migration_rulebook=MigrationRulebook(entries=[], version=1), analysis=analysis,
        snapshot_oid="snapshot-1",
    )

    proposal = derive_plan_proposal(inputs)
    kinds = {(slice_.kind, tuple(slice_.source_modules)) for slice_ in proposal.slices}

    assert (SliceKind.Implementation, (module_id(1),)) in kinds
    assert (SliceKind.Implementation, (module_id(2),)) in kinds
    assert (SliceKind.TestGeneration, (module_id(1),)) in kinds
    assert all(slice_.kind is not SliceKind.TestTranslation for slice_ in proposal.slices)
    generated = next(
        slice_ for slice_ in proposal.slices if slice_.kind is SliceKind.TestGeneration
    )
    assert generated.artifact_tasks == ()


def test_artifact_derivation_is_data_driven_and_keeps_resource_out_of_translation_scope() -> None:
    tasks = derive_artifact_tasks(
        [
            ArtifactFact(
                path="schema.pb.go",
                artifact_kind=ArtifactKind.GeneratedCode,
                source_path="schema.proto",
            ),
            ArtifactFact(path="docker-compose.yml", artifact_kind=ArtifactKind.DeclarativeConfig),
            ArtifactFact(path="schema.sql", artifact_kind=ArtifactKind.ResourceFile),
        ],
        owner_local_ref="CT",
    )

    actions = {task.kind: task.action for task in tasks}
    assert actions[ArtifactKind.GeneratedCode] is ArtifactAction.Generate
    assert actions[ArtifactKind.DeclarativeConfig] is ArtifactAction.Translate
    assert actions[ArtifactKind.ResourceFile] is ArtifactAction.Copy
    assert (
        next(task for task in tasks if task.kind is ArtifactKind.GeneratedCode).source_path
        == "schema.proto"
    )
    assert (
        next(task for task in tasks if task.kind is ArtifactKind.ResourceFile).translation is False
    )


def test_ambiguous_or_text_fallback_anchor_degrades_to_module_summary(
    planning_inputs: object,
) -> None:
    analysis = planning_inputs.analysis.model_copy(
        update={
            "capability": AnalysisCapability.TextFallback,
            "reference_sites": [
                ReferenceSite(
                    symbol="run",
                    site=SourceRange(
                        file_path="src/a.ts",
                        start=SourcePosition(line=1, column=0),
                        end=SourcePosition(line=1, column=3),
                    ),
                    ambiguous=True,
                )
            ],
        }
    )

    anchor = resolve_test_generation_anchor(ProjectModuleId(module_id(1)), analysis)

    assert anchor.level == "MODULE"
    assert anchor.degraded is True
    assert "fallback" in anchor.reason.lower()


def test_group_names_are_stable_and_unique_after_normalization() -> None:
    assert normalize_group_names(["User Models", "user-models", "!!!"]) == (
        "user-models",
        "user-models-2",
        "group",
    )

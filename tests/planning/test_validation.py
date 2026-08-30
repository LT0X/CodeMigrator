from __future__ import annotations

import pytest

from codemigrator.analysis import (
    CoverageDerivation,
    CoverageEntry,
    ModuleRole,
)
from codemigrator.core import (
    ArtifactKind,
    PlanEdgeKind,
    ProjectModuleId,
    SliceKind,
    StableErrorCode,
)
from codemigrator.planning import (
    ArtifactAction,
    ArtifactTask,
    EdgeProvenance,
    PlanEdgeProposal,
    PlanLedger,
    PlanningLimits,
    PlanProposal,
    PlanRejected,
    PlanSliceProposal,
    PlanValidator,
)

from .conftest import module_id


def proposal(*slices: PlanSliceProposal, edges: tuple[PlanEdgeProposal, ...] = ()) -> PlanProposal:
    return PlanProposal(
        slices=slices,
        edges=edges,
        integration_ranks={slice_.local_ref: index for index, slice_ in enumerate(slices)},
        planner_rationale=[],
    )


def slice_(
    local_ref: str,
    module: int,
    path: str,
    *,
    kind: SliceKind = SliceKind.Implementation,
    root: str = "target",
) -> PlanSliceProposal:
    return PlanSliceProposal(
        local_ref=local_ref,
        kind=kind,
        source_modules=[ProjectModuleId(module_id(module))],
        write_paths=[path],
        create_roots=[root],
    )


def edge(
    from_: str,
    to: str,
    *,
    kind: PlanEdgeKind = PlanEdgeKind.Requires,
    provenance: EdgeProvenance = EdgeProvenance.Structural,
) -> PlanEdgeProposal:
    return PlanEdgeProposal(from_=from_, to=to, kind=kind, provenance=provenance)


def codes(report: object) -> set[StableErrorCode]:
    return {violation.code for violation in report.violations}  # type: ignore[attr-defined]


def test_valid_proposal_passes_all_machine_guards(planning_inputs: object) -> None:
    plan = proposal(
        slice_("A", 1, "target/a.py", root="target/a"),
        slice_("B", 2, "target/b.py", root="target/b"),
        edges=(edge("A", "B"),),
    )

    report = PlanValidator().validate(plan, planning_inputs)

    assert report.accepted is True
    assert report.violations == ()
    assert report.source_coverage == {"src/a.ts": "A", "src/b.ts": "B"}
    assert report.cycle_check == "PASS"
    assert report.rank_check == "PASS"


def test_scope_conflict_is_rejected_even_when_ordered_before_is_present(
    planning_inputs: object,
) -> None:
    plan = proposal(
        slice_("A", 1, "target/shared.py", root="target/a"),
        slice_("B", 2, "target/shared.py", root="target/b"),
        edges=(edge("A", "B", kind=PlanEdgeKind.OrderedBefore),),
    )

    report = PlanValidator().validate(plan, planning_inputs)

    assert report.accepted is False
    assert StableErrorCode.PLAN_SCOPE_CONFLICT in codes(report)


def test_blueprint_boundary_rejects_paths_outside_declared_target_prefix(
    planning_inputs: object,
) -> None:
    inputs = planning_inputs.model_copy(
        update={
            "target_project_blueprint": planning_inputs.target_project_blueprint.model_copy(
                update={"module_boundaries": [{"target_path_prefix": "pkg"}]}
            )
        }
    )
    plan = proposal(slice_("A", 1, "target/a.py", root="target/a"))

    report = PlanValidator().validate(plan, inputs)

    assert report.accepted is False
    assert StableErrorCode.PLAN_BLUEPRINT_VIOLATION in codes(report)


@pytest.mark.parametrize(
    ("source_modules", "expected_code"),
    [
        ([1], StableErrorCode.PLAN_COVERAGE_INVALID),
        ([1, 1], StableErrorCode.PLAN_COVERAGE_INVALID),
    ],
)
def test_source_file_coverage_must_be_exactly_once(
    planning_inputs: object,
    source_modules: list[int],
    expected_code: StableErrorCode,
) -> None:
    slices = tuple(
        slice_(f"A{index}", module, f"target/{index}.py", root=f"target/{index}")
        for index, module in enumerate(source_modules)
    )
    plan = proposal(*slices)

    report = PlanValidator().validate(plan, planning_inputs)

    assert report.accepted is False
    assert expected_code in codes(report)


def test_every_slice_source_module_must_exist_in_analysis_facts(
    planning_inputs: object,
) -> None:
    plan = proposal(
        slice_("A", 1, "target/a.py", root="target/a"),
        slice_("B", 2, "target/b.py", root="target/b"),
        PlanSliceProposal(
            local_ref="TG-UNKNOWN",
            kind=SliceKind.TestGeneration,
            source_modules=[ProjectModuleId(module_id(99))],
            write_paths=["target/tests/generated.py"],
            create_roots=["target/tests"],
        ),
    )

    report = PlanValidator().validate(plan, planning_inputs)

    assert report.accepted is False
    assert StableErrorCode.PLAN_COVERAGE_INVALID in codes(report)


def test_cycle_is_rejected_before_freeze(planning_inputs: object) -> None:
    plan = proposal(
        slice_("A", 1, "target/a.py", root="target/a"),
        slice_("B", 2, "target/b.py", root="target/b"),
        edges=(edge("A", "B"), edge("B", "A")),
    )

    report = PlanValidator().validate(plan, planning_inputs)

    assert report.accepted is False
    assert StableErrorCode.PLAN_CYCLE in codes(report)


def test_self_edge_is_reported_as_a_cycle(planning_inputs: object) -> None:
    plan = proposal(
        slice_("A", 1, "target/a.py", root="target/a"),
        slice_("B", 2, "target/b.py", root="target/b"),
        edges=(edge("A", "A"),),
    )

    report = PlanValidator().validate(plan, planning_inputs)

    assert StableErrorCode.PLAN_CYCLE in codes(report)


def test_rank_must_increase_on_every_edge(planning_inputs: object) -> None:
    plan = PlanProposal(
        slices=(
            slice_("A", 1, "target/a.py", root="target/a"),
            slice_("B", 2, "target/b.py", root="target/b"),
        ),
        edges=(edge("A", "B"),),
        integration_ranks={"A": 1, "B": 0},
        planner_rationale=(),
    )

    report = PlanValidator().validate(plan, planning_inputs)

    assert report.accepted is False
    assert StableErrorCode.PLAN_RANK_INCONSISTENT in codes(report)


def test_unknown_import_cannot_be_submitted_as_requires(planning_inputs: object) -> None:
    plan = proposal(
        slice_("A", 1, "target/a.py", root="target/a"),
        slice_("B", 2, "target/b.py", root="target/b"),
        edges=(
            edge(
                "A",
                "B",
                provenance=EdgeProvenance.ImportUnknown,
            ),
        ),
    )

    report = PlanValidator().validate(plan, planning_inputs)

    assert report.accepted is False
    assert StableErrorCode.PLAN_EDGE_INVALID in codes(report)


def test_resource_artifact_cannot_use_a_translation_slice_write_scope(
    planning_inputs: object,
) -> None:
    implementation = slice_("A", 1, "target/a.py", root="target/a")
    plan = PlanProposal(
        slices=(
            implementation.model_copy(
                update={
                    "artifact_tasks": (
                        ArtifactTask(
                            kind=ArtifactKind.ResourceFile,
                            action=ArtifactAction.Copy,
                            source_path="schema.sql",
                            target_path="target/a/schema.sql",
                        ),
                    )
                }
            ),
            slice_("B", 2, "target/b.py", root="target/b"),
        ),
        edges=(),
        integration_ranks={"A": 0, "B": 1},
        planner_rationale=(),
    )

    report = PlanValidator().validate(plan, planning_inputs)

    assert report.accepted is False
    assert StableErrorCode.PLAN_SCOPE_CONFLICT in codes(report)


def test_test_slice_cannot_edge_to_its_tested_implementation(planning_inputs: object) -> None:
    analysis = planning_inputs.analysis.model_copy(
        update={
            "modules": [
                *planning_inputs.analysis.modules,
                planning_inputs.analysis.modules[0].model_copy(
                    update={
                        "module_id": module_id(3),
                        "file_paths": ["tests/a.test.ts"],
                        "role": ModuleRole.Test,
                    }
                ),
            ],
            "coverage": [
                CoverageEntry(
                    test_file="tests/a.test.ts",
                    tested_modules=[ProjectModuleId(module_id(1))],
                    derivation=CoverageDerivation.ImportGraph,
                )
            ],
        }
    )
    inputs = planning_inputs.model_copy(update={"analysis": analysis})
    plan = PlanProposal(
        slices=(
            slice_("I", 1, "target/a.py", root="target/a"),
            slice_("T", 3, "target/test_a.py", kind=SliceKind.TestTranslation, root="target/tests"),
        ),
        edges=(edge("T", "I"),),
        integration_ranks={"I": 0, "T": 1},
        planner_rationale=(),
    )

    report = PlanValidator().validate(plan, inputs)

    assert report.accepted is False
    assert StableErrorCode.PLAN_EDGE_INVALID in codes(report)


def test_size_limits_reject_a_proposal_without_persisting_partial_state(
    planning_inputs: object,
) -> None:
    inputs = planning_inputs.model_copy(
        update={
            "limits": PlanningLimits(
                max_slices=1,
                max_edges=500,
                max_write_paths_per_slice=200,
                max_total_write_paths=2000,
            )
        }
    )
    plan = proposal(
        slice_("A", 1, "target/a.py", root="target/a"),
        slice_("B", 2, "target/b.py", root="target/b"),
    )
    ledger = PlanLedger()

    with pytest.raises(PlanRejected) as raised:
        ledger.freeze(plan, inputs)

    assert getattr(raised.value, "code", None) is StableErrorCode.PLAN_SIZE_EXCEEDED
    assert ledger.records == ()

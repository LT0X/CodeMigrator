from __future__ import annotations

import pytest

from codemigrator.core import PlanEdgeKind, StableErrorCode
from codemigrator.planning import (
    EdgeProvenance,
    PlanEdgeProposal,
    PlanningLimits,
    PlanProposal,
    PlanSliceProposal,
    PlanValidator,
)


def empty_analysis_inputs(planning_inputs: object) -> object:
    return planning_inputs.model_copy(
        update={"analysis": planning_inputs.analysis.model_copy(update={"modules": []})}
    )


def bare_slice(index: int, *, paths: tuple[str, ...] = ()) -> PlanSliceProposal:
    return PlanSliceProposal(
        local_ref=f"S{index}",
        kind="IMPLEMENTATION",
        source_modules=(),
        write_paths=paths or (f"target/{index}.py",),
        create_roots=(f"target/root-{index}",),
    )


def make_proposal(
    slices: tuple[PlanSliceProposal, ...],
    edges: tuple[PlanEdgeProposal, ...] = (),
) -> PlanProposal:
    return PlanProposal(
        slices=slices,
        edges=edges,
        integration_ranks={slice_.local_ref: index for index, slice_ in enumerate(slices)},
        planner_rationale=(),
    )


@pytest.mark.parametrize(
    ("limits", "slices", "expected"),
    [
        (PlanningLimits(max_slices=100), 101, StableErrorCode.PLAN_SIZE_EXCEEDED),
        (
            PlanningLimits(max_slices=1000, max_edges=500),
            502,
            StableErrorCode.PLAN_SIZE_EXCEEDED,
        ),
    ],
)
def test_slice_and_edge_limits_are_configurable_and_reject_overflow(
    planning_inputs: object,
    limits: PlanningLimits,
    slices: int,
    expected: StableErrorCode,
) -> None:
    items = tuple(bare_slice(index) for index in range(slices))
    edges = tuple(
        PlanEdgeProposal(
            from_=f"S{index}",
            to=f"S{index + 1}",
            kind=PlanEdgeKind.OrderedBefore,
            provenance=EdgeProvenance.Structural,
        )
        for index in range(slices - 1)
    )
    inputs = empty_analysis_inputs(planning_inputs).model_copy(update={"limits": limits})

    report = PlanValidator().validate(make_proposal(items, edges), inputs)

    assert report.accepted is False
    assert expected in {violation.code for violation in report.violations}


def test_single_and_total_write_scope_limits_use_the_aligned_boundaries(
    planning_inputs: object,
) -> None:
    inputs = empty_analysis_inputs(planning_inputs).model_copy(
        update={
            "limits": PlanningLimits(
                max_slices=100,
                max_edges=500,
                max_write_paths_per_slice=200,
                max_total_write_paths=2000,
            )
        }
    )
    too_many_in_one = make_proposal(
        (bare_slice(0, paths=tuple(f"target/file-{index}.py" for index in range(201))),)
    )
    total_paths = tuple(
        f"target/bulk-{index}.py" for index in range(2001)
    )
    too_many_total = make_proposal((bare_slice(0, paths=total_paths),))

    first = PlanValidator().validate(too_many_in_one, inputs)
    second = PlanValidator(
        limits=PlanningLimits(
            max_slices=100,
            max_edges=500,
            max_write_paths_per_slice=3000,
            max_total_write_paths=2000,
        )
    ).validate(too_many_total, inputs)

    assert StableErrorCode.PLAN_SIZE_EXCEEDED in {
        violation.code for violation in first.violations
    }
    assert StableErrorCode.PLAN_SIZE_EXCEEDED in {
        violation.code for violation in second.violations
    }

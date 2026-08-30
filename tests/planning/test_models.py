from __future__ import annotations

import pytest
from pydantic import ValidationError

from codemigrator.core import ArtifactKind, DossierEntry, ProjectModuleId, SliceKind
from codemigrator.planning import (
    ArtifactAction,
    ArtifactTask,
    EdgeProvenance,
    PlanEdgeProposal,
    PlanProposal,
    PlanSliceProposal,
)

from .conftest import module_id


def rationale() -> list[DossierEntry]:
    return [DossierEntry(kind="planner", content="grouped by module", anchors=[], advisory=True)]


def test_plan_proposal_is_closed_and_normalizes_scope() -> None:
    proposal = PlanProposal(
        slices=[
            PlanSliceProposal(
                local_ref="A",
                kind=SliceKind.Implementation,
                source_modules=[ProjectModuleId(module_id(1))],
                write_paths=["z.py", "a.py", "z.py"],
                create_roots=["src"],
                rationale=rationale(),
            )
        ],
        edges=[],
        integration_ranks={"A": 0},
        planner_rationale=rationale(),
    )

    assert proposal.slices[0].write_paths == ("a.py", "z.py")
    assert proposal.slices[0].create_roots == ("src",)
    with pytest.raises(ValidationError):
        PlanProposal.model_validate({**proposal.model_dump(), "unexpected": True})


def test_plan_slice_rejects_duplicate_local_modules_and_invalid_ref() -> None:
    with pytest.raises(ValidationError):
        PlanSliceProposal(
            local_ref="not a ref",
            kind=SliceKind.Implementation,
            source_modules=[module_id(1)],
            write_paths=["src/a.py"],
            create_roots=[],
        )
    with pytest.raises(ValidationError):
        PlanSliceProposal(
            local_ref="A",
            kind=SliceKind.Implementation,
            source_modules=[module_id(1), module_id(1)],
            write_paths=["src/a.py"],
            create_roots=[],
        )


def test_edge_requires_known_provenance_and_closed_endpoints() -> None:
    edge = PlanEdgeProposal(
        from_="A", to="B", kind="REQUIRES", provenance=EdgeProvenance.ImportStatic
    )
    assert edge.from_ == "A"
    assert edge.model_dump(by_alias=True)["from"] == "A"
    with pytest.raises(ValidationError):
        PlanEdgeProposal(
            from_="A", to="B", kind="REQUIRES", provenance="unknown", extra=True
        )


def test_artifact_task_is_data_driven_and_kind_action_pairs_are_closed() -> None:
    task = ArtifactTask(
        kind=ArtifactKind.GeneratedCode,
        action=ArtifactAction.Generate,
        source_path="schema.proto",
        target_path="generated/messages.py",
    )
    assert task.translation is False
    assert task.generated is True
    with pytest.raises(ValidationError):
        ArtifactTask(
            kind=ArtifactKind.GeneratedCode,
            action=ArtifactAction.Translate,
            source_path="schema.proto",
            target_path="generated/messages.py",
        )


def test_test_generation_slice_carries_generated_quality_and_firewall_contract() -> None:
    generated = PlanSliceProposal(
        local_ref="TG-A",
        kind=SliceKind.TestGeneration,
        source_modules=[module_id(1)],
        write_paths=["target/tests/a_test.py"],
        create_roots=["target/tests"],
    )

    assert generated.generated is True
    assert generated.generation_tag == "GENERATED"
    assert generated.minimum_nontrivial_assertions == 1
    assert generated.information_firewall is True

import uuid

from codemigrator.core import RepairDecisionId, RunId, WriteScope, WriteScopeOut
from codemigrator.runtime.repair import (
    ActiveWriter,
    RepairSlice,
    evaluate_joint_repair_dispatch,
)


def scope(*paths: str, roots: tuple[str, ...] = ()) -> WriteScope:
    return WriteScope(out=WriteScopeOut(write_paths=list(paths), create_roots=list(roots)))


def test_joint_scope_requires_terminal_set_and_no_inflight_overlap():
    run_id = RunId(uuid.uuid4())
    decision_id = RepairDecisionId(uuid.uuid4())
    slices = (
        RepairSlice("slice-a", "INTEGRATED", scope("src/a.py")),
        RepairSlice("slice-b", "TERMINAL", scope("src/b.py")),
    )
    admitted = evaluate_joint_repair_dispatch(
        run_id=run_id, repair_decision_id=decision_id, repair_set=slices, active_writers=()
    )
    assert admitted.admitted is True
    assert admitted.session is not None
    assert admitted.session.run_id == run_id
    assert admitted.session.repair_decision_id == decision_id
    assert admitted.session.joint_write_scope.out.write_paths == ["src/a.py", "src/b.py"]


def test_nonterminal_repair_member_blocks_dispatch():
    result = evaluate_joint_repair_dispatch(
        run_id=RunId(uuid.uuid4()),
        repair_decision_id=RepairDecisionId(uuid.uuid4()),
        repair_set=(RepairSlice("slice-a", "RUNNING", scope("src/a.py")),),
        active_writers=(),
    )
    assert result.admitted is False
    assert result.reason == "REPAIR_SET_NOT_TERMINAL"


def test_inflight_scope_intersection_blocks_dispatch_but_disjoint_writer_does_not():
    common = dict(
        run_id=RunId(uuid.uuid4()),
        repair_decision_id=RepairDecisionId(uuid.uuid4()),
        repair_set=(RepairSlice("slice-a", "INTEGRATED", scope("src/a.py")),),
    )
    blocked = evaluate_joint_repair_dispatch(
        **common,
        active_writers=(ActiveWriter("slice-c", "RUNNING", scope("src/a.py")),),
    )
    assert blocked.admitted is False
    assert blocked.reason == "IN_FLIGHT_SCOPE_CONFLICT"

    allowed = evaluate_joint_repair_dispatch(
        **common,
        active_writers=(ActiveWriter("slice-c", "RUNNING", scope("src/c.py")),),
    )
    assert allowed.admitted is True


def test_file_root_and_nested_root_are_scope_conflicts():
    result = evaluate_joint_repair_dispatch(
        run_id=RunId(uuid.uuid4()),
        repair_decision_id=RepairDecisionId(uuid.uuid4()),
        repair_set=(RepairSlice("slice-a", "INTEGRATED", scope("src/a.py")),),
        active_writers=(ActiveWriter("slice-c", "CHECKPOINT_PENDING", scope(roots=("src",))),),
    )
    assert result.reason == "IN_FLIGHT_SCOPE_CONFLICT"

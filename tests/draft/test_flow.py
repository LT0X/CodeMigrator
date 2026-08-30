import pytest

from codemigrator.runtime.draft import DraftFlow
from codemigrator.runtime.draft_models import (
    AskUserQuestion,
    DraftExecRequest,
    DraftStage,
    ExplorationReport,
    QuestionOption,
)


def report(path: str, domain_path: str = "src") -> ExplorationReport:
    return ExplorationReport(
        domain_path=domain_path,
        anchors=[
            {
                "file_path": path,
                "start": {"line": 1, "column": 0},
                "end": {"line": 1, "column": 5},
            }
        ],
        coverage=[path],
        confidence_reason="The deterministic fixture resolves the file.",
    )


def test_flow_requires_exploration_before_artifacts_and_calibration(artifacts) -> None:
    flow = DraftFlow()
    assert flow.stage is DraftStage.Explore
    flow.submit_report(report("src/a.py"))
    flow.finish_exploration(["src/a.py"])
    assert flow.stage is DraftStage.Align

    revision = flow.save_artifacts(artifacts)
    assert revision.revision_number == 1
    assert flow.stage is DraftStage.Draft
    flow.begin_calibration()
    assert flow.stage is DraftStage.Calibrate

    with pytest.raises(ValueError, match="2 or 3"):
        flow.trial_translate(
            ["src/a.py"],
            {"src/a.py": "rulebook output"},
            {"src/a.py": "freeform output"},
        )


def test_alignment_questions_can_precede_finalizing_artifacts(artifacts) -> None:
    flow = DraftFlow()
    flow.submit_report(report("src/a.py"))
    flow.finish_exploration(["src/a.py"])
    revision = flow.seed_artifacts(artifacts)
    question = AskUserQuestion(
        revision_id=revision.revision_id,
        prompt="Keep the module boundary?",
        options=(
            QuestionOption(
                key="keep",
                label="Keep",
                impact="Preserves evidence boundaries.",
                recommended=True,
            ),
            QuestionOption(
                key="merge",
                label="Merge",
                impact="Broadens the domain.",
                recommended=False,
            ),
        ),
    )
    flow.ask_user(question)
    flow.finalize_alignment()

    assert flow.stage is DraftStage.Draft


def test_flow_validates_report_domains_fanout_and_allows_alignment_questions(artifacts) -> None:
    flow = DraftFlow(max_fanout=2)
    flow.submit_report(report("src/a/main.py", domain_path="src/a"))
    flow.submit_report(report("src/b/main.py", domain_path="src/b"))
    flow.finish_exploration(["src/a/main.py", "src/b/main.py"])
    flow.save_artifacts(artifacts)
    question = AskUserQuestion(
        revision_id=flow.ledger.current_revision.revision_id,
        prompt="Keep the domains separate?",
        options=(
            QuestionOption(
                key="yes",
                label="Keep separate",
                impact="Preserves independent checks.",
                recommended=True,
            ),
            QuestionOption(
                key="no",
                label="Merge",
                impact="Broadens the change scope.",
                recommended=False,
            ),
        ),
    )
    assert flow.ask_user(question).question_id == question.question_id

    too_many = DraftFlow()
    for index in range(7):
        too_many.submit_report(
            report(
                f"src/domain_{index}/file_{index}.py",
                domain_path=f"src/domain_{index}",
            )
        )
    with pytest.raises(ValueError, match="fanout"):
        too_many.finish_exploration(
            [f"src/domain_{index}/file_{index}.py" for index in range(7)]
        )

    wrong_domain = DraftFlow()
    wrong_domain.submit_report(report("src/b.py", domain_path="src/a"))
    with pytest.raises(ValueError, match="domain"):
        wrong_domain.finish_exploration(["src/b.py"])


def test_flow_uses_machine_domain_skeleton_when_module_candidates_are_supplied() -> None:
    module_files = {
        f"src/module_{index}": [f"src/module_{index}/main.py"]
        for index in range(7)
    }
    flow = DraftFlow(module_files=module_files)
    for index in range(7):
        flow.submit_report(
            report(
                f"src/module_{index}/main.py",
                domain_path=f"src/module_{index}",
            )
        )

    with pytest.raises(ValueError, match="fanout"):
        flow.finish_exploration(
            [f"src/module_{index}/main.py" for index in range(7)]
        )


def test_trial_translation_is_side_by_side_and_discarded(artifacts) -> None:
    flow = DraftFlow()
    flow.submit_report(report("src/b/main.py", domain_path="src/b"))
    flow.submit_report(report("src/a/main.py", domain_path="src/a"))
    flow.finish_exploration(["src/a/main.py", "src/b/main.py"])
    flow.save_artifacts(artifacts)
    flow.begin_calibration()

    trials = flow.trial_translate(
        ["src/b/main.py", "src/a/main.py"],
        {"src/a/main.py": "rulebook a", "src/b/main.py": "rulebook b"},
        {"src/a/main.py": "freeform a", "src/b/main.py": "freeform b"},
    )

    assert [trial.file_path for trial in trials] == ["src/a/main.py", "src/b/main.py"]
    assert all(trial.discarded for trial in trials)
    assert flow.side_effects == ()

    changed = artifacts.model_copy(
        update={
            "migration_rulebook": artifacts.migration_rulebook.model_copy(
                update={"version": 2}
            )
        }
    )
    revised = flow.revise_artifacts(changed)
    assert revised.revision_number == 2
    assert flow.stage is DraftStage.Draft


def test_draft_tools_and_unconfirmed_side_effects_are_closed(artifacts) -> None:
    flow = DraftFlow()
    assert flow.tool_is_allowed("ReadFile")
    assert flow.tool_is_allowed("QuerySourceAst")
    assert flow.tool_is_allowed("Exec")
    assert not flow.tool_is_allowed("WriteFile")
    assert not flow.tool_is_allowed("EditFile")
    assert not flow.tool_is_allowed("Shell")
    assert flow.validate_exec(DraftExecRequest(operation="ReadFile", path="src/a.py"))
    assert flow.side_effects == ()


def test_confirmation_freezes_current_revision_after_trial(artifacts) -> None:
    flow = DraftFlow()
    flow.submit_report(report("src/a/main.py", domain_path="src/a"))
    flow.submit_report(report("src/b/main.py", domain_path="src/b"))
    flow.finish_exploration(["src/a/main.py", "src/b/main.py"])
    revision = flow.save_artifacts(artifacts)
    flow.begin_calibration()
    flow.trial_translate(
        ["src/a/main.py", "src/b/main.py"],
        {"src/a/main.py": "rulebook a", "src/b/main.py": "rulebook b"},
        {"src/a/main.py": "freeform a", "src/b/main.py": "freeform b"},
    )

    receipt = flow.confirm()

    assert flow.stage is DraftStage.Confirmed
    assert receipt.revision_id == revision.revision_id
    assert receipt.frozen_artifact_bundle.understanding_dossier.sha256
    assert flow.side_effects == ()

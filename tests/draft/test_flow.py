import pytest

from codemigrator.runtime.draft import DraftFlow
from codemigrator.runtime.draft_models import DraftStage, ExplorationReport


def report(path: str) -> ExplorationReport:
    return ExplorationReport(
        domain_path="src",
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


def test_trial_translation_is_side_by_side_and_discarded(artifacts) -> None:
    flow = DraftFlow()
    flow.submit_report(report("src/b.py"))
    flow.submit_report(report("src/a.py"))
    flow.finish_exploration(["src/a.py", "src/b.py"])
    flow.save_artifacts(artifacts)
    flow.begin_calibration()

    trials = flow.trial_translate(
        ["src/b.py", "src/a.py"],
        {"src/a.py": "rulebook a", "src/b.py": "rulebook b"},
        {"src/a.py": "freeform a", "src/b.py": "freeform b"},
    )

    assert [trial.file_path for trial in trials] == ["src/a.py", "src/b.py"]
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
    assert flow.side_effects == ()


def test_confirmation_freezes_current_revision_after_trial(artifacts) -> None:
    flow = DraftFlow()
    flow.submit_report(report("src/a.py"))
    flow.submit_report(report("src/b.py"))
    flow.finish_exploration(["src/a.py", "src/b.py"])
    revision = flow.save_artifacts(artifacts)
    flow.begin_calibration()
    flow.trial_translate(
        ["src/a.py", "src/b.py"],
        {"src/a.py": "rulebook a", "src/b.py": "rulebook b"},
        {"src/a.py": "freeform a", "src/b.py": "freeform b"},
    )

    receipt = flow.confirm()

    assert flow.stage is DraftStage.Confirmed
    assert receipt.revision_id == revision.revision_id
    assert receipt.frozen_artifact_bundle.understanding_dossier.sha256
    assert flow.side_effects == ()

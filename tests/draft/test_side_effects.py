from codemigrator.runtime.draft import DraftFlow
from codemigrator.runtime.draft_models import ExplorationReport


def test_unconfirmed_draft_has_zero_run_side_effects(artifacts) -> None:
    flow = DraftFlow()
    flow.submit_report(
        ExplorationReport(
            domain_path="src",
            anchors=[
                {
                    "file_path": "src/a.py",
                    "start": {"line": 1, "column": 0},
                    "end": {"line": 1, "column": 1},
                }
            ],
            coverage=["src/a.py"],
            confidence_reason="Fixture confidence is deterministic.",
        )
    )
    flow.finish_exploration(["src/a.py"])
    flow.save_artifacts(artifacts)

    assert flow.side_effects == ()
    assert flow.run_count == 0
    assert flow.run_event_count == 0
    assert flow.slice_count == 0
    assert flow.candidate_count == 0
    assert flow.managed_output_count == 0

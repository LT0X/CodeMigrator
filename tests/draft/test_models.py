import pytest
from pydantic import ValidationError

from codemigrator.runtime.draft_models import (
    ExplorationReport,
    ExploreReassignment,
    FocusBrief,
    FocusHighlight,
    QuestionOption,
    ReadOnlyDraftTool,
)


def test_focus_brief_and_reassignment_are_closed_contracts() -> None:
    brief = FocusBrief(
        domain_paths=["src/z", "src/a", "src/a"],
        highlights=[
            FocusHighlight(path="src/z.py", kind="risk_hotspot", reason="dynamic import")
        ],
        budget_hint="deep review",
    )
    advice = ExploreReassignment(
        op="refocus",
        domain_paths=["src/z", "src/a"],
        reason_summary="investigate the dynamic import before merging reports",
        focus_brief=brief,
    )

    assert brief.domain_paths == ("src/a", "src/z")
    assert advice.as_advice_payload()["op"] == "refocus"
    assert advice.as_advice_payload()["focus_brief"]["budget_hint"] == "deep review"
    with pytest.raises(ValidationError):
        FocusBrief(domain_paths=["src/a"], highlights=[], budget_hint="")
    with pytest.raises(ValidationError):
        FocusBrief(
            domain_paths=["src/a"],
            highlights=[],
            budget_hint="deep",
            unexpected=True,
        )


def test_exploration_report_requires_reasoned_confidence_and_safe_ranges() -> None:
    report = ExplorationReport(
        domain_path="src",
        anchors=[
            {
                "file_path": "src/a.py",
                "start": {"line": 1, "column": 0},
                "end": {"line": 2, "column": 0},
            }
        ],
        coverage=["src/a.py"],
        confidence_reason="AST resolved the import and the module boundary.",
    )

    assert report.anchors[0].file_path == "src/a.py"
    assert report.coverage == ("src/a.py",)
    with pytest.raises(ValidationError):
        ExplorationReport(
            domain_path="src",
            anchors=[],
            coverage=["src/a.py"],
            confidence_reason="",
        )


def test_draft_tool_boundary_only_allows_read_only_orchestration() -> None:
    assert {tool.value for tool in ReadOnlyDraftTool} == {
        "ReadFile",
        "QuerySourceAst",
        "Exec",
    }


def test_question_options_require_one_recommendation_and_impact() -> None:
    assert QuestionOption(
        key="preserve",
        label="Preserve boundary",
        impact="Limits cross-domain changes.",
        recommended=True,
    ).recommended is True
    with pytest.raises(ValidationError):
        QuestionOption(
            key="preserve",
            label="Preserve boundary",
            impact="",
            recommended=True,
        )

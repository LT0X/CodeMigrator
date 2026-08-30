from codemigrator.core import DossierEntry
from codemigrator.runtime.draft_validation import check_dossier_consistency

from .conftest import make_dossier, make_spec


def test_dossier_consistency_accepts_parseable_in_scope_anchors() -> None:
    result = check_dossier_consistency(
        make_dossier(["src/a.py"]),
        make_spec(),
        ["src/a.py"],
        unresolved_conflict_count=0,
    )

    assert result.valid is True
    assert result.reasons == ()


def test_dossier_consistency_rejects_malformed_anchor_and_scope_escape() -> None:
    dossier = make_dossier(["src/a.py"]).model_copy(deep=True)
    dossier.semantic_modules = [
        DossierEntry.model_construct(
            kind="semantic-module",
            content="bad anchors",
            anchors=[{"file": "outside.py", "start_line": 0}],
            advisory=False,
        )
    ]

    result = check_dossier_consistency(
        dossier,
        make_spec(),
        ["src/a.py"],
        unresolved_conflict_count=0,
    )

    assert result.valid is False
    assert "anchor" in " ".join(result.reasons)
    assert "semantic module" in " ".join(result.reasons)


def test_dossier_consistency_rejects_unresolved_merge_conflicts() -> None:
    result = check_dossier_consistency(
        make_dossier(),
        make_spec(),
        ["src/a.py"],
        unresolved_conflict_count=2,
    )

    assert result.valid is False
    assert result.unresolved_conflict_count == 2
    assert result.reason_code == "DOSSIER_INCONSISTENT"


def test_advisory_empty_entries_are_allowed_but_non_advisory_empty_entries_are_not() -> None:
    dossier = make_dossier().model_copy(deep=True)
    dossier.strategy_advice = [
        DossierEntry.model_construct(
            kind="strategy",
            content="follow up later",
            anchors=[],
            advisory=True,
        )
    ]
    assert check_dossier_consistency(dossier, make_spec(), ["src/a.py"], 0).valid is True

    dossier.strategy_advice = [
        DossierEntry.model_construct(
            kind="strategy",
            content="missing evidence",
            anchors=[],
            advisory=False,
        )
    ]
    assert check_dossier_consistency(dossier, make_spec(), ["src/a.py"], 0).valid is False

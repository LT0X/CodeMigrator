from codemigrator.runtime.draft_models import DomainSkeleton
from codemigrator.runtime.draft_validation import (
    build_domain_skeleton,
    validate_exact_coverage,
)


def test_domain_skeleton_is_deterministic_and_splits_large_domains_by_child_directory() -> None:
    files = [
        *(f"src/big/alpha/file_{index:02d}.py" for index in range(11)),
        *(f"src/big/beta/file_{index:02d}.py" for index in range(10)),
    ]

    skeleton = build_domain_skeleton({"src/big": list(reversed(files))})

    assert tuple(domain.domain_path for domain in skeleton) == (
        "src/big/alpha",
        "src/big/beta",
    )
    assert validate_exact_coverage(skeleton, files).valid is True
    assert build_domain_skeleton({"src/big": files}) == skeleton


def test_domain_skeleton_rejects_more_than_the_configured_exploration_fanout() -> None:
    modules = {f"src/module_{index}": [f"src/module_{index}/main.py"] for index in range(7)}

    try:
        build_domain_skeleton(modules)
    except ValueError as exc:
        assert "fanout" in str(exc)
    else:
        raise AssertionError("fanout overflow must be rejected")


def test_domain_skeleton_can_represent_repository_root_files() -> None:
    skeleton = build_domain_skeleton({".": ["go.mod", "README.md"]})

    assert len(skeleton) == 1
    assert skeleton[0].domain_path == "."
    assert skeleton[0].files == ("README.md", "go.mod")


def test_exact_coverage_reports_missing_duplicate_and_unknown_files() -> None:
    skeleton = (
        DomainSkeleton(
            domain_path="src/a",
            files=("src/a.py", "src/a.py", "src/extra.py"),
        ),
    )

    result = validate_exact_coverage(skeleton, ["src/a.py", "src/missing.py"])

    assert result.valid is False
    assert result.duplicate_files == ("src/a.py",)
    assert result.missing_files == ("src/missing.py",)
    assert result.unknown_files == ("src/extra.py",)

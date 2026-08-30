from __future__ import annotations

import uuid

import pytest

from codemigrator.core import (
    canonical_json_bytes,
    integration_key,
    normalize_repo_relative_paths,
    validate_branch_prefix,
)


@pytest.mark.parametrize("value", ["a", "feature/x", "a-1/bug2", "x" * 32])
def test_branch_prefix_accepts_valid_ascii_segments(value: str) -> None:
    assert validate_branch_prefix(value) == value


@pytest.mark.parametrize(
    "value",
    ["", "x" * 33, "/feature", "feature/", "feature//x", "feature/.", "feature/..", "feature/.git", "Feature/x", "feature_1"],
)
def test_branch_prefix_rejects_invalid_segments(value: str) -> None:
    with pytest.raises(ValueError):
        validate_branch_prefix(value)


@pytest.mark.parametrize("value", ["/absolute", "a\\b", "a/../b", "a/./b", "a/.git/b", "a\x00b"])
def test_repo_paths_reject_unsafe_forms(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_repo_relative_paths([value])


def test_repo_paths_dedupe_and_sort_by_utf8_bytes() -> None:
    assert normalize_repo_relative_paths(["z.py", "é.py", "a.py", "z.py"]) == [
        "a.py",
        "z.py",
        "é.py",
    ]


def test_integration_key_uses_rank_then_uuid_bytes() -> None:
    slice_id = uuid.UUID("00000000-0000-0000-0000-00000000000a")

    assert integration_key(7, slice_id) == (7, slice_id.bytes)


def test_canonical_json_bytes_is_stable_and_compact() -> None:
    assert canonical_json_bytes({"b": 1, "a": "x"}) == b'{"a":"x","b":1}'

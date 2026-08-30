from __future__ import annotations

import uuid

import pytest

from codemigrator.core import (
    AdviceId,
    CandidateGeneration,
    RunId,
    new_uuid7,
    validate_candidate_generation,
)


def test_new_uuid7_returns_version_seven_uuid() -> None:
    value = new_uuid7()

    assert isinstance(value, uuid.UUID)
    assert value.version == 7


@pytest.mark.parametrize("value", [0, 1, 2])
def test_candidate_generation_accepts_only_zero_one_two(value: int) -> None:
    assert validate_candidate_generation(value) == value


@pytest.mark.parametrize("value", [-1, 3, 1.0, "1", True])
def test_candidate_generation_rejects_non_generation_values(value: object) -> None:
    with pytest.raises(ValueError):
        validate_candidate_generation(value)


def test_id_aliases_are_uuid_newtypes() -> None:
    assert RunId.__supertype__ is uuid.UUID
    assert AdviceId.__supertype__ is uuid.UUID
    assert CandidateGeneration.__supertype__ is int

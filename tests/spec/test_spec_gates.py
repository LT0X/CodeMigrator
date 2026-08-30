import json

import pytest

from codemigrator.core import StableErrorCode
from codemigrator.core.enums import CheckAction
from codemigrator.core.spec import (
    DescriptorResolution,
    InMemoryDescriptorRegistry,
    InMemorySpecRepository,
    RequiredCheckSelection,
    SpecArtifact,
    validate_spec_bytes,
)

SOURCE_DIGEST = "a" * 64
TARGET_DIGEST = "b" * 64
IMAGE_DIGEST = "c" * 64
CHECK_DIGESTS = {
    CheckAction.Compile: "1" * 64,
    CheckAction.Test: "2" * 64,
    CheckAction.Lint: "3" * 64,
}


def registry(
    *,
    source_descriptor_sha256: str = SOURCE_DIGEST,
    target_descriptor_sha256: str = TARGET_DIGEST,
    toolchain_image_digest: str = IMAGE_DIGEST,
    grammar_available: bool = True,
    image_available: bool = True,
) -> InMemoryDescriptorRegistry:
    resolution = DescriptorResolution(
        source_language_id="go",
        target_language_id="python",
        descriptor_version="1.0.0",
        source_descriptor_sha256=source_descriptor_sha256,
        target_descriptor_sha256=target_descriptor_sha256,
        toolchain_image_digest=toolchain_image_digest,
        checks=tuple(
            RequiredCheckSelection(action=action, template_sha256=digest)
            for action, digest in CHECK_DIGESTS.items()
        ),
        grammar_available=grammar_available,
        image_available=image_available,
    )
    return InMemoryDescriptorRegistry({("go", "python"): resolution})


def valid_payload() -> dict[str, object]:
    return {
        "schema": "codemigrator.migration-spec",
        "version": 3,
        "name": "go-to-python",
        "description": "translate the project",
        "source_language_id": "go",
        "target_language_id": "python",
        "descriptor_lock": {
            "descriptor_version": "1.0.0",
            "source_descriptor_sha256": SOURCE_DIGEST,
            "target_descriptor_sha256": TARGET_DIGEST,
            "toolchain_image_digest": IMAGE_DIGEST,
        },
        "scope": {"include": ["tests/", "src/", "src/"], "exclude": ["src/generated/"]},
        "required_checks": [
            {"action": "TEST", "template_sha256": CHECK_DIGESTS[CheckAction.Test]},
            {"action": "COMPILE", "template_sha256": CHECK_DIGESTS[CheckAction.Compile]},
        ],
        "decomposition": {
            "module_granularity": "package",
            "max_parallelism": 2,
            "test_grouping": "BY_MODULE",
        },
    }


def codes(result: object) -> list[StableErrorCode]:
    return [problem.code for problem in result.problems]  # type: ignore[union-attr]


def test_valid_spec_passes_all_gates_and_normalizes_business_fields() -> None:
    result = validate_spec_bytes(json.dumps(valid_payload()).encode(), registry=registry())

    assert result.accepted is True
    assert result.spec is not None
    assert result.spec.scope.include == ["src/", "tests/"]
    assert result.spec.scope.exclude == ["src/generated/"]
    assert result.canonical_bytes is not None
    assert result.canonical_sha256 is not None
    assert result.problems == ()
    assert result.side_effects == 0


def test_byte_and_json_gate_rejects_before_schema_or_resource_gate() -> None:
    too_large = validate_spec_bytes(b"{" + b"x" * (256 * 1024), registry=registry())
    assert codes(too_large) == [StableErrorCode.SPEC_TOO_LARGE]

    duplicate = validate_spec_bytes(b'{"schema":"a","schema":"b"}', registry=registry())
    assert codes(duplicate) == [StableErrorCode.SPEC_DUPLICATE_KEY]

    bom = validate_spec_bytes(b"\xef\xbb\xbf{}", registry=registry())
    assert codes(bom) == [StableErrorCode.SPEC_JSON_INVALID]

    invalid_utf8 = validate_spec_bytes(b"\xff", registry=registry())
    assert codes(invalid_utf8) == [StableErrorCode.SPEC_JSON_INVALID]


def test_json_depth_gate_rejects_depth_33() -> None:
    document: object = {}
    for _ in range(32):
        document = {"nested": document}

    result = validate_spec_bytes(json.dumps(document).encode(), registry=registry())

    assert codes(result) == [StableErrorCode.SPEC_DEPTH_EXCEEDED]


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_language_id", ""),
        ("target_language_id", "Go"),
        ("name", "é" * 129),
        ("description", "é" * 513),
        ("scope", {"include": []}),
        (
            "descriptor_lock",
            {
                "descriptor_version": "not-semver",
                "source_descriptor_sha256": SOURCE_DIGEST,
                "target_descriptor_sha256": TARGET_DIGEST,
                "toolchain_image_digest": IMAGE_DIGEST,
            },
        ),
    ],
)
def test_schema_gate_rejects_invalid_typed_fields(field: str, value: object) -> None:
    payload = valid_payload()
    payload[field] = value

    result = validate_spec_bytes(json.dumps(payload).encode(), registry=registry())

    assert StableErrorCode.SPEC_SCHEMA_INVALID in codes(result)
    assert result.side_effects == 0


def test_schema_gate_rejects_unsupported_version_with_dedicated_code() -> None:
    payload = valid_payload()
    payload["version"] = 2

    result = validate_spec_bytes(json.dumps(payload).encode(), registry=registry())

    assert codes(result) == [StableErrorCode.SPEC_SCHEMA_UNSUPPORTED]


def test_schema_gate_forbids_unknown_fields_at_every_level() -> None:
    payload = valid_payload()
    payload["unexpected"] = True
    payload["descriptor_lock"]["shell"] = "echo unsafe"  # type: ignore[index]
    payload["scope"]["write_scope"] = ["src"]  # type: ignore[index]

    result = validate_spec_bytes(json.dumps(payload).encode(), registry=registry())

    assert StableErrorCode.SPEC_SCHEMA_INVALID in codes(result)
    assert result.canonical_bytes is None


def test_scope_matcher_accepts_finite_patterns_and_permanently_excludes_git() -> None:
    result = validate_spec_bytes(json.dumps(valid_payload()).encode(), registry=registry())
    scope = result.spec.scope  # type: ignore[union-attr]

    assert scope.includes("src/main.go")
    assert scope.includes("tests/unit/test_main.go")
    assert not scope.includes("src/generated/api.go")
    assert not scope.includes(".git/config")


@pytest.mark.parametrize(
    "pattern",
    ["**/*.go", "src/?.go", "src/[a].go", "src/{a,b}.go", "/src/", "src//x", "../src", ".git/"],
)
def test_scope_gate_rejects_patterns_outside_the_finite_language(pattern: str) -> None:
    payload = valid_payload()
    payload["scope"] = {"include": [pattern]}

    result = validate_spec_bytes(json.dumps(payload).encode(), registry=registry())

    assert codes(result) == [StableErrorCode.SPEC_SCHEMA_INVALID]


def test_exclude_must_be_contained_by_include() -> None:
    payload = valid_payload()
    payload["scope"] = {"include": ["src/"], "exclude": ["tests/"]}

    result = validate_spec_bytes(json.dumps(payload).encode(), registry=registry())

    assert codes(result) == [StableErrorCode.SPEC_SCHEMA_INVALID]


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({}, StableErrorCode.DESCRIPTOR_NOT_FOUND),
        ({"source_descriptor_sha256": "d" * 64}, StableErrorCode.DESCRIPTOR_DIGEST_MISMATCH),
        ({"grammar_available": False}, StableErrorCode.DESCRIPTOR_NOT_FOUND),
        ({"image_available": False}, StableErrorCode.TOOLCHAIN_IMAGE_UNAVAILABLE),
    ],
)
def test_resource_gate_rejects_without_creating_side_effects(
    kwargs: dict[str, object], expected: StableErrorCode
) -> None:
    active_registry = registry(**kwargs)
    if not kwargs:
        active_registry = InMemoryDescriptorRegistry({})

    result = validate_spec_bytes(json.dumps(valid_payload()).encode(), registry=active_registry)

    assert codes(result) == [expected]
    assert result.side_effects == 0
    assert result.canonical_bytes is None


def test_check_gate_requires_compile_and_test_and_rejects_unsupported_or_duplicate_pairs() -> None:
    missing_compile = valid_payload()
    missing_compile["required_checks"] = [
        {"action": "TEST", "template_sha256": CHECK_DIGESTS[CheckAction.Test]}
    ]
    result = validate_spec_bytes(json.dumps(missing_compile).encode(), registry=registry())
    assert codes(result) == [StableErrorCode.CHECK_SET_INCOMPLETE]

    unsupported = valid_payload()
    unsupported["required_checks"].append({"action": "LINT", "template_sha256": "4" * 64})  # type: ignore[union-attr]
    result = validate_spec_bytes(json.dumps(unsupported).encode(), registry=registry())
    assert codes(result) == [StableErrorCode.CHECK_ACTION_UNSUPPORTED]

    duplicate = valid_payload()
    duplicate["required_checks"].append(duplicate["required_checks"][0])  # type: ignore[index, union-attr]
    result = validate_spec_bytes(json.dumps(duplicate).encode(), registry=registry())
    assert codes(result) == [StableErrorCode.SPEC_SCHEMA_INVALID]


def test_canonical_hash_is_stable_across_object_and_supported_array_order() -> None:
    first = valid_payload()
    second = valid_payload()
    second["scope"] = {"exclude": ["src/generated/"], "include": ["src/", "tests/"]}
    second["required_checks"] = list(reversed(second["required_checks"]))  # type: ignore[arg-type]

    first_result = validate_spec_bytes(json.dumps(first).encode(), registry=registry())
    second_result = validate_spec_bytes(json.dumps(second).encode(), registry=registry())

    assert first_result.canonical_bytes == second_result.canonical_bytes
    assert first_result.canonical_sha256 == second_result.canonical_sha256


def test_in_memory_insert_or_get_preserves_spec_identity_and_rejects_referenced_delete() -> None:
    result = validate_spec_bytes(json.dumps(valid_payload()).encode(), registry=registry())
    artifact = SpecArtifact.from_result(result)
    repository = InMemorySpecRepository()

    first = repository.insert_or_get(artifact)
    second = repository.insert_or_get(artifact)

    assert first.spec_id == second.spec_id
    repository.mark_referenced(first.spec_id)
    with pytest.raises(ValueError, match="SPEC_IN_USE"):
        repository.delete(first.spec_id)

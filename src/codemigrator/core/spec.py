"""Pure Spec v3 gates, canonicalization, and deterministic test ports."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from .enums import CheckAction
from .errors import StableErrorCode
from .ids import SpecId, new_uuid7
from .models.spec import MigrationSpec, RequiredCheckSelection
from .paths import canonical_json_bytes
from .ports import (
    DescriptorRegistry,
    DescriptorResolution,
    InMemoryDescriptorRegistry,
)
from .scope import normalize_scope_paths

MAX_SPEC_BYTES = 256 * 1024
MAX_SPEC_DEPTH = 32
MAX_PROBLEMS = 100
SPEC_SCHEMA = "codemigrator.migration-spec"
SPEC_VERSION = 3


class _DuplicateKeyError(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


def _object_depth(value: object, depth: int = 1) -> int:
    if not isinstance(value, (dict, list)):
        return depth - 1
    children: Iterable[object] = value.values() if isinstance(value, dict) else value
    return max(((_object_depth(child, depth + 1)) for child in children), default=depth)


def _json_problem(code: StableErrorCode, message: str) -> SpecProblem:
    return SpecProblem(pointer="", code=code, message=message)


@dataclass(frozen=True)
class SpecProblem:
    """Stable, ordered projection of a single gate problem."""

    pointer: str
    code: StableErrorCode
    message: str


@dataclass(frozen=True)
class LimitedProblems:
    problems: tuple[SpecProblem, ...]
    truncated: bool


def _json_pointer(location: tuple[object, ...]) -> str:
    if not location:
        return ""
    segments = []
    for item in location:
        value = str(item).replace("~", "~0").replace("/", "~1")
        segments.append(value)
    return "/" + "/".join(segments)


def limit_problems(problems: Iterable[SpecProblem]) -> LimitedProblems:
    ordered = sorted(problems, key=lambda problem: problem.pointer.encode("utf-8"))
    return LimitedProblems(tuple(ordered[:MAX_PROBLEMS]), len(ordered) > MAX_PROBLEMS)


@dataclass(frozen=True)
class SpecValidationResult:
    spec: MigrationSpec | None
    canonical_bytes: bytes | None
    canonical_sha256: str | None
    problems: tuple[SpecProblem, ...] = ()
    truncated: bool = False
    side_effects: int = 0

    @property
    def accepted(self) -> bool:
        return self.spec is not None and not self.problems


@dataclass(frozen=True)
class SpecArtifact:
    spec: MigrationSpec
    canonical_bytes: bytes
    canonical_sha256: str

    @classmethod
    def from_result(cls, result: SpecValidationResult) -> SpecArtifact:
        if not result.accepted or result.spec is None or result.canonical_bytes is None:
            raise ValueError("cannot create SpecArtifact from rejected Spec")
        if result.canonical_sha256 is None:
            raise ValueError("accepted Spec is missing canonical hash")
        return cls(result.spec, result.canonical_bytes, result.canonical_sha256)


@dataclass(frozen=True)
class SpecRecord:
    spec_id: SpecId
    artifact: SpecArtifact


class SpecInUseError(ValueError):
    """Raised when an immutable Spec has a Run reference."""

    code = StableErrorCode.SPEC_IN_USE


class InMemorySpecRepository:
    """Insert-or-get test double; SQL persistence belongs to runtime."""

    def __init__(self) -> None:
        self._records: dict[bytes, SpecRecord] = {}
        self._referenced: set[Any] = set()

    def insert_or_get(self, artifact: SpecArtifact) -> SpecRecord:
        existing = self._records.get(artifact.canonical_bytes)
        if existing is not None:
            return existing
        record = SpecRecord(spec_id=SpecId(new_uuid7()), artifact=artifact)
        self._records[artifact.canonical_bytes] = record
        return record

    def mark_referenced(self, spec_id: Any) -> None:
        self._referenced.add(spec_id)

    def delete(self, spec_id: Any) -> None:
        if spec_id in self._referenced:
            raise SpecInUseError("SPEC_IN_USE")
        for canonical_bytes, record in tuple(self._records.items()):
            if record.spec_id == spec_id:
                del self._records[canonical_bytes]
                return
        raise KeyError(spec_id)


def _validation_problems(error: ValidationError) -> LimitedProblems:
    problems = []
    for item in error.errors(include_url=False):
        location = tuple(item.get("loc", ()))
        problems.append(
            SpecProblem(
                pointer=_json_pointer(location),
                code=StableErrorCode.SPEC_SCHEMA_INVALID,
                message=str(item.get("msg", "invalid Spec")),
            )
        )
    return limit_problems(problems)


def _parse_json_gate(raw: bytes) -> tuple[object | None, LimitedProblems | None]:
    if not isinstance(raw, bytes):
        return None, LimitedProblems(
            (_json_problem(StableErrorCode.SPEC_JSON_INVALID, "Spec must be UTF-8 JSON bytes"),),
            False,
        )
    if len(raw) > MAX_SPEC_BYTES:
        return None, LimitedProblems(
            (_json_problem(StableErrorCode.SPEC_TOO_LARGE, "Spec exceeds 256 KiB"),), False
        )
    if raw.startswith(b"\xef\xbb\xbf"):
        return None, LimitedProblems(
            (_json_problem(StableErrorCode.SPEC_JSON_INVALID, "UTF-8 BOM is not allowed"),), False
        )
    try:
        text = raw.decode("utf-8")
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except _DuplicateKeyError as exc:
        return None, LimitedProblems(
            (_json_problem(StableErrorCode.SPEC_DUPLICATE_KEY, f"duplicate key: {exc}"),), False
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return None, LimitedProblems(
            (_json_problem(StableErrorCode.SPEC_JSON_INVALID, str(exc)),), False
        )
    if _object_depth(value) > MAX_SPEC_DEPTH:
        return None, LimitedProblems(
            (_json_problem(StableErrorCode.SPEC_DEPTH_EXCEEDED, "Spec nesting exceeds 32"),), False
        )
    return value, None


def _schema_gate(document: object) -> tuple[MigrationSpec | None, LimitedProblems | None]:
    if not isinstance(document, dict):
        return None, LimitedProblems(
            (_json_problem(StableErrorCode.SPEC_SCHEMA_INVALID, "Spec root must be an object"),),
            False,
        )
    if document.get("schema") != SPEC_SCHEMA or document.get("version") != SPEC_VERSION:
        return None, LimitedProblems(
            (
                _json_problem(
                    StableErrorCode.SPEC_SCHEMA_UNSUPPORTED, "unsupported Spec schema or version"
                ),
            ),
            False,
        )
    try:
        return MigrationSpec.model_validate(document), None
    except ValidationError as exc:
        return None, _validation_problems(exc)


def _resource_gate(
    spec: MigrationSpec, registry: DescriptorRegistry | None
) -> tuple[DescriptorResolution | None, LimitedProblems | None]:
    if registry is None:
        return None, LimitedProblems(
            (_json_problem(StableErrorCode.DESCRIPTOR_NOT_FOUND, "descriptor pair not found"),),
            False,
        )
    resolution = registry.resolve(spec.source_language_id, spec.target_language_id)
    if resolution is None:
        return None, LimitedProblems(
            (_json_problem(StableErrorCode.DESCRIPTOR_NOT_FOUND, "descriptor pair not found"),),
            False,
        )
    if (
        resolution.source_language_id != spec.source_language_id
        or resolution.target_language_id != spec.target_language_id
    ):
        return None, LimitedProblems(
            (
                _json_problem(
                    StableErrorCode.DESCRIPTOR_NOT_FOUND, "descriptor language pair mismatch"
                ),
            ),
            False,
        )
    lock = spec.descriptor_lock
    if (
        lock.descriptor_version != resolution.descriptor_version
        or lock.source_descriptor_sha256
        != resolution.source_descriptor_sha256.removeprefix("sha256:").lower()
        or lock.target_descriptor_sha256
        != resolution.target_descriptor_sha256.removeprefix("sha256:").lower()
        or lock.toolchain_image_digest
        != resolution.toolchain_image_digest.removeprefix("sha256:").lower()
    ):
        return None, LimitedProblems(
            (
                _json_problem(
                    StableErrorCode.DESCRIPTOR_DIGEST_MISMATCH,
                    "descriptor lock does not match installed resources",
                ),
            ),
            False,
        )
    if not resolution.grammar_available:
        return None, LimitedProblems(
            (_json_problem(StableErrorCode.DESCRIPTOR_NOT_FOUND, "source grammar is unavailable"),),
            False,
        )
    if not resolution.image_available:
        return None, LimitedProblems(
            (
                _json_problem(
                    StableErrorCode.TOOLCHAIN_IMAGE_UNAVAILABLE, "toolchain image is unavailable"
                ),
            ),
            False,
        )
    return resolution, None


def _checks_gate(spec: MigrationSpec, resolution: DescriptorResolution) -> LimitedProblems | None:
    seen: set[tuple[object, str]] = set()
    supported = resolution.supported_checks
    for index, selection in enumerate(spec.required_checks):
        pair = (selection.action, selection.template_sha256)
        if pair in seen:
            return LimitedProblems(
                (
                    SpecProblem(
                        f"/required_checks/{index}",
                        StableErrorCode.SPEC_SCHEMA_INVALID,
                        "duplicate required check",
                    ),
                ),
                False,
            )
        seen.add(pair)
        if pair not in supported:
            return LimitedProblems(
                (
                    SpecProblem(
                        f"/required_checks/{index}",
                        StableErrorCode.CHECK_ACTION_UNSUPPORTED,
                        "check template is not registered",
                    ),
                ),
                False,
            )
    actions = {selection.action for selection in spec.required_checks}
    missing = [
        action.value for action in (CheckAction.Compile, CheckAction.Test) if action not in actions
    ]
    if missing:
        return LimitedProblems(
            (
                SpecProblem(
                    "/required_checks",
                    StableErrorCode.CHECK_SET_INCOMPLETE,
                    "missing required checks: " + ", ".join(missing),
                ),
            ),
            False,
        )
    return None


def _canonical_business_document(spec: MigrationSpec) -> dict[str, object]:
    document = spec.model_dump(mode="json", by_alias=True, exclude_none=True)
    scope = document["scope"]
    assert isinstance(scope, dict)
    scope["include"] = normalize_scope_paths(spec.scope.include)
    scope["exclude"] = normalize_scope_paths(spec.scope.exclude)
    checks = document["required_checks"]
    assert isinstance(checks, list)
    document["required_checks"] = sorted(
        checks,
        key=lambda item: (
            str(item["action"]).encode("utf-8"),
            str(item["template_sha256"]).encode("utf-8"),
        ),
    )
    return document


def validate_spec_bytes(
    raw: bytes, *, registry: DescriptorRegistry | None = None
) -> SpecValidationResult:
    """Run the four fixed gates and return a side-effect-free result."""

    document, json_problems = _parse_json_gate(raw)
    if json_problems is not None:
        return SpecValidationResult(
            None, None, None, json_problems.problems, json_problems.truncated
        )
    spec, schema_problems = _schema_gate(document)
    if schema_problems is not None or spec is None:
        problems = schema_problems or LimitedProblems((), False)
        return SpecValidationResult(None, None, None, problems.problems, problems.truncated)
    resolution, resource_problems = _resource_gate(spec, registry)
    if resource_problems is not None or resolution is None:
        problems = resource_problems or LimitedProblems((), False)
        return SpecValidationResult(None, None, None, problems.problems, problems.truncated)
    check_problems = _checks_gate(spec, resolution)
    if check_problems is not None:
        return SpecValidationResult(
            None, None, None, check_problems.problems, check_problems.truncated
        )
    business_document = _canonical_business_document(spec)
    canonical = canonical_json_bytes(business_document)
    return SpecValidationResult(
        MigrationSpec.model_validate(business_document),
        canonical,
        hashlib.sha256(canonical).hexdigest(),
    )


def validate_spec(
    raw: bytes, *, registry: DescriptorRegistry | None = None
) -> SpecValidationResult:
    """Compatibility alias for the byte-oriented four-gate validator."""

    return validate_spec_bytes(raw, registry=registry)


__all__ = [
    "DescriptorResolution",
    "InMemoryDescriptorRegistry",
    "InMemorySpecRepository",
    "LimitedProblems",
    "MAX_PROBLEMS",
    "MAX_SPEC_BYTES",
    "MAX_SPEC_DEPTH",
    "MigrationSpec",
    "RequiredCheckSelection",
    "SpecArtifact",
    "SpecInUseError",
    "SpecProblem",
    "SpecRecord",
    "SpecValidationResult",
    "limit_problems",
    "validate_spec",
    "validate_spec_bytes",
]

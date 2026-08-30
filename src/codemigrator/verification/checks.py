"""Deterministic verification check selection and exact-set guards."""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from codemigrator.core import (
    ArtifactRef,
    CheckAction,
    CheckCommandTemplate,
    CheckId,
    CheckResult,
    CheckStatus,
    DerivedVerificationGuard,
    DiagnosticSeverity,
    ReceiptId,
    RequiredCheck,
    Sha256,
    StableErrorCode,
    Unknown,
    canonical_json_bytes,
    new_uuid7,
    normalize_repo_relative_paths,
)


class VerificationLayer(str, Enum):
    """The three M-10 verification subjects."""

    LOCAL = "LOCAL"
    INTEGRATION = "INTEGRATION"
    FINAL = "FINAL"


@dataclass(frozen=True)
class CheckSpec:
    """A frozen required check with its descriptor invocation identity."""

    required_check: RequiredCheck
    template: CheckCommandTemplate
    invocation_hash: Sha256
    test_files: tuple[str, ...] = ()

    @property
    def check_id(self) -> CheckId:
        return self.required_check.id

    @property
    def should_skip_empty_test(self) -> bool:
        return self.required_check.action is CheckAction.Test and not self.test_files


@dataclass(frozen=True)
class SkippedEmptyResult:
    """A typed Test empty-set receipt that never invokes an executor."""

    result: CheckResult
    disposition: str = "SkippedEmpty"

    @property
    def status(self) -> CheckStatus:
        return self.result.status


@dataclass(frozen=True)
class CheckSetValidation:
    """Exact-set validation and its derived guard."""

    errors: tuple[StableErrorCode, ...]
    guard: DerivedVerificationGuard


def _id_key(value: object) -> bytes:
    return str(value).encode("utf-8")


def frozen_required_checks_sha256(required_checks: Iterable[RequiredCheck]) -> Sha256:
    """Hash the UTF-8-byte-sorted canonical CheckId list."""

    check_ids = sorted((str(item.id) for item in required_checks), key=lambda value: value.encode())
    return Sha256(hashlib.sha256(canonical_json_bytes(check_ids)).hexdigest())


def _invocation_hash(required: RequiredCheck, template: CheckCommandTemplate) -> Sha256:
    payload = {
        "template_sha256": str(required.template_sha256),
        "program": template.program,
        "argv": list(template.argv),
        "timeout_secs": template.timeout_secs,
    }
    return Sha256(hashlib.sha256(canonical_json_bytes(payload)).hexdigest())


def _eligible_test_files(
    test_files: Iterable[str],
    test_coverage: Mapping[str, Iterable[object]],
    integrated_slices: set[object],
    *,
    require_coverage: bool,
) -> tuple[str, ...]:
    normalized = normalize_repo_relative_paths(list(test_files))
    eligible = [
        path
        for path in normalized
        if (
            path in test_coverage and set(test_coverage[path]).issubset(integrated_slices)
            if require_coverage
            else True
        )
    ]
    return tuple(eligible)


def instantiate_checks(
    layer: VerificationLayer,
    required_checks: Sequence[RequiredCheck],
    templates_by_sha256: Mapping[str, CheckCommandTemplate],
    *,
    test_files: Iterable[str] = (),
    test_coverage: Mapping[str, Iterable[object]] | None = None,
    integrated_slices: Iterable[object] = (),
    candidate_path: str | None = None,
) -> tuple[CheckSpec, ...]:
    """Instantiate only the checks allowed at ``layer``.

    ``candidate_path`` is accepted at the port boundary for callers that also
    materialize a subject.  It is deliberately unused: temporary paths must
    never enter invocation identity or fingerprints.
    """

    del candidate_path
    allowed = {
        VerificationLayer.LOCAL: {CheckAction.Compile, CheckAction.TypeCheck},
        VerificationLayer.INTEGRATION: {
            CheckAction.Compile,
            CheckAction.Lint,
            CheckAction.TypeCheck,
            CheckAction.Test,
        },
        VerificationLayer.FINAL: {CheckAction.Test},
    }[layer]
    coverage = test_coverage or {}
    integrated = set(integrated_slices)
    eligible = _eligible_test_files(
        test_files,
        coverage,
        integrated,
        require_coverage=layer is VerificationLayer.INTEGRATION,
    )
    specs: list[CheckSpec] = []
    for required in required_checks:
        if required.action not in allowed:
            continue
        try:
            template = templates_by_sha256[str(required.template_sha256)]
        except KeyError as exc:
            raise ValueError(f"missing descriptor template: {required.template_sha256}") from exc
        if template.action is not required.action:
            raise ValueError(f"descriptor template action mismatch: {required.id}")
        specs.append(
            CheckSpec(
                required_check=required,
                template=template,
                invocation_hash=_invocation_hash(required, template),
                test_files=eligible if required.action is CheckAction.Test else (),
            )
        )
    return tuple(sorted(specs, key=lambda item: _id_key(item.check_id)))


def _empty_artifact() -> ArtifactRef:
    return ArtifactRef(
        sha256=Sha256(hashlib.sha256(b"").hexdigest()),
        size=0,
        media_type="application/octet-stream",
    )


def make_skipped_empty_result(spec: CheckSpec, receipt_id: ReceiptId | None) -> SkippedEmptyResult:
    """Create the typed Passed/SkippedEmpty result for an empty Test set."""

    result = CheckResult(
        check_id=spec.check_id,
        invocation_hash=spec.invocation_hash,
        status=CheckStatus.Passed,
        receipt_id=receipt_id or ReceiptId(new_uuid7()),
        stdout=_empty_artifact(),
        stderr=_empty_artifact(),
        diagnostics=[],
    )
    return SkippedEmptyResult(result=result)


def validate_check_results(
    specs: Sequence[CheckSpec], results: Sequence[CheckResult]
) -> CheckSetValidation:
    """Validate exact CheckId coverage and invocation identities."""

    expected = {spec.check_id: spec for spec in specs}
    result_counts = Counter(result.check_id for result in results)
    expected_counts = Counter(spec.check_id for spec in specs)
    errors: list[StableErrorCode] = []
    if any(count > 1 for count in result_counts.values()) or any(
        count > 1 for count in expected_counts.values()
    ):
        errors.append(StableErrorCode.CHECK_DUPLICATE)
    if any(check_id not in result_counts for check_id in expected):
        errors.append(StableErrorCode.CHECK_MISSING)
    if any(check_id not in expected for check_id in result_counts):
        errors.append(StableErrorCode.CHECK_UNEXPECTED)
    if any(
        result.check_id in expected
        and result.invocation_hash != expected[result.check_id].invocation_hash
        for result in results
    ):
        errors.append(StableErrorCode.INVOCATION_HASH_MISMATCH)

    unknown_count = sum(
        1
        for result in results
        for diagnostic in result.diagnostics
        if diagnostic.severity is DiagnosticSeverity.Error
        and isinstance(diagnostic.target, Unknown)
    )
    all_passed = (
        not errors
        and len(results) == len(specs)
        and all(result.status is CheckStatus.Passed for result in results)
    )
    return CheckSetValidation(
        errors=tuple(dict.fromkeys(errors)),
        guard=DerivedVerificationGuard(
            all_required_checks_passed=all_passed,
            error_unknown_count=unknown_count,
        ),
    )

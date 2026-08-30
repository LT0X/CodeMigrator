"""Deterministic proposal guards and atomic in-memory plan freezing."""

from __future__ import annotations

import copy
import hashlib
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from codemigrator.analysis import ArtifactFact, ArtifactKind, ModuleCoverage, ModuleFact, ModuleRole
from codemigrator.core import (
    MigrationSlice,
    PlanEdge,
    ProjectModuleId,
    SliceKind,
    StableErrorCode,
    WriteScope,
    WriteScopeOut,
    canonical_json_bytes,
    integration_key,
    new_uuid7,
)
from codemigrator.core.ids import SliceId

from .models import (
    EdgeProvenance,
    FrozenPlan,
    PlanningInputs,
    PlanningLimits,
    PlanProposal,
    PlanValidation,
    PlanViolation,
)


class PlanRejected(ValueError):
    """Raised when a proposal cannot be frozen."""

    def __init__(self, validation: PlanValidation) -> None:
        self.validation = validation
        self.code = (
            validation.violations[0].code
            if validation.violations
            else StableErrorCode.PLAN_PROPOSAL_INVALID
        )
        super().__init__(f"plan proposal rejected: {self.code.value}")


class PlanValidator:
    """Apply all planning-time guards without changing the proposal."""

    def __init__(self, *, limits: PlanningLimits | None = None) -> None:
        self._limits = limits

    def validate(self, proposal: PlanProposal, inputs: PlanningInputs) -> PlanValidation:
        violations: list[PlanViolation] = []
        limits = self._limits or inputs.limits
        modules = {module.module_id: module for module in inputs.analysis.modules}
        expected_files = _in_scope_source_files(inputs)
        actual_coverage: dict[str, list[str]] = defaultdict(list)

        violations.extend(_validate_slice_modules(proposal, inputs, modules))
        for slice_proposal in proposal.slices:
            if slice_proposal.kind not in {SliceKind.Implementation, SliceKind.TestTranslation}:
                continue
            for module_id in slice_proposal.source_modules:
                module = modules.get(module_id)
                if module is None:
                    continue
                for path in module.file_paths:
                    path_text = str(path)
                    if path_text in expected_files:
                        actual_coverage[path_text].append(slice_proposal.local_ref)

        violations.extend(_validate_edges(proposal))
        violations.extend(_validate_artifacts(proposal, inputs))
        violations.extend(_validate_artifact_scopes(proposal))
        violations.extend(_validate_test_edges(proposal, inputs))
        violations.extend(_validate_scope_conflicts(proposal))
        violations.extend(_validate_blueprint(proposal, inputs))

        source_coverage: dict[str, str] = {}
        for source_path in sorted(expected_files, key=lambda item: item.encode("utf-8")):
            owners = actual_coverage.get(source_path, [])
            if len(owners) == 1:
                source_coverage[source_path] = owners[0]
            elif not owners:
                source_coverage[source_path] = "MISSING"
                violations.append(
                    _violation(
                        StableErrorCode.PLAN_COVERAGE_INVALID,
                        "/slices",
                        "in-scope source file is not covered by a slice",
                        path=source_path,
                    )
                )
            else:
                source_coverage[source_path] = "|".join(sorted(set(owners)))
                violations.append(
                    _violation(
                        StableErrorCode.PLAN_COVERAGE_INVALID,
                        "/slices",
                        "in-scope source file is covered more than once",
                        path=source_path,
                        owners=sorted(set(owners)),
                    )
                )

        cycle_failed = _has_cycle(proposal)
        if cycle_failed:
            violations.append(
                _violation(
                    StableErrorCode.PLAN_CYCLE,
                    "/edges",
                    "proposal edges contain a directed cycle",
                )
            )

        rank_violations = _validate_ranks(proposal)
        violations.extend(rank_violations)

        violations.extend(_validate_limits(proposal, limits))
        return PlanValidation(
            accepted=not violations,
            violations=tuple(violations),
            source_coverage=source_coverage,
            checked_scope_pairs=len(proposal.slices) * (len(proposal.slices) - 1) // 2,
            cycle_check="FAIL" if cycle_failed else "PASS",
            blueprint_check=(
                "FAIL"
                if any(
                    violation.code is StableErrorCode.PLAN_BLUEPRINT_VIOLATION
                    for violation in violations
                )
                else "PASS"
            ),
            rank_check="FAIL" if rank_violations else "PASS",
            size_check=(
                "FAIL"
                if any(
                    violation.code is StableErrorCode.PLAN_SIZE_EXCEEDED
                    for violation in violations
                )
                else "PASS"
            ),
        )


def _validate_slice_modules(
    proposal: PlanProposal,
    inputs: PlanningInputs,
    modules: Mapping[ProjectModuleId, ModuleFact],
) -> list[PlanViolation]:
    expected_roles = {
        SliceKind.Implementation: ModuleRole.Source,
        SliceKind.TestTranslation: ModuleRole.Test,
        SliceKind.TestGeneration: ModuleRole.Source,
    }
    status_by_module = {
        status.module: status.status for status in inputs.analysis.coverage_status
    }
    violations: list[PlanViolation] = []
    for slice_index, slice_proposal in enumerate(proposal.slices):
        expected_role = expected_roles.get(slice_proposal.kind)
        for module_index, module_id in enumerate(slice_proposal.source_modules):
            pointer = f"/slices/{slice_index}/source_modules/{module_index}"
            module = modules.get(module_id)
            if module is None:
                violations.append(
                    _violation(
                        StableErrorCode.PLAN_COVERAGE_INVALID,
                        pointer,
                        "source module is not present in analysis facts",
                        module_id=str(module_id),
                    )
                )
                continue
            if expected_role is not None and module.role is not expected_role:
                violations.append(
                    _violation(
                        StableErrorCode.PLAN_COVERAGE_INVALID,
                        pointer,
                        "slice source module role does not match its Slice kind",
                        expected_role=expected_role.value,
                        actual_role=module.role.value,
                    )
                )
            out_of_scope = [
                str(path) for path in module.file_paths if not inputs.spec.scope.includes(str(path))
            ]
            if out_of_scope:
                violations.append(
                    _violation(
                        StableErrorCode.PLAN_COVERAGE_INVALID,
                        pointer,
                        "slice source module contains files outside the migration scope",
                        paths=out_of_scope,
                    )
                )
            if slice_proposal.kind is SliceKind.TestGeneration and status_by_module.get(
                module_id
            ) is not ModuleCoverage.EmptyTestSuite:
                violations.append(
                    _violation(
                        StableErrorCode.PLAN_COVERAGE_INVALID,
                        pointer,
                        "TestGeneration requires an EmptyTestSuite source module",
                        status=(
                            status_by_module[module_id].value
                            if module_id in status_by_module
                            else "MISSING"
                        ),
                    )
                )
    return violations


class PlanLedger:
    """A transaction-shaped ledger that publishes only accepted frozen plans."""

    def __init__(self, validator: PlanValidator | None = None) -> None:
        self.validator = validator or PlanValidator()
        self._records: list[FrozenPlan] = []

    @property
    def records(self) -> tuple[FrozenPlan, ...]:
        return tuple(copy.deepcopy(record) for record in self._records)

    @property
    def persisted_count(self) -> int:
        return len(self._records)

    def freeze(self, proposal: PlanProposal, inputs: PlanningInputs) -> FrozenPlan:
        validation = self.validator.validate(proposal, inputs)
        if not validation.accepted:
            raise PlanRejected(validation)

        local_ref_to_id = {
            slice_proposal.local_ref: SliceId(new_uuid7())
            for slice_proposal in proposal.slices
        }
        slices = tuple(
            MigrationSlice(
                id=local_ref_to_id[slice_proposal.local_ref],
                kind=slice_proposal.kind,
                source_modules=list(slice_proposal.source_modules),
                write_scope=WriteScope(
                    out=WriteScopeOut(
                        write_paths=list(slice_proposal.write_paths),
                        create_roots=list(slice_proposal.create_roots),
                    )
                ),
                required_checks=list(slice_proposal.required_checks),
                integration_rank=proposal.integration_ranks[slice_proposal.local_ref],
                proposal_ref=None,
            )
            for slice_proposal in proposal.slices
        )
        edges = tuple(
            PlanEdge.model_validate(
                {
                    "from": local_ref_to_id[edge.from_],
                    "to": local_ref_to_id[edge.to],
                    "kind": edge.kind,
                }
            )
            for edge in proposal.edges
        )
        edge_provenance = tuple(edge.provenance for edge in proposal.edges)
        edge_evidence = tuple(edge.evidence for edge in proposal.edges)
        integration_order = tuple(
            local_ref_to_id[local_ref]
            for local_ref in sorted(
                local_ref_to_id,
                key=lambda ref: integration_key(
                    proposal.integration_ranks[ref], local_ref_to_id[ref]
                ),
            )
        )
        frozen_without_hash = {
            "snapshot_oid": inputs.snapshot_oid,
            "slices": slices,
            "edges": edges,
            "edge_provenance": edge_provenance,
            "edge_evidence": edge_evidence,
            "validation": validation,
            "integration_order": integration_order,
            "local_ref_to_id": local_ref_to_id,
            "artifact_tasks": {
                slice_proposal.local_ref: slice_proposal.artifact_tasks
                for slice_proposal in proposal.slices
                if slice_proposal.artifact_tasks
            },
            "planner_rationale": proposal.planner_rationale,
            "frozen_artifacts": inputs.frozen_artifacts,
            "proposal": proposal,
        }
        plan_hash = compute_plan_hash(frozen_without_hash)
        frozen = FrozenPlan(
            plan_hash=plan_hash,
            snapshot_oid=inputs.snapshot_oid,
            slices=slices,
            edges=edges,
            edge_provenance=edge_provenance,
            edge_evidence=edge_evidence,
            validation=validation,
            integration_order=integration_order,
            local_ref_to_id=local_ref_to_id,
            artifact_tasks={
                slice_proposal.local_ref: slice_proposal.artifact_tasks
                for slice_proposal in proposal.slices
                if slice_proposal.artifact_tasks
            },
            planner_rationale=tuple(proposal.planner_rationale),
            frozen_artifacts=inputs.frozen_artifacts,
            proposal=proposal,
        )
        self._records.append(copy.deepcopy(frozen))
        return copy.deepcopy(frozen)


def compute_plan_hash(payload: Mapping[str, Any] | FrozenPlan) -> str:
    """Hash every frozen plan fact through the core canonical JSON helper."""

    if isinstance(payload, FrozenPlan):
        payload = {
            "snapshot_oid": payload.snapshot_oid,
            "frozen_artifacts": payload.frozen_artifacts,
            "proposal": payload.proposal,
            "slices": payload.slices,
            "edges": payload.edges,
            "edge_provenance": payload.edge_provenance,
            "edge_evidence": payload.edge_evidence,
            "validation": payload.validation,
            "integration_order": payload.integration_order,
            "local_ref_to_id": payload.local_ref_to_id,
            "artifact_tasks": payload.artifact_tasks,
            "planner_rationale": payload.planner_rationale,
        }

    normalized = {
        "snapshot_oid": payload["snapshot_oid"],
        "frozen_artifacts": _json_value(payload["frozen_artifacts"]),
        "proposal": _json_value(payload["proposal"]),
        "slices": _json_value(payload["slices"]),
        "edges": _json_value(payload["edges"]),
        "edge_provenance": _json_value(payload.get("edge_provenance", ())),
        "edge_evidence": _json_value(payload.get("edge_evidence", ())),
        "validation": _json_value(payload["validation"]),
        "integration_order": [str(item) for item in payload["integration_order"]],
        "local_ref_to_id": {
            key: str(value) for key, value in sorted(payload["local_ref_to_id"].items())
        },
        "artifact_tasks": _json_value(payload["artifact_tasks"]),
        "planner_rationale": _json_value(payload["planner_rationale"]),
    }
    return hashlib.sha256(canonical_json_bytes(normalized)).hexdigest()


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, set):
        return sorted((_json_value(item) for item in value), key=str)
    if hasattr(value, "model_dump"):
        return _json_value(value.model_dump(mode="json", by_alias=True))
    return value


def _in_scope_source_files(inputs: PlanningInputs) -> dict[str, ProjectModuleId]:
    artifact_paths = {str(artifact.path) for artifact in inputs.analysis.artifacts}
    expected: dict[str, ProjectModuleId] = {}
    for module in inputs.analysis.modules:
        if module.role is not ModuleRole.Source and module.role is not ModuleRole.Test:
            continue
        for path in module.file_paths:
            path_text = str(path)
            if inputs.spec.scope.includes(path_text) and path_text not in artifact_paths:
                expected[path_text] = module.module_id
    return expected


def _validate_edges(proposal: PlanProposal) -> list[PlanViolation]:
    refs = {slice_proposal.local_ref for slice_proposal in proposal.slices}
    violations: list[PlanViolation] = []
    seen: set[tuple[str, str, str]] = set()
    for index, edge in enumerate(proposal.edges):
        pointer = f"/edges/{index}"
        key = (edge.from_, edge.to, edge.kind.value)
        if edge.from_ not in refs or edge.to not in refs:
            violations.append(
                _violation(
                    StableErrorCode.PLAN_EDGE_INVALID,
                    pointer,
                    "edge endpoint is not a slice in this proposal",
                )
            )
        if edge.from_ == edge.to and edge.from_ not in refs:
            violations.append(
                _violation(StableErrorCode.PLAN_EDGE_INVALID, pointer, "self edges are not allowed")
            )
        if key in seen:
            violations.append(
                _violation(StableErrorCode.PLAN_EDGE_INVALID, pointer, "duplicate edge")
            )
        seen.add(key)
        if edge.provenance is EdgeProvenance.ImportUnknown and edge.kind.value == "REQUIRES":
            violations.append(
                _violation(
                    StableErrorCode.PLAN_EDGE_INVALID,
                    pointer,
                    "Unknown import facts may only produce OrderedBefore edges",
                )
            )
        if edge.provenance is EdgeProvenance.WriteScopeConflict:
            violations.append(
                _violation(
                    StableErrorCode.PLAN_SCOPE_CONFLICT,
                    pointer,
                    "write conflicts cannot be hidden by a planning edge",
                )
            )
    return violations


def _validate_test_edges(proposal: PlanProposal, inputs: PlanningInputs) -> list[PlanViolation]:
    modules = {module.module_id: module for module in inputs.analysis.modules}
    tested_by_test_module: dict[ProjectModuleId, set[ProjectModuleId]] = defaultdict(set)
    for coverage in inputs.analysis.coverage:
        test_module_ids = [
            module.module_id
            for module in inputs.analysis.modules
            if coverage.test_file in module.file_paths
        ]
        for test_module_id in test_module_ids:
            tested_by_test_module[test_module_id].update(coverage.tested_modules)

    kind_by_ref = {slice_.local_ref: slice_.kind for slice_ in proposal.slices}
    modules_by_ref = {
        slice_.local_ref: set(slice_.source_modules) for slice_ in proposal.slices
    }
    violations: list[PlanViolation] = []
    for index, edge in enumerate(proposal.edges):
        test_ref = next(
            (
                ref
                for ref, kind in kind_by_ref.items()
                if kind.value in {"TEST_TRANSLATION", "TEST_GENERATION"}
                and ref in {edge.from_, edge.to}
            ),
            None,
        )
        if test_ref is None:
            continue
        implementation_ref = edge.to if test_ref == edge.from_ else edge.from_
        if (
            kind_by_ref.get(implementation_ref) is None
            or kind_by_ref[implementation_ref].value != "IMPLEMENTATION"
        ):
            continue
        tested_modules: set[ProjectModuleId] = set()
        if kind_by_ref[test_ref] is SliceKind.TestGeneration:
            tested_modules.update(modules_by_ref[test_ref])
        else:
            for module_id in modules_by_ref[test_ref]:
                if modules.get(module_id) is not None:
                    tested_modules.update(tested_by_test_module.get(module_id, set()))
        if tested_modules.intersection(modules_by_ref[implementation_ref]):
            violations.append(
                _violation(
                    StableErrorCode.PLAN_EDGE_INVALID,
                    f"/edges/{index}",
                    "test slices must not edge to their tested implementation slice",
                )
            )
    return violations


def _validate_artifacts(
    proposal: PlanProposal, inputs: PlanningInputs
) -> list[PlanViolation]:
    facts = tuple(inputs.analysis.artifacts)
    tasks = tuple(
        (slice_index, task_index, task)
        for slice_index, slice_proposal in enumerate(proposal.slices)
        for task_index, task in enumerate(slice_proposal.artifact_tasks)
    )
    violations: list[PlanViolation] = []

    def task_matches_fact(task: Any, fact: ArtifactFact) -> bool:
        if task.artifact_path is not None:
            return str(task.artifact_path) == str(fact.path)
        return str(task.source_path) == str(fact.source_path or fact.path)

    for fact in facts:
        matches = [item for item in tasks if task_matches_fact(item[2], fact)]
        if len(matches) != 1:
            violations.append(
                _violation(
                    StableErrorCode.PLAN_COVERAGE_INVALID,
                    "/slices",
                    "each analyzed artifact must be assigned to exactly one artifact task",
                    artifact_path=str(fact.path),
                    task_count=len(matches),
                )
            )
            continue
        task = matches[0][2]
        if task.kind is not fact.artifact_kind:
            violations.append(
                _violation(
                    StableErrorCode.PLAN_COVERAGE_INVALID,
                    f"/slices/{matches[0][0]}/artifact_tasks/{matches[0][1]}",
                    "artifact task kind does not match the analyzed artifact",
                    artifact_path=str(fact.path),
                )
            )

    for slice_index, task_index, task in tasks:
        matching_facts = [fact for fact in facts if task_matches_fact(task, fact)]
        if len(matching_facts) != 1:
            violations.append(
                _violation(
                    StableErrorCode.PLAN_COVERAGE_INVALID,
                    f"/slices/{slice_index}/artifact_tasks/{task_index}",
                    "artifact task does not identify exactly one analyzed artifact",
                    task_source_path=str(task.source_path),
                )
            )
    return violations


def _validate_artifact_scopes(proposal: PlanProposal) -> list[PlanViolation]:
    violations: list[PlanViolation] = []
    translation_slices = tuple(
        (index, slice_proposal)
        for index, slice_proposal in enumerate(proposal.slices)
        if slice_proposal.kind
        in {SliceKind.Implementation, SliceKind.TestTranslation, SliceKind.TestGeneration}
    )
    for index, slice_proposal in enumerate(proposal.slices):
        for task_index, task in enumerate(slice_proposal.artifact_tasks):
            if task.kind is not ArtifactKind.ResourceFile:
                continue
            if any(
                _scopes_overlap(
                    (task.target_path,),
                    (),
                    target_slice.write_paths,
                    target_slice.create_roots,
                )
                for _target_index, target_slice in translation_slices
            ):
                violations.append(
                    _violation(
                        StableErrorCode.PLAN_SCOPE_CONFLICT,
                        f"/slices/{index}/artifact_tasks/{task_index}",
                        "ResourceFile targets must not enter a translation slice write scope",
                    )
                )
    return violations


def _validate_scope_conflicts(proposal: PlanProposal) -> list[PlanViolation]:
    violations: list[PlanViolation] = []
    for left_index, left in enumerate(proposal.slices):
        for right_index in range(left_index + 1, len(proposal.slices)):
            right = proposal.slices[right_index]
            if _scopes_overlap(
                left.write_paths,
                left.create_roots,
                right.write_paths,
                right.create_roots,
            ):
                violations.append(
                    _violation(
                        StableErrorCode.PLAN_SCOPE_CONFLICT,
                        f"/slices/{left_index}/write_paths",
                        "slice write scopes must be pairwise disjoint",
                        other=right.local_ref,
                    )
                )
    return violations


def _scopes_overlap(
    left_paths: Iterable[str],
    left_roots: Iterable[str],
    right_paths: Iterable[str],
    right_roots: Iterable[str],
) -> bool:
    left_paths_set = set(left_paths)
    right_paths_set = set(right_paths)
    if left_paths_set.intersection(right_paths_set):
        return True
    if any(_path_is_in_root(path, root) for path in left_paths_set for root in right_roots):
        return True
    if any(_path_is_in_root(path, root) for path in right_paths_set for root in left_roots):
        return True
    return any(
        _path_is_in_root(left_root, right_root) or _path_is_in_root(right_root, left_root)
        for left_root in left_roots
        for right_root in right_roots
    )


def _path_is_in_root(path: str, root: str) -> bool:
    return path == root or path.startswith(f"{root}/")


def _validate_blueprint(proposal: PlanProposal, inputs: PlanningInputs) -> list[PlanViolation]:
    prefixes: set[str] = set()
    for boundary in inputs.target_project_blueprint.module_boundaries:
        for key in ("target_path_prefix", "target_prefix", "path_prefix", "target_root"):
            value = boundary.get(key)
            if isinstance(value, str) and value:
                prefixes.add(value.rstrip("/"))
    for principle in inputs.target_project_blueprint.target_layout_principles:
        prefixes.update(_layout_principle_prefixes(principle))
    if not prefixes:
        return []
    violations: list[PlanViolation] = []
    for slice_index, slice_proposal in enumerate(proposal.slices):
        for path in (*slice_proposal.write_paths, *slice_proposal.create_roots):
            if not any(_path_is_in_root(path, prefix) for prefix in prefixes):
                violations.append(
                    _violation(
                        StableErrorCode.PLAN_BLUEPRINT_VIOLATION,
                        f"/slices/{slice_index}",
                        "target path is outside all Blueprint target prefixes",
                        path=path,
                        allowed_prefixes=sorted(prefixes),
                    )
                )
    return violations


def _layout_principle_prefixes(principle: str) -> set[str]:
    """Extract only explicit path constraints from opaque layout guidance."""

    patterns = (
        r"\b(?:target\s+)?layout\s+(?:prefix|root)\s*[:=]\s*[`\"']?([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*)",
        r"\b(?:target\s+)?(?:paths?|files?)\s+under\s+[`\"']?([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*)",
    )
    return {
        match.group(1).rstrip("/")
        for pattern in patterns
        if (match := re.search(pattern, principle, flags=re.IGNORECASE)) is not None
    }


def _has_cycle(proposal: PlanProposal) -> bool:
    refs = {slice_proposal.local_ref for slice_proposal in proposal.slices}
    adjacency: dict[str, set[str]] = {ref: set() for ref in refs}
    indegree = {ref: 0 for ref in refs}
    for edge in proposal.edges:
        if edge.from_ in refs and edge.to in refs:
            if edge.to not in adjacency[edge.from_]:
                adjacency[edge.from_].add(edge.to)
                indegree[edge.to] += 1
    queue = sorted((ref for ref, degree in indegree.items() if degree == 0))
    visited_count = 0
    while queue:
        ref = queue.pop(0)
        visited_count += 1
        for child in sorted(adjacency[ref]):
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    return visited_count != len(refs)


def _validate_ranks(proposal: PlanProposal) -> list[PlanViolation]:
    refs = {slice_proposal.local_ref for slice_proposal in proposal.slices}
    violations: list[PlanViolation] = []
    if set(proposal.integration_ranks) != refs:
        violations.append(
            _violation(
                StableErrorCode.PLAN_PROPOSAL_INVALID,
                "/integration_ranks",
                "integration_ranks must contain exactly one non-negative rank per slice",
            )
        )
    for ref, rank in proposal.integration_ranks.items():
        if ref in refs and (type(rank) is not int or rank < 0):
            violations.append(
                _violation(
                    StableErrorCode.PLAN_PROPOSAL_INVALID,
                    f"/integration_ranks/{ref}",
                    "integration rank must be a non-negative integer",
                )
            )
    for index, edge in enumerate(proposal.edges):
        if edge.from_ in refs and edge.to in refs:
            from_rank = proposal.integration_ranks.get(edge.from_)
            to_rank = proposal.integration_ranks.get(edge.to)
            if from_rank is not None and to_rank is not None and from_rank >= to_rank:
                violations.append(
                    _violation(
                        StableErrorCode.PLAN_RANK_INCONSISTENT,
                        f"/edges/{index}",
                        "every edge must point from a lower integration rank to a higher rank",
                        from_rank=from_rank,
                        to_rank=to_rank,
                    )
                )
    return violations


def _validate_limits(proposal: PlanProposal, limits: PlanningLimits) -> list[PlanViolation]:
    violations: list[PlanViolation] = []
    if len(proposal.slices) > limits.max_slices:
        violations.append(
            _violation(StableErrorCode.PLAN_SIZE_EXCEEDED, "/slices", "slice limit exceeded")
        )
    if len(proposal.edges) > limits.max_edges:
        violations.append(
            _violation(StableErrorCode.PLAN_SIZE_EXCEEDED, "/edges", "edge limit exceeded")
        )
    total_paths = 0
    for index, slice_proposal in enumerate(proposal.slices):
        path_count = len(slice_proposal.write_paths)
        total_paths += path_count
        if path_count > limits.max_write_paths_per_slice:
            violations.append(
                _violation(
                    StableErrorCode.PLAN_SIZE_EXCEEDED,
                    f"/slices/{index}/write_paths",
                    "single-slice write path limit exceeded",
                )
            )
    if total_paths > limits.max_total_write_paths:
        violations.append(
            _violation(
                StableErrorCode.PLAN_SIZE_EXCEEDED,
                "/slices",
                "total write scope limit exceeded",
            )
        )
    return violations


def _violation(
    code: StableErrorCode, pointer: str, message: str, **details: object
) -> PlanViolation:
    return PlanViolation(code=code, pointer=pointer, message=message, details=details)


def validate_plan(proposal: PlanProposal, inputs: PlanningInputs) -> PlanValidation:
    """Convenience entry point for the pure validation operation."""

    return PlanValidator().validate(proposal, inputs)


def freeze_plan(proposal: PlanProposal, inputs: PlanningInputs) -> FrozenPlan:
    """Convenience entry point that atomically freezes one accepted proposal."""

    return PlanLedger().freeze(proposal, inputs)


__all__ = [
    "PlanLedger",
    "PlanRejected",
    "PlanValidator",
    "compute_plan_hash",
    "freeze_plan",
    "validate_plan",
]

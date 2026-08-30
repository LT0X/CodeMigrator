"""Read-only, closed-schema source navigation over an AnalysisResult."""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Annotated, Literal

from pydantic import Field

from codemigrator.core import ProjectModuleId, RepoRelativePath, StableErrorCode
from codemigrator.core._base import CoreModel
from codemigrator.core.paths import _validate_repo_relative_path

from .models import (
    AnalysisCapability,
    AnalysisResult,
    ModuleTarget,
    SourcePosition,
    SourceRange,
)
from .ports import ProjectionKey, ProjectionStore, SnapshotSource


class _QueryModel(CoreModel):
    pass


class FindSymbol(_QueryModel):
    kind: Literal["FIND_SYMBOL"]
    symbol: str = Field(min_length=1)
    module: ProjectModuleId | None = None


class GotoDefinition(_QueryModel):
    kind: Literal["GOTO_DEFINITION"]
    use_site: SourceRange


class FindReferences(_QueryModel):
    kind: Literal["FIND_REFERENCES"]
    symbol: str = Field(min_length=1)


class FindCallers(_QueryModel):
    kind: Literal["FIND_CALLERS"]
    symbol_or_range: str = Field(min_length=1)


class FindCallees(_QueryModel):
    kind: Literal["FIND_CALLEES"]
    symbol_or_range: str = Field(min_length=1)


class FindImpact(_QueryModel):
    kind: Literal["FIND_IMPACT"]
    symbol_or_module: str = Field(min_length=1)
    direction: Literal["UPSTREAM", "DOWNSTREAM", "BOTH"]


class SearchContext(_QueryModel):
    kind: Literal["SEARCH_CONTEXT"]
    query: str = Field(min_length=1)
    module: ProjectModuleId | None = None


class ExtractSubtree(_QueryModel):
    kind: Literal["EXTRACT_SUBTREE"]
    range: SourceRange


SourceAstQuery = Annotated[
    FindSymbol
    | GotoDefinition
    | FindReferences
    | FindCallers
    | FindCallees
    | FindImpact
    | SearchContext
    | ExtractSubtree,
    Field(discriminator="kind"),
]


class QueryHit(_QueryModel):
    range: SourceRange
    symbol_kind: str | None = None
    text: str | None = None


class QueryResult(_QueryModel):
    hits: list[QueryHit]
    truncated: bool = False
    error: StableErrorCode | None = None


class QuerySourceAst:
    """Pure read service; all durable facts come from the supplied projection."""

    MAX_HITS = 200
    MAX_TEXT_BYTES = 256 * 1024
    MAX_TIMEOUT_SECONDS = 60.0

    def __init__(
        self,
        *,
        result: AnalysisResult,
        snapshot: SnapshotSource,
        max_hits: int = 200,
        max_text_bytes: int = 256 * 1024,
        timeout_seconds: float = 60.0,
        projection_store: ProjectionStore | None = None,
        projection_key: ProjectionKey | None = None,
    ) -> None:
        if max_hits < 1 or max_text_bytes < 1 or timeout_seconds <= 0:
            raise ValueError("query limits and timeout must be positive")
        if max_hits > self.MAX_HITS:
            raise ValueError(f"max_hits cannot exceed {self.MAX_HITS}")
        if max_text_bytes > self.MAX_TEXT_BYTES:
            raise ValueError(f"max_text_bytes cannot exceed {self.MAX_TEXT_BYTES}")
        if timeout_seconds > self.MAX_TIMEOUT_SECONDS:
            raise ValueError(f"timeout_seconds cannot exceed {self.MAX_TIMEOUT_SECONDS}")
        self.result = result
        self.snapshot = snapshot
        self.max_hits = max_hits
        self.max_text_bytes = max_text_bytes
        self.timeout_seconds = timeout_seconds
        self._deadline = 0.0
        if projection_store is not None:
            key = projection_key or ProjectionKey(
                snapshot_oid=result.snapshot_oid,
                descriptor_sha256=result.descriptor_sha256,
            )
            artifact = projection_store.read(key)
            if artifact is not None:
                self.result = artifact.result

    def query(self, request: SourceAstQuery) -> QueryResult:
        self._deadline = time.monotonic() + self.timeout_seconds
        if (
            isinstance(
                request,
                (FindSymbol, GotoDefinition, FindReferences, FindCallers, FindCallees, FindImpact),
            )
            and self.result.capability is AnalysisCapability.TextFallback
        ):
            raise ValueError(
                f"{StableErrorCode.TEXT_FALLBACK_UNSUPPORTED.value}: "
                "symbol navigation requires PSF-2"
            )
        if isinstance(request, FindSymbol):
            self._validate_query_text(request.symbol)
            hits = [
                self._hit(binding.definition, binding.kind.value, binding.signature_text)
                for binding in self.result.symbol_bindings
                if binding.symbol == request.symbol
                and (request.module is None or binding.module == request.module)
            ]
            return self._limit(hits)
        if isinstance(request, FindReferences):
            self._validate_query_text(request.symbol)
            hits = [
                self._hit(reference.site, None, None)
                for reference in self.result.reference_sites
                if reference.symbol == request.symbol
            ]
            return self._limit(hits)
        if isinstance(request, SearchContext):
            return self._search(request)
        if isinstance(request, ExtractSubtree):
            return self._extract(request)
        if isinstance(request, GotoDefinition):
            self._ensure_snapshot_path(request.use_site.file_path)
            for reference in self.result.reference_sites:
                if reference.site == request.use_site and reference.binding is not None:
                    return self._limit([self._hit(reference.binding, None, None)])
            return self._limit([])
        if isinstance(request, (FindCallers, FindCallees)):
            self._validate_query_text(request.symbol_or_range)
            return self._limit(self._call_graph_hits(request.symbol_or_range, request))
        self._validate_query_text(request.symbol_or_module)
        return self._limit(self._impact_hits(request.symbol_or_module, request.direction))

    def _validate_query_text(self, value: str) -> None:
        if value.startswith(("/", "~", ".")) or "\\" in value or "\x00" in value:
            raise ValueError(f"{StableErrorCode.PATH_OUTSIDE_SNAPSHOT.value}: unsafe query path")

    def _ensure_snapshot_path(self, path: str) -> None:
        try:
            safe_path = _validate_repo_relative_path(path)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{StableErrorCode.PATH_OUTSIDE_SNAPSHOT.value}: {path}") from exc
        if (
            safe_path not in self.snapshot.paths
            or safe_path == ".git"
            or safe_path.startswith(".git/")
        ):
            raise ValueError(f"{StableErrorCode.PATH_OUTSIDE_SNAPSHOT.value}: {safe_path}")

    def _check_timeout(self) -> None:
        if time.monotonic() > self._deadline:
            raise ValueError(f"{StableErrorCode.QUERY_TIMEOUT.value}: query exceeded timeout")

    @staticmethod
    def _hit(source_range: SourceRange, symbol_kind: str | None, text: str | None) -> QueryHit:
        return QueryHit(range=source_range, symbol_kind=symbol_kind, text=text)

    def _limit(self, hits: Iterable[QueryHit]) -> QueryResult:
        ordered = sorted(
            hits,
            key=lambda hit: (
                str(hit.range.file_path).encode("utf-8"),
                hit.range.start.line,
                hit.range.start.column,
            ),
        )
        truncated = len(ordered) > self.max_hits
        selected = ordered[: self.max_hits]
        text_bytes = 0
        bounded: list[QueryHit] = []
        for hit in selected:
            if hit.text is None:
                bounded.append(hit)
                continue
            remaining = self.max_text_bytes - text_bytes
            encoded = hit.text.encode("utf-8")
            if len(encoded) > remaining:
                bounded.append(
                    hit.model_copy(update={"text": encoded[:remaining].decode("utf-8", "ignore")})
                )
                truncated = True
                break
            bounded.append(hit)
            text_bytes += len(encoded)
        return QueryResult(
            hits=bounded,
            truncated=truncated,
            error=StableErrorCode.TRUNCATED if truncated else None,
        )

    def _search(self, request: SearchContext) -> QueryResult:
        hits: list[QueryHit] = []
        for path in self.snapshot.paths:
            self._check_timeout()
            if path == ".git" or path.startswith(".git/"):
                continue
            if request.module is not None and self._module_for_path(path) != request.module:
                continue
            try:
                text = self.snapshot.read(path).decode("utf-8")
            except UnicodeDecodeError:
                continue
            for line_number, line in enumerate(text.splitlines(keepends=True), start=1):
                self._check_timeout()
                column = line.find(request.query)
                if column >= 0:
                    content = line.rstrip("\r\n")
                    hits.append(
                        self._hit(
                            SourceRange(
                                file_path=RepoRelativePath(path),
                                start=SourcePosition(line=line_number, column=column),
                                end=SourcePosition(
                                    line=line_number, column=column + len(request.query)
                                ),
                            ),
                            None,
                            content,
                        )
                    )
        return self._limit(hits)

    def _extract(self, request: ExtractSubtree) -> QueryResult:
        self._ensure_snapshot_path(request.range.file_path)
        lines = self.snapshot.read(request.range.file_path).decode("utf-8").splitlines()
        start = request.range.start.line - 1
        end = request.range.end.line
        text = "\n".join(lines[start:end])
        return self._limit([self._hit(request.range, None, text)])

    def _module_range(self, module_id: ProjectModuleId) -> SourceRange | None:
        for module in self.result.modules:
            if module.module_id == module_id and module.file_paths:
                path = module.file_paths[0]
                return SourceRange(
                    file_path=RepoRelativePath(path),
                    start=SourcePosition(line=1, column=0),
                    end=SourcePosition(line=1, column=0),
                )
        return None

    def _module_for_path(self, path: str) -> ProjectModuleId | None:
        return next(
            (
                module.module_id
                for module in self.result.modules
                if path in {str(item) for item in module.file_paths}
            ),
            None,
        )

    def _range_selector(self, value: str) -> SourceRange | None:
        path, separator, column_text = value.rpartition(":")
        if not separator:
            return None
        path, separator, line_text = path.rpartition(":")
        if not separator or not line_text.isdigit() or not column_text.isdigit():
            return None
        self._ensure_snapshot_path(path)
        line = int(line_text)
        column = int(column_text)
        return SourceRange(
            file_path=RepoRelativePath(path),
            start=SourcePosition(line=line, column=column),
            end=SourcePosition(line=line, column=column),
        )

    @staticmethod
    def _same_position(left: SourceRange, right: SourceRange) -> bool:
        return (
            left.file_path == right.file_path
            and left.start.line == right.start.line
            and left.start.column == right.start.column
        )

    def _call_graph_hits(
        self,
        selector: str,
        request: FindCallers | FindCallees,
    ) -> list[QueryHit]:
        selected_range = self._range_selector(selector)
        hits: list[QueryHit] = []
        for edge in self.result.call_edges:
            if selected_range is None:
                matches = edge.symbol == selector
            elif isinstance(request, FindCallers):
                matches = self._same_position(edge.callee, selected_range)
            else:
                matches = self._same_position(edge.caller, selected_range)
            if not matches:
                continue
            location = edge.caller if isinstance(request, FindCallers) else edge.callee
            hits.append(self._hit(location, None, None))
        return hits

    def _impact_hits(self, value: str, direction: str) -> list[QueryHit]:
        modules = {str(module.module_id): module.module_id for module in self.result.modules}
        selected = modules.get(value)
        if selected is None:
            selected = next(
                (
                    module.module_id
                    for module in self.result.modules
                    if any(export.symbol == value for export in module.exported_symbols)
                ),
                None,
            )
        if selected is None:
            return []
        adjacency: dict[ProjectModuleId, set[ProjectModuleId]] = {}
        for edge in self.result.imports:
            if isinstance(edge.to, ModuleTarget):
                adjacency.setdefault(edge.from_module, set()).add(edge.to.module_id)
        if direction == "DOWNSTREAM":
            graph = adjacency
        elif direction == "UPSTREAM":
            graph = {}
            for source, targets in adjacency.items():
                for target in targets:
                    graph.setdefault(target, set()).add(source)
        else:
            graph = adjacency
            for source, targets in list(adjacency.items()):
                for target in targets:
                    graph.setdefault(target, set()).add(source)
        seen: set[ProjectModuleId] = set()
        pending = [selected]
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            pending.extend(graph.get(current, ()))
        return [
            self._hit(location, None, None)
            for module_id in sorted(seen, key=str)
            if (location := self._module_range(module_id)) is not None
        ]


__all__ = [
    "ExtractSubtree",
    "FindCallers",
    "FindCallees",
    "FindImpact",
    "FindReferences",
    "FindSymbol",
    "GotoDefinition",
    "QueryHit",
    "QueryResult",
    "QuerySourceAst",
    "SearchContext",
    "SourceAstQuery",
]

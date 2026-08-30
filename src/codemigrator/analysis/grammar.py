"""In-process grammar handles and per-grammar crash isolation."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from codemigrator.core import StableErrorCode

from .errors import GrammarFailure

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class SyntaxNode:
    """Small immutable adapter shape shared by grammar implementations and tests."""

    kind: str
    start_byte: int
    end_byte: int
    name: str | None = None
    children: tuple[SyntaxNode, ...] = ()

    def __post_init__(self) -> None:
        if not self.kind:
            raise ValueError("syntax node kind must be non-empty")
        if self.start_byte < 0 or self.end_byte < self.start_byte:
            raise ValueError("syntax node byte range is invalid")


@dataclass(frozen=True)
class GrammarHandle:
    grammar_id: str
    grammar_sha256: str
    parser: Callable[[bytes], object]


@dataclass(frozen=True)
class GrammarCacheKey:
    snapshot_oid: str
    file_path: str
    grammar_sha256: str


class GrammarCircuitBreaker:
    """Open a breaker after two crashes without affecting other grammars."""

    def __init__(self) -> None:
        self._failures: dict[str, int] = {}
        self._open: set[str] = set()

    def run(self, grammar_id: str, operation: Callable[[], T]) -> T:
        if grammar_id in self._open:
            raise GrammarFailure(
                StableErrorCode.ANALYSIS_INFRA_ERROR,
                f"grammar circuit is open: {grammar_id}",
            )
        try:
            result = operation()
        except Exception as exc:
            failures = self._failures.get(grammar_id, 0) + 1
            self._failures[grammar_id] = failures
            if failures >= 2:
                self._open.add(grammar_id)
            raise GrammarFailure(
                StableErrorCode.ANALYSIS_INFRA_ERROR,
                f"grammar parser crashed: {grammar_id}",
            ) from exc
        self._failures.pop(grammar_id, None)
        return result

    def is_open(self, grammar_id: str) -> bool:
        return grammar_id in self._open


class GrammarCache[T]:
    """Bounded process-local AST cache keyed by snapshot, file, and grammar."""

    def __init__(self, *, max_entries: int = 256) -> None:
        if max_entries < 1:
            raise ValueError("grammar cache capacity must be positive")
        self._max_entries = max_entries
        self._handles: OrderedDict[GrammarCacheKey, T] = OrderedDict()

    def get_or_load(
        self,
        snapshot_oid: str,
        file_path: str,
        grammar_sha256: str,
        loader: Callable[[], T],
    ) -> T:
        key = GrammarCacheKey(snapshot_oid, file_path, grammar_sha256)
        if key in self._handles:
            self._handles.move_to_end(key)
            return self._handles[key]
        value = loader()
        self._handles[key] = value
        self._handles.move_to_end(key)
        if len(self._handles) > self._max_entries:
            self._handles.popitem(last=False)
        return value


__all__ = [
    "GrammarCache",
    "GrammarCacheKey",
    "GrammarCircuitBreaker",
    "GrammarFailure",
    "GrammarHandle",
    "SyntaxNode",
]

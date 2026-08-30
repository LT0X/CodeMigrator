"""The deliberately small repository path pattern language for Spec v3."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models.spec import SpecScope


_UNSUPPORTED_PATTERN_CHARS = frozenset(
    ("?", "[", "]", "{", "}", "(", ")", "|", "+", "^", "$", "\\")
)


def validate_scope_pattern(pattern: object) -> str:
    """Validate one finite scope pattern and return it unchanged."""

    if not isinstance(pattern, str) or not pattern:
        raise ValueError("scope pattern must be a non-empty string")
    if "\x00" in pattern:
        raise ValueError("scope pattern must not contain NUL")
    if pattern.startswith(("/", "~")) or "\\" in pattern:
        raise ValueError("scope pattern must be repository-relative")
    if any(character in _UNSUPPORTED_PATTERN_CHARS for character in pattern):
        raise ValueError("scope pattern uses unsupported wildcard or regex syntax")
    if "**" in pattern:
        raise ValueError("scope pattern permits only one trailing-segment star")

    directory = pattern.endswith("/")
    body = pattern[:-1] if directory else pattern
    parts = body.split("/")
    if any(part in {"", ".", "..", ".git"} for part in parts):
        raise ValueError("scope pattern contains an unsafe path segment")
    if parts[0] == ".git":
        raise ValueError("scope pattern cannot start with .git")

    star_positions = [index for index, character in enumerate(body) if character == "*"]
    if star_positions:
        star_part = parts[-1]
        if directory or len(star_positions) > 1 or "*" not in star_part:
            raise ValueError("scope star must occur once in the final file segment")
    return pattern


def _pattern_regex(pattern: str) -> re.Pattern[str]:
    body = pattern[:-1] if pattern.endswith("/") else pattern
    escaped = re.escape(body).replace(r"\*", "[^/]+")
    if pattern.endswith("/"):
        return re.compile(r"^" + escaped + r"/")
    return re.compile(r"^" + escaped + r"$")


def scope_pattern_matches(pattern: str, path: str) -> bool:
    """Match a path without importing glob/fnmatch semantics."""

    if path == ".git" or path.startswith(".git/"):
        return False
    return _pattern_regex(pattern).match(path) is not None


def _literal_directory(pattern: str) -> bool:
    return pattern.endswith("/")


def _pattern_is_contained(include: str, exclude: str) -> bool:
    if _literal_directory(include):
        return exclude.startswith(include)
    if "*" not in include:
        return include == exclude
    if "*" in exclude:
        include_prefix, include_suffix = include.split("*", 1)
        exclude_prefix, exclude_suffix = exclude.split("*", 1)
        return (
            exclude_prefix.startswith(include_prefix)
            and exclude_suffix.endswith(include_suffix)
            and "/" not in exclude_prefix[len(include_prefix) :]
        )
    return scope_pattern_matches(include, exclude)


def excludes_are_contained(includes: Sequence[str], excludes: Sequence[str]) -> bool:
    return all(
        any(_pattern_is_contained(include, exclude) for include in includes) for exclude in excludes
    )


def scope_includes_path(scope: SpecScope, path: str) -> bool:
    if not isinstance(path, str) or path == ".git" or path.startswith(".git/"):
        return False
    return any(scope_pattern_matches(pattern, path) for pattern in scope.include) and not any(
        scope_pattern_matches(pattern, path) for pattern in scope.exclude
    )


def normalize_scope_paths(paths: Sequence[str]) -> list[str]:
    return sorted(set(paths), key=lambda path: path.encode("utf-8"))


__all__ = [
    "excludes_are_contained",
    "normalize_scope_paths",
    "scope_includes_path",
    "scope_pattern_matches",
    "validate_scope_pattern",
]

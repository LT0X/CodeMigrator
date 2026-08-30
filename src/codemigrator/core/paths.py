"""Pure path, branch, ordering, and canonicalization helpers."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import rfc8785
from pydantic import BaseModel


def validate_branch_prefix(value: object) -> str:
    """Validate a safe slash-separated branch prefix."""

    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("ascii", errors="ignore")) != len(value)
    ):
        raise ValueError("branch prefix must contain ASCII characters")
    encoded = value.encode("ascii")
    if not 1 <= len(encoded) <= 32:
        raise ValueError("branch prefix must be between 1 and 32 bytes")
    if value.startswith("/") or value.endswith("/") or "//" in value:
        raise ValueError("branch prefix contains an empty path segment")
    segments = value.split("/")
    if any(segment in {".", "..", ".git"} for segment in segments):
        raise ValueError("branch prefix contains a reserved segment")
    if any(not (char.islower() or char.isdigit() or char in {"-", "/"}) for char in value):
        raise ValueError("branch prefix contains an unsupported character")
    return value


def _repo_path_key(value: str) -> bytes:
    return value.encode("utf-8")


def normalize_repo_relative_paths(paths: Sequence[object]) -> list[str]:
    """Validate, deduplicate, and UTF-8-byte-sort repository-relative paths."""

    if isinstance(paths, (str, bytes)):
        raise TypeError("repository paths must be a sequence of paths")
    normalized: set[str] = set()
    for path in paths:
        if not isinstance(path, str) or not path or "\x00" in path:
            raise ValueError("repository path must be a non-empty string without NUL")
        if path.startswith(("/", "~")) or "\\" in path:
            raise ValueError("repository path must be relative and POSIX-like")
        parts = path.split("/")
        if any(part in {"", ".", "..", ".git"} for part in parts):
            raise ValueError("repository path contains an unsafe segment")
        normalized.add(path)
    return sorted(normalized, key=_repo_path_key)


def _validate_repo_relative_path(value: object) -> str:
    """Validate one repository-relative path at a model boundary."""

    return normalize_repo_relative_paths([value])[0]


def integration_key(integration_rank: int, slice_id: uuid.UUID) -> tuple[int, bytes]:
    """Return the deterministic integration ordering key."""

    if type(integration_rank) is not int:
        raise TypeError("integration rank must be an integer")
    if not isinstance(slice_id, uuid.UUID):
        raise TypeError("slice id must be a UUID")
    return integration_rank, slice_id.bytes


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize JSON-compatible data using RFC 8785 canonicalization."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", by_alias=True)
    elif isinstance(value, Mapping):
        value = dict(value)
    elif isinstance(value, (list, tuple)):
        value = list(value)
    try:
        return rfc8785.dumps(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("value is not canonicalizable JSON") from exc

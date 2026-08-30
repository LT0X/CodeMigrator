"""Canonical bytes for the analysis projection.

The analysis projection deliberately has its own canonicalization rule.  It is
not the RFC 8785 document canonicalization used by the Spec boundary.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel


def _normalize(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _normalize(value.model_dump(mode="json", by_alias=True))
    if isinstance(value, Mapping):
        return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        normalized = [_normalize(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(
                item, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
            ),
        )
    return value


def canonical_bytes(value: Any) -> bytes:
    """Serialize projection values with stable keys, numbers, and UTF-8 bytes."""

    try:
        return json.dumps(
            _normalize(value),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("value is not canonicalizable analysis data") from exc


__all__ = ["canonical_bytes"]

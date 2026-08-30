"""Fail-closed, write-only secret registration and payload scanning."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import quote

FORBIDDEN_FIELDS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "body",
        "command",
        "content",
        "cookie",
        "credential",
        "database_url",
        "dsn",
        "full_path",
        "host_path",
        "message",
        "messages",
        "new_text",
        "old_text",
        "password",
        "path",
        "private_key",
        "prompt",
        "raw_output",
        "script",
        "secret",
        "source",
        "source_code",
        "stderr",
        "stdout",
        "token",
        "tool_output",
    }
)


@dataclass(frozen=True, slots=True)
class RedactionResult:
    """The only public result of a redaction attempt.

    A rejected result deliberately has no payload. Callers must never attempt to
    recover the original value after a fail-closed decision.
    """

    accepted: bool
    value: object | None
    reason: str | None = None


class SecretRegistry:
    """A write-only registry whose values can only be used for safety scanning."""

    def __init__(self) -> None:
        self._fingerprints: tuple[tuple[int, bytes], ...] = ()

    def register(self, secret: str) -> None:
        """Register one secret without exposing it to callers."""

        if not isinstance(secret, str) or not secret:
            raise ValueError("secret must be a non-empty string")
        fingerprints = _fingerprints(_encoded_variants(secret))
        self._fingerprints = tuple(
            dict.fromkeys((*self._fingerprints, *fingerprints))
        )

    def redact(self, value: object) -> RedactionResult:
        """Copy a safe JSON-like value or reject the complete payload."""

        result = _scan_value(value, self._fingerprints)
        if result is _BLOCKED_SECRET:
            return RedactionResult(False, None, "secret_match")
        if result is _BLOCKED_FIELD:
            return RedactionResult(False, None, "sensitive_field")
        if result is _BLOCKED_TYPE:
            return RedactionResult(False, None, "unsupported_value")
        return RedactionResult(True, result)


_BLOCKED_SECRET = object()
_BLOCKED_FIELD = object()
_BLOCKED_TYPE = object()


def _encoded_variants(secret: str) -> tuple[str, ...]:
    escaped = json.dumps(secret, ensure_ascii=True)[1:-1]
    encoded = base64.b64encode(secret.encode("utf-8")).decode("ascii")
    percent_encoded = quote(secret, safe="")
    return tuple(dict.fromkeys((secret, escaped, encoded, percent_encoded)))


def _scan_value(value: object, fingerprints: Sequence[tuple[int, bytes]]) -> object:
    if isinstance(value, str):
        return _BLOCKED_SECRET if _contains_secret(value, fingerprints) else value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Mapping):
        copied: dict[str, object] = {}
        for key, nested in value.items():
            if not isinstance(key, str):
                return _BLOCKED_TYPE
            if key.casefold() in FORBIDDEN_FIELDS:
                return _BLOCKED_FIELD
            scanned = _scan_value(nested, fingerprints)
            if scanned is _BLOCKED_SECRET or scanned is _BLOCKED_FIELD or scanned is _BLOCKED_TYPE:
                return scanned
            copied[key] = scanned
        return copied
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        copied_items: list[object] = []
        for nested in value:
            scanned = _scan_value(nested, fingerprints)
            if scanned is _BLOCKED_SECRET or scanned is _BLOCKED_FIELD or scanned is _BLOCKED_TYPE:
                return scanned
            copied_items.append(scanned)
        return tuple(copied_items) if isinstance(value, tuple) else copied_items
    return _BLOCKED_TYPE


def _fingerprints(variants: Sequence[str]) -> tuple[tuple[int, bytes], ...]:
    return tuple(
        dict.fromkeys(
            (len(variant), hashlib.sha256(variant.encode("utf-8")).digest())
            for variant in variants
            if variant
        )
    )


def _contains_secret(value: str, fingerprints: Sequence[tuple[int, bytes]]) -> bool:
    for length, expected in fingerprints:
        if length > len(value):
            continue
        for start in range(len(value) - length + 1):
            candidate = value[start : start + length].encode("utf-8")
            if hashlib.sha256(candidate).digest() == expected:
                return True
    return False


__all__ = ["FORBIDDEN_FIELDS", "RedactionResult", "SecretRegistry"]

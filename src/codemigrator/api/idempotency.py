"""Small deterministic idempotency adapter for API write routes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256


@dataclass(frozen=True, slots=True)
class IdempotencyResult:
    status_code: int
    body: object
    conflict: bool = False


@dataclass(frozen=True, slots=True)
class _Entry:
    body_digest: str
    status_code: int
    body: object
    expires_at: datetime


class IdempotencyStore:
    """Enforce ``(principal, route, key)`` and 24-hour canonical replay."""

    ttl = timedelta(hours=24)

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._entries: dict[tuple[str, str, str], _Entry] = {}

    def lookup(
        self, principal_id: str, route: str, key: str, canonical_body: bytes
    ) -> IdempotencyResult | None:
        self._purge()
        entry = self._entries.get((principal_id, route, key))
        if entry is None:
            return None
        digest = _digest(canonical_body)
        if entry.body_digest != digest:
            return IdempotencyResult(409, {"code": "IDEMPOTENCY_CONFLICT"}, conflict=True)
        return IdempotencyResult(entry.status_code, entry.body)

    def remember(
        self,
        principal_id: str,
        route: str,
        key: str,
        canonical_body: bytes,
        status_code: int,
        body: object,
    ) -> None:
        self._purge()
        self._entries[(principal_id, route, key)] = _Entry(
            body_digest=_digest(canonical_body),
            status_code=status_code,
            body=body,
            expires_at=self._now() + self.ttl,
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("idempotency clock must return timezone-aware datetime")
        return value.astimezone(UTC)

    def _purge(self) -> None:
        now = self._now()
        self._entries = {
            key: entry
            for key, entry in self._entries.items()
            if entry.expires_at > now
        }


def _digest(canonical_body: bytes) -> str:
    return sha256(canonical_body).hexdigest()


__all__ = ["IdempotencyResult", "IdempotencyStore"]

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from codemigrator.api.idempotency import IdempotencyStore


def test_idempotency_replays_same_body_rejects_different_and_expires() -> None:
    now = datetime(2026, 8, 30, tzinfo=UTC)
    store = IdempotencyStore(clock=lambda: now)
    assert store.lookup("local", "/route", "key", b"one") is None
    store.remember("local", "/route", "key", b"one", 201, {"id": "1"})
    assert store.lookup("local", "/route", "key", b"one").body == {"id": "1"}
    assert store.lookup("local", "/route", "key", b"two").conflict is True
    now = now + timedelta(hours=24, seconds=1)
    assert store.lookup("local", "/route", "key", b"one") is None

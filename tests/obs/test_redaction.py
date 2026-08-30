from __future__ import annotations

import base64
import json
from urllib.parse import quote

import pytest

from codemigrator.core.secrets import FORBIDDEN_FIELDS, SecretRegistry


@pytest.mark.parametrize(
    "encoded",
    [
        "portable-secret",
        json.dumps("portable-secret", ensure_ascii=True)[1:-1],
        base64.b64encode(b"portable-secret").decode("ascii"),
        quote("portable-secret", safe=""),
    ],
)
def test_registered_secret_blocks_all_supported_encodings(encoded: str) -> None:
    registry = SecretRegistry()
    registry.register("portable-secret")

    result = registry.redact({"summary": encoded})

    assert result.accepted is False
    assert result.value is None
    assert result.reason == "secret_match"


def test_sensitive_structure_is_rejected_without_returning_the_payload() -> None:
    registry = SecretRegistry()

    result = registry.redact({"nested": {"content": "source body"}})

    assert result.accepted is False
    assert result.value is None
    assert result.reason == "sensitive_field"


def test_safe_payload_is_copied_and_secret_registry_is_write_only() -> None:
    registry = SecretRegistry()
    payload = {"summary": "safe", "items": ["one", {"status": "READY"}]}

    result = registry.redact(payload)

    assert result.accepted is True
    assert result.value == payload
    assert result.value is not payload
    assert "secrets" not in dir(registry)


def test_secret_registry_state_does_not_contain_registered_secret_text() -> None:
    registry = SecretRegistry()
    registry.register("write-only-secret")

    assert "write-only-secret" not in repr(vars(registry))
    assert "write-only-secret" not in repr(registry)


def test_forbidden_field_set_contains_the_designated_log_sensitive_keys() -> None:
    expected = {"content", "prompt", "authorization", "database_url", "private_key"}

    assert expected <= FORBIDDEN_FIELDS

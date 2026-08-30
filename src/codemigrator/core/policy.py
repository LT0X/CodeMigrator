"""Load immutable-by-convention, versioned core policy resources."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from importlib import resources
from typing import Any


_RESOURCE_PATHS = {
    "core://phase-tool-policy/v2": ("phase-tool-policy/v2.json", 2),
    "core://verification-policy/v1": ("verification-policy/v1.json", 1),
    "core://session-budget/v1": ("session-budget/v1.json", 1),
    "core://session-templates/v1": ("session-templates/v1.json", 1),
}


@dataclass(frozen=True)
class ResourceDocument:
    """A parsed resource plus the bytes used to derive its digest."""

    uri: str
    version: int
    sha256: str
    raw_bytes: bytes
    payload: dict[str, Any]


def load_resource(uri: str) -> ResourceDocument:
    """Read one of the built-in versioned resources by its stable URI."""

    try:
        resource_name, version = _RESOURCE_PATHS[uri]
    except KeyError as exc:
        raise ValueError(f"unsupported resource URI: {uri}") from exc

    package_root = resources.files("codemigrator.core.resources")
    resource_path = package_root.joinpath(*resource_name.split("/"))
    raw_bytes = resource_path.read_bytes()
    try:
        decoded = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON resource: {uri}") from exc
    if not isinstance(decoded, dict):
        raise ValueError(f"resource payload must be a JSON object: {uri}")
    return ResourceDocument(
        uri=uri,
        version=version,
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        raw_bytes=raw_bytes,
        payload=copy.deepcopy(decoded),
    )


def _payload(uri: str) -> dict[str, Any]:
    return copy.deepcopy(load_resource(uri).payload)


def load_phase_tool_policy() -> dict[str, list[str]]:
    return _payload("core://phase-tool-policy/v2")


def load_verification_policy() -> dict[str, Any]:
    return _payload("core://verification-policy/v1")


def load_session_budget() -> dict[str, dict[str, int]]:
    return _payload("core://session-budget/v1")


def load_session_templates() -> dict[str, str]:
    return _payload("core://session-templates/v1")

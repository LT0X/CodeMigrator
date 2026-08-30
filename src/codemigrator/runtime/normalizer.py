"""Strictly normalize provider output before it reaches the tool gateway."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from codemigrator.workspace.protocol import ActionProtocolError, parse_action_stream

from .provider import ProviderResponse


class NormalizationError(ValueError):
    """Structured model output cannot be admitted without guessing."""


@dataclass(frozen=True, slots=True)
class ModelAction:
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class NormalizedTurn:
    actions: tuple[ModelAction, ...]
    assistant_text: str
    declared_complete: bool


def normalize_response(response: ProviderResponse) -> NormalizedTurn:
    actions: list[ModelAction] = []
    completed = False
    if response.tool_calls:
        for call in response.tool_calls:
            payload = _parse_object(call.arguments)
            if "tool" not in payload:
                payload = {"tool": call.name, **payload}
            elif payload["tool"] != call.name:
                raise NormalizationError("provider tool name does not match action payload")
            actions.append(ModelAction(payload))
    elif "[cm:action]" in response.content or "[cm:/action]" in response.content:
        for payload in _parse_marked(response.content):
            if payload.get("completed") is True:
                completed = True
            elif isinstance(payload.get("tool"), str):
                actions.append(ModelAction(payload))
            else:
                raise NormalizationError("marked segment is not a tool action or completion")
    elif response.content.lstrip().startswith("{"):
        payload = _parse_object(response.content)
        if payload.get("completed") is True:
            completed = True
        elif isinstance(payload.get("tool"), str):
            actions.append(ModelAction(payload))
        else:
            raise NormalizationError("JSON response is not a tool action or completion")
    return NormalizedTurn(
        actions=tuple(actions),
        assistant_text=response.content,
        declared_complete=completed,
    )


def _parse_marked(content: str) -> tuple[dict[str, Any], ...]:
    try:
        return parse_action_stream(content)
    except ActionProtocolError as exc:
        raise NormalizationError("marked action stream is invalid") from exc


def _parse_object(content: str) -> dict[str, Any]:
    try:
        value = json.loads(
            content,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except (json.JSONDecodeError, NormalizationError) as exc:
        raise NormalizationError("structured response JSON is invalid") from exc
    if not isinstance(value, dict):
        raise NormalizationError("structured response must be a JSON object")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise NormalizationError("structured response contains duplicate keys")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> None:
    raise NormalizationError(f"non-finite JSON value is not allowed: {value}")


__all__ = [
    "ModelAction",
    "NormalizationError",
    "NormalizedTurn",
    "normalize_response",
]

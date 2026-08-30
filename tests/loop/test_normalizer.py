from __future__ import annotations

import pytest

from codemigrator.runtime.normalizer import NormalizationError, normalize_response
from codemigrator.runtime.provider import ProviderResponse, ProviderToolCall, TokenUsage


def _response(content: str, *, finish_reason: str = "stop") -> ProviderResponse:
    return ProviderResponse(
        content=content,
        tool_calls=(),
        finish_reason=finish_reason,
        usage=TokenUsage(input_tokens=1, output_tokens=1, cost_micros=0),
    )


def test_normalizer_preserves_marked_action_order() -> None:
    response = _response(
        "\n".join(
            (
                "[cm:action]",
                '{"tool":"ReadFile","path":"a.py"}',
                "[cm:/action]",
                "[cm:action]",
                '{"tool":"ReadFile","path":"b.py"}',
                "[cm:/action]",
            )
        ),
        finish_reason="tool_calls",
    )

    turn = normalize_response(response)

    assert [action.payload["path"] for action in turn.actions] == ["a.py", "b.py"]
    assert not turn.declared_complete


def test_normalizer_accepts_provider_tool_call_and_completion_json() -> None:
    tool_response = ProviderResponse(
        content="",
        tool_calls=(ProviderToolCall(name="ReadFile", arguments='{"path":"a.py"}'),),
        finish_reason="tool_calls",
        usage=TokenUsage(input_tokens=1, output_tokens=1, cost_micros=0),
    )
    complete_response = _response('{"completed": true}')

    assert normalize_response(tool_response).actions[0].payload["path"] == "a.py"
    assert normalize_response(tool_response).actions[0].payload["tool"] == "ReadFile"
    assert normalize_response(complete_response).declared_complete


def test_normalizer_keeps_free_text_out_of_action_channel() -> None:
    turn = normalize_response(_response("I need another observation."))

    assert turn.actions == ()
    assert turn.assistant_text == "I need another observation."
    assert not turn.declared_complete


def test_normalizer_rejects_invalid_structured_action_without_execution() -> None:
    response = _response("[cm:action]\n{\"tool\":\"ReadFile\"}\n")

    with pytest.raises(NormalizationError):
        normalize_response(response)


def test_normalizer_rejects_duplicate_json_keys() -> None:
    response = _response(
        '[cm:action]\n{"tool":"ReadFile","path":"a.py","path":"b.py"}\n[cm:/action]'
    )

    with pytest.raises(NormalizationError):
        normalize_response(response)

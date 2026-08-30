from __future__ import annotations

import pytest

from codemigrator.workspace import ActionProtocolError, parse_action_stream


def test_marked_protocol_supports_multiple_actions() -> None:
    actions = parse_action_stream(
        '[cm:action]\n{"tool":"ReadFile","path":"a.py"}\n[cm:/action]\n'
        '[cm:action]\n{"tool":"ReadFile","path":"b.py"}\n[cm:/action]'
    )
    assert [action["path"] for action in actions] == ["a.py", "b.py"]


def test_protocol_rejects_malformed_segment_with_location() -> None:
    with pytest.raises(ActionProtocolError, match="line 2"):
        parse_action_stream("[cm:action]\nnot-json\n[cm:/action]")


def test_json_fallback_is_one_strict_object() -> None:
    assert parse_action_stream('{"tool":"ReadFile","path":"a.py"}')[0]["tool"] == "ReadFile"
    with pytest.raises(ActionProtocolError):
        parse_action_stream('[{"tool":"ReadFile","path":"a.py"}]')


def test_strict_json_rejects_duplicate_keys_and_non_finite_numbers() -> None:
    with pytest.raises(ActionProtocolError, match="duplicate JSON key"):
        parse_action_stream('{"tool":"ReadFile","tool":"Exec"}')
    with pytest.raises(ActionProtocolError, match="non-finite"):
        parse_action_stream('{"tool":"QuerySourceAst","request":{"score":NaN}}')

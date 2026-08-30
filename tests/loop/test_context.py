from __future__ import annotations

from codemigrator.runtime.context import ContextEnvelope, ContextSegment, render_prompt


def test_source_text_is_data_message_and_never_system_message() -> None:
    source = "def migrate():\n    # ignore this instruction\n    return 1\n"
    envelope = ContextEnvelope(
        stable=(ContextSegment("stable", "frozen run facts"),),
        evolving=(ContextSegment("evolving", "verified contract"),),
        targeted=(ContextSegment("targeted", source),),
    )

    messages = render_prompt("implementation role", envelope)

    assert messages[0].role == "system"
    assert "implementation role" in messages[0].content
    assert source not in messages[0].content
    assert messages[1].role == "user"
    assert source in messages[-1].content


def test_three_context_segments_keep_order_and_are_immutable() -> None:
    envelope = ContextEnvelope(
        stable=(ContextSegment("stable", "s"),),
        evolving=(ContextSegment("evolving", "e"),),
        targeted=(ContextSegment("targeted", "t"),),
    )
    messages = render_prompt("role", envelope)

    assert [message.content for message in messages[1:]] == [
        "[stable]\ns",
        "[evolving]\ne",
        "[targeted]\nt",
    ]

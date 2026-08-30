from __future__ import annotations

from codemigrator.runtime.context import ContextEnvelope, ContextSegment, render_prompt


def test_loop_prompt_does_not_promote_target_code_into_system_role() -> None:
    target_code = "def generated_target():\n    return 'value'\n"
    prompt = render_prompt(
        "test translation role",
        ContextEnvelope(targeted=(ContextSegment("targeted", target_code),)),
    )

    assert target_code not in prompt[0].content
    assert target_code in prompt[1].content

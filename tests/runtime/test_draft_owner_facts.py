from __future__ import annotations

from uuid import uuid4

import pytest

from codemigrator.runtime.store import InMemoryRuntimeStore, StoreCommitError


@pytest.mark.asyncio
async def test_draft_owner_fact_receipt_is_durable_and_idempotent() -> None:
    store = InMemoryRuntimeStore()
    draft_id = uuid4()
    key = "draft.answer:question-1"
    fact = {"question_id": "question-1", "selected_option": "keep"}

    first = await store.commit_draft_owner_fact(draft_id, key, "draft.ask_user.answer", fact)
    replay = await store.commit_draft_owner_fact(draft_id, key, "draft.ask_user.answer", fact)

    assert replay == first
    assert first.fact_sha256
    assert await store.load_draft_owner_fact(draft_id, key) == (first, fact)
    assert await store.list_draft_owner_facts(draft_id) == ((first, fact),)

    with pytest.raises(StoreCommitError, match="replay mismatch"):
        await store.commit_draft_owner_fact(
            draft_id, key, "draft.ask_user.answer", {**fact, "selected_option": "merge"}
        )


@pytest.mark.asyncio
async def test_draft_owner_fact_commit_failure_does_not_publish_receipt() -> None:
    store = InMemoryRuntimeStore()
    draft_id = uuid4()
    store.fail_next_commit()

    with pytest.raises(StoreCommitError, match="injected"):
        await store.commit_draft_owner_fact(
            draft_id, "draft.closed", "draft.closed", {"closed": True}
        )

    assert await store.list_draft_owner_facts(draft_id) == ()

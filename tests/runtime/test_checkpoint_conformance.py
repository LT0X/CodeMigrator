"""Official LangGraph saver conformance for the pinned protocol version."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest
from langgraph.checkpoint.conformance import checkpointer_test, validate

from codemigrator.runtime.cas import FileHostCAS
from codemigrator.runtime.checkpointer import CasCheckpointSaver
from codemigrator.runtime.store import InMemoryRuntimeStore


@checkpointer_test(name="CodeMigrator CAS saver")
async def cas_saver_factory():
    with TemporaryDirectory() as directory:
        yield CasCheckpointSaver(
            FileHostCAS(Path(directory)),
            InMemoryRuntimeStore(),
            graph_family="run",
            owner_kind="run",
            owner_id=uuid4(),
        )


@pytest.mark.asyncio
async def test_pinned_langgraph_base_saver_conformance():
    report = await validate(
        cas_saver_factory,
        capabilities={"put", "put_writes", "get_tuple", "list", "delete_thread"},
    )
    failures = {
        name: result.failures for name, result in report.results.items() if result.tests_failed
    }
    assert not failures, failures

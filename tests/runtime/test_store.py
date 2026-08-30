from __future__ import annotations

from codemigrator.runtime.contracts import RunState
from codemigrator.runtime.schema import RUNTIME_SCHEMA_SQL
from codemigrator.runtime.store import _decode_state, _dump_json


def test_runtime_state_round_trips_through_json_for_durable_store():
    from .conftest import uid

    state = RunState(run_id=uid())
    assert _decode_state(_dump_json(state)) == state


def test_runtime_schema_contains_separate_run_and_append_only_event_tables():
    assert "CREATE TABLE IF NOT EXISTS runtime_runs" in RUNTIME_SCHEMA_SQL
    assert "CREATE TABLE IF NOT EXISTS runtime_events" in RUNTIME_SCHEMA_SQL
    assert "PRIMARY KEY (run_id, sequence)" in RUNTIME_SCHEMA_SQL

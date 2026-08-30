"""Run-side PostgreSQL schema owned by the runtime composition root."""

RUNTIME_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runtime_runs (
    run_id uuid PRIMARY KEY,
    state jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_events (
    run_id uuid NOT NULL REFERENCES runtime_runs(run_id),
    sequence bigint NOT NULL,
    event_type text NOT NULL,
    data jsonb NOT NULL,
    PRIMARY KEY (run_id, sequence)
);
"""


__all__ = ["RUNTIME_SCHEMA_SQL"]

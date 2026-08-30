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

CREATE TABLE IF NOT EXISTS context_evolution_segments (
    run_id uuid NOT NULL REFERENCES runtime_runs(run_id),
    entry_index bigint NOT NULL CHECK (entry_index >= 0),
    slice_id uuid NOT NULL,
    summary_text text NOT NULL,
    template_sha256 char(64) NOT NULL CHECK (template_sha256 ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id, entry_index)
);

CREATE OR REPLACE FUNCTION reject_context_evolution_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'context evolution segments are append-only';
END;
$$;

DROP TRIGGER IF EXISTS context_evolution_segments_immutable
    ON context_evolution_segments;
CREATE TRIGGER context_evolution_segments_immutable
    BEFORE UPDATE OR DELETE ON context_evolution_segments
    FOR EACH ROW EXECUTE FUNCTION reject_context_evolution_mutation();
"""


__all__ = ["RUNTIME_SCHEMA_SQL"]

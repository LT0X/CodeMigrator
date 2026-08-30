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
    PRIMARY KEY (run_id, entry_index),
    UNIQUE (run_id, slice_id)
);

CREATE OR REPLACE FUNCTION enforce_context_evolution_identity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    frozen_template char(64);
BEGIN
    SELECT template_sha256 INTO frozen_template
    FROM context_evolution_segments
    WHERE run_id = NEW.run_id
    ORDER BY entry_index
    LIMIT 1;
    IF frozen_template IS NOT NULL AND frozen_template <> NEW.template_sha256 THEN
        RAISE EXCEPTION 'context evolution template is frozen per Run';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS context_evolution_identity
    ON context_evolution_segments;
CREATE TRIGGER context_evolution_identity
    BEFORE INSERT ON context_evolution_segments
    FOR EACH ROW EXECUTE FUNCTION enforce_context_evolution_identity();

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

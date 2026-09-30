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
    timestamp_utc timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, sequence)
);

CREATE TABLE IF NOT EXISTS api_command_receipts (
    principal_id text NOT NULL CHECK (length(btrim(principal_id)) > 0),
    route text NOT NULL CHECK (length(btrim(route)) > 0),
    idempotency_key text NOT NULL CHECK (length(btrim(idempotency_key)) BETWEEN 1 AND 256),
    body_sha256 char(64) NOT NULL CHECK (body_sha256 ~ '^[0-9a-f]{64}$'),
    status_code integer NOT NULL CHECK (status_code BETWEEN 200 AND 599),
    response_body jsonb NOT NULL,
    owner_kind text,
    owner_id uuid,
    owner_receipt_key text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    PRIMARY KEY (principal_id, route, idempotency_key),
    CHECK (
        (owner_kind IS NULL AND owner_id IS NULL AND owner_receipt_key IS NULL)
        OR (owner_kind = 'run' AND owner_id IS NOT NULL AND owner_receipt_key IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS run_graph_start_handoffs (
    run_id uuid PRIMARY KEY REFERENCES runtime_runs(run_id),
    receipt_key text NOT NULL CHECK (length(btrim(receipt_key)) BETWEEN 1 AND 256),
    status text NOT NULL CHECK (status IN ('PENDING', 'STARTED')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    started_at timestamptz
);

ALTER TABLE runtime_events ADD COLUMN IF NOT EXISTS timestamp_utc timestamptz;
UPDATE runtime_events
SET timestamp_utc = TIMESTAMPTZ '1970-01-01 00:00:00+00'
WHERE timestamp_utc IS NULL;
ALTER TABLE runtime_events ALTER COLUMN timestamp_utc SET NOT NULL;
ALTER TABLE runtime_events ALTER COLUMN timestamp_utc SET DEFAULT clock_timestamp();

CREATE TABLE IF NOT EXISTS agent_runs (
    agent_run_id uuid PRIMARY KEY,
    owner_kind text NOT NULL CHECK (owner_kind IN ('run', 'draft')),
    owner_id uuid NOT NULL,
    logical_task_key text NOT NULL CHECK (length(btrim(logical_task_key)) > 0),
    thread_id uuid NOT NULL,
    phase text NOT NULL,
    session_kind text NOT NULL,
    model_binding_sha256 char(64) NOT NULL CHECK (model_binding_sha256 ~ '^[0-9a-f]{64}$'),
    context_sha256 char(64) NOT NULL CHECK (context_sha256 ~ '^[0-9a-f]{64}$'),
    toolset_sha256 char(64) NOT NULL CHECK (toolset_sha256 ~ '^[0-9a-f]{64}$'),
    template_sha256 char(64) NOT NULL CHECK (template_sha256 ~ '^[0-9a-f]{64}$'),
    state text NOT NULL,
    exit text,
    metadata jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (owner_kind, owner_id, logical_task_key),
    CONSTRAINT agent_runs_thread_id_unique UNIQUE (thread_id)
);

CREATE TABLE IF NOT EXISTS agent_run_receipts (
    receipt_id uuid PRIMARY KEY,
    agent_run_id uuid NOT NULL UNIQUE REFERENCES agent_runs(agent_run_id),
    category text NOT NULL CHECK (length(btrim(category)) > 0),
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS draft_owner_facts (
    draft_id uuid NOT NULL,
    receipt_key text NOT NULL CHECK (length(receipt_key) BETWEEN 1 AND 256),
    category text NOT NULL CHECK (length(category) BETWEEN 1 AND 64),
    fact_sha256 char(64) NOT NULL CHECK (fact_sha256 ~ '^[0-9a-f]{64}$'),
    fact jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (draft_id, receipt_key)
);

CREATE TABLE IF NOT EXISTS draft_session_events (
    draft_id uuid NOT NULL,
    receipt_key text NOT NULL,
    sequence bigint NOT NULL CHECK (sequence > 0),
    event_type text NOT NULL,
    data jsonb NOT NULL,
    timestamp_utc timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (draft_id, sequence)
);

ALTER TABLE draft_session_events ADD COLUMN IF NOT EXISTS receipt_key text;

CREATE TABLE IF NOT EXISTS cas_objects (
    digest char(64) PRIMARY KEY CHECK (digest ~ '^[0-9a-f]{64}$'),
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS cas_object_refs (
    owner_kind text NOT NULL,
    owner_id uuid NOT NULL,
    reference_key text NOT NULL,
    digest char(64) NOT NULL REFERENCES cas_objects(digest),
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (owner_kind, owner_id, reference_key)
);
CREATE INDEX IF NOT EXISTS cas_object_refs_digest_idx ON cas_object_refs(digest);

CREATE TABLE IF NOT EXISTS graph_threads (
    thread_id uuid PRIMARY KEY,
    graph_family text NOT NULL,
    owner_kind text NOT NULL,
    owner_id uuid NOT NULL
);

CREATE TABLE IF NOT EXISTS graph_checkpoints (
    thread_id uuid NOT NULL REFERENCES graph_threads(thread_id),
    checkpoint_ns text NOT NULL,
    checkpoint_id text NOT NULL,
    parent_checkpoint_id text,
    digest char(64) NOT NULL REFERENCES cas_objects(digest),
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);

CREATE TABLE IF NOT EXISTS graph_pending_writes (
    thread_id uuid NOT NULL REFERENCES graph_threads(thread_id),
    checkpoint_ns text NOT NULL,
    checkpoint_id text NOT NULL,
    task_id text NOT NULL,
    write_index integer NOT NULL,
    digest char(64) NOT NULL REFERENCES cas_objects(digest),
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, write_index)
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

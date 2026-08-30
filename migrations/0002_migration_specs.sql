-- CM-SPEC-001: immutable Spec v3 persistence shape; runtime owns execution.
CREATE TABLE migration_specs (
    spec_id UUID PRIMARY KEY,
    schema_name TEXT NOT NULL CHECK (schema_name = 'codemigrator.migration-spec'),
    version INTEGER NOT NULL CHECK (version = 3),
    name TEXT NOT NULL,
    raw_json JSONB NOT NULL,
    canonical_json JSONB NOT NULL,
    canonical_sha256 CHAR(64) NOT NULL,
    source_language_id TEXT NOT NULL,
    target_language_id TEXT NOT NULL,
    descriptor_lock JSONB NOT NULL,
    scope JSONB NOT NULL,
    canonical_checks JSONB NOT NULL,
    decomposition JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (source_language_id <> target_language_id),
    CONSTRAINT migration_specs_canonical_sha256_key UNIQUE (canonical_sha256)
);

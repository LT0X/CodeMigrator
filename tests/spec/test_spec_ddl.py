from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_migration_specs_ddl_preserves_canonical_insert_or_get_facts() -> None:
    ddl = (ROOT / "migrations/0002_migration_specs.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE migration_specs" in ddl
    for column in (
        "spec_id",
        "raw_json",
        "canonical_json",
        "canonical_sha256",
        "source_language_id",
        "target_language_id",
        "descriptor_lock",
        "scope",
        "canonical_checks",
        "decomposition",
        "CONSTRAINT migration_specs_canonical_sha256_key UNIQUE (canonical_sha256)",
    ):
        assert column in ddl
    assert "UPDATE migration_specs" not in ddl

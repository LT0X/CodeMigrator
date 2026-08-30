# CodeMigrator

CodeMigrator is a Python 3.12+ system for deterministic, evidence-driven
cross-language code migration. The repository is a single `src/` package with
eight bounded subpackages and a small app + PostgreSQL deployment baseline.

## Development

Install the locked environment with uv:

```bash
uv sync --dev
uv run pytest -q
uv run lint-imports
```

Start the local two-service baseline with a password supplied outside the
repository, for example `POSTGRES_PASSWORD=change-me docker compose up --build`.

The public application entry point is `codemigrator-app`. Language-specific
facts live in `descriptors/`; the Python package must not grow language plugins.
Design and task-alignment records remain in the private `my_space/` workspace.

## Repository boundaries

- `src/codemigrator/core/`: stable public contracts.
- `src/codemigrator/{analysis,planning,verification}/`: pure domain logic.
- `src/codemigrator/{workspace,sandbox}/`: controlled execution surfaces.
- `src/codemigrator/{runtime,api}/`: orchestration and external projection.
- `descriptors/`: declarative source/target toolchain facts.
- `migrations/`: versioned PostgreSQL SQL migrations.
- `deploy/`: app, target-toolchain, and sandbox deployment assets.

No credentials, design documents, plugins, sandbox worker, UDS protocol, or
generated build output belong in the repository.

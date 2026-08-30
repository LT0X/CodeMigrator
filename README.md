# CodeMigrator

CodeMigrator is a deterministic, evidence-driven system for cross-language
source-code migration. It combines static analysis, migration planning,
controlled workspace operations, isolated execution, verification, and
auditable run orchestration in a single Python application.

The system is designed for migrations that must be explainable and
reproducible. A migration is therefore treated as a sequence of frozen facts,
bounded proposals, controlled writes, trusted execution receipts, and
verification evidence rather than as an unrestricted code-generation task.

## Core principles

- **Deterministic decisions** — canonical contracts, stable identifiers,
  explicit state transitions, and reproducible validation rules define the
  control-plane behavior.
- **Evidence before advancement** — candidate and verified states advance only
  from accepted, attributable execution and verification evidence.
- **Explicit write boundaries** — every workspace write is constrained by the
  frozen plan and its write scopes.
- **Isolation by construction** — model/tool activity and checks are executed
  through controlled workspace and sandbox boundaries.
- **Auditable orchestration** — run events, receipts, diagnostics, fingerprints,
  and integration facts provide a replayable history of material decisions.
- **Declarative toolchains** — source and target language facts belong in
  `descriptors/`, not in language-specific branches inside the Python package.

## Architecture

The application uses a Python `src/` layout with eight bounded subpackages.
The dependency direction points toward `core`; the runtime is the composition
root and is the only layer that assembles side-effecting adapters.

| Package | Responsibility |
| --- | --- |
| `codemigrator.core` | Stable IDs, enums, error codes, Pydantic contracts, canonicalization, paths, scopes, and versioned policy resources. |
| `codemigrator.analysis` | Read-only tree-sitter analysis, module/dependency/test-coverage graphs, and analysis projections. |
| `codemigrator.planning` | Deterministic validation and freezing of migration proposals into bounded plans, slices, and dependency edges. |
| `codemigrator.workspace` | Candidate workspaces, checkpoints, safe paths, write-scope enforcement, tool gateway, and controlled file/Git surfaces. |
| `codemigrator.sandbox` | In-process bubblewrap/cgroup integration, long-lived volumes, verification materialization, and resource limits. |
| `codemigrator.verification` | Layered check selection, execution-fact reduction, diagnostics, attribution evidence, fingerprints, and verification guards. |
| `codemigrator.runtime` | Run actors, transactional orchestration, scheduling, integration, recovery, lifecycle management, and application composition. |
| `codemigrator.api` | REST/SSE projections, idempotent commands, `If-Match` handling, and external error mapping. |

The runtime keeps the control plane single-writer per Run. Expensive model,
tool, Git, sandbox, and check operations occur behind explicit ports and return
typed receipts to the actor. The API exposes projections and commands; it does
not independently decide domain state.

## Migration lifecycle

At a high level, a migration follows this controlled sequence:

1. Register the source and target toolchains from declarative descriptors.
2. Freeze the migration specification and its required checks.
3. Analyze the source repository and construct deterministic dependency and
   coverage facts.
4. Validate and freeze a bounded migration plan with DAG dependencies and
   write scopes.
5. Create candidate workspaces and execute migration work through the approved
   tool and sandbox boundaries.
6. Run local, integration, and final verification checks, preserving raw
   receipts and derived evidence.
7. Attribute failures, regenerate or repair candidates when permitted, and
   integrate verified changes through controlled Git operations.
8. Produce a deterministic report from verified facts and recorded evidence.

The exact transitions, failure semantics, idempotency rules, and event
contracts are owned by the package contracts and the corresponding design
specifications.

## Repository layout

```text
.
├── apps/codemigrator-cli/       Application entry-point assets
├── descriptors/                Declarative source/target toolchain facts
├── deploy/                     Container and sandbox deployment assets
├── migrations/                 Versioned PostgreSQL schema migrations
├── src/codemigrator/           Python application package
├── test_fixtures/              Deterministic analysis and contract fixtures
├── tests/                      Unit, contract, integration-boundary tests
├── compose.yaml                Local app and PostgreSQL services
├── pyproject.toml              Build, dependency, test, lint, and type config
└── uv.lock                     Locked Python dependency resolution
```

Internal design material, alignment records, progress notes, temporary
staging files, local credentials, and agent instructions are deliberately
maintained outside the public source tree. They are local-only workspace data
and must not be committed or force-added.

## Requirements

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/) for dependency and environment management
- Docker with Compose support for the local PostgreSQL-backed application
- Linux with bubblewrap and cgroup v2 when running sandbox-backed workflows

The package metadata contains the complete runtime and development dependency
sets. The locked environment should be used for reproducible local checks.

## Installation

From the repository root:

```bash
uv sync --dev
```

Run the package import smoke check:

```bash
uv run python -c "import codemigrator"
```

The application console entry point is:

```bash
uv run codemigrator-app
```

Runtime configuration and infrastructure credentials must be supplied by the
local environment. Do not place credentials in source files, descriptors,
Compose manifests, or documentation.

## Local services

The repository includes a two-service Compose baseline:

- `app` — the CodeMigrator application container with the sandbox-related
  permissions and delegated cgroup mount required by the deployment model.
- `postgres` — PostgreSQL 17 for the control-plane ledger and event history.

Supply values from the shell or a local environment file that is excluded from
version control:

```bash
POSTGRES_PASSWORD='change-me' \
CODEMIGRATOR_CGROUP_DELEGATED_DIR='/path/to/delegated/cgroup' \
docker compose up --build
```

`POSTGRES_PASSWORD` is required by the database service. The delegated cgroup
directory is required by the app service when sandbox lifecycle management is
enabled. Review `compose.yaml` and the deployment assets before granting
additional host capabilities.

## Verification and quality checks

The preferred deterministic checks are:

```bash
uv run pytest -q
uv run ruff check src tests
uv run mypy src
uv run lint-imports
uv run python -m compileall -q src
```

For a focused package test, select the relevant directory, for example:

```bash
uv run pytest -q tests/core
uv run pytest -q tests/verification
```

Tests should prefer deterministic fakes, contract fixtures, and boundary
assertions. Real model calls are reserved for behavior that cannot be
established by rules or local test doubles, such as provider-specific usage
receipts or a complete model session.

## Toolchain descriptors

`descriptors/` is the declarative source of language and toolchain facts. A
descriptor may define grammar and toolchain metadata, supported migration
capabilities, and required checks. It must not contain credentials, executable
application logic, prompts, arbitrary command injection, or write scopes. The
Python package consumes descriptor facts through validated contracts.

## Security boundaries

CodeMigrator treats source repositories, generated files, model output, tool
output, and verification results as distinct trust boundaries. In particular:

- untrusted model output is validated before it can become a plan or workspace
  action;
- file operations are restricted to safe repository-relative paths and frozen
  write scopes;
- sandboxed execution does not receive PostgreSQL, host Git, or control-plane
  network access;
- raw logs and large bodies are stored through controlled artifact references;
- credentials and private operational material are excluded from version
  control.

Changes that alter a contract, write boundary, sandbox permission, migration
schema, or event meaning require corresponding tests and documentation updates.

## Development guidance

Keep domain logic deterministic and side-effect free whenever possible. Put
environment reads and adapter construction in the runtime composition root.
Reuse public contracts from `codemigrator.core` instead of defining parallel
enums or error vocabularies. Add tests for success paths, boundary conditions,
failure paths, idempotency, and security invariants together with each change.

Before submitting a change, run the complete quality checks above and inspect
the resulting diff for credentials, private workspace material, generated
artifacts, and accidental dependency-boundary violations.

## License

No license is granted by this repository unless a license file is added and
explicitly states otherwise.

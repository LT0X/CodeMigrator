import hashlib
import json
import re
import tomllib
from pathlib import Path

from codemigrator.core.models.descriptor import (
    SourceToolchain,
    TargetToolchain,
    TreeSitterGrammarRef,
)

ROOT = Path(__file__).parents[2]
EXPECTED_SUBPACKAGES = {
    "core",
    "analysis",
    "planning",
    "workspace",
    "verification",
    "sandbox",
    "runtime",
    "api",
}


def test_exact_eight_subpackages_and_readme_boundaries() -> None:
    package_root = ROOT / "src" / "codemigrator"
    actual = {
        path.name
        for path in package_root.iterdir()
        if path.is_dir() and (path / "__init__.py").is_file()
    }
    assert actual == EXPECTED_SUBPACKAGES
    required_sections = ("负责", "不负责", "允许依赖", "公共入口")
    for package in sorted(EXPECTED_SUBPACKAGES):
        readme = package_root / package / "README.md"
        assert readme.is_file()
        content = readme.read_text(encoding="utf-8")
        assert all(section in content for section in required_sections)


def test_only_codemigrator_app_console_script_exists() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = project["project"]["scripts"]
    assert set(scripts) == {"codemigrator-app"}
    assert scripts["codemigrator-app"] == "codemigrator.runtime:main"


def test_python_baseline_and_required_dependencies_are_declared() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["requires-python"] == ">=3.12"
    dependencies = "\n".join(project["project"]["dependencies"])
    for package in (
        "pydantic",
        "uuid-utils",
        "rfc8785",
        "semver",
        "fastapi",
        "sqlalchemy",
        "asyncpg",
        "tree-sitter",
        "structlog",
        "opentelemetry-api",
        "opentelemetry-sdk",
        "httpx",
    ):
        assert package in dependencies


def test_go_grammar_digest_matches_descriptor() -> None:
    descriptor = json.loads(
        (ROOT / "descriptors/source/go/descriptor.json").read_text(encoding="utf-8")
    )
    grammar_path = ROOT / "descriptors/source/go" / descriptor["parser"]["grammar_path"]
    digest_path = ROOT / "descriptors/source/go/grammar/grammar.sha256"
    expected = descriptor["parser"]["grammar_sha256"]
    assert grammar_path.is_file()
    assert digest_path.read_text(encoding="utf-8").strip() == expected
    assert hashlib.sha256(grammar_path.read_bytes()).hexdigest() == expected


def test_descriptor_pair_is_go_to_python_only() -> None:
    source_languages = {
        path.name for path in (ROOT / "descriptors/source").iterdir() if path.is_dir()
    }
    target_languages = {
        path.name for path in (ROOT / "descriptors/target").iterdir() if path.is_dir()
    }
    assert source_languages == {"go"}
    assert target_languages == {"python"}
    assert (
        json.loads((ROOT / "descriptors/source/go/descriptor.json").read_text())["language_role"]
        == "source"
    )
    assert (
        json.loads((ROOT / "descriptors/target/python/descriptor.json").read_text())[
            "language_role"
        ]
        == "target"
    )


def test_descriptor_payloads_match_core_toolchain_contracts() -> None:
    source = json.loads(
        (ROOT / "descriptors/source/go/descriptor.json").read_text(encoding="utf-8")
    )
    target = json.loads(
        (ROOT / "descriptors/target/python/descriptor.json").read_text(encoding="utf-8")
    )
    source_payload = {key: source[key] for key in SourceToolchain.model_fields if key in source}
    source_payload["parser"] = {
        key: source["parser"][key] for key in TreeSitterGrammarRef.model_fields
    }
    SourceToolchain.model_validate(source_payload)
    assert source["parser"]["grammar_carrier"] == "shared-library"
    assert source["parser"]["grammar_path"] == "grammar/tree-sitter-go.so"
    TargetToolchain.model_validate(
        {key: target[key] for key in TargetToolchain.model_fields if key in target}
    )
    assert target["allowed_domains"] == ["files.pythonhosted.org", "pypi.org"]
    assert all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", domain)
        for domain in target["allowed_domains"]
    )
    assert all(
        isinstance(rule, dict)
        and isinstance(rule.get("pattern"), str)
        and isinstance(rule.get("artifact_kind"), str)
        for rule in target["artifact_rules"]
    )


def test_target_toolchain_digest_matches_build_manifest() -> None:
    descriptor = json.loads(
        (ROOT / "descriptors/target/python/descriptor.json").read_text(encoding="utf-8")
    )
    manifest = json.loads(
        (ROOT / "deploy/image-build-manifest.json").read_text(encoding="utf-8")
    )
    digest = descriptor["toolchain_image_digest"]
    assert digest == manifest["target_python"]["digest"]
    assert digest.startswith("sha256:")
    assert len(digest) == len("sha256:") + 64


def test_retired_plugin_and_rpc_shapes_are_absent() -> None:
    assert not (ROOT / "plugins").exists()
    assert not (ROOT / "sandbox-worker").exists()
    project_text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "sandbox-worker" not in project_text
    assert "protobuf" not in project_text
    assert "SOCK_SEQPACKET" not in project_text


def test_product_entry_directories_are_not_core_subpackages() -> None:
    package_names = {
        path.name
        for path in (ROOT / "src/codemigrator").iterdir()
        if path.is_dir() and (path / "__init__.py").is_file()
    }
    assert "apps" not in package_names
    assert "web" not in package_names
    assert (ROOT / "apps/codemigrator-cli").is_dir()
    assert (ROOT / "web").is_dir()
    assert not (ROOT / "docs").exists()


def test_migration_and_frozen_test_directories_exist() -> None:
    migration_files = sorted((ROOT / "migrations").glob("*.sql"))
    assert [path.name for path in migration_files] == ["0001_schema_migrations.sql"]
    assert "CREATE TABLE" in migration_files[0].read_text(encoding="utf-8")
    for directory in (
        "contracts",
        "recovery",
        "security",
        "infra",
        "core",
        "analysis",
        "planning",
        "workspace",
        "verification",
        "sandbox",
        "runtime",
        "api",
    ):
        assert (ROOT / "tests" / directory).is_dir()


def test_import_contracts_cover_the_frozen_dependency_layers() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    contracts = project["tool"]["importlinter"]["contracts"]
    names = {contract["name"] for contract in contracts}
    assert names == {"frozen layers", "forbidden product imports", "independent domain packages"}
    layer_contract = next(contract for contract in contracts if contract["name"] == "frozen layers")
    assert layer_contract["layers"] == [
        "codemigrator.core",
        "codemigrator.analysis | codemigrator.planning | codemigrator.verification",
        "codemigrator.sandbox | codemigrator.workspace",
        "codemigrator.api",
        "codemigrator.runtime",
    ]


def test_ci_runs_locked_quality_gates() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    for command in (
        "uv sync --frozen",
        "pytest",
        "lint-imports",
        "ruff check",
        "mypy",
        "os.environ",
    ):
        assert command in workflow


def test_deploy_files_contain_no_credentials_or_host_sockets() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert re.search(r"postgres:\s*\n\s*image:\s*postgres:17", compose)
    assert set(re.findall(r"^\s{2}([a-z][a-z0-9_-]*):\s*$", compose, re.MULTILINE)) >= {
        "app",
        "postgres",
    }
    for forbidden in ("docker.sock", "/var/run/docker", "uds", "password:", "PRIVATE_KEY"):
        assert forbidden not in compose
    assert "env_file:" not in compose
    assert "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?" in compose
    assert "my_space/.env" not in compose
    assert "seccomp=unconfined" in compose
    assert "SYS_ADMIN" in compose
    assert "CODEMIGRATOR_CGROUP_DELEGATED_DIR:?" in compose
    assert "target: /sys/fs/cgroup" in compose
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert "my_space" in dockerignore
    assert "tests" in dockerignore
    assert ".env" in dockerignore
    assert "*.env" in dockerignore
    dockerfile = (ROOT / "deploy/Dockerfile").read_text(encoding="utf-8")
    assert "python:3.12-slim" in dockerfile
    assert "COPY pyproject.toml uv.lock README.md ./" in dockerfile
    assert "COPY descriptors ./descriptors" in dockerfile
    assert "COPY migrations ./migrations" in dockerfile
    assert (
        'test: ["CMD", "uv", "run", "--no-dev", "python", "-c", "import codemigrator"]'
        in compose
    )
    target_dockerfile = (ROOT / "deploy/images/target-python/Dockerfile").read_text(
        encoding="utf-8"
    )
    for tool in ("uv", "pytest", "ruff", "mypy"):
        assert tool in target_dockerfile
    assert '"pytest==${PYTEST_VERSION}"' in target_dockerfile
    assert '"ruff==${RUFF_VERSION}"' in target_dockerfile
    assert '"mypy==${MYPY_VERSION}"' in target_dockerfile
    assert "ARG PYTEST_VERSION=8.4.2" in target_dockerfile
    assert "ARG RUFF_VERSION=0.16.5" in target_dockerfile
    assert "ARG MYPY_VERSION=1.20.2" in target_dockerfile

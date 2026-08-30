import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_core_has_no_internal_package_dependency_in_pyproject_contract() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    layer_contract = next(
        contract
        for contract in project["tool"]["importlinter"]["contracts"]
        if contract["name"] == "frozen layers"
    )
    assert layer_contract["layers"][0] == "codemigrator.core"


def test_runtime_is_the_only_declared_application_entrypoint() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["scripts"] == {"codemigrator-app": "codemigrator.runtime:main"}

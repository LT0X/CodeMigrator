from __future__ import annotations

import pytest

from codemigrator.core import SessionKind
from codemigrator.runtime import (
    AgentLoop,
    LockedModelBinding,
    ProviderRegistry,
    StaticTemplateCatalog,
)
from codemigrator.runtime.templates import CatalogError


def test_core_template_catalog_has_nine_session_kinds_and_drafting() -> None:
    catalog = StaticTemplateCatalog.from_core()

    assert set(catalog.slots) == {item.value for item in SessionKind} | {"DRAFTING"}
    assert catalog.template("DRAFTING")
    assert len(catalog.manifest_sha256) == 64


def test_loop_public_facade_exports_new_execution_contracts() -> None:
    assert AgentLoop is not None
    assert LockedModelBinding is not None
    assert ProviderRegistry is not None
    assert StaticTemplateCatalog is not None


@pytest.mark.parametrize(
    "templates",
    [
        {"IMPLEMENTATION": "only one"},
        {**{item.value: "role" for item in SessionKind}, "DRAFTING": "role", "EXTRA": "bad"},
        {item.value: "role" for item in SessionKind} | {"DRAFTING": ""},
    ],
)
def test_template_catalog_rejects_missing_extra_or_empty_slot(templates: dict[str, str]) -> None:
    with pytest.raises(CatalogError):
        StaticTemplateCatalog(templates)

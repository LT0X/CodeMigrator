"""Static, versioned session-template catalog."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from codemigrator.core import SessionKind, load_resource, load_session_templates


class CatalogError(ValueError):
    """The trusted session-template manifest is not exactly the expected shape."""


@dataclass(frozen=True, slots=True, init=False)
class StaticTemplateCatalog:
    _templates: Mapping[str, str]
    manifest_sha256: str

    def __init__(self, templates: Mapping[str, str], manifest_sha256: str = "") -> None:
        expected = {item.value for item in SessionKind} | {"DRAFTING"}
        if set(templates) != expected:
            raise CatalogError("session template catalog must contain exactly ten slots")
        if any(not isinstance(value, str) or not value for value in templates.values()):
            raise CatalogError("session template slots must be non-empty text")
        object.__setattr__(self, "_templates", MappingProxyType(dict(templates)))
        object.__setattr__(self, "manifest_sha256", manifest_sha256)

    @classmethod
    def from_core(cls) -> StaticTemplateCatalog:
        resource = load_resource("core://session-templates/v1")
        return cls(load_session_templates(), resource.sha256)

    @property
    def slots(self) -> tuple[str, ...]:
        return tuple(self._templates)

    def template(self, slot: str) -> str:
        try:
            return self._templates[slot]
        except KeyError as exc:
            raise CatalogError("unknown session template slot") from exc


__all__ = ["CatalogError", "StaticTemplateCatalog"]

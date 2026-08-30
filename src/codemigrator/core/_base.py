"""Shared Pydantic configuration for core models."""

from pydantic import BaseModel, ConfigDict


class CoreModel(BaseModel):
    """Base model enforcing closed public contracts."""

    model_config = ConfigDict(
        extra="forbid",
        arbitrary_types_allowed=True,
        protected_namespaces=(),
    )

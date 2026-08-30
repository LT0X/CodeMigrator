"""Small, deterministic observation client for CodeMigrator runs."""

from .models import Projection, RunEvent, SliceProjection
from .projector import project_events

__all__ = ["Projection", "RunEvent", "SliceProjection", "project_events"]

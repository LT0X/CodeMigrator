from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Protocol

from .models import RunEvent


class EventSource(Protocol):
    def events(self) -> Iterable[RunEvent]: ...


class MockEventSource:
    """Deterministic Wave 1 source; the REST/SSE adapter can replace this boundary."""

    def events(self) -> Iterator[RunEvent]:
        from .mock import mock_events

        yield from mock_events()

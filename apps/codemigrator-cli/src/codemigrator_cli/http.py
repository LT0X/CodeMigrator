from __future__ import annotations

import json
from collections.abc import Iterator
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen

from .models import RunEvent


class HttpEventSource:
    """REST/SSE transport; projection and rendering stay independent of HTTP."""

    def __init__(self, base_url: str, run_id: str, *, after_sequence: int = 0) -> None:
        if not base_url.strip() or not run_id.strip():
            raise ValueError("base_url and run_id must not be empty")
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        self.base_url = base_url.rstrip("/") + "/"
        self.run_id = run_id
        self.after_sequence = after_sequence

    def events(self) -> Iterator[RunEvent]:
        url = urljoin(
            self.base_url,
            f"migrations/{quote(self.run_id, safe='')}/events",
        )
        request = Request(
            url,
            headers={
                "Accept": "text/event-stream",
                "Last-Event-ID": str(self.after_sequence),
            },
        )
        with urlopen(request, timeout=30) as response:  # noqa: S310 - endpoint is deployment configuration
            data_lines: list[str] = []
            for raw_line in response:
                line = raw_line.decode("utf-8").rstrip("\r\n")
                if not line:
                    event = _event_from_lines(data_lines)
                    if event is not None:
                        self.after_sequence = event.sequence
                        yield event
                    data_lines = []
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            event = _event_from_lines(data_lines)
            if event is not None:
                self.after_sequence = event.sequence
                yield event


def _event_from_lines(lines: list[str]) -> RunEvent | None:
    if not lines:
        return None
    payload = json.loads("\n".join(lines))
    if not isinstance(payload, dict):
        raise ValueError("SSE event must be an object")
    sequence = payload.get("sequence")
    event_type = payload.get("type")
    event_data = payload.get("data")
    if (
        type(sequence) is not int
        or sequence < 1
        or not isinstance(event_type, str)
        or not isinstance(event_data, dict)
    ):
        raise ValueError("SSE event envelope is invalid")
    timestamp = payload.get("timestamp_utc", "")
    if not isinstance(timestamp, str):
        timestamp = ""
    return RunEvent(sequence, event_type, event_data, timestamp)

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from typing import Protocol
from urllib.error import HTTPError
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen

from .models import RunEvent


class EventSource(Protocol):
    def events(self) -> Iterable[RunEvent]: ...


class RunControl(Protocol):
    def show(self, run_id: str) -> dict[str, object]: ...

    def cancel(self, run_id: str, expected_version: int) -> dict[str, object]: ...


class StaleVersionError(RuntimeError):
    """The server rejected an If-Match version."""


class MockEventSource:
    """Deterministic local source used when no API endpoint is configured."""

    def events(self) -> Iterator[RunEvent]:
        from .mock import mock_events

        yield from mock_events()


class MockRunControl:
    def show(self, run_id: str) -> dict[str, object]:
        return {"run_id": run_id, "status": "COMPLETED", "version": 14}

    def cancel(self, run_id: str, expected_version: int) -> dict[str, object]:
        return {"run_id": run_id, "status": "CANCELLED", "version": expected_version + 1}


class HttpRunControl:
    """Authenticated API command/query boundary for show and If-Match cancel."""

    def __init__(self, base_url: str, *, token: str) -> None:
        if not base_url.strip() or not token:
            raise ValueError("base_url and token are required")
        self.base_url = base_url.rstrip("/") + "/"
        self.token = token

    def show(self, run_id: str) -> dict[str, object]:
        return self._request("GET", f"migrations/{quote(run_id, safe='')}")

    def cancel(self, run_id: str, expected_version: int) -> dict[str, object]:
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("expected_version must be non-negative")
        return self._request(
            "DELETE",
            f"migrations/{quote(run_id, safe='')}",
            headers={"If-Match": f'"{expected_version}"'},
            stale_version_on_conflict=True,
        )

    def create(
        self, payload: Mapping[str, object], idempotency_key: str
    ) -> dict[str, object]:
        if not isinstance(payload, Mapping):
            raise ValueError("CreateRun request must be an object")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key must not be empty")
        body = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return self._request(
            "POST",
            "migrations",
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": idempotency_key,
            },
            body=body,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        stale_version_on_conflict: bool = False,
    ) -> dict[str, object]:
        request_headers = {"Accept": "application/json", "Authorization": f"Bearer {self.token}"}
        request_headers.update(headers or {})
        request = Request(
            urljoin(self.base_url, path),
            data=body,
            method=method,
            headers=request_headers,
        )
        try:
            with urlopen(request, timeout=30) as response:  # noqa: S310 - deployment URL is explicit
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if stale_version_on_conflict and exc.code in {409, 412}:
                raise StaleVersionError("server rejected If-Match version") from exc
            raise RuntimeError(f"API request failed: {exc.code}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("API response must be an object")
        return payload

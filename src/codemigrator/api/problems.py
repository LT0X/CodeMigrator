"""RFC 9457 errors with a deliberately small safe extension surface."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from fastapi.responses import JSONResponse


@dataclass(frozen=True, slots=True)
class ApiError(Exception):
    status_code: int
    detail: str
    code: str
    run_id: UUID | None = None
    retryable: bool = False


def problem_response(error: ApiError, request_id: str) -> JSONResponse:
    body: dict[str, object] = {
        "type": f"https://codemigrator.dev/problems/{error.code.lower()}",
        "title": error.code.replace("_", " ").title(),
        "status": error.status_code,
        "detail": error.detail,
        "request_id": request_id,
        "retryable": error.retryable,
    }
    if error.run_id is not None:
        body["run_id"] = str(error.run_id)
    return JSONResponse(
        status_code=error.status_code,
        content=body,
        media_type="application/problem+json",
    )


__all__ = ["ApiError", "problem_response"]

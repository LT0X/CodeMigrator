from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    COMPLETED = 0
    PARTIALLY_COMPLETED = 2
    FAILED = 3
    CANCELLED = 4
    UNKNOWN = 5
    LOCAL_CANCEL_CONFIRMED = 130

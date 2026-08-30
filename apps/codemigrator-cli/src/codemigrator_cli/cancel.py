from __future__ import annotations

from enum import Enum


class CancelAction(str, Enum):
    REQUEST = "request"
    EXIT = "exit"
    WAIT = "wait"


class CancelController:
    """Models the two-interrupt contract without terminating a worker locally."""

    def __init__(self) -> None:
        self._interrupts = 0
        self.confirmed = False

    def interrupt(self) -> CancelAction:
        self._interrupts += 1
        return CancelAction.REQUEST if self._interrupts == 1 else CancelAction.EXIT

    def observe(self, status: str) -> CancelAction:
        if status == "CANCELLED":
            self.confirmed = True
            return CancelAction.WAIT
        return CancelAction.WAIT

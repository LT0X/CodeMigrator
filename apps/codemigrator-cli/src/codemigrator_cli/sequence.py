from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class SequenceCursor:
    cursor: int = 0
    connection: str = "disconnected"

    def accept(self, sequence: int) -> str:
        if type(sequence) is not int or sequence < 1:
            raise ValueError("event sequence must be a positive integer")
        if sequence <= self.cursor:
            return "duplicate"
        if sequence != self.cursor + 1:
            self.connection = "catching-up"
            return "gap"
        self.cursor = sequence
        if self.connection in {"disconnected", "connecting", "catching-up"}:
            self.connection = "connected"
        return "accepted"

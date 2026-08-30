"""Deterministic completeness-audit records and three-round state machine."""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import Enum


class AuditState(str, Enum):
    ReviseRules = "REVISE_RULES"
    Clean = "CLEAN"
    Escalate = "ESCALATE"


@dataclass(frozen=True)
class AuditSample:
    random_files: tuple[str, ...]
    adversarial_files: tuple[str, ...]


@dataclass(frozen=True)
class AuditFinding:
    description: str


@dataclass(frozen=True)
class AuditDiff:
    findings: tuple[AuditFinding, ...]


@dataclass(frozen=True)
class AuditRound:
    diff: AuditDiff


@dataclass(frozen=True)
class AuditRecord:
    sample: AuditSample
    rounds: tuple[AuditRound, ...]
    state: AuditState


class CompletenessAuditor:
    """Own only the deterministic audit framework, not review-session orchestration."""

    def __init__(self, *, seed: int) -> None:
        self._random = random.Random(seed)
        self._rounds: list[AuditRound] = []

    def sample(
        self,
        *,
        files: list[str],
        random_count: int,
        adversarial: list[str],
    ) -> AuditSample:
        adversarial_files = tuple(sorted(set(adversarial)))
        candidates = sorted(set(files) - set(adversarial_files))
        count = min(max(random_count, 0), len(candidates))
        random_files = tuple(sorted(self._random.sample(candidates, count)))
        return AuditSample(random_files=random_files, adversarial_files=adversarial_files)

    def record_round(self, round_: AuditRound) -> AuditState:
        self._rounds.append(round_)
        if not round_.diff.findings:
            return AuditState.Clean
        if len(self._rounds) >= 3:
            return AuditState.Escalate
        return AuditState.ReviseRules

    @property
    def rounds(self) -> tuple[AuditRound, ...]:
        return tuple(self._rounds)


__all__ = [
    "AuditDiff",
    "AuditFinding",
    "AuditRecord",
    "AuditRound",
    "AuditSample",
    "AuditState",
    "CompletenessAuditor",
]

"""Run-wallet accounting and deterministic budget decisions."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .provider import TokenUsage


def _non_negative(value: int, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    input_tokens: int
    output_tokens: int
    cost_micros: int

    def __post_init__(self) -> None:
        for name in ("input_tokens", "output_tokens", "cost_micros"):
            value = _non_negative(getattr(self, name), name)
            if value == 0:
                raise ValueError(f"{name} must be greater than zero")


@dataclass(frozen=True, slots=True)
class BudgetUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_micros: int = 0

    def __post_init__(self) -> None:
        for name in ("input_tokens", "output_tokens", "cost_micros"):
            _non_negative(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class BudgetEvaluation:
    usage: BudgetUsage
    utilization: float
    warning: bool
    exhausted: bool


class RunWallet:
    """An actor-facing, idempotent wallet for provider usage receipts."""

    def __init__(self, limits: BudgetLimits) -> None:
        self.limits = limits
        self._usage = BudgetUsage()
        self._receipt_ids: set[str] = set()
        self._lock = asyncio.Lock()

    @property
    def usage(self) -> BudgetUsage:
        return self._usage

    async def admit(self, _identity: object) -> bool:
        async with self._lock:
            return not self._is_exhausted(self._usage)

    async def record(self, receipt: object) -> bool:
        receipt_id = getattr(receipt, "receipt_id", None)
        usage = getattr(receipt, "usage", None)
        if not isinstance(receipt_id, str) or not receipt_id or not isinstance(usage, TokenUsage):
            raise TypeError("wallet requires a typed usage receipt")
        async with self._lock:
            if receipt_id in self._receipt_ids:
                return not self._is_exhausted(self._usage)
            evaluation = evaluate_budget(
                self._usage,
                self.limits,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost_micros=usage.cost_micros,
            )
            self._receipt_ids.add(receipt_id)
            self._usage = evaluation.usage
            return not evaluation.exhausted

    def _is_exhausted(self, usage: BudgetUsage) -> bool:
        return (
            max(
                usage.input_tokens / self.limits.input_tokens,
                usage.output_tokens / self.limits.output_tokens,
                usage.cost_micros / self.limits.cost_micros,
            )
            >= 1.0
        )


def evaluate_budget(
    previous: BudgetUsage,
    limits: BudgetLimits,
    *,
    input_tokens: int,
    output_tokens: int,
    cost_micros: int,
    warning_already_emitted: bool = False,
) -> BudgetEvaluation:
    """Accumulate usage and evaluate the 80% warning/100% breaker thresholds."""

    additions = {
        "input_tokens": _non_negative(input_tokens, "input_tokens"),
        "output_tokens": _non_negative(output_tokens, "output_tokens"),
        "cost_micros": _non_negative(cost_micros, "cost_micros"),
    }
    usage = BudgetUsage(
        input_tokens=previous.input_tokens + additions["input_tokens"],
        output_tokens=previous.output_tokens + additions["output_tokens"],
        cost_micros=previous.cost_micros + additions["cost_micros"],
    )
    utilization = max(
        usage.input_tokens / limits.input_tokens,
        usage.output_tokens / limits.output_tokens,
        usage.cost_micros / limits.cost_micros,
    )
    return BudgetEvaluation(
        usage=usage,
        utilization=utilization,
        warning=not warning_already_emitted and utilization >= 0.8,
        exhausted=utilization >= 1.0,
    )


__all__ = ["BudgetEvaluation", "BudgetLimits", "BudgetUsage", "RunWallet", "evaluate_budget"]

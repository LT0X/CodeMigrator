"""Run-wallet accounting and deterministic budget decisions."""

from __future__ import annotations

from dataclasses import dataclass


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


__all__ = ["BudgetEvaluation", "BudgetLimits", "BudgetUsage", "evaluate_budget"]

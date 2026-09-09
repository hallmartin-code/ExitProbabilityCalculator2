"""Deterministic financial calculations.

Every number that reaches the report is computed here, not reasoned about. These
functions are pure, total, and return ``None`` rather than raising or silently
substituting zero when the inputs cannot support an answer.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple


class CalculationError(ValueError):
    """Raised for inputs that are structurally invalid rather than merely absent."""


def safe_divide(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    """Divide, returning None for absent inputs or a zero denominator."""
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def cagr(begin_value: Optional[float], end_value: Optional[float], years: float) -> Optional[float]:
    """Compound annual growth rate as a decimal fraction.

    Undefined when the starting value is not strictly positive -- growth from zero
    is infinite, and reporting a huge number there would be misleading rather than
    informative.
    """
    if begin_value is None or end_value is None:
        return None
    if years <= 0:
        raise CalculationError("CAGR requires a positive number of years.")
    if begin_value <= 0:
        return None
    if end_value < 0:
        return None
    return (end_value / begin_value) ** (1.0 / years) - 1.0


def growth_rate_from_history(points: Sequence[Tuple[int, float]]) -> Optional[float]:
    """Annualised growth implied by a series of (year, revenue) observations.

    Uses the first and last points, which is the honest reading of a short deck
    series; a regression over three noisy points would imply precision that is not
    there.
    """
    clean = sorted({int(y): float(v) for y, v in points}.items())
    if len(clean) < 2:
        return None
    (y0, v0), (y1, v1) = clean[0], clean[-1]
    span = y1 - y0
    if span <= 0:
        return None
    return cagr(v0, v1, float(span))


def arr_from_mrr(mrr: Optional[float]) -> Optional[float]:
    if mrr is None:
        return None
    return mrr * 12.0


def runway_months(
    cash: Optional[float], monthly_burn: Optional[float]
) -> Optional[float]:
    """Months of runway. A non-positive burn means the company is not consuming cash."""
    if cash is None or monthly_burn is None:
        return None
    if monthly_burn <= 0:
        return math.inf
    return cash / monthly_burn


def implied_monthly_burn(
    cash: Optional[float], runway: Optional[float]
) -> Optional[float]:
    """Back out monthly burn from a stated cash balance and runway."""
    if cash is None or runway is None or runway <= 0 or math.isinf(runway):
        return None
    return cash / runway


def revenue_multiple(
    transaction_value: Optional[float], revenue: Optional[float]
) -> Optional[float]:
    if revenue is None or revenue <= 0:
        return None
    return safe_divide(transaction_value, revenue)


def ebitda_multiple(
    transaction_value: Optional[float], ebitda: Optional[float]
) -> Optional[float]:
    """EBITDA multiple. Undefined for non-positive EBITDA, where it is meaningless."""
    if ebitda is None or ebitda <= 0:
        return None
    return safe_divide(transaction_value, ebitda)


def capital_efficiency(
    revenue: Optional[float], capital_raised: Optional[float]
) -> Optional[float]:
    """Revenue generated per dollar of capital raised."""
    if capital_raised is None or capital_raised <= 0:
        return None
    return safe_divide(revenue, capital_raised)


def percentile(values: Sequence[float], pct: float) -> Optional[float]:
    """Linear-interpolated percentile. ``pct`` is 0-100."""
    if not values:
        return None
    if not 0.0 <= pct <= 100.0:
        raise CalculationError("Percentile must be between 0 and 100.")
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * (pct / 100.0)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[int(position)]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def median(values: Sequence[float]) -> Optional[float]:
    return percentile(values, 50.0)


def compound_dilution(rounds: int, dilution_per_round: float) -> Optional[float]:
    """Total ownership given up across ``rounds`` successive raises.

    Dilution compounds multiplicatively: two 20% rounds give up 36%, not 40%.
    """
    if rounds < 0:
        raise CalculationError("Round count cannot be negative.")
    if not 0.0 <= dilution_per_round < 1.0:
        raise CalculationError("Dilution per round must be in [0, 1).")
    if rounds == 0:
        return 0.0
    retained = (1.0 - dilution_per_round) ** rounds
    return 1.0 - retained


def years_to_target_revenue(
    current_revenue: Optional[float],
    target_revenue: float,
    growth_rate: Optional[float],
    default_years: float,
) -> Optional[float]:
    """Years of compounding needed to grow from current to target revenue.

    Falls back to ``default_years`` when growth is unknown, and returns None when
    growth is non-positive and the target is above current revenue -- a company
    that is not growing does not arrive at a larger number by waiting.
    """
    if target_revenue <= 0:
        raise CalculationError("Target revenue must be positive.")
    if current_revenue is None:
        return default_years
    if current_revenue >= target_revenue:
        return 0.0
    if growth_rate is None:
        return default_years
    if growth_rate <= 0:
        return None
    return math.log(target_revenue / current_revenue) / math.log(1.0 + growth_rate)


def normalize_to_percent(weights: Dict[str, float]) -> Dict[str, float]:
    """Scale non-negative weights so they sum to 100."""
    if not weights:
        raise CalculationError("Cannot normalise an empty weight set.")
    if any(w < 0 for w in weights.values()):
        raise CalculationError("Weights must be non-negative.")
    total = sum(weights.values())
    if total <= 0:
        # Degenerate input: fall back to a uniform distribution rather than
        # dividing by zero, and let the caller's confidence score carry the doubt.
        share = 100.0 / len(weights)
        return {k: share for k in weights}
    return {k: (v / total) * 100.0 for k, v in weights.items()}


def round_to_increment(
    percentages: Dict[str, float], increment: float = 5.0
) -> Dict[str, float]:
    """Round a percentage distribution to a coarse increment, preserving a 100 total.

    Uses largest-remainder allocation, then places any residual on the largest
    bucket. The output avoids the false precision of values like 17.43% while
    still summing to exactly 100.
    """
    if not percentages:
        raise CalculationError("Cannot round an empty distribution.")
    if increment <= 0 or 100 % increment != 0:
        raise CalculationError("Increment must divide 100 evenly.")

    units_total = int(round(100.0 / increment))
    exact = {k: v / increment for k, v in percentages.items()}
    floors = {k: int(math.floor(v)) for k, v in exact.items()}
    remainder_units = units_total - sum(floors.values())

    # Hand out the remaining units to the largest fractional parts.
    by_fraction = sorted(exact.items(), key=lambda kv: kv[1] - math.floor(kv[1]), reverse=True)
    for i in range(max(remainder_units, 0)):
        floors[by_fraction[i % len(by_fraction)][0]] += 1

    result = {k: v * increment for k, v in floors.items()}

    # Guard against any residual from pathological inputs.
    drift = 100.0 - sum(result.values())
    if abs(drift) > 1e-9:
        largest = max(result, key=lambda k: result[k])
        result[largest] = max(0.0, result[largest] + drift)
    return result


def apply_floor_and_renormalize(
    percentages: Dict[str, float],
    key: str,
    floor: float,
    ceiling: Optional[float] = None,
) -> Dict[str, float]:
    """Force one bucket within [floor, ceiling], rebalancing the others proportionally.

    Used to keep the no-liquidity outcome always visible and never absurd.
    """
    if key not in percentages:
        raise CalculationError(f"Unknown bucket: {key}")
    result = dict(percentages)
    target = result[key]
    target = max(target, floor)
    if ceiling is not None:
        target = min(target, ceiling)
    if abs(target - result[key]) < 1e-9:
        return result

    others = {k: v for k, v in result.items() if k != key}
    remaining = 100.0 - target
    other_total = sum(others.values())
    if other_total <= 0:
        share = remaining / len(others) if others else 0.0
        rebalanced = {k: share for k in others}
    else:
        rebalanced = {k: v / other_total * remaining for k, v in others.items()}
    rebalanced[key] = target
    return rebalanced


def ownership_after_rounds(
    current_ownership: Optional[float], rounds: int, dilution_per_round: float
) -> Optional[float]:
    """Project an ownership stake forward through future financing rounds."""
    if current_ownership is None:
        return None
    if not 0.0 <= current_ownership <= 1.0:
        raise CalculationError("Ownership must be expressed as a fraction in [0, 1].")
    total_dilution = compound_dilution(rounds, dilution_per_round)
    if total_dilution is None:
        return None
    return current_ownership * (1.0 - total_dilution)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def weighted_score(drivers: Sequence[Tuple[float, float]]) -> float:
    """Combine (score_0_to_10, weight) pairs into a 0-100 dimension score."""
    total_weight = sum(w for _, w in drivers)
    if total_weight <= 0:
        return 0.0
    raw = sum(s * w for s, w in drivers) / total_weight
    return clamp(raw * 10.0, 0.0, 100.0)


def summarize_range(values: List[float], low_pct: float, high_pct: float) -> Optional[Tuple[float, float]]:
    """Percentile range over a value set, returned low-first."""
    if not values:
        return None
    low = percentile(values, low_pct)
    high = percentile(values, high_pct)
    if low is None or high is None:
        return None
    return (min(low, high), max(low, high))

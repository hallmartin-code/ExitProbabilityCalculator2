"""Display formatting for the report and the UI.

Formatting is where false precision creeps into an investor document, so these
helpers round to scale-appropriate significance and always make absence explicit
rather than printing a bare zero or a blank.
"""

from __future__ import annotations

import math
from typing import Optional

#: What the report prints where a value was never disclosed.
NOT_DISCLOSED = "Not disclosed"


def money(value: Optional[float], *, decimals: Optional[int] = None) -> str:
    """Format USD at an appropriate scale: $12.4M, $1.2B, $450K.

    Chooses the unit from the magnitude and defaults to one decimal below $100
    of that unit, none above -- so $8.4M and $250M, never $8.40M or $250.0M.
    """
    if value is None:
        return NOT_DISCLOSED
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return NOT_DISCLOSED

    sign = "-" if value < 0 else ""
    magnitude = abs(float(value))

    if magnitude >= 1_000_000_000:
        scaled, suffix = magnitude / 1_000_000_000, "B"
    elif magnitude >= 1_000_000:
        scaled, suffix = magnitude / 1_000_000, "M"
    elif magnitude >= 1_000:
        scaled, suffix = magnitude / 1_000, "K"
    else:
        return f"{sign}${magnitude:,.0f}"

    places = decimals if decimals is not None else (1 if scaled < 100 else 0)
    return f"{sign}${scaled:,.{places}f}{suffix}"


def money_range(low: Optional[float], high: Optional[float]) -> str:
    """Format a valuation range, collapsing to a single figure when bounds match."""
    if low is None and high is None:
        return NOT_DISCLOSED
    if low is None or high is None:
        return money(low if low is not None else high)
    if abs(high - low) < 1e-9:
        return money(low)
    return f"{money(low)}-{money(high)}"


def percent(value: Optional[float], *, decimals: int = 0, already_percent: bool = False) -> str:
    """Format a decimal fraction (0.18) or an existing percentage (18.0) as '18%'."""
    if value is None:
        return NOT_DISCLOSED
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return NOT_DISCLOSED
    pct = float(value) if already_percent else float(value) * 100.0
    return f"{pct:,.{decimals}f}%"


def multiple(value: Optional[float]) -> str:
    if value is None:
        return NOT_DISCLOSED
    return f"{value:,.1f}x"


def months(value: Optional[float]) -> str:
    if value is None:
        return NOT_DISCLOSED
    if math.isinf(value):
        return "Cash-flow positive"
    return f"{value:,.0f} mo"


def count(value: Optional[int], noun: str = "") -> str:
    if value is None:
        return NOT_DISCLOSED
    label = f" {noun}" if noun else ""
    return f"{value:,}{label}"


def score(value: Optional[float], out_of: int = 100) -> str:
    if value is None:
        return NOT_DISCLOSED
    return f"{value:.0f}/{out_of}"


def truncate(text: str, limit: int, ellipsis: str = "...") -> str:
    """Trim to a character budget on a word boundary.

    The one-page constraint is hard, so text that would overflow is cut here
    deliberately rather than being allowed to push the layout onto page two.
    """
    if text is None:
        return ""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    if limit <= len(ellipsis):
        return text[:limit]
    cut = text[: limit - len(ellipsis)]
    boundary = cut.rfind(" ")
    if boundary > limit * 0.6:
        cut = cut[:boundary]
    return cut.rstrip(" ,;:.") + ellipsis


def title_case(text: Optional[str]) -> str:
    if not text:
        return NOT_DISCLOSED
    return " ".join(word.capitalize() if word.islower() else word for word in text.split())


def sentence(text: Optional[str]) -> str:
    """Normalise whitespace and guarantee terminal punctuation."""
    if not text:
        return ""
    cleaned = " ".join(text.split())
    if cleaned and cleaned[-1] not in ".!?":
        cleaned += "."
    return cleaned

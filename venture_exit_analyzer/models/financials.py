"""Financial profile and funding history."""

from __future__ import annotations

from datetime import date
from typing import List, Optional

from pydantic import BaseModel, Field

from .evidence import EvidencedFloat, EvidencedStr


class RevenuePoint(BaseModel):
    """One historical revenue observation."""

    year: int
    revenue: float = Field(ge=0.0)


class FinancialProfile(BaseModel):
    """All amounts in USD, all rates as decimal fractions (0.35 == 35%)."""

    annual_revenue: EvidencedFloat = Field(default_factory=EvidencedFloat.missing)
    arr: EvidencedFloat = Field(default_factory=EvidencedFloat.missing)
    historical_revenue: List[RevenuePoint] = Field(default_factory=list)
    revenue_growth_rate: EvidencedFloat = Field(default_factory=EvidencedFloat.missing)
    gross_margin: EvidencedFloat = Field(default_factory=EvidencedFloat.missing)
    ebitda_margin: EvidencedFloat = Field(default_factory=EvidencedFloat.missing)
    monthly_burn: EvidencedFloat = Field(default_factory=EvidencedFloat.missing)
    runway_months: EvidencedFloat = Field(default_factory=EvidencedFloat.missing)
    current_cash: EvidencedFloat = Field(default_factory=EvidencedFloat.missing)
    capital_raised_to_date: EvidencedFloat = Field(default_factory=EvidencedFloat.missing)
    current_round: EvidencedStr = Field(default_factory=EvidencedStr.missing)
    amount_raising: EvidencedFloat = Field(default_factory=EvidencedFloat.missing)
    expected_future_capital: EvidencedFloat = Field(default_factory=EvidencedFloat.missing)

    @property
    def best_revenue(self) -> Optional[float]:
        """ARR preferred over annual revenue when both are present.

        ARR is the forward-looking recurring figure buyers underwrite; trailing annual
        revenue is the fallback when no recurring figure was disclosed.
        """
        if self.arr.is_present:
            return self.arr.value
        return self.annual_revenue.value

    @property
    def best_revenue_label(self) -> str:
        return "ARR" if self.arr.is_present else "Revenue"


class FundingRound(BaseModel):
    round_date: Optional[date] = None
    round_type: str = "Unknown"
    amount_raised: Optional[float] = Field(default=None, ge=0.0)
    pre_money_valuation: Optional[float] = Field(default=None, ge=0.0)
    post_money_valuation: Optional[float] = Field(default=None, ge=0.0)
    lead_investors: List[str] = Field(default_factory=list)


class FundingHistory(BaseModel):
    rounds: List[FundingRound] = Field(default_factory=list)

    @property
    def total_raised(self) -> Optional[float]:
        amounts = [r.amount_raised for r in self.rounds if r.amount_raised is not None]
        return sum(amounts) if amounts else None

    @property
    def latest_post_money(self) -> Optional[float]:
        """Post-money of the most recent round that disclosed one."""
        dated = [r for r in self.rounds if r.post_money_valuation is not None]
        if not dated:
            return None
        with_date = [r for r in dated if r.round_date is not None]
        if with_date:
            return max(with_date, key=lambda r: r.round_date).post_money_valuation
        return dated[-1].post_money_valuation

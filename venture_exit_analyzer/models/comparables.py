"""Comparable exit transactions supplied by the user (manual, CSV or JSON).

Comparables are never invented by the application. Every row here originates with
the user; the analysis layer only computes multiples and ranges from them.
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, model_validator


class BuyerType(str, Enum):
    STRATEGIC = "strategic"
    FINANCIAL = "financial"
    PUBLIC_MARKET = "public_market"
    UNKNOWN = "unknown"


class TransactionType(str, Enum):
    ACQUISITION = "acquisition"
    MERGER = "merger"
    IPO = "ipo"
    ASSET_SALE = "asset_sale"
    UNKNOWN = "unknown"


class ComparableExit(BaseModel):
    company: str
    sector: Optional[str] = None
    transaction_date: Optional[date] = None
    buyer: Optional[str] = None
    buyer_type: BuyerType = BuyerType.UNKNOWN
    transaction_type: TransactionType = TransactionType.UNKNOWN
    transaction_value: Optional[float] = Field(default=None, ge=0.0)
    revenue_at_exit: Optional[float] = Field(default=None, ge=0.0)
    ebitda_at_exit: Optional[float] = None
    revenue_multiple: Optional[float] = Field(default=None, ge=0.0)
    ebitda_multiple: Optional[float] = None
    growth_rate: Optional[float] = None
    strategic_rationale: Optional[str] = None

    @model_validator(mode="after")
    def _derive_multiples(self) -> "ComparableExit":
        """Fill in multiples that can be computed from value and financials.

        Only derives what is arithmetically implied -- it never guesses a
        transaction value from a multiple it also had to guess.
        """
        if (
            self.revenue_multiple is None
            and self.transaction_value is not None
            and self.revenue_at_exit
        ):
            object.__setattr__(self, "revenue_multiple", self.transaction_value / self.revenue_at_exit)
        if (
            self.ebitda_multiple is None
            and self.transaction_value is not None
            and self.ebitda_at_exit
            and self.ebitda_at_exit > 0
        ):
            object.__setattr__(self, "ebitda_multiple", self.transaction_value / self.ebitda_at_exit)
        return self

    @property
    def has_usable_revenue_multiple(self) -> bool:
        return self.revenue_multiple is not None and self.revenue_multiple > 0


class ComparableSet(BaseModel):
    comparables: List[ComparableExit] = Field(default_factory=list)

    def for_buyer_type(self, buyer_type: BuyerType) -> List[ComparableExit]:
        return [c for c in self.comparables if c.buyer_type == buyer_type]

    @property
    def usable_revenue_multiples(self) -> List[float]:
        return [c.revenue_multiple for c in self.comparables if c.has_usable_revenue_multiple]

    def __len__(self) -> int:
        return len(self.comparables)

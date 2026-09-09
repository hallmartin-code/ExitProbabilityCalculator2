"""Company, market, customer, IP and ownership profiles."""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field

from .evidence import Evidenced, EvidencedBool, EvidencedFloat, EvidencedInt, EvidencedStr


class Stage(str, Enum):
    """Company financing stage. Ordered from earliest to latest."""

    PRE_SEED = "pre_seed"
    SEED = "seed"
    SERIES_A = "series_a"
    SERIES_B = "series_b"
    SERIES_C = "series_c"
    GROWTH = "growth"
    UNKNOWN = "unknown"

    @property
    def ordinal(self) -> int:
        """Maturity index 0-5; UNKNOWN maps to seed-equivalent for scoring neutrality."""
        order = {
            Stage.PRE_SEED: 0,
            Stage.SEED: 1,
            Stage.SERIES_A: 2,
            Stage.SERIES_B: 3,
            Stage.SERIES_C: 4,
            Stage.GROWTH: 5,
        }
        return order.get(self, 1)

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").title().replace("Series ", "Series ")


class CompanyProfile(BaseModel):
    name: EvidencedStr = Field(default_factory=EvidencedStr.missing)
    website: EvidencedStr = Field(default_factory=EvidencedStr.missing)
    headquarters: EvidencedStr = Field(default_factory=EvidencedStr.missing)
    sector: EvidencedStr = Field(default_factory=EvidencedStr.missing)
    subsector: EvidencedStr = Field(default_factory=EvidencedStr.missing)
    business_model: EvidencedStr = Field(default_factory=EvidencedStr.missing)
    stage: Evidenced[Stage] = Field(default_factory=Evidenced[Stage].missing)
    founding_year: EvidencedInt = Field(default_factory=EvidencedInt.missing)
    current_financing_stage: EvidencedStr = Field(default_factory=EvidencedStr.missing)

    @property
    def display_name(self) -> str:
        return self.name.or_default("Unnamed company")

    @property
    def stage_value(self) -> Stage:
        v = self.stage.value
        return v if isinstance(v, Stage) else Stage.UNKNOWN


class MarketProfile(BaseModel):
    tam: EvidencedFloat = Field(default_factory=EvidencedFloat.missing)
    sam: EvidencedFloat = Field(default_factory=EvidencedFloat.missing)
    som: EvidencedFloat = Field(default_factory=EvidencedFloat.missing)
    market_growth_rate: EvidencedFloat = Field(
        default_factory=EvidencedFloat.missing, description="Decimal fraction, e.g. 0.18 for 18%."
    )
    competitive_position: EvidencedStr = Field(default_factory=EvidencedStr.missing)
    market_share: EvidencedFloat = Field(default_factory=EvidencedFloat.missing)
    category_leadership: EvidencedBool = Field(default_factory=EvidencedBool.missing)
    differentiation: EvidencedStr = Field(default_factory=EvidencedStr.missing)
    competitive_advantages: List[str] = Field(default_factory=list)
    switching_costs: EvidencedStr = Field(default_factory=EvidencedStr.missing)
    network_effects: EvidencedBool = Field(default_factory=EvidencedBool.missing)
    distribution_advantage: EvidencedStr = Field(default_factory=EvidencedStr.missing)


class CustomerProfile(BaseModel):
    customer_count: EvidencedInt = Field(default_factory=EvidencedInt.missing)
    customer_type: EvidencedStr = Field(default_factory=EvidencedStr.missing)
    major_customers: List[str] = Field(default_factory=list)
    customer_concentration: EvidencedFloat = Field(
        default_factory=EvidencedFloat.missing,
        description="Revenue share of top customer(s) as a decimal fraction.",
    )
    contract_duration_months: EvidencedFloat = Field(default_factory=EvidencedFloat.missing)
    recurring_revenue_pct: EvidencedFloat = Field(default_factory=EvidencedFloat.missing)
    retention_rate: EvidencedFloat = Field(default_factory=EvidencedFloat.missing)
    net_revenue_retention: EvidencedFloat = Field(default_factory=EvidencedFloat.missing)
    acquisition_profile: EvidencedStr = Field(default_factory=EvidencedStr.missing)


class IPProfile(BaseModel):
    patents_granted: EvidencedInt = Field(default_factory=EvidencedInt.missing)
    patent_applications: EvidencedInt = Field(default_factory=EvidencedInt.missing)
    trade_secrets: EvidencedBool = Field(default_factory=EvidencedBool.missing)
    proprietary_technology: EvidencedStr = Field(default_factory=EvidencedStr.missing)
    proprietary_data: EvidencedStr = Field(default_factory=EvidencedStr.missing)
    regulatory_approvals: List[str] = Field(default_factory=list)
    clinical_assets: List[str] = Field(default_factory=list)
    manufacturing_ip: EvidencedStr = Field(default_factory=EvidencedStr.missing)
    licensing_agreements: List[str] = Field(default_factory=list)

    @property
    def total_patent_positions(self) -> Optional[int]:
        """Granted + pending, or None when neither was disclosed."""
        g, a = self.patents_granted.value, self.patent_applications.value
        if g is None and a is None:
            return None
        return (g or 0) + (a or 0)


class OwnershipProfile(BaseModel):
    founder_ownership: EvidencedFloat = Field(default_factory=EvidencedFloat.missing)
    employee_ownership: EvidencedFloat = Field(default_factory=EvidencedFloat.missing)
    investor_ownership: EvidencedFloat = Field(default_factory=EvidencedFloat.missing)
    option_pool: EvidencedFloat = Field(default_factory=EvidencedFloat.missing)
    preferred_stock_outstanding: EvidencedStr = Field(default_factory=EvidencedStr.missing)
    debt: EvidencedFloat = Field(default_factory=EvidencedFloat.missing)
    convertible_securities: EvidencedStr = Field(default_factory=EvidencedStr.missing)
    safes: EvidencedStr = Field(default_factory=EvidencedStr.missing)
    major_investors: List[str] = Field(default_factory=list)

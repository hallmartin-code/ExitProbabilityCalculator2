"""Analysis output structures: scores, probabilities, and the assembled dossier."""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from .company import CompanyProfile, CustomerProfile, IPProfile, MarketProfile, OwnershipProfile
from .comparables import ComparableSet
from .evidence import EvidenceClass
from .financials import FinancialProfile, FundingHistory


class TimeHorizon(str, Enum):
    Y1_3 = "1_3_years"
    Y3_5 = "3_5_years"
    Y5_7 = "5_7_years"
    Y7_10 = "7_10_years"

    @property
    def label(self) -> str:
        return {
            TimeHorizon.Y1_3: "1-3 Years",
            TimeHorizon.Y3_5: "3-5 Years",
            TimeHorizon.Y5_7: "5-7 Years",
            TimeHorizon.Y7_10: "7-10 Years",
        }[self]

    @property
    def midpoint_years(self) -> float:
        return {
            TimeHorizon.Y1_3: 2.0,
            TimeHorizon.Y3_5: 4.0,
            TimeHorizon.Y5_7: 6.0,
            TimeHorizon.Y7_10: 8.5,
        }[self]


class Scenario(str, Enum):
    STRATEGIC = "strategic_acquisition"
    FINANCIAL = "financial_acquisition"
    IPO = "ipo"
    NO_LIQUIDITY = "no_liquidity"

    @property
    def label(self) -> str:
        return {
            Scenario.STRATEGIC: "Strategic Acquisition",
            Scenario.FINANCIAL: "Financial Acquisition",
            Scenario.IPO: "IPO",
            Scenario.NO_LIQUIDITY: "No Liquidity",
        }[self]


#: Ranking of the qualitative impact labels used on milestones.
IMPACT_ORDER: Dict[str, int] = {"Very High": 4, "High": 3, "Medium": 2, "Low": 1}


class ScoreDriver(BaseModel):
    """One weighted factor inside a dimension score.

    ``score`` is the 0-10 assessment of the factor; ``weight`` is its share of the
    dimension. ``evidence`` states plainly what the score was based on, including
    when the answer was that nothing was disclosed.
    """

    factor: str
    score: float = Field(ge=0.0, le=10.0)
    weight: float = Field(ge=0.0, le=1.0)
    evidence: str
    evidence_class: EvidenceClass = EvidenceClass.INFERRED

    @property
    def contribution(self) -> float:
        """Points this driver contributes to the 0-100 dimension score."""
        return self.score * 10.0 * self.weight

    @property
    def is_evidenced(self) -> bool:
        return self.evidence_class != EvidenceClass.MISSING


class DimensionScore(BaseModel):
    """A 0-100 score with the full driver breakdown that produced it."""

    dimension: str
    score: float = Field(ge=0.0, le=100.0)
    drivers: List[ScoreDriver] = Field(default_factory=list)
    summary: str = ""

    @property
    def evidence_coverage(self) -> float:
        """Weight-share of drivers backed by actual disclosed information (0-1).

        This is the honest measure of how much of the score rests on evidence
        rather than on neutral defaults, and it feeds the confidence model.
        """
        total_weight = sum(d.weight for d in self.drivers)
        if total_weight <= 0:
            return 0.0
        return sum(d.weight for d in self.drivers if d.is_evidenced) / total_weight

    def top_drivers(self, n: int = 3, evidenced_only: bool = True) -> List[ScoreDriver]:
        pool = [d for d in self.drivers if d.is_evidenced] if evidenced_only else list(self.drivers)
        return sorted(pool, key=lambda d: d.contribution, reverse=True)[:n]

    def weakest_drivers(self, n: int = 3) -> List[ScoreDriver]:
        scored = [d for d in self.drivers if d.is_evidenced]
        return sorted(scored, key=lambda d: d.score)[:n]


class HorizonProbabilities(BaseModel):
    """Scenario probabilities for one time horizon, as percentages summing to 100."""

    horizon: TimeHorizon
    strategic_acquisition: float = Field(ge=0.0, le=100.0)
    financial_acquisition: float = Field(ge=0.0, le=100.0)
    ipo: float = Field(ge=0.0, le=100.0)
    no_liquidity: float = Field(ge=0.0, le=100.0)

    def as_dict(self) -> Dict[Scenario, float]:
        return {
            Scenario.STRATEGIC: self.strategic_acquisition,
            Scenario.FINANCIAL: self.financial_acquisition,
            Scenario.IPO: self.ipo,
            Scenario.NO_LIQUIDITY: self.no_liquidity,
        }

    @property
    def total(self) -> float:
        return (
            self.strategic_acquisition + self.financial_acquisition + self.ipo + self.no_liquidity
        )

    @property
    def most_likely(self) -> Scenario:
        """The highest-probability outcome, with ties resolved conservatively.

        When an exit route ties with the no-liquidity case, no-liquidity wins.
        Asserting an exit is the stronger claim, and a tie is not evidence for it.
        """
        outcomes = self.as_dict()
        highest = max(outcomes.values())
        if outcomes[Scenario.NO_LIQUIDITY] >= highest:
            return Scenario.NO_LIQUIDITY
        return max(outcomes.items(), key=lambda kv: kv[1])[0]

    @property
    def most_likely_liquidity_path(self) -> Scenario:
        """Highest-probability liquidity outcome, excluding the no-exit case."""
        liquid = {k: v for k, v in self.as_dict().items() if k != Scenario.NO_LIQUIDITY}
        return max(liquid.items(), key=lambda kv: kv[1])[0]


class AcquirerProfile(BaseModel):
    """A category of buyer, or a named buyer supplied by the user."""

    category: str
    is_named_buyer: bool = False
    strategic_rationale: str = ""
    attractive_assets: List[str] = Field(default_factory=list)
    concerns: List[str] = Field(default_factory=list)
    unlocking_milestones: List[str] = Field(default_factory=list)
    strategic_fit: Optional[float] = Field(default=None, ge=0.0, le=10.0)
    financial_capacity: Optional[float] = Field(default=None, ge=0.0, le=10.0)
    likelihood: str = "Moderate"


class Milestone(BaseModel):
    milestone: str
    exit_impact: str = "Medium"  # Very High | High | Medium | Low
    relevant_scenarios: List[str] = Field(default_factory=list)
    rationale: str = ""
    impact_rank: float = 0.0

    @property
    def impact_weight(self) -> int:
        return IMPACT_ORDER.get(self.exit_impact, 2)


class ValuationRange(BaseModel):
    """A directional valuation range with its derivation made explicit."""

    scenario: Scenario
    low: Optional[float] = None
    high: Optional[float] = None
    method: str = ""
    basis: str = ""
    is_defensible: bool = False
    insufficient_reason: Optional[str] = None


class CapitalRequirement(BaseModel):
    current_cash: Optional[float] = None
    runway_months: Optional[float] = None
    monthly_burn: Optional[float] = None
    additional_capital_low: Optional[float] = None
    additional_capital_high: Optional[float] = None
    expected_rounds: Optional[int] = None
    estimated_dilution_low: Optional[float] = None
    estimated_dilution_high: Optional[float] = None
    assumptions: List[str] = Field(default_factory=list)
    is_estimable: bool = False


class ConfidenceAssessment(BaseModel):
    score: float = Field(ge=0.0, le=100.0)
    band: str = "Low Confidence"
    components: Dict[str, float] = Field(default_factory=dict)
    rationale: List[str] = Field(default_factory=list)


class ValuationDriver(BaseModel):
    driver: str
    direction: str  # "positive" | "negative"
    evidence: str = ""
    magnitude: str = "Medium"


class DealDossier(BaseModel):
    """Everything known about the company, before analysis runs."""

    company: CompanyProfile = Field(default_factory=CompanyProfile)
    financials: FinancialProfile = Field(default_factory=FinancialProfile)
    market: MarketProfile = Field(default_factory=MarketProfile)
    customers: CustomerProfile = Field(default_factory=CustomerProfile)
    ip: IPProfile = Field(default_factory=IPProfile)
    ownership: OwnershipProfile = Field(default_factory=OwnershipProfile)
    funding_history: FundingHistory = Field(default_factory=FundingHistory)
    comparables: ComparableSet = Field(default_factory=ComparableSet)
    named_buyers: List[str] = Field(default_factory=list)
    source_files: List[str] = Field(default_factory=list)
    extraction_warnings: List[str] = Field(default_factory=list)


class ExitScenarioAnalysis(BaseModel):
    """The complete analysis result rendered into the report and the JSON record."""

    analysis_date: date = Field(default_factory=date.today)
    generated_at: datetime = Field(default_factory=datetime.now)

    strategic_score: DimensionScore
    financial_score: DimensionScore
    ipo_score: DimensionScore
    liquidity_risk_score: DimensionScore

    probabilities: List[HorizonProbabilities] = Field(default_factory=list)
    primary_scenario: Scenario = Scenario.NO_LIQUIDITY
    primary_horizon: Optional[TimeHorizon] = None

    confidence: ConfidenceAssessment
    acquirer_profiles: List[AcquirerProfile] = Field(default_factory=list)
    milestones: List[Milestone] = Field(default_factory=list)
    capital_requirement: CapitalRequirement = Field(default_factory=CapitalRequirement)
    valuation_ranges: List[ValuationRange] = Field(default_factory=list)
    valuation_drivers: List[ValuationDriver] = Field(default_factory=list)

    exit_profile_summary: str = ""
    assumptions: List[str] = Field(default_factory=list)
    limitations: List[str] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)

    def probabilities_for(self, horizon: TimeHorizon) -> Optional[HorizonProbabilities]:
        return next((p for p in self.probabilities if p.horizon == horizon), None)

    @property
    def dimension_scores(self) -> Dict[str, DimensionScore]:
        return {
            "strategic_acquisition": self.strategic_score,
            "financial_acquisition": self.financial_score,
            "ipo_readiness": self.ipo_score,
            "liquidity_risk": self.liquidity_risk_score,
        }

    def valuation_for(self, scenario: Scenario) -> Optional[ValuationRange]:
        return next((v for v in self.valuation_ranges if v.scenario == scenario), None)

    @property
    def positive_drivers(self) -> List[ValuationDriver]:
        return [d for d in self.valuation_drivers if d.direction == "positive"]

    @property
    def negative_drivers(self) -> List[ValuationDriver]:
        return [d for d in self.valuation_drivers if d.direction == "negative"]

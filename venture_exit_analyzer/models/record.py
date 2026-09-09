"""The structured TEN Capital record.

The JSON contract the application exports and, with explicit consent, transmits.
Validated with Pydantic so a malformed record is caught here rather than at the
receiving end.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .analysis import (
    DealDossier,
    ExitScenarioAnalysis,
    Scenario,
    TimeHorizon,
)
from .evidence import Evidenced

ANALYSIS_TYPE = "venture_exit_scenario"
SCHEMA_VERSION = "1.0"


class UserBlock(BaseModel):
    user_id: str = ""
    name: str = ""
    email: str = ""


class CompanyBlock(BaseModel):
    name: str = ""
    sector: str = ""
    subsector: str = ""
    stage: str = ""
    revenue: Optional[float] = None
    growth_rate: Optional[float] = None


class HorizonBlock(BaseModel):
    strategic_acquisition: Optional[float] = None
    financial_acquisition: Optional[float] = None
    ipo: Optional[float] = None
    no_liquidity: Optional[float] = None


class AnalysisBlock(BaseModel):
    analysis_date: str = ""
    confidence_score: Optional[float] = None
    confidence_band: str = ""
    primary_scenario: str = ""
    primary_horizon: Optional[str] = None
    time_horizons: Dict[str, HorizonBlock] = Field(default_factory=dict)
    dimension_scores: Dict[str, float] = Field(default_factory=dict)


class CapitalBlock(BaseModel):
    raised_to_date: Optional[float] = None
    current_raise: Optional[float] = None
    current_cash: Optional[float] = None
    runway_months: Optional[float] = None
    monthly_burn: Optional[float] = None
    estimated_additional_capital_required: Dict[str, Optional[float]] = Field(
        default_factory=lambda: {"low": None, "high": None}
    )
    expected_financing_rounds: Optional[int] = None


class ConsentBlock(BaseModel):
    """Consent is tracked as three independent decisions, never inferred.

    Uploading a document is not consent to analyse it, analysing it is not consent
    to retain it, and retaining it is not consent to transmit it to TEN Capital.
    """

    analysis_consent: bool = False
    data_storage_consent: bool = False
    share_with_ten_capital: bool = False
    consent_timestamp: Optional[str] = None

    @property
    def may_transmit(self) -> bool:
        return bool(self.share_with_ten_capital)


class TenCapitalRecord(BaseModel):
    analysis_type: str = ANALYSIS_TYPE
    schema_version: str = SCHEMA_VERSION
    user: UserBlock = Field(default_factory=UserBlock)
    company: CompanyBlock = Field(default_factory=CompanyBlock)
    analysis: AnalysisBlock = Field(default_factory=AnalysisBlock)
    capital: CapitalBlock = Field(default_factory=CapitalBlock)
    exit_scenarios: List[Dict[str, Any]] = Field(default_factory=list)
    likely_acquirer_profiles: List[Dict[str, Any]] = Field(default_factory=list)
    milestones: List[Dict[str, Any]] = Field(default_factory=list)
    valuation_drivers: List[Dict[str, Any]] = Field(default_factory=list)
    valuation_ranges: List[Dict[str, Any]] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)
    assumptions: List[str] = Field(default_factory=list)
    limitations: List[str] = Field(default_factory=list)
    evidence_trail: List[Dict[str, Any]] = Field(default_factory=list)
    source_files: List[str] = Field(default_factory=list)
    consent: ConsentBlock = Field(default_factory=ConsentBlock)


def _evidence_entry(field_name: str, evidenced: Evidenced) -> Optional[Dict[str, Any]]:
    """One row of the provenance trail, for values that are actually present."""
    if not evidenced.is_present:
        return None
    return {
        "field": field_name,
        "value": evidenced.value if not hasattr(evidenced.value, "value") else str(evidenced.value),
        "source": evidenced.source,
        "source_page_or_slide": evidenced.source_page_or_slide,
        "evidence_class": evidenced.evidence_class.value,
        "confidence": round(evidenced.confidence, 3),
    }


def _evidence_trail(dossier: DealDossier) -> List[Dict[str, Any]]:
    fin, market, customers, ip, company = (
        dossier.financials,
        dossier.market,
        dossier.customers,
        dossier.ip,
        dossier.company,
    )
    candidates = [
        ("company.name", company.name),
        ("company.sector", company.sector),
        ("company.stage", company.stage),
        ("company.founding_year", company.founding_year),
        ("financials.arr", fin.arr),
        ("financials.annual_revenue", fin.annual_revenue),
        ("financials.revenue_growth_rate", fin.revenue_growth_rate),
        ("financials.gross_margin", fin.gross_margin),
        ("financials.ebitda_margin", fin.ebitda_margin),
        ("financials.monthly_burn", fin.monthly_burn),
        ("financials.runway_months", fin.runway_months),
        ("financials.current_cash", fin.current_cash),
        ("financials.capital_raised_to_date", fin.capital_raised_to_date),
        ("financials.amount_raising", fin.amount_raising),
        ("market.tam", market.tam),
        ("market.sam", market.sam),
        ("market.market_growth_rate", market.market_growth_rate),
        ("customers.customer_count", customers.customer_count),
        ("customers.recurring_revenue_pct", customers.recurring_revenue_pct),
        ("customers.net_revenue_retention", customers.net_revenue_retention),
        ("customers.customer_concentration", customers.customer_concentration),
        ("ip.patents_granted", ip.patents_granted),
        ("ip.patent_applications", ip.patent_applications),
    ]
    entries = [_evidence_entry(name, value) for name, value in candidates]
    return [entry for entry in entries if entry is not None]


def build_record(
    dossier: DealDossier,
    analysis: ExitScenarioAnalysis,
    consent: ConsentBlock,
    user: Optional[UserBlock] = None,
) -> TenCapitalRecord:
    """Assemble the exportable record from a completed analysis."""
    company = dossier.company
    fin = dossier.financials

    horizons: Dict[str, HorizonBlock] = {}
    scenarios: List[Dict[str, Any]] = []
    for probabilities in analysis.probabilities:
        horizons[probabilities.horizon.value] = HorizonBlock(
            strategic_acquisition=probabilities.strategic_acquisition,
            financial_acquisition=probabilities.financial_acquisition,
            ipo=probabilities.ipo,
            no_liquidity=probabilities.no_liquidity,
        )
        for scenario, value in probabilities.as_dict().items():
            scenarios.append(
                {
                    "scenario": scenario.value,
                    "horizon": probabilities.horizon.value,
                    "probability_percent": value,
                }
            )

    capital = analysis.capital_requirement

    return TenCapitalRecord(
        user=user or UserBlock(),
        company=CompanyBlock(
            name=company.display_name,
            sector=company.sector.or_default(""),
            subsector=company.subsector.or_default(""),
            stage=company.stage_value.value if company.stage.is_present else "",
            revenue=fin.best_revenue,
            growth_rate=fin.revenue_growth_rate.value,
        ),
        analysis=AnalysisBlock(
            analysis_date=analysis.analysis_date.isoformat(),
            confidence_score=round(analysis.confidence.score, 1),
            confidence_band=analysis.confidence.band,
            primary_scenario=analysis.primary_scenario.value,
            primary_horizon=(
                analysis.primary_horizon.value if analysis.primary_horizon else None
            ),
            time_horizons=horizons,
            dimension_scores={
                name: round(score.score, 1)
                for name, score in analysis.dimension_scores.items()
            },
        ),
        capital=CapitalBlock(
            raised_to_date=fin.capital_raised_to_date.value,
            current_raise=fin.amount_raising.value,
            current_cash=capital.current_cash,
            runway_months=capital.runway_months,
            monthly_burn=capital.monthly_burn,
            estimated_additional_capital_required={
                "low": capital.additional_capital_low,
                "high": capital.additional_capital_high,
            },
            expected_financing_rounds=capital.expected_rounds,
        ),
        exit_scenarios=scenarios,
        likely_acquirer_profiles=[
            {
                "category": profile.category,
                "is_named_buyer": profile.is_named_buyer,
                "strategic_rationale": profile.strategic_rationale,
                "attractive_assets": profile.attractive_assets,
                "concerns": profile.concerns,
                "unlocking_milestones": profile.unlocking_milestones,
                "strategic_fit": profile.strategic_fit,
                "financial_capacity": profile.financial_capacity,
                "likelihood": profile.likelihood,
            }
            for profile in analysis.acquirer_profiles
        ],
        milestones=[
            {
                "milestone": milestone.milestone,
                "exit_impact": milestone.exit_impact,
                "relevant_scenarios": milestone.relevant_scenarios,
                "rationale": milestone.rationale,
            }
            for milestone in analysis.milestones
        ],
        valuation_drivers=[
            {
                "driver": driver.driver,
                "direction": driver.direction,
                "evidence": driver.evidence,
                "magnitude": driver.magnitude,
            }
            for driver in analysis.valuation_drivers
        ],
        valuation_ranges=[
            {
                "scenario": valuation.scenario.value,
                "low": valuation.low,
                "high": valuation.high,
                "method": valuation.method,
                "basis": valuation.basis,
                "is_defensible": valuation.is_defensible,
                "insufficient_reason": valuation.insufficient_reason,
            }
            for valuation in analysis.valuation_ranges
        ],
        risks=analysis.risks,
        assumptions=analysis.assumptions,
        limitations=analysis.limitations,
        evidence_trail=_evidence_trail(dossier),
        source_files=dossier.source_files,
        consent=consent,
    )

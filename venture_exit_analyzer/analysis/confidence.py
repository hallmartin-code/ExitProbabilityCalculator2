"""Analysis confidence.

Confidence is not probability. A 60% strategic-acquisition probability computed
from a deck with almost no financial disclosure and no comparables is a weak
conclusion; the same number computed from full disclosure and six comparables is
a strong one. This module scores how much weight the analysis itself deserves.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..config.loader import load_scoring_config
from ..models.analysis import ConfidenceAssessment, DealDossier, DimensionScore
from ..models.evidence import Evidenced
from ..utils.calculations import clamp

#: The disclosures a competent exit analysis really depends on.
CORE_FINANCIAL_FIELDS = (
    "annual_revenue_or_arr",
    "revenue_growth_rate",
    "gross_margin",
    "ebitda_margin",
    "monthly_burn",
    "runway_months",
    "capital_raised_to_date",
)


def _financial_completeness(dossier: DealDossier) -> tuple[float, List[str]]:
    fin = dossier.financials
    present: Dict[str, bool] = {
        "annual_revenue_or_arr": fin.best_revenue is not None,
        "revenue_growth_rate": fin.revenue_growth_rate.is_present,
        "gross_margin": fin.gross_margin.is_present,
        "ebitda_margin": fin.ebitda_margin.is_present,
        "monthly_burn": fin.monthly_burn.is_present,
        "runway_months": fin.runway_months.is_present,
        "capital_raised_to_date": fin.capital_raised_to_date.is_present,
    }
    found = sum(1 for field in CORE_FINANCIAL_FIELDS if present[field])
    missing = [field.replace("_", " ") for field in CORE_FINANCIAL_FIELDS if not present[field]]
    return found / len(CORE_FINANCIAL_FIELDS), missing


def _market_completeness(dossier: DealDossier) -> float:
    market, customers = dossier.market, dossier.customers
    signals = [
        market.tam.is_present,
        market.market_growth_rate.is_present,
        customers.customer_count.is_present,
        customers.recurring_revenue_pct.is_present or customers.net_revenue_retention.is_present,
    ]
    return sum(1 for s in signals if s) / len(signals)


def _assumption_consistency(dossier: DealDossier) -> tuple[float, List[str]]:
    """Penalise internal contradictions found during extraction and review."""
    issues = [w for w in dossier.extraction_warnings if "reconcile" in w or "mis-read" in w]
    if not issues:
        return 1.0, []
    return max(0.0, 1.0 - 0.35 * len(issues)), issues


def _evidence_weighted_confidence(values: List[Evidenced]) -> float:
    """Mean confidence across the evidenced values that are actually present."""
    present = [v.confidence for v in values if v.is_present]
    if not present:
        return 0.0
    return sum(present) / len(present)


def assess_confidence(
    dossier: DealDossier,
    dimensions: List[DimensionScore],
    config: Optional[Dict[str, Any]] = None,
) -> ConfidenceAssessment:
    """Produce the 0-100 confidence score and its band."""
    cfg = config or load_scoring_config()
    conf_cfg = cfg["confidence"]
    weights = conf_cfg["weights"]

    rationale: List[str] = []

    # -- deck completeness: how much of the scoring rested on real evidence.
    coverages = [d.evidence_coverage for d in dimensions]
    deck_completeness = sum(coverages) / len(coverages) if coverages else 0.0
    rationale.append(
        f"{deck_completeness:.0%} of scoring weight was backed by disclosed evidence "
        f"rather than neutral defaults."
    )

    # -- financial data quality: presence, discounted by extraction confidence.
    financial_presence, missing_financials = _financial_completeness(dossier)
    fin = dossier.financials
    extraction_quality = _evidence_weighted_confidence(
        [
            fin.arr,
            fin.annual_revenue,
            fin.revenue_growth_rate,
            fin.gross_margin,
            fin.ebitda_margin,
            fin.monthly_burn,
            fin.runway_months,
            fin.capital_raised_to_date,
        ]
    )
    financial_quality = financial_presence * (0.6 + 0.4 * extraction_quality)
    if missing_financials:
        rationale.append(f"Financial disclosures missing: {', '.join(missing_financials)}.")
    else:
        rationale.append("All core financial disclosures were available.")

    # -- comparables support
    saturation = int(conf_cfg["comparables_saturation"])
    usable = len(dossier.comparables.usable_revenue_multiples)
    comparables_support = clamp(usable / saturation, 0.0, 1.0) if saturation else 0.0
    if usable == 0:
        rationale.append(
            "No comparable transactions with usable multiples were supplied, so no "
            "valuation range could be triangulated."
        )
    else:
        rationale.append(
            f"{usable} comparable transaction(s) with usable revenue multiples were supplied."
        )

    # -- stage predictability
    stage = dossier.company.stage_value
    stage_table = conf_cfg["stage_predictability"]
    stage_predictability = float(stage_table.get(stage.ordinal, stage_table.get(1, 0.4)))
    rationale.append(
        f"Company stage ({stage.label}) carries a predictability factor of "
        f"{stage_predictability:.0%}; earlier-stage outcomes are inherently less predictable."
    )

    market_quality = _market_completeness(dossier)
    consistency, issues = _assumption_consistency(dossier)
    for issue in issues:
        rationale.append(f"Inconsistency reduced confidence: {issue}")

    components = {
        "deck_completeness": deck_completeness,
        "financial_data_quality": financial_quality,
        "comparables_support": comparables_support,
        "stage_predictability": stage_predictability,
        "market_data_quality": market_quality,
        "assumption_consistency": consistency,
    }

    score = sum(components[name] * float(weight) for name, weight in weights.items()) * 100.0
    score = clamp(score, 0.0, 100.0)

    bands = conf_cfg["bands"]
    if score >= float(bands["high"]):
        band = "High Confidence"
    elif score >= float(bands["moderate"]):
        band = "Moderate Confidence"
    else:
        band = "Low Confidence"

    return ConfidenceAssessment(
        score=score,
        band=band,
        components={k: round(v * 100.0, 1) for k, v in components.items()},
        rationale=rationale,
    )

"""Narrative generation for the exit-profile paragraph.

The analysis is complete before this runs. The model is handed the conclusions
and asked only to write them well; it is given no latitude to reach its own.

The allow-list built here is what makes that safe. Every figure the deterministic
analysis produced is collected, and the generated prose is rejected if it contains
any figure outside that set. A model cannot, therefore, print a probability or a
valuation the report does not itself compute.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ..models.analysis import (
    DealDossier,
    ExitScenarioAnalysis,
    Scenario,
    TimeHorizon,
)
from ..utils.formatting import money, percent


def _rationale_points(dossier: DealDossier) -> List[str]:
    """Evidenced reasons a buyer would transact, in the deck's own terms."""
    points: List[str] = []
    ip, fin, customers, market = (
        dossier.ip,
        dossier.financials,
        dossier.customers,
        dossier.market,
    )
    if ip.regulatory_approvals:
        points.append(f"holds {', '.join(ip.regulatory_approvals)}")
    positions = ip.total_patent_positions
    if positions:
        points.append(f"{positions} patent position(s)")
    if fin.best_revenue is not None:
        points.append(f"{money(fin.best_revenue)} of {fin.best_revenue_label}")
    if customers.recurring_revenue_pct.is_present:
        points.append(
            f"{percent(customers.recurring_revenue_pct.value)} of revenue recurring"
        )
    if customers.customer_count.is_present:
        points.append(f"{customers.customer_count.value:,} customers")
    if market.category_leadership.value is True:
        points.append("claimed category leadership")
    return points


def build_narrative_facts(
    dossier: DealDossier,
    analysis: ExitScenarioAnalysis,
    liquidity_path: Scenario,
    liquidity_horizon: Optional[TimeHorizon],
    liquidity_probability: float,
) -> Tuple[Dict[str, Any], List[str]]:
    """Assemble the facts the narrative may use, and the figures it may print.

    Returns ``(facts, allowed_figures)``. Anything not in ``allowed_figures`` is
    treated as invented when it appears in generated prose.
    """
    buyer_category = next(
        (
            profile.category
            for profile in analysis.acquirer_profiles
            if not profile.is_named_buyer and profile.strategic_fit is not None
        ),
        "industry incumbents",
    )

    probability_table = {
        p.horizon.label: {
            scenario.label: f"{value:.0f}%" for scenario, value in p.as_dict().items()
        }
        for p in analysis.probabilities
    }

    facts: Dict[str, Any] = {
        "company_name": dossier.company.display_name,
        "sector": dossier.company.sector.or_default("not disclosed"),
        "most_likely_outcome_overall": analysis.primary_scenario.label,
        "most_likely_outcome_horizon": (
            analysis.primary_horizon.label if analysis.primary_horizon else None
        ),
        "no_liquidity_is_the_single_most_likely_outcome": (
            analysis.primary_scenario == Scenario.NO_LIQUIDITY
        ),
        "most_plausible_liquidity_route": liquidity_path.label,
        "liquidity_route_probability": f"{liquidity_probability:.0f}%",
        "liquidity_route_horizon": (
            liquidity_horizon.label if liquidity_horizon else "the period assessed"
        ),
        "likely_buyer_category": buyer_category,
        "scenario_probabilities_by_horizon": probability_table,
        "evidenced_rationale": _rationale_points(dossier),
        "analysis_confidence": f"{analysis.confidence.score:.0f}/100 ({analysis.confidence.band})",
    }

    valuation = analysis.valuation_for(liquidity_path)
    if valuation is not None and valuation.is_defensible:
        facts["indicative_exit_range"] = (
            f"{money(valuation.low)} to {money(valuation.high)}"
        )

    allowed = _allowed_figures(dossier, analysis, facts)
    return facts, allowed


def _allowed_figures(
    dossier: DealDossier,
    analysis: ExitScenarioAnalysis,
    facts: Dict[str, Any],
) -> List[str]:
    """Every figure the deterministic analysis actually produced."""
    allowed: List[str] = []

    # Probabilities, at every horizon.
    for probabilities in analysis.probabilities:
        for value in probabilities.as_dict().values():
            allowed.append(f"{value:.0f}%")

    # Money and rates carried in the dossier.
    fin, customers, market = dossier.financials, dossier.customers, dossier.market
    money_values = [
        fin.best_revenue,
        fin.current_cash.value,
        fin.monthly_burn.value,
        fin.capital_raised_to_date.value,
        fin.amount_raising.value,
        market.tam.value,
        market.sam.value,
        analysis.capital_requirement.additional_capital_low,
        analysis.capital_requirement.additional_capital_high,
    ]
    for valuation in analysis.valuation_ranges:
        money_values.extend([valuation.low, valuation.high])
    allowed.extend(money(value) for value in money_values if value is not None)

    rate_values = [
        fin.revenue_growth_rate.value,
        fin.gross_margin.value,
        fin.ebitda_margin.value,
        customers.recurring_revenue_pct.value,
        customers.net_revenue_retention.value,
        customers.retention_rate.value,
        customers.customer_concentration.value,
        market.market_growth_rate.value,
        market.market_share.value,
        analysis.capital_requirement.estimated_dilution_low,
        analysis.capital_requirement.estimated_dilution_high,
    ]
    allowed.extend(percent(value) for value in rate_values if value is not None)

    # Counts that read as figures.
    positions = dossier.ip.total_patent_positions
    if positions is not None:
        allowed.append(str(positions))
    if customers.customer_count.is_present:
        allowed.append(f"{customers.customer_count.value:,}")

    # Anything already spelled out in the facts handed to the model.
    for value in facts.values():
        if isinstance(value, str):
            allowed.append(value)
        elif isinstance(value, list):
            allowed.extend(str(item) for item in value)

    return [item for item in allowed if item and item != "Not disclosed"]

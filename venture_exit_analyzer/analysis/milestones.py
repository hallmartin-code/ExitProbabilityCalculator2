"""Value-creation milestones.

Milestones are generated from the gaps the scoring actually found, so the list
is specific to this company rather than a generic checklist. Each is ranked by
the estimated impact on exit probability and tagged with the routes it serves.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..config.loader import load_scoring_config
from ..models.analysis import DealDossier, DimensionScore, Milestone, Scenario
from ..utils.formatting import money, percent

#: Revenue rungs used to pick the next meaningful scale milestone.
REVENUE_LADDER = [1_000_000, 5_000_000, 10_000_000, 25_000_000, 50_000_000, 100_000_000]


def _next_revenue_target(revenue: Optional[float]) -> Optional[float]:
    if revenue is None:
        return REVENUE_LADDER[1]
    for rung in REVENUE_LADDER:
        if rung > revenue * 1.35:
            return rung
    return None


def build_milestones(
    dossier: DealDossier,
    strategic: DimensionScore,
    financial: DimensionScore,
    ipo: DimensionScore,
    liquidity_risk: DimensionScore,
    config: Optional[Dict[str, Any]] = None,
) -> List[Milestone]:
    """Derive and rank the milestones that would most improve exit probability."""
    cfg = config or load_scoring_config()
    milestones: List[Milestone] = []
    fin, customers, ip, market = (
        dossier.financials,
        dossier.customers,
        dossier.ip,
        dossier.market,
    )
    revenue = fin.best_revenue

    # -- revenue scale ------------------------------------------------------
    target = _next_revenue_target(revenue)
    if target is not None:
        label = fin.best_revenue_label
        strategic_threshold = float(cfg["capital"]["exit_readiness_revenue"]["strategic_acquisition"])
        financial_threshold = float(cfg["capital"]["exit_readiness_revenue"]["financial_acquisition"])
        scenarios = [Scenario.STRATEGIC.label]
        if target >= financial_threshold:
            scenarios.append(Scenario.FINANCIAL.label)
        if target >= float(cfg["probability"]["ipo_revenue_floor"]):
            scenarios.append(Scenario.IPO.label)
        milestones.append(
            Milestone(
                milestone=f"Reach {money(target)} {label}",
                exit_impact="Very High" if target >= strategic_threshold else "High",
                relevant_scenarios=scenarios,
                rationale=(
                    f"Currently at {money(revenue)}. {money(target)} is the scale at which "
                    f"{' and '.join(scenarios).lower()} buyers engage seriously."
                    if revenue
                    else f"No revenue disclosed. {money(target)} would establish commercial proof."
                ),
                impact_rank=10.0 if target >= strategic_threshold else 8.5,
            )
        )

    # -- profitability ------------------------------------------------------
    if not fin.ebitda_margin.is_present or fin.ebitda_margin.value <= 0:
        current = (
            f"currently {percent(fin.ebitda_margin.value)}"
            if fin.ebitda_margin.is_present
            else "not disclosed"
        )
        milestones.append(
            Milestone(
                milestone="Reach EBITDA breakeven",
                exit_impact="High",
                relevant_scenarios=[Scenario.FINANCIAL.label, Scenario.IPO.label],
                rationale=(
                    f"EBITDA margin is {current}. Financial buyers underwrite cash flow and "
                    f"cannot transact without a credible path to profitability."
                ),
                impact_rank=9.0,
            )
        )

    # -- regulatory ---------------------------------------------------------
    if not ip.regulatory_approvals:
        sector = (dossier.company.sector.value or "").lower()
        if "health" in sector:
            milestones.append(
                Milestone(
                    milestone="Secure regulatory clearance or approval",
                    exit_impact="Very High",
                    relevant_scenarios=[Scenario.STRATEGIC.label],
                    rationale=(
                        "No clearance is disclosed. In regulated healthcare markets, approval "
                        "is the single largest de-risking event and is time an acquirer "
                        "cannot compress."
                    ),
                    impact_rank=9.5,
                )
            )

    # -- IP -----------------------------------------------------------------
    positions = ip.total_patent_positions
    if not positions:
        milestones.append(
            Milestone(
                milestone="File and prosecute core patent applications",
                exit_impact="High",
                relevant_scenarios=[Scenario.STRATEGIC.label],
                rationale=(
                    "No patent position is disclosed. Without defensible IP, a strategic "
                    "buyer can replicate rather than acquire."
                ),
                impact_rank=8.0,
            )
        )

    # -- customer concentration --------------------------------------------
    if customers.customer_concentration.is_present and customers.customer_concentration.value >= 0.25:
        milestones.append(
            Milestone(
                milestone="Reduce top-customer concentration below 15%",
                exit_impact="High",
                relevant_scenarios=[Scenario.FINANCIAL.label, Scenario.IPO.label],
                rationale=(
                    f"Top customer represents {percent(customers.customer_concentration.value)} "
                    f"of revenue. Concentration at this level reduces the multiple a buyer "
                    f"will pay and can fail diligence outright."
                ),
                impact_rank=8.5,
            )
        )

    # -- gross margin -------------------------------------------------------
    if fin.gross_margin.is_present and fin.gross_margin.value < 0.60:
        milestones.append(
            Milestone(
                milestone="Expand gross margin above 60%",
                exit_impact="Medium",
                relevant_scenarios=[Scenario.FINANCIAL.label, Scenario.IPO.label],
                rationale=(
                    f"Gross margin of {percent(fin.gross_margin.value)} constrains the "
                    f"achievable exit multiple across every route."
                ),
                impact_rank=6.5,
            )
        )

    # -- recurring revenue --------------------------------------------------
    if not customers.recurring_revenue_pct.is_present or customers.recurring_revenue_pct.value < 0.50:
        milestones.append(
            Milestone(
                milestone="Establish a majority-recurring revenue base",
                exit_impact="High",
                relevant_scenarios=[Scenario.FINANCIAL.label, Scenario.IPO.label],
                rationale=(
                    "Recurring revenue is the single largest determinant of the multiple a "
                    "financial buyer or public investor will pay."
                ),
                impact_rank=7.5,
            )
        )

    # -- runway -------------------------------------------------------------
    runway = fin.runway_months.value
    if runway is not None and runway < 18:
        milestones.append(
            Milestone(
                milestone="Extend runway beyond 18 months",
                exit_impact="High",
                relevant_scenarios=[Scenario.STRATEGIC.label, Scenario.FINANCIAL.label],
                rationale=(
                    f"At {runway:.0f} months, the company would negotiate any exit from a "
                    f"position of financing pressure, which depresses outcomes."
                ),
                impact_rank=8.0,
            )
        )

    # -- governance for the IPO route ---------------------------------------
    if ipo.score >= 55 and revenue and revenue >= float(cfg["probability"]["ipo_revenue_floor"]) * 0.5:
        milestones.append(
            Milestone(
                milestone="Build public-company governance and audited reporting",
                exit_impact="Medium",
                relevant_scenarios=[Scenario.IPO.label],
                rationale=(
                    "Scale is approaching the range where a listing is plausible; governance "
                    "readiness becomes the binding constraint."
                ),
                impact_rank=6.0,
            )
        )

    # -- market evidence ----------------------------------------------------
    if not market.tam.is_present:
        milestones.append(
            Milestone(
                milestone="Establish a sourced, bottom-up market size",
                exit_impact="Low",
                relevant_scenarios=[Scenario.STRATEGIC.label, Scenario.IPO.label],
                rationale="No market size was disclosed, limiting the case for scale to any buyer.",
                impact_rank=4.0,
            )
        )

    milestones.sort(key=lambda m: (m.impact_weight, m.impact_rank), reverse=True)
    return milestones

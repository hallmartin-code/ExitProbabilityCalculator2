"""Likely acquirer analysis.

The application names acquirer *categories*, which follow from the company's
sector and business model. It never invents a named buyer: a specific company
name carries an implication of research the tool has not done. Named buyers are
scored only when the user supplies them, and buyers appearing in user-supplied
comparables are surfaced as evidence, attributed to that source.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from ..models.analysis import AcquirerProfile, DealDossier, DimensionScore, Scenario
from ..models.comparables import BuyerType
from ..utils.formatting import money, percent


def _likelihood_label(score: float) -> str:
    if score >= 7.5:
        return "High"
    if score >= 5.0:
        return "Moderate"
    return "Low"


def _attractive_assets(dossier: DealDossier) -> List[str]:
    """What a buyer would actually be paying for, from disclosed evidence only."""
    assets: List[str] = []
    ip, fin, customers, market = dossier.ip, dossier.financials, dossier.customers, dossier.market

    if ip.regulatory_approvals:
        assets.append(f"Regulatory clearance ({', '.join(ip.regulatory_approvals)})")
    positions = ip.total_patent_positions
    if positions:
        assets.append(f"{positions} patent position(s)")
    if fin.best_revenue:
        assets.append(f"{money(fin.best_revenue)} of {fin.best_revenue_label.lower()}")
    if customers.customer_count.is_present:
        assets.append(f"{customers.customer_count.value:,} installed customers")
    if customers.recurring_revenue_pct.is_present and customers.recurring_revenue_pct.value >= 0.5:
        assets.append(f"{percent(customers.recurring_revenue_pct.value)} recurring revenue")
    if ip.proprietary_data.is_present:
        assets.append("Proprietary dataset")
    if market.category_leadership.value is True:
        assets.append("Category-leading position")
    return assets or ["No distinctive assets were evidenced in the source materials"]


def _concerns(dossier: DealDossier, liquidity_risk: DimensionScore) -> List[str]:
    concerns: List[str] = []
    fin, customers = dossier.financials, dossier.customers

    if customers.customer_concentration.is_present and customers.customer_concentration.value >= 0.25:
        concerns.append(
            f"Customer concentration at {percent(customers.customer_concentration.value)}"
        )
    if fin.ebitda_margin.is_present and fin.ebitda_margin.value < 0:
        concerns.append(f"Operating losses at {percent(fin.ebitda_margin.value)} EBITDA margin")
    if fin.runway_months.is_present and fin.runway_months.value < 12:
        concerns.append(f"Only {fin.runway_months.value:.0f} months of runway")
    if not dossier.ip.total_patent_positions and not dossier.ip.regulatory_approvals:
        concerns.append("No disclosed IP or regulatory barrier to entry")
    if fin.best_revenue is None:
        concerns.append("No disclosed revenue")

    for driver in liquidity_risk.top_drivers(2):
        if driver.score >= 7.0:
            concerns.append(driver.factor.replace("_", " ").capitalize())
    return concerns[:4] or ["No material concerns identified from the disclosures"]


def _strategic_categories(dossier: DealDossier) -> List[Dict[str, str]]:
    """Buyer categories appropriate to the sector, described generically."""
    sector = (dossier.company.sector.value or "").lower()
    subsector = (dossier.company.subsector.value or "").lower()

    if "health" in sector:
        if "diagnos" in subsector or "device" in subsector:
            return [
                {
                    "category": "Diagnostics and life-science instrument incumbents",
                    "rationale": "Established players acquire cleared products to extend their "
                    "menu and pull incremental volume through an installed instrument base.",
                },
                {
                    "category": "Health-system platform and EHR vendors",
                    "rationale": "Platform vendors buy clinical workflow products to deepen "
                    "their footprint inside existing hospital accounts.",
                },
                {
                    "category": "Clinical laboratory networks",
                    "rationale": "Reference laboratories acquire technology that lowers cost "
                    "per test or shortens turnaround across their own operations.",
                },
            ]
        return [
            {
                "category": "Pharmaceutical and biotechnology incumbents",
                "rationale": "Acquire clinical or platform assets to fill pipeline gaps.",
            },
            {
                "category": "Healthcare services and payor organisations",
                "rationale": "Acquire tools that reduce cost of care within their own populations.",
            },
        ]
    if "technology" in sector:
        return [
            {
                "category": "Enterprise software platform vendors",
                "rationale": "Acquire adjacent products to expand seats and wallet share "
                "within an existing enterprise customer base.",
            },
            {
                "category": "Adjacent-category technology companies",
                "rationale": "Acquire to enter a neighbouring category faster than they "
                "could build, using an existing sales motion.",
            },
            {
                "category": "Systems integrators and channel partners",
                "rationale": "Acquire capability they currently resell or subcontract.",
            },
        ]
    return [
        {
            "category": "Industry incumbents",
            "rationale": "Acquire to defend market position and absorb an emerging competitor.",
        },
        {
            "category": "Adjacent-market entrants",
            "rationale": "Acquire to enter this market with an established product and customer base.",
        },
    ]


def build_acquirer_profiles(
    dossier: DealDossier,
    strategic: DimensionScore,
    financial: DimensionScore,
    liquidity_risk: DimensionScore,
    milestones: Optional[List[str]] = None,
) -> List[AcquirerProfile]:
    """Build acquirer category profiles, plus scored entries for named buyers."""
    profiles: List[AcquirerProfile] = []
    assets = _attractive_assets(dossier)
    concerns = _concerns(dossier, liquidity_risk)
    unlocking = milestones or []

    strategic_fit = strategic.score / 10.0
    financial_fit = financial.score / 10.0

    for category in _strategic_categories(dossier):
        profiles.append(
            AcquirerProfile(
                category=category["category"],
                is_named_buyer=False,
                strategic_rationale=category["rationale"],
                attractive_assets=assets,
                concerns=concerns,
                unlocking_milestones=unlocking[:3],
                strategic_fit=round(strategic_fit, 1),
                likelihood=_likelihood_label(strategic_fit),
            )
        )

    profiles.append(
        AcquirerProfile(
            category="Private equity and growth-equity sponsors",
            is_named_buyer=False,
            strategic_rationale=(
                "Sponsors underwrite recurring revenue and a path to profitability. "
                "Relevance rises sharply once the company reaches scale with positive "
                "or near-positive EBITDA."
            ),
            attractive_assets=assets,
            concerns=concerns,
            unlocking_milestones=unlocking[:3],
            financial_capacity=round(financial_fit, 1),
            likelihood=_likelihood_label(financial_fit),
        )
    )

    # -- buyers evidenced by the user's own comparable set -------------------
    comparable_buyers = sorted(
        {
            c.buyer
            for c in dossier.comparables.comparables
            if c.buyer and c.buyer_type == BuyerType.STRATEGIC
        }
    )
    if comparable_buyers:
        profiles.append(
            AcquirerProfile(
                category="Acquirers active in the supplied comparable set",
                is_named_buyer=False,
                strategic_rationale=(
                    "These buyers completed transactions in the comparable set the user "
                    "supplied: " + ", ".join(comparable_buyers[:6]) + ". Their presence "
                    "evidences an active buyer market; it is not a claim of interest in "
                    "this company."
                ),
                attractive_assets=assets,
                concerns=concerns,
                unlocking_milestones=unlocking[:3],
                strategic_fit=round(strategic_fit, 1),
                likelihood=_likelihood_label(strategic_fit),
            )
        )

    # -- user-supplied named buyers -----------------------------------------
    for buyer in dossier.named_buyers:
        profiles.append(
            AcquirerProfile(
                category=buyer,
                is_named_buyer=True,
                strategic_rationale=(
                    f"{buyer} was identified by the user as a potential acquirer. Strategic "
                    f"fit is scored from this company's assets, not from any information "
                    f"about {buyer}'s intentions, which the application does not hold."
                ),
                attractive_assets=assets,
                concerns=concerns,
                unlocking_milestones=unlocking[:3],
                strategic_fit=round(strategic_fit, 1),
                likelihood=_likelihood_label(strategic_fit),
            )
        )

    return profiles

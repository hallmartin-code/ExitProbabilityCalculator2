"""Liquidity risk: the probability that no exit happens at all.

This dimension is scored in the opposite direction to the other three. A high
factor score means more risk. It exists because the most common venture outcome
is not a small exit -- it is no exit, and a report that omits that possibility is
not decision-support, it is advocacy.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..models.analysis import DealDossier, DimensionScore
from ..models.evidence import EvidenceClass
from ..utils.calculations import runway_months as compute_runway
from ..utils.formatting import money, percent
from .scoring_base import DriverBuilder, keyword_signal

DIMENSION = "liquidity_risk"

COMPETITIVE_PRESSURE_KEYWORDS = (
    "competitor", "competitive landscape", "incumbent", "crowded", "well-funded rival",
)
CAPITAL_INTENSITY_KEYWORDS = (
    "manufacturing", "facility", "factory", "capex", "clinical trial", "phase ii",
    "phase iii", "fleet", "infrastructure build",
)
REGULATORY_RISK_KEYWORDS = (
    "fda", "regulatory", "clinical", "approval", "reimbursement", "cms", "compliance",
)
TECH_RISK_KEYWORDS = (
    "prototype", "proof of concept", "pilot", "early stage", "research",
    "preclinical", "in development",
)


def score_liquidity_risk(
    dossier: DealDossier, deck_text: str = "", config: Optional[Dict[str, Any]] = None
) -> DimensionScore:
    builder = DriverBuilder(DIMENSION, config)
    fin, market, customers, ip = dossier.financials, dossier.market, dossier.customers, dossier.ip

    # -- funding dependency, driven by runway
    runway = fin.runway_months.value
    if runway is None:
        runway = compute_runway(fin.current_cash.value, fin.monthly_burn.value)
    if runway is not None:
        if runway == float("inf"):
            builder.score(
                "funding_dependency", 1.0,
                "Company is not consuming cash and is not dependent on further financing.",
                EvidenceClass.CALCULATED,
            )
        else:
            risk = 9.5 if runway < 6 else (8.0 if runway < 12 else (5.5 if runway < 18 else 3.0))
            builder.score(
                "funding_dependency",
                risk,
                f"Runway of {runway:.0f} months. Shorter runway raises the chance of a "
                f"down round, a distressed sale, or a wind-down before any exit.",
                EvidenceClass.CALCULATED,
            )
    else:
        builder.missing("funding_dependency", "no runway, cash or burn disclosed")

    # -- weak growth
    if fin.revenue_growth_rate.is_present:
        growth = fin.revenue_growth_rate.value
        risk = 1.5 if growth >= 0.60 else (3.5 if growth >= 0.30 else (6.0 if growth >= 0.10 else 8.5))
        builder.score(
            "weak_growth",
            risk,
            f"Revenue growth of {percent(growth)}.",
            fin.revenue_growth_rate.evidence_class,
        )
    else:
        builder.missing("weak_growth", "no growth rate disclosed or derivable")

    # -- small addressable market
    if market.tam.is_present:
        tam = market.tam.value
        risk = 2.0 if tam >= 5_000_000_000 else (4.0 if tam >= 1_000_000_000 else 7.5)
        builder.score(
            "small_market", risk, f"TAM of {money(tam)}.", market.tam.evidence_class
        )
    else:
        builder.missing("small_market", "no market size disclosed")

    builder.flag(
        "competitive_pressure",
        keyword_signal(deck_text, COMPETITIVE_PRESSURE_KEYWORDS, minimum_hits=2),
        true_score=6.0,
        false_score=5.0,
        true_evidence="Deck acknowledges a competitive field, which is credible but raises execution risk.",
        false_evidence="Competition not discussed.",
        missing_reason="competitive landscape not addressed",
    )

    if customers.customer_concentration.is_present:
        concentration = customers.customer_concentration.value
        risk = 9.0 if concentration >= 0.50 else (7.0 if concentration >= 0.30 else (4.5 if concentration >= 0.15 else 2.0))
        builder.score(
            "customer_concentration",
            risk,
            f"Top-customer concentration of {percent(concentration)}.",
            customers.customer_concentration.evidence_class,
        )
    else:
        builder.missing("customer_concentration", "customer concentration not disclosed")

    # -- regulatory risk: exposure is a risk, but an approval already in hand
    # materially retires it.
    regulatory_exposure = keyword_signal(deck_text, REGULATORY_RISK_KEYWORDS, minimum_hits=2)
    if ip.regulatory_approvals:
        builder.score(
            "regulatory_risk",
            3.0,
            f"Regulatory exposure exists but clearance is already held: "
            f"{', '.join(ip.regulatory_approvals)}.",
            EvidenceClass.EXPLICIT,
        )
    elif regulatory_exposure:
        builder.score(
            "regulatory_risk",
            7.0,
            "Deck indicates a regulated pathway with no approvals disclosed as obtained.",
            EvidenceClass.INFERRED,
        )
    else:
        builder.missing("regulatory_risk", "regulatory exposure not described")

    # -- technology risk
    revenue = fin.best_revenue
    if revenue is not None and revenue > 1_000_000:
        builder.score(
            "technology_risk",
            2.5,
            f"{money(revenue)} of revenue demonstrates the technology works in production.",
            EvidenceClass.CALCULATED,
        )
    elif keyword_signal(deck_text, TECH_RISK_KEYWORDS, minimum_hits=2):
        builder.score(
            "technology_risk",
            7.5,
            "Deck language indicates a pre-commercial or developmental technology.",
            EvidenceClass.INFERRED,
        )
    else:
        builder.missing("technology_risk", "technology maturity not established")

    # -- unit economics
    if fin.gross_margin.is_present:
        margin = fin.gross_margin.value
        risk = 2.0 if margin >= 0.70 else (4.0 if margin >= 0.50 else (6.5 if margin >= 0.30 else 8.5))
        builder.score(
            "poor_unit_economics",
            risk,
            f"Gross margin of {percent(margin)}.",
            fin.gross_margin.evidence_class,
        )
    else:
        builder.missing("poor_unit_economics", "no gross margin disclosed")

    builder.flag(
        "capital_intensity",
        keyword_signal(deck_text, CAPITAL_INTENSITY_KEYWORDS, minimum_hits=2),
        true_score=7.0,
        false_score=4.0,
        true_evidence="Business requires manufacturing, clinical or infrastructure investment before exit.",
        false_evidence="No heavy capital requirements described.",
        missing_reason="capital intensity not addressed",
    )

    # -- buyer scarcity
    comparable_count = len(dossier.comparables)
    if dossier.named_buyers:
        builder.score(
            "buyer_scarcity",
            3.0,
            f"{len(dossier.named_buyers)} potential buyer(s) identified by the user.",
            EvidenceClass.USER_SUPPLIED,
        )
    elif comparable_count >= 3:
        builder.score(
            "buyer_scarcity",
            3.5,
            f"{comparable_count} comparable transactions supplied, evidencing an active buyer market.",
            EvidenceClass.USER_SUPPLIED,
        )
    elif comparable_count > 0:
        builder.score(
            "buyer_scarcity",
            6.0,
            f"Only {comparable_count} comparable transaction(s) supplied; buyer depth is unproven.",
            EvidenceClass.USER_SUPPLIED,
        )
    else:
        builder.missing("buyer_scarcity", "no comparable transactions or named buyers supplied")

    # -- valuation expectations: last post-money against current revenue.
    post_money = dossier.funding_history.latest_post_money
    if post_money and revenue and revenue > 0:
        implied = post_money / revenue
        risk = 8.5 if implied > 40 else (6.5 if implied > 20 else (4.0 if implied > 10 else 2.5))
        builder.score(
            "valuation_expectations",
            risk,
            f"Last post-money of {money(post_money)} implies {implied:.0f}x revenue. "
            f"A high entry multiple narrows the set of buyers who can clear it.",
            EvidenceClass.CALCULATED,
        )
    else:
        builder.missing("valuation_expectations", "no prior valuation and revenue pair available")

    # -- ownership complications
    ownership = dossier.ownership
    if ownership.founder_ownership.is_present:
        founder = ownership.founder_ownership.value
        risk = 7.5 if founder < 0.15 else (5.0 if founder < 0.30 else 2.5)
        builder.score(
            "ownership_complications",
            risk,
            f"Founder ownership of {percent(founder)}. Very low founder ownership can "
            f"misalign incentives through an exit process.",
            ownership.founder_ownership.evidence_class,
        )
    elif ownership.safes.is_present or ownership.convertible_securities.is_present:
        builder.score(
            "ownership_complications",
            6.0,
            "Convertible instruments outstanding with no cap table summary provided.",
            EvidenceClass.INFERRED,
        )
    else:
        builder.missing("ownership_complications", "no cap table information disclosed")

    builder.fill_remaining()
    return builder.build(summary=_summarize(dossier, runway))


def _summarize(dossier: DealDossier, runway: Optional[float]) -> str:
    fragments = []
    if runway is not None and runway != float("inf") and runway < 12:
        fragments.append(f"only {runway:.0f} months of runway")
    if not dossier.comparables.comparables and not dossier.named_buyers:
        fragments.append("no evidenced buyer market")
    if dossier.financials.best_revenue is None:
        fragments.append("no disclosed revenue")
    if not fragments:
        return "No dominant single source of liquidity risk identified from the disclosures."
    return "Principal liquidity risks: " + ", ".join(fragments) + "."

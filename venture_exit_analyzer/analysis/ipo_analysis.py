"""IPO readiness.

The public markets impose the highest bar of the three liquidity routes, and the
scale requirement is not negotiable. This dimension uses its own revenue band so
that a company which looks strong against acquisition criteria is not flattered
into looking IPO-ready.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..models.analysis import DealDossier, DimensionScore
from ..models.comparables import BuyerType, TransactionType
from ..models.evidence import EvidenceClass
from ..utils.formatting import money, percent
from .scoring_base import DriverBuilder, keyword_signal

DIMENSION = "ipo_readiness"

GOVERNANCE_KEYWORDS = (
    "board of directors", "independent director", "audited", "audit committee",
    "gaap", "cfo", "governance", "big four",
)
VISIBILITY_KEYWORDS = (
    "category", "market leader", "industry standard", "award", "recognized",
    "press", "gartner", "analyst",
)


def score_ipo_readiness(
    dossier: DealDossier, deck_text: str = "", config: Optional[Dict[str, Any]] = None
) -> DimensionScore:
    builder = DriverBuilder(DIMENSION, config)
    fin, market, customers = dossier.financials, dossier.market, dossier.customers

    revenue = fin.best_revenue
    revenue_evidenced = fin.arr if fin.arr.is_present else fin.annual_revenue

    builder.banded(
        "revenue_scale", revenue_evidenced, "ipo_revenue_scale", fin.best_revenue_label, money
    )
    builder.banded(
        "growth_rate", fin.revenue_growth_rate, "growth_rate", "Revenue growth", lambda v: percent(v)
    )
    builder.banded("market_size", market.tam, "market_size", "TAM", money)

    if market.category_leadership.value is True:
        builder.score(
            "market_leadership", 8.5, "Deck claims category leadership.",
            market.category_leadership.evidence_class,
        )
    elif market.market_share.is_present:
        share = market.market_share.value
        score = 9.0 if share >= 0.25 else (7.0 if share >= 0.10 else 4.5)
        builder.score(
            "market_leadership",
            score,
            f"Market share of {percent(share)}.",
            market.market_share.evidence_class,
        )
    else:
        builder.missing("market_leadership", "no market share or leadership position stated")

    builder.banded("gross_margin", fin.gross_margin, "gross_margin", "Gross margin", lambda v: percent(v))

    # -- operating leverage: whether the loss is narrowing as revenue scales.
    if fin.ebitda_margin.is_present:
        margin = fin.ebitda_margin.value
        score = 9.0 if margin > 0.10 else (7.0 if margin > 0 else (5.0 if margin > -0.30 else 2.5))
        builder.score(
            "operating_leverage",
            score,
            f"EBITDA margin of {percent(margin)} indicates "
            f"{'demonstrated' if margin > 0 else 'unproven'} operating leverage.",
            fin.ebitda_margin.evidence_class,
        )
    else:
        builder.missing("operating_leverage", "no margin trajectory disclosed")

    if customers.net_revenue_retention.is_present:
        builder.banded(
            "revenue_predictability",
            customers.net_revenue_retention,
            "net_revenue_retention",
            "Net revenue retention",
            lambda v: percent(v),
        )
    else:
        builder.banded(
            "revenue_predictability",
            customers.recurring_revenue_pct,
            "recurring_revenue_pct",
            "Recurring revenue share",
            lambda v: percent(v),
        )

    builder.flag(
        "governance_readiness",
        keyword_signal(deck_text, GOVERNANCE_KEYWORDS, minimum_hits=2),
        true_score=6.5,
        false_score=3.0,
        true_evidence="Deck references board structure, audited accounts or finance leadership.",
        false_evidence="No governance infrastructure described.",
        missing_reason="public-company governance readiness not addressed",
    )

    # -- public comparables: only user-supplied IPO comparables count. The
    # application does not supply market data of its own.
    ipo_comps = [
        c
        for c in dossier.comparables.comparables
        if c.transaction_type == TransactionType.IPO or c.buyer_type == BuyerType.PUBLIC_MARKET
    ]
    if ipo_comps:
        builder.score(
            "public_comparables",
            7.5,
            f"{len(ipo_comps)} public-market comparable(s) supplied: "
            f"{', '.join(c.company for c in ipo_comps[:3])}.",
            EvidenceClass.USER_SUPPLIED,
        )
    else:
        builder.missing("public_comparables", "no public-market comparables supplied")

    # -- capital requirements: heavy remaining capital need is an IPO obstacle.
    if fin.amount_raising.is_present and revenue:
        ratio = fin.amount_raising.value / revenue if revenue > 0 else None
        if ratio is not None:
            score = 7.0 if ratio < 1.0 else (5.0 if ratio < 3.0 else 3.0)
            builder.score(
                "capital_requirements",
                score,
                f"Raising {money(fin.amount_raising.value)} against {money(revenue)} of revenue "
                f"({ratio:.1f}x), indicating the company is still capital-hungry relative to scale.",
                EvidenceClass.CALCULATED,
            )
        else:
            builder.missing("capital_requirements", "capital need could not be assessed")
    else:
        builder.missing("capital_requirements", "future capital requirements not disclosed")

    builder.flag(
        "category_visibility",
        keyword_signal(deck_text, VISIBILITY_KEYWORDS, minimum_hits=2),
        true_score=6.5,
        false_score=4.0,
        true_evidence="Deck cites external recognition or category-defining position.",
        false_evidence="No external validation of category position.",
        missing_reason="public profile not addressed",
    )

    # -- investor appetite is a market condition the deck cannot evidence, so it
    # is always scored neutral and always flagged as an unknown.
    builder.missing(
        "investor_appetite",
        "public-market appetite is a forward market condition outside the deck",
    )

    builder.fill_remaining()
    return builder.build(summary=_summarize(dossier, config))


def _summarize(dossier: DealDossier, config: Optional[Dict[str, Any]] = None) -> str:
    from ..config.loader import load_scoring_config

    cfg = config or load_scoring_config()
    floor = float(cfg["probability"]["ipo_revenue_floor"])
    revenue = dossier.financials.best_revenue
    if revenue is None:
        return "No revenue disclosed; IPO readiness cannot be assessed."
    if revenue >= floor:
        return f"At {money(revenue)}, revenue scale is within range of a public listing."
    return (
        f"At {money(revenue)}, revenue is well below the {money(floor)} scale at which a "
        f"listing becomes realistic."
    )

"""Financial (private equity) acquisition readiness.

A financial buyer underwrites cash flow, not vision. This dimension therefore
leans almost entirely on disclosed revenue, margin, retention and efficiency, and
declines to reward narrative.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..models.analysis import DealDossier, DimensionScore
from ..models.evidence import EvidencedFloat, EvidenceClass
from ..utils.calculations import capital_efficiency
from ..utils.formatting import money, percent
from .scoring_base import DriverBuilder, keyword_signal

DIMENSION = "financial_acquisition"

STABILITY_KEYWORDS = (
    "regulated", "recession-resistant", "non-discretionary", "mission-critical",
    "essential", "compliance-driven", "long-term contracts", "multi-year",
)
ROLLUP_KEYWORDS = (
    "fragmented", "consolidation", "roll-up", "rollup", "add-on", "regional operators",
    "highly fragmented",
)


def score_financial_acquisition(
    dossier: DealDossier, deck_text: str = "", config: Optional[Dict[str, Any]] = None
) -> DimensionScore:
    builder = DriverBuilder(DIMENSION, config)
    fin, customers = dossier.financials, dossier.customers

    revenue = fin.best_revenue
    revenue_evidenced = fin.arr if fin.arr.is_present else fin.annual_revenue

    builder.banded("revenue_scale", revenue_evidenced, "revenue_scale", fin.best_revenue_label, money)

    # -- revenue predictability: contract length and recurring share are the
    # two disclosures that actually speak to it.
    if customers.contract_duration_months.is_present:
        duration = customers.contract_duration_months.value
        score = 9.0 if duration >= 36 else (7.5 if duration >= 24 else (6.0 if duration >= 12 else 4.0))
        builder.score(
            "revenue_predictability",
            score,
            f"Average contract duration of {duration:.0f} months.",
            customers.contract_duration_months.evidence_class,
        )
    elif customers.recurring_revenue_pct.is_present:
        builder.banded(
            "revenue_predictability",
            customers.recurring_revenue_pct,
            "recurring_revenue_pct",
            "Recurring revenue share",
            lambda v: percent(v),
        )
    else:
        builder.missing("revenue_predictability", "no contract terms or recurring share disclosed")

    builder.banded(
        "recurring_revenue",
        customers.recurring_revenue_pct,
        "recurring_revenue_pct",
        "Recurring revenue share",
        lambda v: percent(v),
    )

    builder.banded("gross_margin", fin.gross_margin, "gross_margin", "Gross margin", lambda v: percent(v))

    builder.banded(
        "ebitda_potential", fin.ebitda_margin, "ebitda_margin", "EBITDA margin", lambda v: percent(v)
    )

    builder.banded(
        "customer_concentration",
        customers.customer_concentration,
        "customer_concentration",
        "Top-customer concentration",
        lambda v: percent(v),
    )

    # -- capital efficiency
    efficiency = capital_efficiency(revenue, fin.capital_raised_to_date.value)
    if efficiency is not None:
        builder.banded(
            "capital_efficiency",
            EvidencedFloat.calculated(
                efficiency,
                f"{money(revenue)} revenue on {money(fin.capital_raised_to_date.value)} raised",
                confidence=min(
                    revenue_evidenced.confidence, fin.capital_raised_to_date.confidence
                ),
            ),
            "capital_efficiency",
            "Revenue per dollar raised",
            lambda v: f"{v:.2f}x",
        )
    else:
        builder.missing("capital_efficiency", "revenue or capital raised not disclosed")

    # -- cash generation
    if fin.ebitda_margin.is_present:
        margin = fin.ebitda_margin.value
        if margin > 0:
            score, note = 9.0, "generating cash today"
        elif margin > -0.25:
            score, note = 6.0, "approaching breakeven"
        elif margin > -0.75:
            score, note = 3.5, "materially cash-consumptive"
        else:
            score, note = 1.5, "heavily cash-consumptive"
        builder.score(
            "cash_generation",
            score,
            f"EBITDA margin of {percent(margin)} - {note}. "
            f"A financial buyer needs cash flow to service a capital structure.",
            fin.ebitda_margin.evidence_class,
        )
    elif fin.monthly_burn.is_present and fin.monthly_burn.value > 0:
        builder.score(
            "cash_generation",
            2.5,
            f"Burning {money(fin.monthly_burn.value)} per month with no profitability disclosed.",
            EvidenceClass.INFERRED,
        )
    else:
        builder.missing("cash_generation", "no profitability or burn figures disclosed")

    builder.flag(
        "market_stability",
        keyword_signal(deck_text, STABILITY_KEYWORDS, minimum_hits=1),
        true_score=7.0,
        false_score=4.5,
        true_evidence="Deck describes contracted, regulated or non-discretionary demand.",
        false_evidence="No evidence of demand stability.",
        missing_reason="demand durability not addressed",
    )

    # -- leverage capacity follows directly from EBITDA; a company without
    # positive EBITDA cannot support an acquisition debt package.
    if fin.ebitda_margin.is_present and revenue:
        ebitda = fin.ebitda_margin.value * revenue
        if ebitda >= 5_000_000:
            score, note = 9.0, "supports a conventional debt package"
        elif ebitda > 0:
            score, note = 6.0, "positive but below typical leveraged-buyout scale"
        else:
            score, note = 1.5, "cannot service acquisition debt"
        builder.score(
            "leverage_capacity",
            score,
            f"Implied EBITDA of {money(ebitda)} - {note}.",
            EvidenceClass.CALCULATED,
        )
    else:
        builder.missing("leverage_capacity", "EBITDA cannot be computed")

    builder.flag(
        "rollup_potential",
        keyword_signal(deck_text, ROLLUP_KEYWORDS, minimum_hits=1),
        true_score=7.5,
        false_score=4.5,
        true_evidence="Deck describes a fragmented market suited to a buy-and-build strategy.",
        false_evidence="No roll-up dynamic described.",
        missing_reason="market structure not described",
    )

    builder.fill_remaining()
    return builder.build(summary=_summarize(dossier))


def _summarize(dossier: DealDossier) -> str:
    fin = dossier.financials
    revenue = fin.best_revenue
    if revenue is None:
        return "No revenue disclosed; a financial buyer has nothing to underwrite."
    if fin.ebitda_margin.is_present and fin.ebitda_margin.value > 0:
        return (
            f"{money(revenue)} of revenue at a positive EBITDA margin of "
            f"{percent(fin.ebitda_margin.value)} is underwritable by a financial buyer."
        )
    return (
        f"{money(revenue)} of revenue, but without positive EBITDA a financial buyer "
        f"would require a path to profitability first."
    )

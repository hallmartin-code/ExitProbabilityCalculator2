"""Strategic acquisition readiness.

Scores how attractive the company would be to a corporate buyer: what it owns
that a buyer could not easily build, and how much time or market position that
ownership would buy them.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..models.analysis import DealDossier, DimensionScore
from ..models.evidence import EvidencedFloat, EvidenceClass
from ..utils.formatting import money
from .scoring_base import DriverBuilder, keyword_signal

DIMENSION = "strategic_acquisition"

DIFFERENTIATION_KEYWORDS = (
    "proprietary", "patented", "novel", "first-in-class", "unique", "breakthrough",
    "defensible", "moat", "differentiated",
)
DATA_KEYWORDS = (
    "dataset", "data advantage", "annotated", "training data", "data moat",
    "proprietary data", "real-world evidence", "corpus",
)
CONSOLIDATION_KEYWORDS = (
    "consolidation", "consolidating", "roll-up", "rollup", "acquisitive",
    "m&a activity", "acquirers", "strategic buyers", "recent acquisitions",
)
COMPLEMENTARITY_KEYWORDS = (
    "integrates with", "integration", "partner", "channel", "ecosystem",
    "interoperable", "plug-in", "embedded",
)
CUSTOMER_OVERLAP_KEYWORDS = (
    "enterprise", "hospital", "health system", "fortune 500", "oem",
    "distributor", "installed base", "existing customers of",
)


def score_strategic_acquisition(
    dossier: DealDossier, deck_text: str = "", config: Optional[Dict[str, Any]] = None
) -> DimensionScore:
    builder = DriverBuilder(DIMENSION, config)
    market, ip, customers, fin = dossier.market, dossier.ip, dossier.customers, dossier.financials

    # -- strategic relevance: a large, growing market is what makes a target
    # worth a corporate development team's attention in the first place.
    builder.banded(
        "strategic_relevance", market.tam, "market_size", "TAM", money
    )

    # -- technology differentiation
    differentiation = keyword_signal(deck_text, DIFFERENTIATION_KEYWORDS, minimum_hits=2)
    if market.differentiation.is_present:
        builder.score(
            "technology_differentiation",
            7.0,
            f"Differentiation stated: {market.differentiation.value}.",
            market.differentiation.evidence_class,
        )
    else:
        builder.flag(
            "technology_differentiation",
            differentiation,
            true_score=6.5,
            false_score=4.0,
            true_evidence="Deck uses substantive differentiation language (proprietary, novel, defensible).",
            false_evidence="No differentiation language found.",
            missing_reason="no explicit differentiation claim",
        )

    # -- IP defensibility
    patent_positions = ip.total_patent_positions
    if patent_positions is not None:
        source = ip.patents_granted if ip.patents_granted.is_present else ip.patent_applications
        granted = ip.patents_granted.value or 0
        pending = ip.patent_applications.value or 0
        builder.banded(
            "ip_defensibility",
            EvidencedFloat(
                value=float(patent_positions),
                source=source.source,
                source_page_or_slide=source.source_page_or_slide,
                evidence_class=source.evidence_class,
                confidence=source.confidence,
            ),
            "patent_count",
            f"{granted} granted and {pending} pending patent(s)",
            lambda v: f"{int(v)} position(s)",
        )
    elif ip.regulatory_approvals:
        # Regulatory clearance is a genuine barrier even without patents.
        builder.score(
            "ip_defensibility",
            6.0,
            f"No patent count disclosed, but regulatory clearance held: "
            f"{', '.join(ip.regulatory_approvals)}.",
            EvidenceClass.INFERRED,
        )
    else:
        builder.missing("ip_defensibility", "no patents or regulatory position disclosed")

    # -- customer overlap with a plausible buyer's book
    builder.flag(
        "customer_overlap",
        keyword_signal(deck_text, CUSTOMER_OVERLAP_KEYWORDS, minimum_hits=1),
        true_score=6.5,
        false_score=4.0,
        true_evidence="Sells into enterprise or institutional channels a strategic buyer would already serve.",
        false_evidence="No overlapping customer base identified.",
        missing_reason="customer channel not described",
    )

    # -- product complementarity
    builder.flag(
        "product_complementarity",
        keyword_signal(deck_text, COMPLEMENTARITY_KEYWORDS, minimum_hits=1),
        true_score=6.5,
        false_score=4.0,
        true_evidence="Product is positioned to integrate with existing platforms and workflows.",
        false_evidence="No integration or ecosystem positioning described.",
        missing_reason="product fit with buyer portfolios not described",
    )

    # -- market position
    if market.category_leadership.value is True:
        builder.score(
            "market_position", 8.5, "Deck claims category leadership.",
            market.category_leadership.evidence_class,
        )
    elif customers.customer_count.is_present:
        count = customers.customer_count.value
        position_score = 7.5 if count >= 100 else (6.0 if count >= 25 else 4.5)
        builder.score(
            "market_position",
            position_score,
            f"{count:,} customers ({customers.customer_count.describe()}).",
            customers.customer_count.evidence_class,
        )
    else:
        builder.missing("market_position", "no customer count or leadership claim")

    # -- data advantage
    builder.flag(
        "data_advantage",
        keyword_signal(deck_text, DATA_KEYWORDS, minimum_hits=1),
        true_score=7.0,
        false_score=4.0,
        true_evidence="Deck describes a proprietary data asset.",
        false_evidence="No proprietary data asset described.",
        missing_reason="no data asset described",
    )

    # -- regulatory advantage
    if ip.regulatory_approvals:
        builder.score(
            "regulatory_advantage",
            8.5,
            f"Regulatory position held: {', '.join(ip.regulatory_approvals)}. "
            f"This is time a buyer cannot compress.",
            EvidenceClass.EXPLICIT,
        )
    else:
        builder.missing("regulatory_advantage", "no approvals or clearances disclosed")

    # -- time-to-market advantage: revenue plus regulatory clearance is the
    # clearest evidence that a buyer would be purchasing years, not a prototype.
    revenue = fin.best_revenue
    if revenue is not None:
        has_regulatory = bool(ip.regulatory_approvals)
        if revenue >= 5_000_000 and has_regulatory:
            score, note = 8.5, "commercial-stage with regulatory clearance in hand"
        elif revenue >= 5_000_000:
            score, note = 7.0, "commercial-stage with proven deployments"
        elif revenue > 0:
            score, note = 5.0, "early commercial traction"
        else:
            score, note = 3.0, "pre-revenue"
        builder.score(
            "time_to_market_advantage",
            score,
            f"{fin.best_revenue_label} of {money(revenue)} - {note}.",
            EvidenceClass.CALCULATED,
        )
    else:
        builder.missing("time_to_market_advantage", "no revenue disclosed")

    # -- scarcity value
    if ip.regulatory_approvals and (patent_positions or 0) >= 3:
        builder.score(
            "scarcity_value",
            8.0,
            "Combination of granted IP and regulatory clearance makes the asset hard to replicate.",
            EvidenceClass.INFERRED,
        )
    elif patent_positions:
        builder.score(
            "scarcity_value",
            6.0,
            f"{patent_positions} patent position(s) provide partial scarcity.",
            EvidenceClass.INFERRED,
        )
    else:
        builder.missing("scarcity_value", "no evidence of a hard-to-replicate asset")

    # -- buyer consolidation trend
    builder.flag(
        "buyer_consolidation_trend",
        keyword_signal(deck_text, CONSOLIDATION_KEYWORDS, minimum_hits=1),
        true_score=7.0,
        false_score=4.5,
        true_evidence="Deck references sector consolidation or active acquirers.",
        false_evidence="No consolidation activity described.",
        missing_reason="sector M&A activity not addressed",
    )

    builder.fill_remaining()
    return builder.build(summary=_summarize(dossier))


def _summarize(dossier: DealDossier) -> str:
    parts = []
    if dossier.ip.regulatory_approvals:
        parts.append(f"holds {dossier.ip.regulatory_approvals[0]}")
    positions = dossier.ip.total_patent_positions
    if positions:
        parts.append(f"{positions} patent position(s)")
    revenue = dossier.financials.best_revenue
    if revenue:
        parts.append(f"{money(revenue)} {dossier.financials.best_revenue_label}")
    if not parts:
        return "Insufficient disclosure to characterise strategic attractiveness."
    return "Strategic appeal rests on " + ", ".join(parts) + "."

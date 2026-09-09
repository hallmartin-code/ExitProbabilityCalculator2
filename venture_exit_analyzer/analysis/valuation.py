"""Valuation ranges and valuation drivers.

Ranges are derived only from comparable transactions the user supplied. The
application holds no market data of its own and will not manufacture any: when
there are too few comparables, or no revenue to apply a multiple to, it says so
explicitly rather than producing a number that looks authoritative and is not.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..config.loader import load_scoring_config
from ..models.analysis import (
    DealDossier,
    DimensionScore,
    Scenario,
    ValuationDriver,
    ValuationRange,
)
from ..models.comparables import BuyerType
from ..utils.calculations import summarize_range
from ..utils.formatting import money, multiple, percent


def _range_for(
    dossier: DealDossier,
    scenario: Scenario,
    multiples: List[float],
    source_label: str,
    cfg: Dict[str, Any],
) -> ValuationRange:
    """Apply a comparable multiple range to the subject's revenue."""
    val_cfg = cfg["valuation"]
    minimum_comps = int(val_cfg["min_comparables_for_range"])
    revenue = dossier.financials.best_revenue

    if revenue is None:
        return ValuationRange(
            scenario=scenario,
            method="Revenue multiple",
            is_defensible=False,
            insufficient_reason="no revenue disclosed to apply a multiple to",
        )
    if revenue < float(val_cfg["min_revenue_for_valuation"]):
        return ValuationRange(
            scenario=scenario,
            method="Revenue multiple",
            is_defensible=False,
            insufficient_reason=(
                f"revenue of {money(revenue)} is below the "
                f"{money(float(val_cfg['min_revenue_for_valuation']))} floor at which a "
                f"revenue multiple is meaningful"
            ),
        )
    if len(multiples) < minimum_comps:
        return ValuationRange(
            scenario=scenario,
            method="Revenue multiple",
            is_defensible=False,
            insufficient_reason=(
                f"only {len(multiples)} usable {source_label} comparable(s) supplied; "
                f"{minimum_comps} are required"
            ),
        )

    # With a thin sample the interquartile range collapses to a band far tighter
    # than the evidence justifies, so the full spread is used instead.
    use_full_spread = len(multiples) < int(val_cfg["wide_range_below_count"])
    low_pct, high_pct = (
        (0.0, 100.0)
        if use_full_spread
        else (float(val_cfg["low_percentile"]), float(val_cfg["high_percentile"]))
    )
    bounds = summarize_range(multiples, low_pct, high_pct)
    if bounds is None:
        return ValuationRange(
            scenario=scenario,
            method="Revenue multiple",
            is_defensible=False,
            insufficient_reason="comparable multiples could not be summarised",
        )

    low_multiple, high_multiple = bounds

    # Smaller companies transact at lower multiples than the larger comparables
    # that dominate published transaction sets. Applying the raw range to a much
    # smaller subject overstates value, so a size discount is applied and named.
    discount = 1.0
    discount_note = ""
    threshold = float(val_cfg["small_company_revenue_threshold"])
    if revenue < threshold:
        discount = float(val_cfg["small_company_discount"])
        discount_note = (
            f" A {percent(1 - discount)} size discount is applied because revenue of "
            f"{money(revenue)} is below the {money(threshold)} scale typical of the "
            f"comparable set."
        )

    return ValuationRange(
        scenario=scenario,
        low=revenue * low_multiple * discount,
        high=revenue * high_multiple * discount,
        method="Comparable revenue multiple",
        basis=(
            f"{len(multiples)} {source_label} comparable(s), "
            + (
                "full observed spread"
                if use_full_spread
                else f"{int(low_pct)}th-{int(high_pct)}th percentile"
            )
            + f" of {multiple(low_multiple)}-{multiple(high_multiple)} applied to "
            f"{money(revenue)} {dossier.financials.best_revenue_label}.{discount_note}"
        ),
        is_defensible=True,
    )


def compute_valuation_ranges(
    dossier: DealDossier, config: Optional[Dict[str, Any]] = None
) -> List[ValuationRange]:
    """Build indicative ranges for the strategic and financial exit routes."""
    cfg = config or load_scoring_config()
    comparables = dossier.comparables

    strategic_multiples = [
        c.revenue_multiple
        for c in comparables.for_buyer_type(BuyerType.STRATEGIC)
        if c.has_usable_revenue_multiple
    ]
    financial_multiples = [
        c.revenue_multiple
        for c in comparables.for_buyer_type(BuyerType.FINANCIAL)
        if c.has_usable_revenue_multiple
    ]
    all_multiples = comparables.usable_revenue_multiples

    # Fall back to the full comparable set when a buyer-type subset is too thin,
    # and say which set was used.
    strategic_source, strategic_pool = (
        ("strategic", strategic_multiples)
        if len(strategic_multiples) >= int(cfg["valuation"]["min_comparables_for_range"])
        else ("all-buyer", all_multiples)
    )
    financial_source, financial_pool = (
        ("financial", financial_multiples)
        if len(financial_multiples) >= int(cfg["valuation"]["min_comparables_for_range"])
        else ("all-buyer", all_multiples)
    )

    return [
        _range_for(dossier, Scenario.STRATEGIC, strategic_pool, strategic_source, cfg),
        _range_for(dossier, Scenario.FINANCIAL, financial_pool, financial_source, cfg),
    ]


def ipo_revenue_threshold(config: Optional[Dict[str, Any]] = None) -> float:
    cfg = config or load_scoring_config()
    return float(cfg["probability"]["ipo_revenue_floor"])


def identify_valuation_drivers(
    dossier: DealDossier,
    strategic: DimensionScore,
    financial: DimensionScore,
    liquidity_risk: DimensionScore,
) -> List[ValuationDriver]:
    """Name the factors that would raise or lower an exit valuation.

    Drivers are read off the scored evidence rather than restated from the deck's
    own claims, so an unevidenced assertion never becomes a headline strength.
    """
    drivers: List[ValuationDriver] = []
    fin, customers, market, ip = (
        dossier.financials,
        dossier.customers,
        dossier.market,
        dossier.ip,
    )

    # -- positive drivers ---------------------------------------------------
    if fin.revenue_growth_rate.is_present and fin.revenue_growth_rate.value >= 0.40:
        drivers.append(
            ValuationDriver(
                driver="High revenue growth",
                direction="positive",
                evidence=f"Growth of {percent(fin.revenue_growth_rate.value)} "
                f"({fin.revenue_growth_rate.describe()}).",
                magnitude="High",
            )
        )
    if fin.gross_margin.is_present and fin.gross_margin.value >= 0.65:
        drivers.append(
            ValuationDriver(
                driver="Software-grade gross margin",
                direction="positive",
                evidence=f"Gross margin of {percent(fin.gross_margin.value)}.",
                magnitude="High",
            )
        )
    if customers.recurring_revenue_pct.is_present and customers.recurring_revenue_pct.value >= 0.60:
        drivers.append(
            ValuationDriver(
                driver="Predominantly recurring revenue",
                direction="positive",
                evidence=f"{percent(customers.recurring_revenue_pct.value)} of revenue is recurring.",
                magnitude="High",
            )
        )
    if customers.net_revenue_retention.is_present and customers.net_revenue_retention.value >= 1.10:
        drivers.append(
            ValuationDriver(
                driver="Net revenue expansion within the installed base",
                direction="positive",
                evidence=f"Net revenue retention of {percent(customers.net_revenue_retention.value)}.",
                magnitude="High",
            )
        )
    if ip.regulatory_approvals:
        drivers.append(
            ValuationDriver(
                driver="Regulatory clearance already held",
                direction="positive",
                evidence=f"{', '.join(ip.regulatory_approvals)} - time an acquirer cannot compress.",
                magnitude="High",
            )
        )
    positions = ip.total_patent_positions
    if positions and positions >= 3:
        drivers.append(
            ValuationDriver(
                driver="Granted IP position",
                direction="positive",
                evidence=f"{positions} patent position(s) disclosed.",
                magnitude="Medium",
            )
        )
    if market.category_leadership.value is True:
        drivers.append(
            ValuationDriver(
                driver="Category leadership",
                direction="positive",
                evidence="Leadership position claimed in the deck.",
                magnitude="Medium",
            )
        )
    if len(dossier.comparables) >= 3:
        drivers.append(
            ValuationDriver(
                driver="Multiple credible buyers evidenced",
                direction="positive",
                evidence=f"{len(dossier.comparables)} comparable transactions supplied.",
                magnitude="Medium",
            )
        )

    # -- negative drivers ---------------------------------------------------
    if customers.customer_concentration.is_present and customers.customer_concentration.value >= 0.25:
        drivers.append(
            ValuationDriver(
                driver="Customer concentration",
                direction="negative",
                evidence=f"Top customer represents {percent(customers.customer_concentration.value)} of revenue.",
                magnitude="High",
            )
        )
    if fin.ebitda_margin.is_present and fin.ebitda_margin.value < -0.30:
        drivers.append(
            ValuationDriver(
                driver="Deeply negative operating margin",
                direction="negative",
                evidence=f"EBITDA margin of {percent(fin.ebitda_margin.value)} requires further financing before exit.",
                magnitude="High",
            )
        )
    elif not fin.ebitda_margin.is_present:
        drivers.append(
            ValuationDriver(
                driver="Profitability not disclosed",
                direction="negative",
                evidence="No EBITDA or operating margin was provided, limiting what a buyer can underwrite.",
                magnitude="Medium",
            )
        )
    runway = fin.runway_months.value
    if runway is not None and runway < 12:
        drivers.append(
            ValuationDriver(
                driver="Short cash runway",
                direction="negative",
                evidence=f"{runway:.0f} months of runway weakens negotiating position in a sale process.",
                magnitude="High",
            )
        )
    if fin.gross_margin.is_present and fin.gross_margin.value < 0.40:
        drivers.append(
            ValuationDriver(
                driver="Low gross margin",
                direction="negative",
                evidence=f"Gross margin of {percent(fin.gross_margin.value)} limits the achievable multiple.",
                magnitude="High",
            )
        )
    if fin.revenue_growth_rate.is_present and fin.revenue_growth_rate.value < 0.15:
        drivers.append(
            ValuationDriver(
                driver="Slow revenue growth",
                direction="negative",
                evidence=f"Growth of {percent(fin.revenue_growth_rate.value)} is below the level that attracts premium multiples.",
                magnitude="High",
            )
        )
    if not dossier.comparables.comparables:
        drivers.append(
            ValuationDriver(
                driver="No comparable transactions supplied",
                direction="negative",
                evidence="Valuation cannot be triangulated against market evidence.",
                magnitude="Medium",
            )
        )
    if fin.best_revenue is None:
        drivers.append(
            ValuationDriver(
                driver="No disclosed revenue",
                direction="negative",
                evidence="Without revenue, no multiple-based valuation is possible.",
                magnitude="High",
            )
        )

    # Weakest scored factors add colour the rule set above may not have caught.
    for weak in liquidity_risk.top_drivers(2):
        if weak.score >= 7.0:
            drivers.append(
                ValuationDriver(
                    driver=weak.factor.replace("_", " ").capitalize(),
                    direction="negative",
                    evidence=weak.evidence,
                    magnitude="Medium",
                )
            )

    return _dedupe(drivers)


def _dedupe(drivers: List[ValuationDriver]) -> List[ValuationDriver]:
    seen = set()
    unique: List[ValuationDriver] = []
    for driver in drivers:
        key = driver.driver.lower()
        if key not in seen:
            seen.add(key)
            unique.append(driver)
    return unique

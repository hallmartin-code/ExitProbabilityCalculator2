"""Capital required before a plausible liquidity event.

The estimate is built from disclosed burn and the revenue scale each exit route
requires. Where the inputs are not there, the model says the estimate cannot be
made rather than producing a range that rests on nothing.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..config.loader import load_scoring_config
from ..models.analysis import CapitalRequirement, DealDossier, Scenario
from ..utils.calculations import (
    compound_dilution,
    implied_monthly_burn,
    runway_months as compute_runway,
    years_to_target_revenue,
)
from ..utils.formatting import money, months


def _resolve_burn(dossier: DealDossier) -> tuple[Optional[float], Optional[str]]:
    """Monthly burn, taken as disclosed or derived from cash and runway."""
    fin = dossier.financials
    if fin.monthly_burn.is_present and fin.monthly_burn.value > 0:
        # Cite the location plainly. The full provenance string is kept in the
        # dossier for the UI and JSON export, but it reads as debug output on a
        # printed investor page.
        page = fin.monthly_burn.source_page_or_slide
        where = f" (source: page/slide {page})" if page is not None else ""
        return (
            fin.monthly_burn.value,
            f"Monthly burn of {money(fin.monthly_burn.value)} as disclosed{where}.",
        )
    derived = implied_monthly_burn(fin.current_cash.value, fin.runway_months.value)
    if derived:
        return derived, (
            f"Monthly burn of {money(derived)} derived from {money(fin.current_cash.value)} "
            f"of cash over {months(fin.runway_months.value)} of stated runway."
        )
    return None, None


def _resolve_runway(dossier: DealDossier) -> Optional[float]:
    fin = dossier.financials
    if fin.runway_months.is_present:
        return fin.runway_months.value
    return compute_runway(fin.current_cash.value, fin.monthly_burn.value)


def estimate_capital_to_exit(
    dossier: DealDossier,
    target_scenario: Scenario = Scenario.STRATEGIC,
    config: Optional[Dict[str, Any]] = None,
) -> CapitalRequirement:
    """Estimate additional capital required to reach exit readiness.

    The method: work out how long the company needs to reach the revenue scale
    its most likely exit route requires, then fund that period at the current
    burn plus a widening factor for the growth that must be paid for along the way.
    """
    cfg = config or load_scoring_config()
    cap_cfg = cfg["capital"]
    fin = dossier.financials

    burn, burn_note = _resolve_burn(dossier)
    runway = _resolve_runway(dossier)
    assumptions: List[str] = []

    requirement = CapitalRequirement(
        current_cash=fin.current_cash.value,
        runway_months=runway if runway != float("inf") else None,
        monthly_burn=burn,
    )

    if burn is None:
        requirement.is_estimable = False
        requirement.assumptions = [
            "Additional capital to exit readiness could not be estimated: neither a "
            "burn rate nor a cash-and-runway pair was disclosed."
        ]
        return requirement

    if burn_note:
        assumptions.append(burn_note)

    # -- how long until the exit route becomes plausible --------------------
    scenario_key = {
        Scenario.STRATEGIC: "strategic_acquisition",
        Scenario.FINANCIAL: "financial_acquisition",
        Scenario.IPO: "ipo",
    }.get(target_scenario, "strategic_acquisition")

    target_revenue = float(cap_cfg["exit_readiness_revenue"][scenario_key])
    default_years = float(cap_cfg["default_years_to_readiness"])
    revenue = fin.best_revenue
    growth = fin.revenue_growth_rate.value

    # Compounding a triple-digit growth rate forward produces a flattering
    # timeline and therefore an understated capital requirement. Cap it.
    growth_cap = float(cap_cfg["max_projection_growth"])
    capped_growth = growth
    growth_was_capped = False
    if growth is not None and growth > growth_cap:
        capped_growth = growth_cap
        growth_was_capped = True

    years = years_to_target_revenue(revenue, target_revenue, capped_growth, default_years)

    if years is None:
        # Not growing and below the target: no amount of runway alone gets there.
        requirement.is_estimable = False
        requirement.assumptions = assumptions + [
            f"Capital to exit readiness cannot be estimated: revenue of {money(revenue)} "
            f"is below the {money(target_revenue)} typically required for a "
            f"{target_scenario.label.lower()}, and the disclosed growth rate is not "
            f"positive, so the target is not reachable on the current trajectory."
        ]
        return requirement

    if revenue is None:
        assumptions.append(
            f"No revenue disclosed, so a default {default_years:.1f}-year path to exit "
            f"readiness is assumed. This is a placeholder, not a company-specific estimate."
        )
    elif growth is None:
        assumptions.append(
            f"No growth rate available, so a default {default_years:.1f}-year path to "
            f"{money(target_revenue)} of revenue is assumed."
        )
    else:
        growth_note = (
            f" The disclosed growth rate of {growth:.0%} is capped at {capped_growth:.0%} "
            f"for this projection; sustaining it for multiple years is not assumed."
            if growth_was_capped
            else ""
        )
        assumptions.append(
            f"At {money(revenue)} of revenue growing {capped_growth:.0%} annually, reaching the "
            f"{money(target_revenue)} scale typical of a {target_scenario.label.lower()} "
            f"takes approximately {years:.1f} years.{growth_note}"
        )

    # Cap the projection: beyond a decade the compounding assumption stops being
    # informative and the estimate would be spurious.
    years = min(years, 10.0)

    # Reaching exit-readiness revenue is not the same as being paid for it. The
    # company must also be funded through the sale or listing process itself.
    process_months = float(cap_cfg["exit_process_months"])
    total_months = years * 12.0 + process_months
    available_runway = runway if runway and runway != float("inf") else 0.0
    months_to_fund = max(total_months - available_runway, 0.0)
    assumptions.append(
        f"A further {process_months:.0f} months is added for the exit process itself "
        f"(engagement, diligence, negotiation and close), which the company must also fund."
    )

    if months_to_fund <= 0:
        requirement.is_estimable = True
        requirement.additional_capital_low = 0.0
        requirement.additional_capital_high = 0.0
        requirement.expected_rounds = 0
        requirement.assumptions = assumptions + [
            f"Existing runway of {months(runway)} already covers the estimated "
            f"{total_months:.0f} months to exit readiness and completion."
        ]
        return requirement

    # Burn rises as the company scales; the low/high factors bracket that.
    central = burn * months_to_fund
    low = central * float(cap_cfg["range_low_factor"])
    high = central * float(cap_cfg["range_high_factor"])

    round_low = burn * 12.0 * float(cap_cfg["round_size_burn_multiple_low"])
    expected_rounds = max(1, int(round(high / round_low))) if round_low > 0 else 1
    expected_rounds = min(expected_rounds, 4)

    dilution_low = compound_dilution(expected_rounds, float(cap_cfg["dilution_per_round_low"]))
    dilution_high = compound_dilution(expected_rounds, float(cap_cfg["dilution_per_round_high"]))

    assumptions.append(
        f"Funding {months_to_fund:.0f} further months at {money(burn)} per month, net of "
        f"{months(runway) if runway else 'no'} existing runway, with a "
        f"{float(cap_cfg['range_low_factor']):.2f}x-{float(cap_cfg['range_high_factor']):.2f}x "
        f"band to reflect that burn rises as the company scales."
    )
    assumptions.append(
        f"Implies approximately {expected_rounds} further financing round(s) and "
        f"{dilution_low:.0%}-{dilution_high:.0%} additional dilution to existing holders."
    )
    if fin.amount_raising.is_present:
        assumptions.append(
            f"The current {money(fin.amount_raising.value)} raise is included within this range."
        )

    requirement.is_estimable = True
    requirement.additional_capital_low = low
    requirement.additional_capital_high = high
    requirement.expected_rounds = expected_rounds
    requirement.estimated_dilution_low = dilution_low
    requirement.estimated_dilution_high = dilution_high
    requirement.assumptions = assumptions
    return requirement

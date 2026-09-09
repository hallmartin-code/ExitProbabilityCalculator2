"""Report content preparation.

Turns an analysis into display-ready strings, rows and blocks. Deliberately free
of any ReportLab dependency so the content decisions -- what is shown, what is
truncated, what is said when data is absent -- are testable on their own.

The one-page constraint is enforced here by hard caps from ``report_config.yaml``
rather than by letting the renderer overflow and hoping.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..models.analysis import (
    DealDossier,
    ExitScenarioAnalysis,
    Scenario,
    TimeHorizon,
)
from ..utils.formatting import (
    NOT_DISCLOSED,
    money,
    money_range,
    months as fmt_months,
    percent,
    truncate,
)

#: Row order for the scenario outlook table.
SCENARIO_ROWS = [
    Scenario.STRATEGIC,
    Scenario.FINANCIAL,
    Scenario.IPO,
    Scenario.NO_LIQUIDITY,
]


@dataclass
class ReportContent:
    """Everything the renderer needs, already formatted and length-capped."""

    company_name: str
    header_fields: List[Tuple[str, str]] = field(default_factory=list)
    outlook_header: List[str] = field(default_factory=list)
    outlook_rows: List[List[str]] = field(default_factory=list)
    most_likely_path: str = ""
    confidence_line: str = ""
    exit_profile: str = ""
    milestones: List[Tuple[str, str, str]] = field(default_factory=list)
    capital_lines: List[Tuple[str, str]] = field(default_factory=list)
    capital_note: str = ""
    upside: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)
    valuation_lines: List[Tuple[str, str]] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)
    estimate_caption: str = ""
    disclaimer: str = ""
    footer: str = ""
    truncation_notes: List[str] = field(default_factory=list)


def _header_fields(dossier: DealDossier, analysis: ExitScenarioAnalysis) -> List[Tuple[str, str]]:
    company, fin = dossier.company, dossier.financials

    sector = company.sector.value or NOT_DISCLOSED
    if company.subsector.is_present and company.sector.is_present:
        sector = f"{company.sector.value} / {company.subsector.value}"

    stage = (
        company.current_financing_stage.value
        or (company.stage_value.label if company.stage.is_present else NOT_DISCLOSED)
    )

    revenue = fin.best_revenue
    revenue_display = (
        f"{money(revenue)} {fin.best_revenue_label}" if revenue is not None else NOT_DISCLOSED
    )

    return [
        ("Company", company.display_name),
        ("Sector", sector),
        ("Stage", stage),
        ("Current Revenue", revenue_display),
        ("Growth", percent(fin.revenue_growth_rate.value)),
        ("Capital Raised", money(fin.capital_raised_to_date.value)),
        ("Current Raise", money(fin.amount_raising.value)),
    ]


def _outlook_table(
    analysis: ExitScenarioAnalysis, max_horizons: int
) -> Tuple[List[str], List[List[str]], List[str]]:
    """Scenario x horizon probability grid."""
    notes: List[str] = []
    horizons = [p.horizon for p in analysis.probabilities]
    if len(horizons) > max_horizons:
        # Keep the longest horizons, which carry the substantive conclusion.
        horizons = horizons[-max_horizons:]
        notes.append(
            f"Report shows the {max_horizons} longest selected horizons; the full set is "
            f"in the JSON export."
        )

    header = ["Scenario"] + [h.label for h in horizons]
    rows: List[List[str]] = []
    for scenario in SCENARIO_ROWS:
        row = [scenario.label]
        for horizon in horizons:
            probabilities = analysis.probabilities_for(horizon)
            row.append(
                f"{probabilities.as_dict()[scenario]:.0f}%" if probabilities else NOT_DISCLOSED
            )
        rows.append(row)
    return header, rows, notes


def _capital_lines(analysis: ExitScenarioAnalysis) -> Tuple[List[Tuple[str, str]], str]:
    capital = analysis.capital_requirement
    lines: List[Tuple[str, str]] = [
        ("Current runway", fmt_months(capital.runway_months)),
        ("Monthly burn", money(capital.monthly_burn)),
    ]

    if capital.is_estimable:
        lines.append(
            (
                "Additional capital",
                money_range(capital.additional_capital_low, capital.additional_capital_high),
            )
        )
        if capital.expected_rounds is not None:
            rounds = (
                "None required"
                if capital.expected_rounds == 0
                else f"{capital.expected_rounds} further round(s)"
            )
            lines.append(("Expected financing", rounds))
        if capital.estimated_dilution_low is not None and capital.estimated_dilution_high is not None:
            lines.append(
                (
                    "Implied dilution",
                    f"{percent(capital.estimated_dilution_low)}-"
                    f"{percent(capital.estimated_dilution_high)}",
                )
            )
        note = "Directional estimate based on disclosed burn and the revenue scale the likely exit route requires."
    else:
        lines.append(("Additional capital", "Not estimable"))
        note = (
            capital.assumptions[0]
            if capital.assumptions
            else "Insufficient information to estimate capital to exit readiness."
        )
    return lines, note


def _valuation_lines(
    analysis: ExitScenarioAnalysis, ipo_threshold: float, insufficient_text: str
) -> List[Tuple[str, str]]:
    lines: List[Tuple[str, str]] = []
    for scenario in (Scenario.STRATEGIC, Scenario.FINANCIAL):
        valuation = analysis.valuation_for(scenario)
        label = f"{scenario.label.replace(' Acquisition', '')} Exit"
        if valuation is None:
            lines.append((label, insufficient_text))
        elif valuation.is_defensible:
            lines.append((label, money_range(valuation.low, valuation.high)))
        else:
            lines.append((label, insufficient_text))
    lines.append(("IPO Readiness Threshold", f"{money(ipo_threshold)}+ revenue"))
    return lines


#: Openings of the framing statements that the page already makes elsewhere --
#: in the "not a forecast" caption and the disclaimer. Repeating them in the
#: basis section would spend scarce page space saying the same thing twice.
_GENERIC_OPENINGS = (
    "probabilities are directional estimates",
    "this analysis is decision support",
    "factors not disclosed in the source materials",
)


def _page_worthy(items: List[str], limit: int, char_limit: int) -> List[str]:
    """Pick the most company-specific items for the page.

    Generic framing is dropped first; if that leaves nothing, the generic items
    are used rather than showing an empty section.
    """
    specific = [
        item
        for item in items
        if not item.strip().lower().startswith(_GENERIC_OPENINGS)
    ]
    chosen = specific or items
    return [truncate(item, char_limit) for item in chosen[:limit]]


def build_report_content(
    dossier: DealDossier,
    analysis: ExitScenarioAnalysis,
    report_config: Dict[str, Any],
    ipo_threshold: float,
) -> ReportContent:
    """Assemble the one-page report content."""
    limits = report_config["limits"]
    boilerplate = report_config["boilerplate"]
    notes: List[str] = []

    header, rows, outlook_notes = _outlook_table(analysis, int(limits["max_horizons_shown"]))
    notes.extend(outlook_notes)

    # -- most likely path ---------------------------------------------------
    horizon_label = (
        analysis.primary_horizon.label if analysis.primary_horizon else "the period assessed"
    )
    if analysis.primary_scenario == Scenario.NO_LIQUIDITY:
        most_likely = f"No liquidity event within {horizon_label}"
    else:
        most_likely = f"{analysis.primary_scenario.label} ({horizon_label})"

    # -- milestones ---------------------------------------------------------
    max_milestones = int(limits["max_milestones"])
    shown = analysis.milestones[:max_milestones]
    if len(analysis.milestones) > max_milestones:
        notes.append(
            f"{len(analysis.milestones) - max_milestones} further milestone(s) are in the "
            f"JSON export."
        )
    milestones = [
        (
            truncate(m.milestone, 58),
            m.exit_impact,
            truncate(" / ".join(s.replace(" Acquisition", "") for s in m.relevant_scenarios), 30),
        )
        for m in shown
    ]

    capital_lines, capital_note = _capital_lines(analysis)

    # -- valuation drivers --------------------------------------------------
    max_upside = int(limits["max_upside_drivers"])
    max_risks = int(limits["max_risk_drivers"])
    upside = [truncate(d.driver, 62) for d in analysis.positive_drivers[:max_upside]]
    risks = [truncate(d.driver, 62) for d in analysis.negative_drivers[:max_risks]]
    if not upside:
        upside = ["No positive valuation drivers were evidenced in the source materials."]
    if not risks:
        risks = ["No material negative drivers were evidenced in the source materials."]

    return ReportContent(
        company_name=dossier.company.display_name,
        header_fields=_header_fields(dossier, analysis),
        outlook_header=header,
        outlook_rows=rows,
        most_likely_path=most_likely,
        confidence_line=(
            f"{analysis.confidence.score:.0f}/100 ({analysis.confidence.band})"
        ),
        exit_profile=truncate(
            analysis.exit_profile_summary, int(limits["exit_profile_chars"])
        ),
        milestones=milestones,
        capital_lines=capital_lines,
        capital_note=truncate(capital_note, 190),
        upside=upside,
        risks=risks,
        valuation_lines=_valuation_lines(
            analysis, ipo_threshold, boilerplate["insufficient_valuation"]
        ),
        assumptions=_page_worthy(
            analysis.assumptions,
            int(limits["max_assumptions"]),
            int(limits["basis_item_chars"]),
        ),
        limitations=_page_worthy(
            analysis.limitations,
            int(limits["max_limitations"]),
            int(limits["basis_item_chars"]),
        ),
        estimate_caption=boilerplate["estimate_caption"],
        disclaimer=" ".join(str(boilerplate["disclaimer"]).split()),
        footer=boilerplate["footer"],
        truncation_notes=notes,
    )

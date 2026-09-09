"""The blank structural template for the Venture Exit Scenario Report.

This module defines the report's skeleton: every section, every row, every field,
and the format each value takes -- with no company data in it. It is the
canonical answer to "what does this document contain?".

The template is rendered through ``render_report``, the same function that
renders real analyses, so it cannot drift from actual output. If a section is
added to the layout, the template gains it too; if a field is removed, the
template loses it. A test asserts the two stay structurally identical.

Placeholder conventions
-----------------------
``[UPPER CASE IN BRACKETS]``  a text value supplied by the analysis
``$XX.XM``                    a currency amount, scaled and abbreviated
``XX%``                       a percentage, whole numbers only
``XX/100``                    a score
Every row shown is a row the renderer can produce; optional rows are marked in
the accompanying field specification rather than omitted here, so the template
shows the document at its fullest extent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config.loader import load_report_config
from .pdf_generator import render_report
from .report_layout import ReportContent

#: Default location for the generated template artefact.
DEFAULT_TEMPLATE_PATH = Path("templates/venture_exit_scenario_report_template.pdf")

# ---------------------------------------------------------------------------
# Field definitions
# Each entry is (label, placeholder). The labels are the fixed furniture of the
# document; the placeholders show the shape of the value that replaces them.
# ---------------------------------------------------------------------------

#: The metric strip beneath the masthead. Seven fields, always all present --
#: any value the analysis lacks prints as "Not disclosed" rather than vanishing,
#: so the strip never reflows.
HEADER_FIELDS: List[Tuple[str, str]] = [
    ("Company", "[COMPANY NAME]"),
    ("Sector", "[SECTOR / SUBSECTOR]"),
    # Kept short: real values ("Series A") are short, and a long placeholder
    # would wrap mid-word here where actual output never does.
    ("Stage", "[STAGE]"),
    ("Current Revenue", "$XX.XM ARR"),
    ("Growth", "XX%"),
    ("Capital Raised", "$XX.XM"),
    ("Current Raise", "$XX.XM"),
]

#: Scenario rows are fixed and exhaustive: these four outcomes are collectively
#: exhaustive and mutually exclusive, and they always sum to 100% per column.
SCENARIO_ROW_LABELS: List[str] = [
    "Strategic Acquisition",
    "Financial Acquisition",
    "IPO",
    "No Liquidity",
]

#: Horizon columns are variable in number (1 to max_horizons_shown) and in
#: label, driven by the horizons the user selects.
HORIZON_COLUMNS: List[str] = ["[HORIZON 1]", "[HORIZON 2]", "[HORIZON 3]"]

#: Capital block rows. The last three appear only when the estimate is possible;
#: otherwise "Additional capital" reads "Not estimable" and they are omitted.
CAPITAL_FIELDS: List[Tuple[str, str]] = [
    ("Current runway", "XX mo"),
    ("Monthly burn", "$XX.XM"),
    ("Additional capital", "$XXM-$XXM"),
    ("Expected financing", "[N] further round(s)"),
    ("Implied dilution", "XX%-XX%"),
]

#: Indicative exit range rows. Either bound may instead read
#: "Insufficient information for a credible valuation range."
VALUATION_FIELDS: List[Tuple[str, str]] = [
    ("Strategic Exit", "$XXM-$XXM"),
    ("Financial Exit", "$XXM-$XXM"),
    ("IPO Readiness Threshold", "$XXM+ revenue"),
]


def _numbered(placeholder: str, count: int) -> List[str]:
    return [placeholder.replace("#", str(i)) for i in range(1, count + 1)]


def build_template_content(
    report_config: Optional[Dict[str, Any]] = None,
) -> ReportContent:
    """Build the blank report structure, filled with placeholders.

    Row counts come from the same ``report_config.yaml`` limits the live report
    obeys, so the template shows the document at its maximum permitted extent.
    """
    config = report_config or load_report_config()
    limits = config["limits"]
    boilerplate = config["boilerplate"]

    horizons = HORIZON_COLUMNS[: int(limits["max_horizons_shown"])]

    outlook_rows = [
        [label] + ["XX%" for _ in horizons] for label in SCENARIO_ROW_LABELS
    ]

    milestones = [
        (f"[MILESTONE {i}]", "[IMPACT]", "[SCENARIO / SCENARIO]")
        for i in range(1, int(limits["max_milestones"]) + 1)
    ]

    return ReportContent(
        company_name="[COMPANY NAME]",
        header_fields=list(HEADER_FIELDS),
        outlook_header=["Scenario"] + horizons,
        outlook_rows=outlook_rows,
        most_likely_path="[MOST LIKELY OUTCOME] ([HORIZON])",
        confidence_line="XX/100 ([CONFIDENCE BAND])",
        exit_profile=(
            "[EXIT PROFILE NARRATIVE. States the most plausible liquidity route and "
            "its probability, the horizon in which it is most likely, the buyer "
            "category most likely to transact, and the evidenced rationale that "
            "supports it. Where the most likely outcome overall is that no liquidity "
            "event occurs, that is stated first.]"
        ),
        milestones=milestones,
        capital_lines=list(CAPITAL_FIELDS),
        capital_note=(
            "[BASIS NOTE. States what the capital estimate rests on, or why it "
            "could not be made.]"
        ),
        upside=_numbered("[POSITIVE VALUATION DRIVER #]", int(limits["max_upside_drivers"])),
        risks=_numbered("[NEGATIVE VALUATION DRIVER #]", int(limits["max_risk_drivers"])),
        valuation_lines=list(VALUATION_FIELDS),
        assumptions=_numbered("[KEY ASSUMPTION #]", int(limits["max_assumptions"])),
        limitations=_numbered("[LIMITATION #]", int(limits["max_limitations"])),
        estimate_caption=boilerplate["estimate_caption"],
        disclaimer=" ".join(str(boilerplate["disclaimer"]).split()),
        footer=boilerplate["footer"],
        truncation_notes=[],
    )


def generate_template_pdf(
    output_path: str | Path = DEFAULT_TEMPLATE_PATH,
    report_config: Optional[Dict[str, Any]] = None,
) -> Path:
    """Render the blank structural template to a one-page PDF."""
    config = report_config or load_report_config()
    return render_report(build_template_content(config), output_path, config)


if __name__ == "__main__":
    written = generate_template_pdf()
    print(f"Template written to {written}")

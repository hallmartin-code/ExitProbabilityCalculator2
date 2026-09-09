"""The blank structural template.

The template's whole value is that it describes what the application actually
produces. These tests hold it to that: it must render through the real pipeline,
carry no company data, and stay structurally identical to a live report.
"""

from __future__ import annotations

import re

import pytest
from pypdf import PdfReader

from venture_exit_analyzer.config.loader import load_report_config
from venture_exit_analyzer.reporting.pdf_generator import generate_pdf
from venture_exit_analyzer.reporting.report_template import (
    HEADER_FIELDS,
    SCENARIO_ROW_LABELS,
    build_template_content,
    generate_template_pdf,
)


@pytest.fixture
def template_content():
    return build_template_content()


@pytest.fixture
def template_text(tmp_path):
    path = generate_template_pdf(tmp_path / "template.pdf")
    return PdfReader(str(path)).pages[0].extract_text()


class TestTemplateStructure:
    def test_renders_exactly_one_page(self, tmp_path):
        path = generate_template_pdf(tmp_path / "template.pdf")
        assert len(PdfReader(str(path)).pages) == 1

    def test_contains_every_section_heading(self, template_text):
        for heading in (
            "TEN CAPITAL",
            "VENTURE EXIT SCENARIO ANALYSIS",
            "EXIT SCENARIO OUTLOOK",
            "LIKELY EXIT PROFILE",
            "VALUE CREATION MILESTONES",
            "CAPITAL TO EXIT READINESS",
            "EXIT VALUATION DRIVERS",
            "INDICATIVE EXIT RANGE",
            "ANALYTICAL BASIS",
        ):
            assert heading in template_text, f"template missing section: {heading}"

    def test_carries_the_fixed_boilerplate(self, template_text):
        assert "NOT A FORECAST" in template_text.upper()
        assert "not an investment recommendation" in template_text
        assert "Confidential" in template_text

    def test_scenario_rows_are_fixed_and_exhaustive(self, template_content):
        assert [row[0] for row in template_content.outlook_rows] == SCENARIO_ROW_LABELS

    def test_no_liquidity_row_is_always_present(self, template_content):
        assert "No Liquidity" in [row[0] for row in template_content.outlook_rows]

    def test_header_strip_has_all_seven_fields(self, template_content):
        assert len(template_content.header_fields) == 7
        assert template_content.header_fields == HEADER_FIELDS

    def test_row_counts_follow_configured_caps(self, template_content):
        limits = load_report_config()["limits"]
        assert len(template_content.milestones) == int(limits["max_milestones"])
        assert len(template_content.upside) == int(limits["max_upside_drivers"])
        assert len(template_content.risks) == int(limits["max_risk_drivers"])
        assert len(template_content.assumptions) == int(limits["max_assumptions"])
        assert len(template_content.limitations) == int(limits["max_limitations"])
        assert len(template_content.outlook_header) - 1 == int(limits["max_horizons_shown"])


class TestTemplateCarriesNoData:
    """A template that leaked real figures would be worse than no template."""

    def test_no_fixture_company_identifiers(self, template_text):
        lowered = template_text.lower()
        for leaked in ("northwind", "diagnostics", "roche", "philips", "pathai", "510(k)"):
            assert leaked not in lowered, f"template leaked company data: {leaked}"

    def test_no_concrete_currency_amounts(self, template_text):
        """Only $XX-style placeholders may appear, never a real figure."""
        amounts = re.findall(r"\$\s?\d[\d,]*(?:\.\d+)?\s*[KMB]?", template_text)
        assert not amounts, f"template contains concrete amounts: {amounts}"

    def test_no_concrete_percentages(self, template_text):
        percentages = re.findall(r"(?<![X\d])\d[\d,]*(?:\.\d+)?\s*%", template_text)
        assert not percentages, f"template contains concrete percentages: {percentages}"

    def test_no_concrete_confidence_score(self, template_text):
        assert not re.search(r"\b\d{1,3}\s*/\s*100\b", template_text)
        assert "XX/100" in template_text

    def test_placeholders_are_present_where_values_belong(self, template_text):
        for placeholder in ("[COMPANY NAME]", "[SECTOR", "[STAGE]", "XX%", "XX/100"):
            assert placeholder in template_text


class TestTemplateMatchesLiveReport:
    def test_template_matches_live_report_structure(self, dossier, analysis, tmp_path):
        """The template must describe the document the application really emits.

        Compares the fixed furniture -- section headings, scenario row labels,
        metric-strip labels and boilerplate -- between a blank template and a
        populated report. A section added to one and not the other fails here.
        """
        template = PdfReader(
            str(generate_template_pdf(tmp_path / "template.pdf"))
        ).pages[0].extract_text()
        live = PdfReader(
            str(generate_pdf(dossier, analysis, tmp_path / "live.pdf"))
        ).pages[0].extract_text()

        furniture = [
            "EXIT SCENARIO OUTLOOK",
            "LIKELY EXIT PROFILE",
            "VALUE CREATION MILESTONES",
            "CAPITAL TO EXIT READINESS",
            "EXIT VALUATION DRIVERS",
            "INDICATIVE EXIT RANGE",
            "ANALYTICAL BASIS",
            "MOST LIKELY PATH",
            "EXIT ANALYSIS CONFIDENCE",
            "KEY ASSUMPTIONS",
            "LIMITATIONS",
            "UPSIDE",
            "RISKS",
            *SCENARIO_ROW_LABELS,
            *[label for label, _ in HEADER_FIELDS],
            "Strategic Exit",
            "Financial Exit",
            "IPO Readiness Threshold",
            "Current runway",
            "Monthly burn",
            "Additional capital",
        ]
        for item in furniture:
            in_template = item.upper() in template.upper()
            in_live = item.upper() in live.upper()
            assert in_template == in_live, (
                f"'{item}' appears in the "
                f"{'template' if in_template else 'live report'} but not the other; "
                f"the template has drifted from actual output."
            )

    def test_default_template_artefact_is_current(self, tmp_path):
        """The committed template must match what the code generates today."""
        from venture_exit_analyzer.reporting.report_template import DEFAULT_TEMPLATE_PATH

        if not DEFAULT_TEMPLATE_PATH.exists():
            pytest.skip("template artefact has not been generated")

        committed = PdfReader(str(DEFAULT_TEMPLATE_PATH)).pages[0].extract_text()
        fresh = PdfReader(
            str(generate_template_pdf(tmp_path / "fresh.pdf"))
        ).pages[0].extract_text()
        assert committed.split() == fresh.split(), (
            "templates/venture_exit_scenario_report_template.pdf is stale. "
            "Regenerate with: python -m venture_exit_analyzer.reporting.report_template"
        )

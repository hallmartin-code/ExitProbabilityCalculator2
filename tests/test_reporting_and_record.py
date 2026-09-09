"""PDF generation, the JSON record, and consent handling."""

from __future__ import annotations

import json

import pytest
from pypdf import PdfReader

from venture_exit_analyzer.analysis.engine import run_analysis
from venture_exit_analyzer.config.loader import load_report_config, load_scoring_config
from venture_exit_analyzer.models.analysis import Scenario, TimeHorizon
from venture_exit_analyzer.models.record import (
    ANALYSIS_TYPE,
    ConsentBlock,
    TenCapitalRecord,
    UserBlock,
    build_record,
)
from venture_exit_analyzer.reporting.pdf_generator import PDFGenerationError, generate_pdf
from venture_exit_analyzer.reporting.report_layout import build_report_content
from venture_exit_analyzer.services.ten_capital_service import (
    ConsentError,
    MockTransport,
    export_record_json,
    send_to_ten_capital,
)


@pytest.fixture
def report_config():
    return load_report_config()


@pytest.fixture
def ipo_threshold():
    return float(load_scoring_config()["probability"]["ipo_revenue_floor"])


class TestReportLayout:
    def test_header_fields_present(self, dossier, analysis, report_config, ipo_threshold):
        content = build_report_content(dossier, analysis, report_config, ipo_threshold)
        labels = [label for label, _ in content.header_fields]
        assert labels == [
            "Company", "Sector", "Stage", "Current Revenue", "Growth",
            "Capital Raised", "Current Raise",
        ]

    def test_absent_values_read_as_not_disclosed(
        self, empty_dossier, report_config, ipo_threshold
    ):
        analysis = run_analysis(empty_dossier, [TimeHorizon.Y3_5]).analysis
        content = build_report_content(empty_dossier, analysis, report_config, ipo_threshold)
        values = [value for _, value in content.header_fields]
        assert "Not disclosed" in values
        assert "0" not in values

    def test_outlook_rows_cover_all_four_scenarios(
        self, dossier, analysis, report_config, ipo_threshold
    ):
        content = build_report_content(dossier, analysis, report_config, ipo_threshold)
        assert len(content.outlook_rows) == 4
        assert content.outlook_rows[-1][0] == "No Liquidity"

    def test_horizon_count_is_capped(self, dossier, parsed_pdf, report_config, ipo_threshold):
        analysis = run_analysis(dossier, list(TimeHorizon), parsed_pdf.full_text).analysis
        content = build_report_content(dossier, analysis, report_config, ipo_threshold)
        maximum = int(report_config["limits"]["max_horizons_shown"])
        assert len(content.outlook_header) - 1 <= maximum
        assert content.truncation_notes

    def test_valuation_states_insufficiency_explicitly(
        self, empty_dossier, report_config, ipo_threshold
    ):
        analysis = run_analysis(empty_dossier, [TimeHorizon.Y3_5]).analysis
        content = build_report_content(empty_dossier, analysis, report_config, ipo_threshold)
        assert any("Insufficient information" in value for _, value in content.valuation_lines)

    def test_milestones_are_capped(self, dossier, analysis, report_config, ipo_threshold):
        content = build_report_content(dossier, analysis, report_config, ipo_threshold)
        assert len(content.milestones) <= int(report_config["limits"]["max_milestones"])


class TestPDFGeneration:
    def test_renders_exactly_one_page(self, dossier, analysis, tmp_path):
        path = generate_pdf(dossier, analysis, tmp_path / "report.pdf")
        assert path.exists()
        assert len(PdfReader(str(path)).pages) == 1

    def test_empty_dossier_still_renders_one_page(self, empty_dossier, tmp_path):
        analysis = run_analysis(empty_dossier, list(TimeHorizon)).analysis
        path = generate_pdf(empty_dossier, analysis, tmp_path / "empty.pdf")
        assert len(PdfReader(str(path)).pages) == 1

    def test_report_contains_the_expected_sections(self, dossier, analysis, tmp_path):
        path = generate_pdf(dossier, analysis, tmp_path / "sections.pdf")
        text = PdfReader(str(path)).pages[0].extract_text()
        for heading in (
            "TEN CAPITAL",
            "VENTURE EXIT SCENARIO ANALYSIS",
            "EXIT SCENARIO OUTLOOK",
            "LIKELY EXIT PROFILE",
            "VALUE CREATION MILESTONES",
            "CAPITAL TO EXIT READINESS",
            "EXIT VALUATION DRIVERS",
            "INDICATIVE EXIT RANGE",
        ):
            assert heading in text, f"missing section: {heading}"

    def test_report_carries_the_disclaimer_and_footer(self, dossier, analysis, tmp_path):
        path = generate_pdf(dossier, analysis, tmp_path / "disclaimer.pdf")
        text = PdfReader(str(path)).pages[0].extract_text()
        assert "not an investment recommendation" in text
        assert "Confidential" in text

    def test_report_labels_the_estimate_as_directional(self, dossier, analysis, tmp_path):
        path = generate_pdf(dossier, analysis, tmp_path / "caption.pdf")
        text = PdfReader(str(path)).pages[0].extract_text()
        assert "NOT A FORECAST" in text.upper()

    def test_creates_missing_output_directory(self, dossier, analysis, tmp_path):
        path = generate_pdf(dossier, analysis, tmp_path / "nested" / "dir" / "r.pdf")
        assert path.exists()

    def test_unwritable_path_raises_a_clear_error(self, dossier, analysis, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        with pytest.raises(PDFGenerationError):
            generate_pdf(dossier, analysis, blocker / "sub" / "r.pdf")


class TestRecord:
    def test_schema_shape(self, dossier, analysis):
        record = build_record(dossier, analysis, ConsentBlock())
        assert record.analysis_type == ANALYSIS_TYPE
        payload = json.loads(export_record_json(record))
        for key in (
            "analysis_type", "user", "company", "analysis", "capital",
            "exit_scenarios", "likely_acquirer_profiles", "milestones",
            "valuation_drivers", "risks", "assumptions", "limitations",
            "source_files", "consent",
        ):
            assert key in payload

    def test_round_trips_through_validation(self, dossier, analysis):
        record = build_record(dossier, analysis, ConsentBlock())
        restored = TenCapitalRecord.model_validate(json.loads(export_record_json(record)))
        assert restored.company.name == record.company.name
        assert restored.analysis.primary_scenario == record.analysis.primary_scenario

    def test_horizons_are_keyed_by_horizon_name(self, dossier, analysis):
        record = build_record(dossier, analysis, ConsentBlock())
        assert set(record.analysis.time_horizons) == {"3_5_years", "5_7_years"}
        block = record.analysis.time_horizons["5_7_years"]
        assert block.strategic_acquisition is not None

    def test_evidence_trail_records_provenance(self, dossier, analysis):
        record = build_record(dossier, analysis, ConsentBlock())
        assert record.evidence_trail
        arr = next(e for e in record.evidence_trail if e["field"] == "financials.arr")
        assert arr["source_page_or_slide"] == 4
        assert arr["evidence_class"] == "explicitly_stated"

    def test_absent_values_are_null_not_zero(self, empty_dossier):
        analysis = run_analysis(empty_dossier, [TimeHorizon.Y3_5]).analysis
        record = build_record(empty_dossier, analysis, ConsentBlock())
        assert record.company.revenue is None
        assert record.capital.raised_to_date is None


class TestConsent:
    def test_defaults_to_no_consent(self):
        consent = ConsentBlock()
        assert not consent.analysis_consent
        assert not consent.data_storage_consent
        assert not consent.share_with_ten_capital
        assert not consent.may_transmit

    def test_transmission_blocked_without_sharing_consent(self, dossier, analysis, tmp_path):
        record = build_record(dossier, analysis, ConsentBlock(analysis_consent=True))
        with pytest.raises(ConsentError) as exc:
            send_to_ten_capital(record, MockTransport(tmp_path / "outbox"))
        assert "not been approved" in str(exc.value)
        assert not (tmp_path / "outbox").exists(), "nothing may be written without consent"

    def test_sharing_without_analysis_consent_is_blocked(self, dossier, analysis, tmp_path):
        record = build_record(dossier, analysis, ConsentBlock(share_with_ten_capital=True))
        with pytest.raises(ConsentError):
            send_to_ten_capital(record, MockTransport(tmp_path / "outbox"))

    def test_full_consent_permits_transmission(self, dossier, analysis, tmp_path):
        record = build_record(
            dossier,
            analysis,
            ConsentBlock(
                analysis_consent=True,
                data_storage_consent=True,
                share_with_ten_capital=True,
                consent_timestamp="2026-09-09T00:00:00Z",
            ),
            UserBlock(email="analyst@example.com"),
        )
        outbox = tmp_path / "outbox"
        result = send_to_ten_capital(record, MockTransport(outbox))
        assert result.success
        written = list(outbox.glob("*.json"))
        assert len(written) == 1
        payload = json.loads(written[0].read_text(encoding="utf-8"))
        assert payload["consent"]["share_with_ten_capital"] is True

    def test_local_export_is_not_gated_on_sharing(self, dossier, analysis):
        """Exporting to the user's own machine is not transmission."""
        record = build_record(dossier, analysis, ConsentBlock())
        assert json.loads(export_record_json(record))["analysis_type"] == ANALYSIS_TYPE

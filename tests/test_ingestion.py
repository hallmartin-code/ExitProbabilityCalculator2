"""Document extraction and normalisation."""

from __future__ import annotations

from pathlib import Path

import pytest

from venture_exit_analyzer.ingestion.base import (
    DocumentExtractionError,
    ExtractionMethod,
    SourceKind,
    UnsupportedFormatError,
)
from venture_exit_analyzer.ingestion.normalization import normalize_document
from venture_exit_analyzer.ingestion.pdf_parser import parse_pdf
from venture_exit_analyzer.ingestion.ppt_parser import parse_document, parse_pptx
from venture_exit_analyzer.models.company import Stage
from venture_exit_analyzer.models.evidence import EvidenceClass

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


class TestPDFExtraction:
    def test_extracts_all_pages_with_text(self, sample_pdf_path):
        doc = parse_pdf(sample_pdf_path)
        assert doc.kind == SourceKind.PDF
        assert doc.page_count == 9
        assert doc.coverage_ratio() == 1.0
        assert not doc.used_ocr

    def test_preserves_page_numbers_and_titles(self, sample_pdf_path):
        doc = parse_pdf(sample_pdf_path)
        assert doc.pages[0].number == 1
        assert doc.pages[0].title == "Northwind Diagnostics"
        assert doc.page(4).title == "Traction"

    def test_page_text_is_searchable(self, sample_pdf_path):
        doc = parse_pdf(sample_pdf_path)
        assert "ARR of $4.2M" in doc.full_text
        assert doc.pages[0].method == ExtractionMethod.TEXT_LAYER

    def test_missing_file_raises_with_remedy(self):
        with pytest.raises(DocumentExtractionError) as exc:
            parse_pdf(FIXTURE_DIR / "does_not_exist.pdf")
        assert exc.value.remedy

    def test_corrupt_pdf_reports_actionable_error(self):
        with pytest.raises(DocumentExtractionError) as exc:
            parse_pdf(FIXTURE_DIR / "corrupt.pdf")
        assert "corrupt" in str(exc.value).lower()

    def test_image_only_pdf_is_rejected_not_silently_empty(self):
        """An unreadable deck must fail loudly rather than yield an empty dossier."""
        with pytest.raises(DocumentExtractionError) as exc:
            parse_pdf(FIXTURE_DIR / "image_only.pdf", allow_ocr=False)
        assert "no text" in str(exc.value).lower()


class TestPowerPointExtraction:
    def test_extracts_slides_titles_and_notes(self, sample_pptx_path):
        doc = parse_pptx(sample_pptx_path)
        assert doc.kind == SourceKind.PPTX
        assert doc.page_count == 9
        assert doc.pages[0].title == "Northwind Diagnostics"
        assert "Speaker notes" in doc.pages[0].notes

    def test_extracts_tables(self, sample_pptx_path):
        doc = parse_pptx(sample_pptx_path)
        tables = [t for page in doc.pages for t in page.tables]
        assert tables, "expected at least one table"
        assert tables[0].rows[0] == ["Metric", "FY2023", "FY2024"]

    def test_legacy_ppt_is_detected_and_explained(self):
        with pytest.raises(UnsupportedFormatError) as exc:
            parse_pptx(FIXTURE_DIR / "legacy.ppt")
        assert "legacy" in str(exc.value).lower()

    def test_unit_name_is_slide(self, sample_pptx_path):
        assert parse_pptx(sample_pptx_path).unit_name == "slide"


class TestDispatch:
    def test_routes_by_extension(self, sample_pdf_path, sample_pptx_path):
        assert parse_document(sample_pdf_path).kind == SourceKind.PDF
        assert parse_document(sample_pptx_path).kind == SourceKind.PPTX

    def test_unknown_extension_rejected(self, tmp_path):
        path = tmp_path / "notes.docx"
        path.write_bytes(b"irrelevant")
        with pytest.raises(UnsupportedFormatError):
            parse_document(path)


class TestNormalization:
    def test_extracts_headline_financials(self, parsed_pdf):
        d = normalize_document(parsed_pdf)
        assert d.financials.arr.value == pytest.approx(4_200_000)
        assert d.financials.gross_margin.value == pytest.approx(0.78)
        assert d.financials.monthly_burn.value == pytest.approx(310_000)
        assert d.financials.runway_months.value == pytest.approx(17)
        assert d.financials.capital_raised_to_date.value == pytest.approx(11_500_000)

    def test_negative_margin_keeps_its_sign(self, parsed_pdf):
        """'EBITDA margin -45%' must not be read as +45%."""
        d = normalize_document(parsed_pdf)
        assert d.financials.ebitda_margin.value == pytest.approx(-0.45)

    def test_percentage_is_not_read_as_revenue(self, parsed_pdf):
        """'Recurring revenue is 92%' must not become $92 of annual revenue."""
        d = normalize_document(parsed_pdf)
        assert d.financials.annual_revenue.value is None
        assert d.customers.recurring_revenue_pct.value == pytest.approx(0.92)

    def test_nrr_does_not_leak_into_retention_rate(self, parsed_pdf):
        d = normalize_document(parsed_pdf)
        assert d.customers.net_revenue_retention.value == pytest.approx(1.18)
        assert d.customers.retention_rate.value is None

    def test_columnar_table_does_not_create_false_history(self, parsed_pdf):
        """A 'FY2023 FY2024' header must not pair with the row beneath it."""
        d = normalize_document(parsed_pdf)
        assert d.financials.historical_revenue == []

    def test_records_provenance(self, parsed_pdf):
        d = normalize_document(parsed_pdf)
        arr = d.financials.arr
        assert arr.evidence_class == EvidenceClass.EXPLICIT
        assert arr.source_page_or_slide == 4
        assert 0 < arr.confidence <= 1

    def test_missing_values_stay_missing_not_zero(self, parsed_pdf):
        d = normalize_document(parsed_pdf)
        assert d.ownership.founder_ownership.value is None
        assert d.ownership.founder_ownership.evidence_class == EvidenceClass.MISSING
        assert d.ownership.founder_ownership.confidence == 0.0

    def test_infers_sector_stage_and_ip(self, parsed_pdf):
        d = normalize_document(parsed_pdf)
        assert d.company.sector.value == "Healthcare"
        assert d.company.stage_value == Stage.SERIES_A
        assert d.ip.patents_granted.value == 7
        assert d.ip.patent_applications.value == 4
        assert "FDA 510(k) clearance" in d.ip.regulatory_approvals

    def test_pdf_and_pptx_yield_equivalent_dossiers(self, sample_pdf_path, sample_pptx_path):
        """The same deck in two formats must produce the same financial reading."""
        from_pdf = normalize_document(parse_document(sample_pdf_path))
        from_pptx = normalize_document(parse_document(sample_pptx_path))
        assert from_pdf.financials.arr.value == from_pptx.financials.arr.value
        assert from_pdf.financials.gross_margin.value == from_pptx.financials.gross_margin.value
        assert from_pdf.market.tam.value == from_pptx.market.tam.value

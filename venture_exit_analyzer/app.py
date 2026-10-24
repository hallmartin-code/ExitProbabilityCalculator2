"""TEN Capital -- Venture Exit Scenario Analyzer (Streamlit interface).

Run with:  streamlit run venture_exit_analyzer/app.py

The flow is deliberately sequential: upload, consent, extract, review, analyse,
report. The review step is mandatory -- the report cannot be generated until the
user has confirmed the extracted values, because an unreviewed extraction is the
most likely source of a wrong conclusion in the whole pipeline.
"""

from __future__ import annotations

import html
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import streamlit as st

# Streamlit executes this file as a bare script rather than importing it as part
# of the package, so relative imports are unavailable. Put the project root on
# the path and import absolutely, which works both under `streamlit run` and as
# `python -m venture_exit_analyzer.app`.
if __package__ in (None, ""):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from venture_exit_analyzer.analysis.engine import run_analysis
from venture_exit_analyzer.config.loader import (
    ConfigError,
    clear_config_cache,
    load_scoring_config,
)
from venture_exit_analyzer.ingestion.base import DocumentExtractionError
from venture_exit_analyzer.ingestion.normalization import normalize_document
from venture_exit_analyzer.ingestion.ppt_parser import parse_document
from venture_exit_analyzer.models.analysis import DealDossier, TimeHorizon
from venture_exit_analyzer.models.company import Stage
from venture_exit_analyzer.models.comparables import (
    BuyerType,
    ComparableExit,
    ComparableSet,
    TransactionType,
)
from venture_exit_analyzer.models.evidence import Evidenced
from venture_exit_analyzer.models.record import ConsentBlock, UserBlock, build_record
from venture_exit_analyzer.reporting.pdf_generator import PDFGenerationError, generate_pdf
from venture_exit_analyzer.ui import theme
from venture_exit_analyzer.services.claude_service import (
    FIELD_INDEX,
    ClaudeUnavailable,
    ExtractionOutcome,
    FieldSpec,
    apply_user_value,
    build_service,
    missing_field_names,
)
from venture_exit_analyzer.services.email_service import (
    EmailConsentError,
    EmailError,
    build_email_service,
)
from venture_exit_analyzer.services.ten_capital_service import (
    ConsentError,
    TransmissionError,
    export_record_json,
    send_to_ten_capital,
)
from venture_exit_analyzer.utils.formatting import money
from venture_exit_analyzer.utils.validation import (
    InputValidationError,
    load_comparables_csv,
    load_comparables_json,
)

logging.basicConfig(level=logging.INFO)

SUPPORTED_TYPES = ["pdf", "pptx", "pptm", "ppt"]


@st.cache_resource
def get_claude_service():
    """One Claude client per process. Safe to call when no key is configured."""
    return build_service()


@st.cache_resource
def get_email_service():
    """One email service per process. Safe to call when no key is configured."""
    return build_email_service()

HORIZON_LABELS = {h.label: h for h in TimeHorizon}


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------
def init_state() -> None:
    defaults: Dict[str, Any] = {
        "document": None,
        "dossier": None,
        "deck_text": "",
        "analysis_result": None,
        "reviewed": False,
        "pdf_path": None,
        "record": None,
        "horizons": [TimeHorizon.Y3_5, TimeHorizon.Y5_7],
        "consent_analysis": False,
        "consent_storage": False,
        "consent_share": False,
        "consent_timestamp": None,
        "comparable_warnings": [],
        "ai_extraction": True,
        "ai_narrative": True,
        "ai_extraction_outcome": None,
        "email_result": None,
        "email_error": None,
        "emailed_for_pdf": None,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def invalidate_downstream(reason: str = "") -> None:
    """Drop analysis outputs after an input changes.

    Stale results are worse than absent ones -- a report generated before an
    override would silently contradict the values on screen.
    """
    st.session_state["analysis_result"] = None
    st.session_state["pdf_path"] = None
    st.session_state["record"] = None
    st.session_state["email_result"] = None
    st.session_state["email_error"] = None
    st.session_state["emailed_for_pdf"] = None
    if reason:
        st.session_state["invalidation_reason"] = reason


# ---------------------------------------------------------------------------
# Editable evidenced fields
# ---------------------------------------------------------------------------
def _caption_for(evidenced: Evidenced) -> str:
    if not evidenced.is_present:
        return "Not found in the deck - enter manually if known."
    return f"Source: {evidenced.describe()}"


def evidenced_number(
    label: str,
    evidenced: Evidenced,
    key: str,
    *,
    scale: float = 1.0,
    fmt: str = "%.2f",
    help_text: str = "",
    step: float = 1.0,
) -> Evidenced:
    """Render an editable numeric field backed by an Evidenced value.

    An empty box means "not disclosed" and stays that way: it is never coerced to
    zero. A changed value is reclassified as user-supplied so the provenance
    stays truthful.
    """
    current = evidenced.value / scale if evidenced.is_present else None
    entered = st.number_input(
        label,
        value=float(current) if current is not None else None,
        key=key,
        format=fmt,
        step=step,
        help=help_text or None,
        placeholder="Not disclosed",
    )
    st.caption(_caption_for(evidenced))

    if entered is None:
        if evidenced.is_present:
            invalidate_downstream(f"{label} was cleared")
            return Evidenced.missing(note=f"{label} was cleared by the user.")
        return evidenced

    new_value = float(entered) * scale
    if not evidenced.is_present or abs(new_value - float(evidenced.value)) > 1e-9:
        invalidate_downstream(f"{label} was changed")
        return Evidenced.from_user(new_value, note=f"{label} entered or corrected by the user.")
    return evidenced


def evidenced_text(label: str, evidenced: Evidenced, key: str) -> Evidenced:
    current = evidenced.value if evidenced.is_present else ""
    entered = st.text_input(label, value=str(current), key=key, placeholder="Not disclosed")
    st.caption(_caption_for(evidenced))

    entered = entered.strip()
    if not entered:
        if evidenced.is_present:
            invalidate_downstream(f"{label} was cleared")
            return Evidenced.missing(note=f"{label} was cleared by the user.")
        return evidenced
    if not evidenced.is_present or entered != str(evidenced.value):
        invalidate_downstream(f"{label} was changed")
        return Evidenced.from_user(entered)
    return evidenced


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
def render_upload_tab() -> None:
    theme.section("Choose a deck", first=True)
    st.markdown(
        '<div class="tc-filetypes">Drag a deck here, or browse &nbsp;&middot;&nbsp; '
        "<b>.pdf</b> or <b>.pptx</b> &nbsp;&middot;&nbsp; up to 25&nbsp;MB"
        " &nbsp;&middot;&nbsp; legacy .ppt must be re-saved as .pptx</div>",
        unsafe_allow_html=True,
    )
    st.caption(
        "Extraction is deterministic: text and tables are read directly, and OCR is "
        "used only for pages that have no text layer at all."
    )

    uploaded = st.file_uploader(
        "Pitch deck",
        type=SUPPORTED_TYPES,
        accept_multiple_files=False,
        label_visibility="collapsed",
    )

    theme.section("Consent")
    st.write(
        "Uploading a document is not consent to analyse it. Each permission below "
        "is recorded separately and nothing is sent anywhere without the third."
    )

    analysis_consent = st.checkbox(
        "I have the right to analyse this document, and I consent to its analysis.",
        value=st.session_state["consent_analysis"],
        key="cb_analysis",
    )
    storage_consent = st.checkbox(
        "I consent to this analysis being retained locally for my own reference.",
        value=st.session_state["consent_storage"],
        key="cb_storage",
    )
    share_consent = st.checkbox(
        "Share this analysis with TEN Capital.",
        value=st.session_state["consent_share"],
        key="cb_share",
        help="Nothing is transmitted unless this is ticked.",
    )

    if (
        analysis_consent != st.session_state["consent_analysis"]
        or storage_consent != st.session_state["consent_storage"]
        or share_consent != st.session_state["consent_share"]
    ):
        st.session_state["consent_analysis"] = analysis_consent
        st.session_state["consent_storage"] = storage_consent
        st.session_state["consent_share"] = share_consent
        st.session_state["consent_timestamp"] = datetime.now(timezone.utc).isoformat()

    if share_consent and not analysis_consent:
        st.warning(
            "Sharing requires consent to analyse the document. Tick the first box as well."
        )

    theme.section("AI assistance")
    service = get_claude_service()
    if service.available:
        st.success(f"Claude connected. {service.config.describe()}")
        st.session_state["ai_extraction"] = st.checkbox(
            "Use Claude to fill fields the deterministic parser could not find",
            value=st.session_state["ai_extraction"],
            help=(
                "Deterministic parsing always runs first and its values are never "
                "overwritten. Claude only fills gaps, and everything it finds is "
                "marked as AI-extracted for you to confirm."
            ),
        )
        st.session_state["ai_narrative"] = st.checkbox(
            "Use Claude to write the exit-profile paragraph",
            value=st.session_state["ai_narrative"],
            help=(
                "Prose only. Scores, probabilities and valuations stay deterministic, "
                "and any generated figure not present in the computed analysis causes "
                "the narrative to be rejected."
            ),
        )
    else:
        st.info(f"AI assistance unavailable. {service.status}")
        st.caption(
            "The application runs fully without it, using deterministic parsing only."
        )

    theme.section("Extract")

    extract_disabled = uploaded is None or not analysis_consent
    if uploaded is None:
        st.info("Upload a deck to continue.")
    elif not analysis_consent:
        st.info("Consent to analyse the document is required before extraction.")

    if st.button("Extract Deck", type="primary", disabled=extract_disabled):
        _run_extraction(uploaded)

    document = st.session_state["document"]
    if document is not None:
        st.success(
            f"Extracted {document.page_count} {document.unit_name}(s) from "
            f"{document.filename} "
            f"({document.coverage_ratio():.0%} yielded text"
            f"{', OCR used' if document.used_ocr else ''})."
        )
        for warning in document.warnings:
            st.warning(warning)

        outcome = st.session_state["ai_extraction_outcome"]
        if outcome is not None:
            st.info(f"Claude ({outcome.model}): {outcome.summary()}")
            if outcome.applied:
                st.caption("Filled by Claude: " + ", ".join(outcome.applied))
            if outcome.rejected:
                with st.expander("Values Claude proposed that were rejected"):
                    for item in outcome.rejected:
                        st.text(f"- {item}")
        with st.expander("Extracted text by page/slide"):
            for page in document.pages:
                st.markdown(f"**{document.unit_name.title()} {page.number}** - {page.title or '(no title)'}")
                st.text(page.searchable_text()[:1200] or "(no text)")

        if st.session_state["dossier"] is not None:
            render_gap_editor(st.session_state["dossier"], outcome)


# ---------------------------------------------------------------------------
# Missing and rejected data points
# ---------------------------------------------------------------------------
_GAP_SECTIONS = {
    "company": "Company",
    "financials": "Financials",
    "market": "Market",
    "customers": "Customers",
    "ip": "Intellectual property",
    "ownership": "Ownership",
    "named_buyers": "Potential buyers",
}
_GAP_LABEL_OVERRIDES = {
    "arr": "ARR",
    "tam": "TAM",
    "sam": "SAM",
    "som": "SOM",
    "ebitda_margin": "EBITDA margin",
    "named_buyers": "Potential buyers",
    "stage": "Financing stage",
}
#: Display unit per field kind: (label suffix, multiplier from display to canonical).
_GAP_UNITS = {"money": (" ($M)", 1e6), "percent": (" (%)", 0.01), "integer": ("", 1.0)}


def _gap_label(spec: FieldSpec) -> str:
    attribute = spec.name.split(".")[-1]
    label = _GAP_LABEL_OVERRIDES.get(attribute, attribute.replace("_", " ").capitalize())
    if spec.kind == "list":
        return f"{label} (one per line)"
    return label + _GAP_UNITS.get(spec.kind, ("", 1.0))[0]


def _gap_input(spec: FieldSpec, help_text: str) -> Any:
    """Render one empty input for a missing field; return its canonical value or None."""
    key = f"gap_{spec.name}"
    label = _gap_label(spec)
    if spec.name == "company.stage":
        stage = st.selectbox(
            label, [None, *Stage], key=key, help=help_text,
            format_func=lambda s: "Not disclosed" if s is None else s.label,
        )
        return stage.value if stage is not None else None
    if spec.kind == "boolean":
        choice = st.selectbox(label, ["Not disclosed", "Yes", "No"], key=key, help=help_text)
        return None if choice == "Not disclosed" else choice == "Yes"
    if spec.kind == "list":
        lines = st.text_area(label, key=key, help=help_text, placeholder="Not disclosed")
        items = [line.strip() for line in lines.splitlines() if line.strip()]
        return items or None
    if spec.kind == "text":
        return st.text_input(label, key=key, help=help_text, placeholder="Not disclosed").strip() or None

    integer = spec.kind == "integer"
    entered = st.number_input(
        label, value=None, key=key, help=help_text, placeholder="Not disclosed",
        step=1.0 if integer else 0.1, format="%.0f" if integer else "%.2f",
    )
    return None if entered is None else float(entered) * _GAP_UNITS[spec.kind][1]


def _reset_review_widgets() -> None:
    """Drop Review-tab widget state so its fields reload from the dossier.

    A keyed Streamlit widget keeps its own state across reruns; left alone, the
    Review tab would still show an empty box and "clear" a value saved here.
    """
    for key in [k for k in st.session_state.keys() if str(k).startswith("f_")]:
        del st.session_state[key]


def render_gap_editor(dossier: DealDossier, outcome: Optional[ExtractionOutcome]) -> None:
    theme.section("Fill in missing data")
    feedback = st.session_state.pop("gap_feedback", None)
    if feedback:
        if feedback["saved"]:
            st.success("Saved as user-supplied: " + ", ".join(feedback["saved"]))
        for error in feedback["errors"]:
            st.error(error)

    missing = missing_field_names(dossier)
    if not missing:
        st.success("Every tracked data point has a value. Check them on the Review tab.")
        return

    rejected = outcome.rejected_fields if outcome is not None else {}
    rejected_missing = [name for name in missing if name in rejected]
    other_missing = [name for name in missing if name not in rejected]

    st.write(
        f"{len(missing)} data point(s) were not found in the deck"
        + (f", including {len(rejected_missing)} Claude proposed but were rejected" if rejected_missing else "")
        + ". Enter any you know. Saved values are marked as user-supplied, and "
        "anything left blank stays undisclosed. It is not treated as zero."
    )

    entries: Dict[str, Any] = {}
    with st.form("gap_editor"):
        if rejected_missing:
            st.markdown("**Rejected by validation**")
            for name in rejected_missing:
                spec = FIELD_INDEX[name]
                entries[name] = _gap_input(
                    spec, f"{spec.description} Claude's value was rejected: {rejected[name]}."
                )
                st.caption(f"Rejected: {rejected[name]}")

        by_section: Dict[str, List[str]] = {}
        for name in other_missing:
            by_section.setdefault(name.split(".")[0], []).append(name)
        for section, names in by_section.items():
            with st.expander(f"{_GAP_SECTIONS.get(section, section.title())} ({len(names)} missing)"):
                columns = st.columns(2)
                for index, name in enumerate(names):
                    with columns[index % 2]:
                        spec = FIELD_INDEX[name]
                        entries[name] = _gap_input(spec, spec.description)

        submitted = st.form_submit_button("Save entered values", type="primary")

    if not submitted:
        return

    saved: List[str] = []
    errors: List[str] = []
    for name, value in entries.items():
        if value is None:
            continue
        label = _gap_label(FIELD_INDEX[name])
        try:
            apply_user_value(dossier, name, value)
        except ValueError as exc:
            errors.append(f"{label}: {exc}")
            continue
        saved.append(label)

    if not saved and not errors:
        st.info("No values were entered.")
        return
    if saved:
        invalidate_downstream("missing values were entered")
        _reset_review_widgets()
    st.session_state["gap_feedback"] = {"saved": saved, "errors": errors}
    st.rerun()


def _run_extraction(uploaded) -> None:
    suffix = Path(uploaded.name).suffix or ".pdf"
    tmp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
            handle.write(uploaded.getbuffer())
            tmp_path = Path(handle.name)

        with st.spinner("Reading the deck..."):
            document = parse_document(tmp_path)
            dossier = normalize_document(document)
        dossier.source_files = [uploaded.name]

        service = get_claude_service()
        st.session_state["ai_extraction_outcome"] = None
        if st.session_state["ai_extraction"] and service.available:
            try:
                with st.spinner("Asking Claude to fill the remaining gaps..."):
                    st.session_state["ai_extraction_outcome"] = service.extract(
                        document, dossier
                    )
            except ClaudeUnavailable as exc:
                st.warning(f"AI extraction skipped: {exc}")
            except Exception as exc:  # noqa: BLE001
                logging.exception("AI extraction failed")
                st.warning(
                    f"AI extraction failed, continuing with deterministic values only: {exc}"
                )

        st.session_state["document"] = document
        st.session_state["dossier"] = dossier
        st.session_state["deck_text"] = document.full_text
        st.session_state["reviewed"] = False
        invalidate_downstream("a new deck was extracted")
        _reset_review_widgets()
        for key in [k for k in st.session_state.keys() if str(k).startswith("gap_")]:
            del st.session_state[key]
        st.rerun()

    except DocumentExtractionError as exc:
        st.error(f"{exc.message}")
        if exc.remedy:
            st.info(exc.remedy)
    except Exception as exc:  # noqa: BLE001 - surfaced to the user, not swallowed
        logging.exception("Unexpected extraction failure")
        st.error(f"The deck could not be processed: {exc}")
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def render_review_tab() -> None:
    dossier: Optional[DealDossier] = st.session_state["dossier"]
    theme.section("Review extracted values", first=True)

    if dossier is None:
        st.info(
            "No deck has been extracted yet. You can still run an analysis on manually "
            "entered values by starting a blank dossier below."
        )
        if st.button("Start a blank dossier"):
            st.session_state["dossier"] = DealDossier()
            st.session_state["reviewed"] = False
            invalidate_downstream("a blank dossier was created")
            st.rerun()
        return

    st.write(
        "Every value below shows where it came from. Correct anything the parser got "
        "wrong; corrections are recorded as user-supplied. Leave a field empty when a "
        "figure was genuinely not disclosed -- it will not be treated as zero."
    )

    for warning in dossier.extraction_warnings:
        st.warning(warning)

    company, fin = dossier.company, dossier.financials
    market, customers, ip = dossier.market, dossier.customers, dossier.ip
    ownership = dossier.ownership

    with st.expander("Company", expanded=True):
        col1, col2 = st.columns(2)
        with col1:
            company.name = evidenced_text("Company name", company.name, "f_name")
            company.sector = evidenced_text("Sector", company.sector, "f_sector")
            company.website = evidenced_text("Website", company.website, "f_web")
        with col2:
            company.subsector = evidenced_text("Subsector", company.subsector, "f_subsector")
            company.business_model = evidenced_text(
                "Business model", company.business_model, "f_model"
            )
            company.headquarters = evidenced_text("Headquarters", company.headquarters, "f_hq")

        current_stage = company.stage_value
        options = [s for s in Stage]
        chosen = st.selectbox(
            "Financing stage",
            options,
            index=options.index(current_stage),
            format_func=lambda s: s.label,
            key="f_stage",
        )
        if chosen != current_stage:
            company.stage = Evidenced.from_user(chosen)
            company.current_financing_stage = Evidenced.from_user(chosen.label)
            invalidate_downstream("the financing stage was changed")
        st.caption(_caption_for(company.stage))

    with st.expander("Financials", expanded=True):
        st.caption("Enter amounts in millions of USD, and rates as percentages.")
        col1, col2, col3 = st.columns(3)
        with col1:
            fin.arr = evidenced_number("ARR ($M)", fin.arr, "f_arr", scale=1e6, step=0.1)
            fin.annual_revenue = evidenced_number(
                "Annual revenue ($M)", fin.annual_revenue, "f_rev", scale=1e6, step=0.1
            )
            fin.revenue_growth_rate = evidenced_number(
                "Revenue growth (%)", fin.revenue_growth_rate, "f_growth", scale=0.01, step=1.0
            )
        with col2:
            fin.gross_margin = evidenced_number(
                "Gross margin (%)", fin.gross_margin, "f_gm", scale=0.01, step=1.0
            )
            fin.ebitda_margin = evidenced_number(
                "EBITDA margin (%)", fin.ebitda_margin, "f_ebitda", scale=0.01, step=1.0
            )
            fin.monthly_burn = evidenced_number(
                "Monthly burn ($M)", fin.monthly_burn, "f_burn", scale=1e6, step=0.01, fmt="%.3f"
            )
        with col3:
            fin.current_cash = evidenced_number(
                "Cash on hand ($M)", fin.current_cash, "f_cash", scale=1e6, step=0.1
            )
            fin.runway_months = evidenced_number(
                "Runway (months)", fin.runway_months, "f_runway", step=1.0, fmt="%.0f"
            )
            fin.capital_raised_to_date = evidenced_number(
                "Capital raised ($M)", fin.capital_raised_to_date, "f_raised", scale=1e6, step=0.1
            )
        fin.amount_raising = evidenced_number(
            "Amount being raised ($M)", fin.amount_raising, "f_raising", scale=1e6, step=0.1
        )

    with st.expander("Market and customers"):
        col1, col2 = st.columns(2)
        with col1:
            market.tam = evidenced_number("TAM ($M)", market.tam, "f_tam", scale=1e6, step=1.0)
            market.sam = evidenced_number("SAM ($M)", market.sam, "f_sam", scale=1e6, step=1.0)
            market.market_growth_rate = evidenced_number(
                "Market growth (%)", market.market_growth_rate, "f_mgrowth", scale=0.01, step=1.0
            )
            leadership = st.checkbox(
                "Category leader",
                value=bool(market.category_leadership.value),
                key="f_leader",
            )
            if leadership != bool(market.category_leadership.value):
                market.category_leadership = Evidenced.from_user(leadership)
                invalidate_downstream("category leadership was changed")
        with col2:
            customers.customer_count = evidenced_number(
                "Customer count", customers.customer_count, "f_cust", step=1.0, fmt="%.0f"
            )
            customers.recurring_revenue_pct = evidenced_number(
                "Recurring revenue (%)", customers.recurring_revenue_pct, "f_rec",
                scale=0.01, step=1.0,
            )
            customers.net_revenue_retention = evidenced_number(
                "Net revenue retention (%)", customers.net_revenue_retention, "f_nrr",
                scale=0.01, step=1.0,
            )
            customers.customer_concentration = evidenced_number(
                "Top-customer concentration (%)", customers.customer_concentration, "f_conc",
                scale=0.01, step=1.0,
            )

    with st.expander("Intellectual property and ownership"):
        col1, col2 = st.columns(2)
        with col1:
            ip.patents_granted = evidenced_number(
                "Patents granted", ip.patents_granted, "f_pat", step=1.0, fmt="%.0f"
            )
            ip.patent_applications = evidenced_number(
                "Patent applications", ip.patent_applications, "f_pend", step=1.0, fmt="%.0f"
            )
            approvals = st.text_area(
                "Regulatory approvals (one per line)",
                value="\n".join(ip.regulatory_approvals),
                key="f_approvals",
            )
            new_approvals = [line.strip() for line in approvals.splitlines() if line.strip()]
            if new_approvals != ip.regulatory_approvals:
                ip.regulatory_approvals = new_approvals
                invalidate_downstream("regulatory approvals were changed")
        with col2:
            ownership.founder_ownership = evidenced_number(
                "Founder ownership (%)", ownership.founder_ownership, "f_founder",
                scale=0.01, step=1.0,
            )
            buyers = st.text_area(
                "Potential buyers to score (one per line)",
                value="\n".join(dossier.named_buyers),
                key="f_buyers",
                help="Only buyers you name are scored. The application does not invent them.",
            )
            new_buyers = [line.strip() for line in buyers.splitlines() if line.strip()]
            if new_buyers != dossier.named_buyers:
                dossier.named_buyers = new_buyers
                invalidate_downstream("named buyers were changed")

    st.divider()
    missing = _missing_critical(dossier)
    if missing:
        st.warning(
            "The following are not disclosed and will be scored neutrally, which "
            "lowers analysis confidence: " + ", ".join(missing)
        )

    if st.button("Confirm inputs reviewed", type="primary"):
        st.session_state["reviewed"] = True
        st.success("Inputs confirmed. You can now run the analysis.")

    if st.session_state["reviewed"]:
        st.info("Inputs have been marked as reviewed.")


def _missing_critical(dossier: DealDossier) -> List[str]:
    checks = [
        ("revenue", dossier.financials.best_revenue is not None),
        ("revenue growth", dossier.financials.revenue_growth_rate.is_present),
        ("gross margin", dossier.financials.gross_margin.is_present),
        ("EBITDA margin", dossier.financials.ebitda_margin.is_present),
        ("monthly burn", dossier.financials.monthly_burn.is_present),
        ("TAM", dossier.market.tam.is_present),
    ]
    return [name for name, present in checks if not present]


def render_comparables_tab() -> None:
    theme.section("Comparable exit transactions", first=True)
    st.write(
        "Valuation ranges are computed only from comparables you supply. The "
        "application holds no market data of its own and will not invent transactions."
    )

    dossier: Optional[DealDossier] = st.session_state["dossier"]
    if dossier is None:
        st.info("Extract a deck or start a blank dossier first.")
        return

    uploaded = st.file_uploader(
        "Upload comparables (CSV or JSON)", type=["csv", "json"], key="comps_upload"
    )
    if uploaded is not None and st.button("Load comparables"):
        try:
            content = uploaded.getvalue()
            if uploaded.name.lower().endswith(".json"):
                comparables, warnings = load_comparables_json(content)
            else:
                comparables, warnings = load_comparables_csv(content)
            dossier.comparables = comparables
            st.session_state["comparable_warnings"] = warnings
            invalidate_downstream("comparables were loaded")
            st.success(f"Loaded {len(comparables)} comparable transaction(s).")
        except InputValidationError as exc:
            st.error(str(exc))
        except Exception as exc:  # noqa: BLE001
            logging.exception("Comparable load failed")
            st.error(f"The file could not be read: {exc}")

    for warning in st.session_state["comparable_warnings"]:
        st.warning(warning)

    st.markdown("**Add a comparable manually**")
    with st.form("manual_comparable", clear_on_submit=True):
        col1, col2, col3 = st.columns(3)
        with col1:
            name = st.text_input("Company")
            buyer = st.text_input("Buyer")
        with col2:
            buyer_type = st.selectbox(
                "Buyer type", list(BuyerType), format_func=lambda b: b.value.replace("_", " ").title()
            )
            value = st.number_input("Transaction value ($M)", min_value=0.0, value=0.0, step=1.0)
        with col3:
            revenue = st.number_input("Revenue at exit ($M)", min_value=0.0, value=0.0, step=0.1)
            ebitda = st.number_input("EBITDA at exit ($M)", value=0.0, step=0.1)

        if st.form_submit_button("Add comparable"):
            if not name.strip():
                st.error("A company name is required.")
            elif value <= 0 or revenue <= 0:
                st.error(
                    "A transaction value and revenue at exit are both required to "
                    "compute a usable multiple."
                )
            else:
                dossier.comparables.comparables.append(
                    ComparableExit(
                        company=name.strip(),
                        buyer=buyer.strip() or None,
                        buyer_type=buyer_type,
                        transaction_type=TransactionType.ACQUISITION,
                        transaction_value=value * 1e6,
                        revenue_at_exit=revenue * 1e6,
                        ebitda_at_exit=ebitda * 1e6 if ebitda else None,
                    )
                )
                invalidate_downstream("a comparable was added")
                st.success(f"Added {name.strip()}.")

    if dossier.comparables.comparables:
        st.markdown("**Current comparable set**")
        st.dataframe(
            [
                {
                    "Company": c.company,
                    "Buyer": c.buyer or "-",
                    "Type": c.buyer_type.value,
                    "Value": money(c.transaction_value),
                    "Revenue": money(c.revenue_at_exit),
                    "Rev multiple": f"{c.revenue_multiple:.1f}x" if c.revenue_multiple else "-",
                }
                for c in dossier.comparables.comparables
            ],
            width="stretch",
            hide_index=True,
        )
        if st.button("Clear all comparables"):
            dossier.comparables = ComparableSet()
            st.session_state["comparable_warnings"] = []
            invalidate_downstream("comparables were cleared")
            st.rerun()
    else:
        st.info(
            "No comparables supplied. The report will state that no credible valuation "
            "range could be produced."
        )


def render_analysis_tab() -> None:
    theme.section("Run the exit analysis", first=True)
    dossier: Optional[DealDossier] = st.session_state["dossier"]

    if dossier is None:
        st.info("Extract a deck or start a blank dossier first.")
        return

    selected = st.multiselect(
        "Time horizons",
        list(HORIZON_LABELS),
        default=[h.label for h in st.session_state["horizons"]],
        key="horizon_select",
    )
    horizons = [HORIZON_LABELS[label] for label in selected]
    if horizons != st.session_state["horizons"]:
        st.session_state["horizons"] = horizons
        invalidate_downstream("time horizons were changed")

    if not horizons:
        st.warning("Select at least one time horizon.")
        return

    if not st.session_state["reviewed"]:
        st.warning(
            "Confirm the extracted inputs on the Review Inputs tab before running the "
            "analysis. Running on unreviewed extraction is the most common cause of a "
            "wrong conclusion."
        )

    if st.button(
        "Run Exit Analysis", type="primary", disabled=not st.session_state["reviewed"]
    ):
        try:
            service = get_claude_service()
            use_ai = st.session_state["ai_narrative"] and service.available
            with st.spinner("Scoring exit readiness..."):
                st.session_state["analysis_result"] = run_analysis(
                    dossier,
                    horizons,
                    st.session_state["deck_text"],
                    claude_service=service if use_ai else None,
                )
            st.session_state["pdf_path"] = None
            # The sidebar and step rail render before this tab body, so without a
            # rerun they would report the previous state until the next click.
            st.rerun()
        except ConfigError as exc:
            st.error(f"Configuration problem: {exc}")
        except Exception as exc:  # noqa: BLE001
            logging.exception("Analysis failed")
            st.error(f"The analysis could not be completed: {exc}")

    result = st.session_state["analysis_result"]
    if result is None:
        return

    analysis = result.analysis
    _render_analysis_summary(analysis, result)


def _render_analysis_summary(analysis, result) -> None:
    st.divider()
    col1, col2, col3 = st.columns(3)
    col1.metric("Most likely outcome", analysis.primary_scenario.label)
    col2.metric(
        "Analysis confidence",
        f"{analysis.confidence.score:.0f}/100",
        analysis.confidence.band,
    )
    col3.metric(
        "Horizon",
        analysis.primary_horizon.label if analysis.primary_horizon else "-",
    )

    outcome = getattr(result, "narrative_outcome", None)
    if outcome is not None:
        if outcome.accepted:
            st.caption(
                "The exit-profile paragraph was written by Claude from the computed "
                "figures. Every figure in it was verified against the analysis."
            )
        elif outcome.rejection_reason:
            st.warning(
                f"Claude's narrative was rejected and the deterministic text was kept. "
                f"{outcome.rejection_reason}"
            )

    theme.section("Scenario probabilities")
    st.caption("Directional scenario estimate - not a forecast.")
    st.dataframe(
        [
            {
                "Scenario": scenario.label,
                **{
                    p.horizon.label: f"{p.as_dict()[scenario]:.0f}%"
                    for p in analysis.probabilities
                },
            }
            for scenario in analysis.probabilities[0].as_dict()
        ],
        width="stretch",
        hide_index=True,
    )

    theme.section("How each score was reached")
    for name, dimension in analysis.dimension_scores.items():
        with st.expander(
            f"{name.replace('_', ' ').title()} - {dimension.score:.0f}/100 "
            f"({dimension.evidence_coverage:.0%} evidence-backed)"
        ):
            st.caption(dimension.summary)
            st.dataframe(
                [
                    {
                        "Factor": driver.factor.replace("_", " ").title(),
                        "Score": f"{driver.score:.1f}/10",
                        "Weight": f"{driver.weight:.0%}",
                        "Contribution": f"{driver.contribution:.1f}",
                        "Evidence": driver.evidence,
                    }
                    for driver in dimension.drivers
                ],
                width="stretch",
                hide_index=True,
            )

    with st.expander("Probability derivation"):
        for computation in result.computations:
            st.markdown(f"**{computation.horizon.label}**")
            for trace in computation.traces:
                st.text(f"  {trace.scenario.label:<24} weight {trace.weight:.4f}  ({trace.explain()})")
            for note in computation.notes:
                st.caption(note)

    theme.section("Assumptions")
    for assumption in analysis.assumptions:
        st.markdown(f"- {assumption}")
    theme.section("Limitations")
    for limitation in analysis.limitations:
        st.markdown(f"- {limitation}")


def render_report_tab() -> None:
    theme.section("Report and export", first=True)
    dossier: Optional[DealDossier] = st.session_state["dossier"]
    result = st.session_state["analysis_result"]

    if dossier is None or result is None:
        st.info("Run the analysis first.")
        return

    if not st.session_state["reviewed"]:
        st.error(
            "Inputs have not been reviewed. Confirm them on the Review Inputs tab "
            "before generating a report."
        )
        return

    analysis = result.analysis

    consent = ConsentBlock(
        analysis_consent=st.session_state["consent_analysis"],
        data_storage_consent=st.session_state["consent_storage"],
        share_with_ten_capital=st.session_state["consent_share"],
        consent_timestamp=st.session_state["consent_timestamp"],
    )
    user = UserBlock(
        name=st.session_state.get("user_name", ""),
        email=st.session_state.get("user_email", ""),
    )
    record = build_record(dossier, analysis, consent, user)
    st.session_state["record"] = record

    if st.button("Generate PDF", type="primary"):
        try:
            output_dir = Path(tempfile.gettempdir()) / "ten_capital_reports"
            safe = "".join(
                ch if ch.isalnum() or ch in "-_" else "_"
                for ch in dossier.company.display_name
            )[:50]
            path = output_dir / f"exit_scenario_{safe}_{datetime.now():%Y%m%d_%H%M%S}.pdf"
            with st.spinner("Rendering the one-page report..."):
                st.session_state["pdf_path"] = str(generate_pdf(dossier, analysis, path))
            _maybe_email_results(dossier, analysis, consent, record)
            st.rerun()
        except PDFGenerationError as exc:
            st.error(f"The report could not be generated: {exc}")
        except Exception as exc:  # noqa: BLE001
            logging.exception("PDF generation failed")
            st.error(f"Unexpected error while rendering the report: {exc}")

    pdf_path = st.session_state["pdf_path"]
    if pdf_path and Path(pdf_path).exists():
        with open(pdf_path, "rb") as handle:
            st.download_button(
                "Download PDF",
                data=handle.read(),
                file_name=Path(pdf_path).name,
                mime="application/pdf",
                type="primary",
            )

    st.divider()
    _render_email_section(dossier, analysis, consent, record)

    st.divider()
    theme.section("Structured record")

    st.download_button(
        "Export JSON",
        data=export_record_json(record),
        file_name=f"ten_capital_exit_record_{datetime.now():%Y%m%d_%H%M%S}.json",
        mime="application/json",
    )

    st.divider()
    theme.section("Send to TEN Capital")
    if not consent.share_with_ten_capital:
        st.info(
            "Sharing consent has not been given, so this record cannot be transmitted. "
            "Tick 'Share this analysis with TEN Capital' on the Upload tab to enable it."
        )
    else:
        st.warning(
            "This will transmit the full structured record, including extracted "
            "financials, to TEN Capital."
        )
        if st.button("Send to TEN Capital"):
            try:
                outcome = send_to_ten_capital(record)
                st.success(outcome.detail)
            except ConsentError as exc:
                st.error(str(exc))
            except TransmissionError as exc:
                st.error(f"Transmission failed: {exc}")


def _maybe_email_results(dossier, analysis, consent, record) -> None:
    """Send results automatically after a report is generated.

    Guarded against Streamlit's reruns by recording which PDF was emailed, so a
    page refresh cannot send the same analysis twice.
    """
    service = get_email_service()
    pdf_path = st.session_state["pdf_path"]

    if not service.config.auto_send or not service.available:
        return
    if st.session_state["emailed_for_pdf"] == pdf_path:
        return

    try:
        service.check_consent(consent)
    except EmailConsentError as exc:
        # Held in state rather than written directly, so it survives the rerun
        # that refreshes the status rail.
        st.session_state["email_error"] = f"Results were not emailed. {exc}"
        return

    _send_email(dossier, analysis, consent, record, pdf_path, automatic=True)


def _send_email(dossier, analysis, consent, record, pdf_path, automatic: bool) -> None:
    service = get_email_service()
    st.session_state["email_error"] = None
    try:
        with st.spinner("Emailing results..."):
            outcome = service.send_analysis(
                dossier, analysis, consent, pdf_path=pdf_path, record=record
            )
        st.session_state["email_result"] = outcome
        st.session_state["emailed_for_pdf"] = pdf_path
    except EmailConsentError as exc:
        st.session_state["email_error"] = str(exc)
    except EmailError as exc:
        # Delivery failure must never look like analysis failure. The report is
        # already generated and downloadable.
        st.session_state["email_error"] = (
            f"{exc} The report is unaffected and can be downloaded above."
        )
    except Exception as exc:  # noqa: BLE001
        logging.exception("Email send failed")
        st.session_state["email_error"] = (
            f"Unexpected error while emailing: {exc} "
            f"The report is unaffected and can be downloaded above."
        )


def _render_email_section(dossier, analysis, consent, record) -> None:
    theme.section("Email results")
    service = get_email_service()

    if not service.available:
        st.info(f"Email delivery unavailable. {service.config.describe()}")
        return

    st.caption(
        f"Recipient: {service.config.recipient} - the one-page report and the "
        f"structured record are attached."
    )

    outcome = st.session_state["email_result"]
    error = st.session_state["email_error"]
    if outcome is not None:
        st.success(f"{outcome.detail} (id {outcome.message_id})")
    if error:
        st.error(error)

    if not consent.analysis_consent:
        st.warning(
            "Results cannot be emailed without consent to analyse the source document. "
            "Tick the first consent box on the Upload tab."
        )
        return

    pdf_path = st.session_state["pdf_path"]
    already_sent = st.session_state["emailed_for_pdf"] == pdf_path and pdf_path is not None
    label = "Re-send results email" if already_sent else "Email results now"
    if st.button(label):
        _send_email(dossier, analysis, consent, record, pdf_path, automatic=False)
        st.rerun()

    if service.config.auto_send:
        st.caption("Results are emailed automatically each time a report is generated.")


def _workflow_steps() -> List[Tuple[str, str]]:
    """Workflow state for the step rail.

    The active step is the first incomplete one, so the rail always points at
    what to do next rather than only recording what is done.
    """
    done = [
        bool(st.session_state["document"]) or bool(st.session_state["dossier"]),
        bool(st.session_state["reviewed"]),
        bool(st.session_state["analysis_result"]),
        bool(st.session_state["pdf_path"]),
    ]
    labels = ["Upload", "Review", "Analyse", "Report"]
    active_index = next((i for i, complete in enumerate(done) if not complete), None)
    return [
        (
            label,
            "done" if complete else ("active" if index == active_index else "idle"),
        )
        for index, (label, complete) in enumerate(zip(labels, done))
    ]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    st.set_page_config(
        page_title="TEN Capital Network - Exit Scenario Analyzer",
        page_icon=theme.page_icon(),
        layout="wide",
    )
    theme.inject()
    init_state()

    theme.brand_header()

    email_service = get_email_service()
    recipient = email_service.config.recipient if email_service.available else None
    note = (
        "The uploaded deck is processed on the server. A copy of every generated "
        f"one-pager is emailed to <code>{html.escape(recipient)}</code>."
        if recipient
        else "The uploaded deck is processed on the server. Result emails are not "
        "configured, so nothing is sent anywhere."
    )
    theme.hero(
        eyebrow="Exit Scenario Analyzer",
        title_html=(
            'Pitch Deck<span class="arrow">&rarr;</span>'
            '<span class="to">Exit Scenario One&#8209;Pager</span>'
        ),
        lede=(
            "Upload a pitch deck and get a single-page investor PDF of directional "
            "liquidity scenarios. Scores and probabilities are computed "
            "deterministically; Claude fills extraction gaps and writes the prose."
        ),
        note_html=note,
    )
    theme.step_rail(_workflow_steps())

    try:
        load_scoring_config()
    except ConfigError as exc:
        st.error(f"The scoring configuration is invalid: {exc}")
        st.stop()

    with st.sidebar:
        theme.section("Your details", first=True)
        st.text_input("Name", key="user_name")
        st.text_input("Email", key="user_email")
        st.divider()
        theme.section("Status", first=True)
        rows = [
            ("done" if st.session_state["document"] else "idle",
             "Deck extracted" if st.session_state["document"] else "No deck yet"),
            ("done" if st.session_state["reviewed"] else "attention",
             "Inputs reviewed" if st.session_state["reviewed"] else "Inputs not reviewed"),
            ("done" if st.session_state["analysis_result"] else "idle",
             "Analysis run" if st.session_state["analysis_result"] else "Analysis not run"),
            ("done" if get_claude_service().available else "idle",
             "Claude connected" if get_claude_service().available else "Claude not configured"),
            ("done" if email_service.available else "idle",
             "Email connected" if email_service.available else "Email not configured"),
        ]
        st.markdown(
            "<br>".join(theme.status_dot(state, label) for state, label in rows),
            unsafe_allow_html=True,
        )
        if email_service.available:
            st.caption(f"Results go to {email_service.config.recipient}")
        if st.session_state.get("invalidation_reason") and not st.session_state["analysis_result"]:
            st.caption(
                f"Previous results were cleared because {st.session_state['invalidation_reason']}."
            )
        st.divider()
        if st.button("Reload configuration"):
            clear_config_cache()
            st.success("Configuration reloaded.")

    upload_tab, review_tab, comparables_tab, analysis_tab, report_tab = st.tabs(
        ["Upload", "Review Inputs", "Comparable Exits", "Run Analysis", "Report"]
    )
    with upload_tab:
        render_upload_tab()
    with review_tab:
        render_review_tab()
    with comparables_tab:
        render_comparables_tab()
    with analysis_tab:
        render_analysis_tab()
    with report_tab:
        render_report_tab()

    theme.footer()


if __name__ == "__main__":
    main()

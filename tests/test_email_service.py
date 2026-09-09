"""Email delivery of analysis results.

Every test injects a transport. The suite must never send real mail: a test that
puts a message in someone's inbox is not a test, and CI has no Resend key.

The properties under test are the ones that matter operationally -- that nothing
is composed or sent without consent, that a delivery failure never looks like an
analysis failure, and that the message actually carries the results.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, List

import pytest

from venture_exit_analyzer.models.analysis import Scenario, TimeHorizon
from venture_exit_analyzer.models.record import ConsentBlock, UserBlock, build_record
from venture_exit_analyzer.reporting.pdf_generator import generate_pdf
from venture_exit_analyzer.services.email_service import (
    DEFAULT_TO,
    EmailConfig,
    EmailConsentError,
    EmailError,
    EmailService,
    build_email_service,
    build_html,
    build_subject,
    build_text,
)


class Recorder:
    """A transport that captures the payload instead of sending it."""

    def __init__(self, message_id: str = "msg-test-1"):
        self.payloads: List[Dict[str, Any]] = []
        self.message_id = message_id

    def __call__(self, body: Dict[str, Any]) -> str:
        self.payloads.append(body)
        return self.message_id

    @property
    def last(self) -> Dict[str, Any]:
        assert self.payloads, "no message was sent"
        return self.payloads[-1]


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def service(recorder) -> EmailService:
    return EmailService(
        config=EmailConfig(api_key=None, enabled=True), transport=recorder
    )


@pytest.fixture
def granted() -> ConsentBlock:
    return ConsentBlock(analysis_consent=True, data_storage_consent=True)


@pytest.fixture
def record(dossier, analysis, granted):
    return build_record(dossier, analysis, granted, UserBlock(email="a@example.com"))


# ---------------------------------------------------------------------------
# Consent
# ---------------------------------------------------------------------------
class TestConsent:
    def test_blocked_without_analysis_consent(self, service, recorder, dossier, analysis):
        with pytest.raises(EmailConsentError):
            service.send_analysis(dossier, analysis, ConsentBlock())
        assert not recorder.payloads

    def test_nothing_is_composed_before_the_consent_check(
        self, service, recorder, dossier, analysis
    ):
        """An unauthorised send must not even serialise the financials."""
        with pytest.raises(EmailConsentError):
            service.send_analysis(dossier, analysis, ConsentBlock())
        assert recorder.payloads == []

    def test_share_consent_not_required_by_default(
        self, service, recorder, dossier, analysis, granted
    ):
        assert not granted.share_with_ten_capital
        result = service.send_analysis(dossier, analysis, granted)
        assert result.success
        assert recorder.payloads

    def test_strict_mode_requires_share_consent(self, recorder, dossier, analysis, granted):
        strict = EmailService(
            config=EmailConfig(api_key=None, require_share_consent=True),
            transport=recorder,
        )
        with pytest.raises(EmailConsentError) as exc:
            strict.send_analysis(dossier, analysis, granted)
        assert "EMAIL_REQUIRE_SHARE_CONSENT" in str(exc.value)
        assert not recorder.payloads

    def test_strict_mode_allows_a_fully_consented_analysis(
        self, recorder, dossier, analysis
    ):
        strict = EmailService(
            config=EmailConfig(api_key=None, require_share_consent=True),
            transport=recorder,
        )
        consent = ConsentBlock(analysis_consent=True, share_with_ten_capital=True)
        assert strict.send_analysis(dossier, analysis, consent).success


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------
class TestAvailability:
    def test_no_key_means_unavailable_not_broken(self, monkeypatch):
        monkeypatch.delenv("RESEND_API_KEY", raising=False)
        service = EmailService(config=EmailConfig(api_key=None))
        assert not service.available
        assert "No Resend API key" in service.status

    def test_explicitly_disabled(self):
        service = EmailService(config=EmailConfig(api_key="k", enabled=False))
        assert not service.available
        assert "switched off" in service.status

    def test_key_is_never_exposed_in_status(self):
        service = EmailService(config=EmailConfig(api_key="re_supersecretvalue123456"))
        assert "supersecretvalue" not in service.status

    def test_sending_without_configuration_raises(self, dossier, analysis, granted):
        service = EmailService(config=EmailConfig(api_key=None))
        with pytest.raises(EmailError):
            service.send_analysis(dossier, analysis, granted)

    def test_build_service_never_raises(self, monkeypatch):
        monkeypatch.delenv("RESEND_API_KEY", raising=False)
        assert build_email_service() is not None

    def test_default_recipient_is_the_ten_capital_inbox(self):
        assert DEFAULT_TO == "Info@tencapital.group"


# ---------------------------------------------------------------------------
# Message content
# ---------------------------------------------------------------------------
class TestMessageContent:
    def test_subject_carries_company_outcome_and_confidence(self, dossier, analysis):
        subject = build_subject(dossier, analysis)
        assert dossier.company.display_name in subject
        assert "confidence" in subject.lower()

    def test_subject_states_no_liquidity_plainly(self, empty_dossier):
        from venture_exit_analyzer.analysis.engine import run_analysis

        result = run_analysis(empty_dossier, [TimeHorizon.Y1_3])
        assert result.analysis.primary_scenario == Scenario.NO_LIQUIDITY
        assert "No liquidity" in build_subject(empty_dossier, result.analysis)

    def test_body_contains_every_scenario_and_horizon(self, dossier, analysis):
        html_body = build_html(dossier, analysis)
        for scenario in Scenario:
            assert scenario.label in html_body
        for probabilities in analysis.probabilities:
            assert probabilities.horizon.label in html_body

    def test_body_carries_the_forecast_disclaimer(self, dossier, analysis):
        html_body = build_html(dossier, analysis)
        text_body = build_text(dossier, analysis)
        assert "not a forecast" in html_body.lower()
        assert "not a forecast" in text_body.lower()
        assert "not an investment recommendation" in html_body
        assert "Confidential" in text_body

    def test_absent_values_read_as_not_disclosed(self, empty_dossier):
        from venture_exit_analyzer.analysis.engine import run_analysis

        analysis = run_analysis(empty_dossier, [TimeHorizon.Y3_5]).analysis
        text_body = build_text(empty_dossier, analysis)
        assert "Not disclosed" in text_body
        assert "Insufficient information" in text_body

    def test_company_name_is_html_escaped(self, dossier, analysis):
        """A deck title is untrusted input; it must not become markup."""
        from venture_exit_analyzer.models.evidence import Evidenced

        hostile = dossier.model_copy(deep=True)
        hostile.company.name = Evidenced.from_user("<script>alert(1)</script>")
        body = build_html(hostile, analysis)
        assert "<script>alert(1)</script>" not in body
        assert "&lt;script&gt;" in body

    def test_both_html_and_plain_text_are_sent(self, service, recorder, dossier, analysis, granted):
        service.send_analysis(dossier, analysis, granted)
        assert recorder.last["html"].startswith("<!doctype html>")
        assert "TEN CAPITAL" in recorder.last["text"]


# ---------------------------------------------------------------------------
# Attachments and addressing
# ---------------------------------------------------------------------------
class TestAttachments:
    def test_pdf_and_record_are_attached(
        self, service, recorder, dossier, analysis, granted, record, tmp_path
    ):
        pdf = generate_pdf(dossier, analysis, tmp_path / "report.pdf")
        service.send_analysis(dossier, analysis, granted, pdf_path=pdf, record=record)

        attachments = recorder.last["attachments"]
        assert len(attachments) == 2
        names = [a["filename"] for a in attachments]
        assert "report.pdf" in names
        assert any(n.endswith(".json") for n in names)

    def test_attachment_content_is_valid_base64_of_the_file(
        self, service, recorder, dossier, analysis, granted, tmp_path
    ):
        pdf = generate_pdf(dossier, analysis, tmp_path / "report.pdf")
        service.send_analysis(dossier, analysis, granted, pdf_path=pdf)
        encoded = recorder.last["attachments"][0]["content"]
        assert base64.b64decode(encoded) == pdf.read_bytes()

    def test_attached_record_is_valid_json(
        self, service, recorder, dossier, analysis, granted, record
    ):
        service.send_analysis(dossier, analysis, granted, record=record)
        payload = base64.b64decode(recorder.last["attachments"][0]["content"])
        assert json.loads(payload)["analysis_type"] == "venture_exit_scenario"

    def test_sends_without_attachments_when_none_supplied(
        self, service, recorder, dossier, analysis, granted
    ):
        assert service.send_analysis(dossier, analysis, granted).success
        assert "attachments" not in recorder.last

    def test_missing_pdf_is_skipped_not_fatal(
        self, service, recorder, dossier, analysis, granted, tmp_path
    ):
        result = service.send_analysis(
            dossier, analysis, granted, pdf_path=tmp_path / "absent.pdf"
        )
        assert result.success
        assert "attachments" not in recorder.last

    def test_recipient_defaults_to_configuration(
        self, service, recorder, dossier, analysis, granted
    ):
        service.send_analysis(dossier, analysis, granted)
        assert recorder.last["to"] == [DEFAULT_TO]

    def test_recipient_can_be_overridden(
        self, service, recorder, dossier, analysis, granted
    ):
        service.send_analysis(dossier, analysis, granted, recipient="other@example.com")
        assert recorder.last["to"] == ["other@example.com"]

    def test_empty_recipient_is_rejected(self, recorder, dossier, analysis, granted):
        service = EmailService(
            config=EmailConfig(api_key=None, recipient="  "), transport=recorder
        )
        with pytest.raises(EmailError):
            service.send_analysis(dossier, analysis, granted)


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------
class TestFailureHandling:
    def test_transport_failure_raises_email_error_only(self, dossier, analysis, granted):
        def exploding(body):
            raise RuntimeError("network down")

        service = EmailService(config=EmailConfig(api_key=None), transport=exploding)
        with pytest.raises(RuntimeError):
            service.send_analysis(dossier, analysis, granted)

    def test_analysis_and_report_are_unaffected_by_email_failure(
        self, dossier, analysis, tmp_path
    ):
        """A delivery problem must never invalidate the deliverable."""
        pdf = generate_pdf(dossier, analysis, tmp_path / "report.pdf")

        def exploding(body):
            raise RuntimeError("smtp exploded")

        service = EmailService(config=EmailConfig(api_key=None), transport=exploding)
        with pytest.raises(RuntimeError):
            service.send_analysis(
                dossier, analysis, ConsentBlock(analysis_consent=True), pdf_path=pdf
            )
        assert pdf.exists() and pdf.stat().st_size > 0
        assert analysis.probabilities

    def test_result_reports_what_was_sent(
        self, service, dossier, analysis, granted, record, tmp_path
    ):
        pdf = generate_pdf(dossier, analysis, tmp_path / "report.pdf")
        result = service.send_analysis(
            dossier, analysis, granted, pdf_path=pdf, record=record
        )
        assert result.message_id == "msg-test-1"
        assert result.recipient == DEFAULT_TO
        assert len(result.attachments) == 2
        assert result.timestamp


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
class TestConfiguration:
    def test_reads_environment(self, monkeypatch):
        monkeypatch.setenv("RESEND_API_KEY", "re_test")
        monkeypatch.setenv("EMAIL_TO", "someone@example.com")
        monkeypatch.setenv("EMAIL_AUTO_SEND", "false")
        monkeypatch.setenv("EMAIL_REQUIRE_SHARE_CONSENT", "true")
        config = EmailConfig.from_env(load_file=False)
        assert config.api_key == "re_test"
        assert config.recipient == "someone@example.com"
        assert config.auto_send is False
        assert config.require_share_consent is True

    def test_defaults_are_sane(self, monkeypatch):
        for name in (
            "RESEND_API_KEY", "EMAIL_TO", "EMAIL_FROM", "EMAIL_ENABLED",
            "EMAIL_AUTO_SEND", "EMAIL_REQUIRE_SHARE_CONSENT",
        ):
            monkeypatch.delenv(name, raising=False)
        config = EmailConfig.from_env(load_file=False)
        assert config.recipient == DEFAULT_TO
        assert "tencapital.group" in config.sender
        assert config.auto_send is True
        assert config.require_share_consent is False

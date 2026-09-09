"""Email delivery of analysis results via Resend.

This is the second of two modules that send data outside the process, and it is
held to the same rule as the first: the consent check happens here, before any
transport is constructed, so there is no code path to a network call that skips
it.

Consent model
-------------
Sending requires ``analysis_consent`` -- an analysis you had no right to run is
one you have no right to circulate. It does not require
``share_with_ten_capital`` by default, because that flag governs filing the
structured record into TEN Capital's systems, whereas this email is an
operational notification to the operator's own inbox. Deployments that want the
stricter reading can set ``EMAIL_REQUIRE_SHARE_CONSENT=true``.

Failure is never fatal. No key, no network, or a Resend error leaves the
analysis and the report exactly as they were; the interface reports what
happened and the user can retry or download the PDF directly.
"""

from __future__ import annotations

import base64
import html
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..models.analysis import (
    DealDossier,
    ExitScenarioAnalysis,
    Scenario,
)
from ..models.record import ConsentBlock, TenCapitalRecord
from ..utils.env import env_bool, env_float, load_dotenv, redact
from ..utils.formatting import money, money_range, months as fmt_months, percent

logger = logging.getLogger(__name__)

RESEND_ENDPOINT = "https://api.resend.com/emails"

API_KEY_ENV = "RESEND_API_KEY"
FROM_ENV = "EMAIL_FROM"
TO_ENV = "EMAIL_TO"
ENABLED_ENV = "EMAIL_ENABLED"
AUTO_SEND_ENV = "EMAIL_AUTO_SEND"
REQUIRE_SHARE_ENV = "EMAIL_REQUIRE_SHARE_CONSENT"

#: The verified sending domain is tencapital.group; the local part identifies
#: which tool produced the message.
DEFAULT_FROM = "TEN Capital Exit Analyzer <exit-analyzer@tencapital.group>"
DEFAULT_TO = "Info@tencapital.group"
DEFAULT_TIMEOUT = 30.0

#: Resend accepts up to 40MB per message. The report is a few kilobytes; this
#: cap exists so a pathological JSON record cannot produce a rejected send.
MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024


class EmailConsentError(PermissionError):
    """Raised when sending is attempted without the required consent."""


class EmailError(RuntimeError):
    """Raised when a send attempt fails."""


@dataclass
class EmailResult:
    success: bool
    detail: str
    message_id: Optional[str] = None
    recipient: str = ""
    timestamp: str = ""
    attachments: List[str] = field(default_factory=list)


@dataclass
class EmailConfig:
    api_key: Optional[str] = None
    sender: str = DEFAULT_FROM
    recipient: str = DEFAULT_TO
    enabled: bool = True
    auto_send: bool = True
    require_share_consent: bool = False
    timeout: float = DEFAULT_TIMEOUT

    @classmethod
    def from_env(cls, load_file: bool = True) -> "EmailConfig":
        if load_file:
            load_dotenv()
        return cls(
            api_key=(os.environ.get(API_KEY_ENV) or "").strip() or None,
            sender=(os.environ.get(FROM_ENV) or DEFAULT_FROM).strip(),
            recipient=(os.environ.get(TO_ENV) or DEFAULT_TO).strip(),
            enabled=env_bool(ENABLED_ENV, True),
            auto_send=env_bool(AUTO_SEND_ENV, True),
            require_share_consent=env_bool(REQUIRE_SHARE_ENV, False),
            timeout=env_float("EMAIL_TIMEOUT_SECONDS", DEFAULT_TIMEOUT),
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key) and self.enabled

    def describe(self) -> str:
        if not self.enabled:
            return "Email delivery is switched off (EMAIL_ENABLED=false)."
        if not self.api_key:
            return (
                f"No Resend API key found. Set {API_KEY_ENV} to enable result emails. "
                f"The application runs fully without it."
            )
        return (
            f"Sending to {self.recipient} as {self.sender}, key {redact(self.api_key)}."
        )


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------
def build_subject(dossier: DealDossier, analysis: ExitScenarioAnalysis) -> str:
    """A subject line that is useful in a crowded inbox and in search."""
    company = dossier.company.display_name
    outcome = (
        "No liquidity"
        if analysis.primary_scenario == Scenario.NO_LIQUIDITY
        else analysis.primary_scenario.label
    )
    return (
        f"Exit Scenario Analysis - {company} - {outcome} "
        f"({analysis.confidence.score:.0f}/100 confidence)"
    )


def _summary_rows(dossier: DealDossier, analysis: ExitScenarioAnalysis) -> List[tuple]:
    fin = dossier.financials
    revenue = fin.best_revenue
    capital = analysis.capital_requirement
    return [
        ("Company", dossier.company.display_name),
        (
            "Sector",
            " / ".join(
                part
                for part in (dossier.company.sector.value, dossier.company.subsector.value)
                if part
            )
            or "Not disclosed",
        ),
        (
            "Stage",
            dossier.company.current_financing_stage.or_default(
                dossier.company.stage_value.label
                if dossier.company.stage.is_present
                else "Not disclosed"
            ),
        ),
        (
            "Revenue",
            f"{money(revenue)} {fin.best_revenue_label}" if revenue is not None else "Not disclosed",
        ),
        ("Growth", percent(fin.revenue_growth_rate.value)),
        ("Capital raised", money(fin.capital_raised_to_date.value)),
        ("Current raise", money(fin.amount_raising.value)),
        ("Runway", fmt_months(capital.runway_months)),
    ]


def _valuation_rows(analysis: ExitScenarioAnalysis) -> List[tuple]:
    rows = []
    for scenario in (Scenario.STRATEGIC, Scenario.FINANCIAL):
        valuation = analysis.valuation_for(scenario)
        label = f"{scenario.label.replace(' Acquisition', '')} exit"
        if valuation is not None and valuation.is_defensible:
            rows.append((label, money_range(valuation.low, valuation.high)))
        else:
            rows.append((label, "Insufficient information for a credible range"))
    capital = analysis.capital_requirement
    rows.append(
        (
            "Capital to exit readiness",
            money_range(capital.additional_capital_low, capital.additional_capital_high)
            if capital.is_estimable
            else "Not estimable",
        )
    )
    return rows


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def build_html(dossier: DealDossier, analysis: ExitScenarioAnalysis) -> str:
    """Compose the HTML body.

    Table-based layout with inline styles, because email clients are not
    browsers and will not honour a stylesheet.
    """
    horizons = analysis.probabilities
    horizon_headers = "".join(
        f'<th style="padding:6px 10px;text-align:right;font-size:11px;'
        f'letter-spacing:.04em;color:#5C5C5C;text-transform:uppercase;'
        f'border-bottom:1px solid #C9C9C9;">{_esc(p.horizon.label)}</th>'
        for p in horizons
    )

    scenario_rows = ""
    for scenario in (
        Scenario.STRATEGIC,
        Scenario.FINANCIAL,
        Scenario.IPO,
        Scenario.NO_LIQUIDITY,
    ):
        emphasis = "font-weight:700;" if scenario == Scenario.NO_LIQUIDITY else ""
        cells = "".join(
            f'<td style="padding:6px 10px;text-align:right;font-weight:700;'
            f'border-bottom:1px solid #EEEEEC;">{p.as_dict()[scenario]:.0f}%</td>'
            for p in horizons
        )
        scenario_rows += (
            f'<tr><td style="padding:6px 10px;{emphasis}'
            f'border-bottom:1px solid #EEEEEC;">{_esc(scenario.label)}</td>{cells}</tr>'
        )

    def definition_table(rows: List[tuple]) -> str:
        body = "".join(
            f'<tr><td style="padding:5px 10px 5px 0;color:#5C5C5C;width:45%;">{_esc(k)}</td>'
            f'<td style="padding:5px 0;font-weight:700;">{_esc(v)}</td></tr>'
            for k, v in rows
        )
        return f'<table style="width:100%;border-collapse:collapse;font-size:13px;">{body}</table>'

    milestones = "".join(
        f'<li style="margin-bottom:5px;"><strong>{_esc(m.milestone)}</strong> '
        f'<span style="color:#5C5C5C;">- {_esc(m.exit_impact)} impact</span></li>'
        for m in analysis.milestones[:5]
    ) or '<li style="color:#5C5C5C;">None identified.</li>'

    upside = "".join(
        f'<li style="margin-bottom:4px;color:#1F5C3D;">{_esc(d.driver)}</li>'
        for d in analysis.positive_drivers[:3]
    ) or '<li style="color:#5C5C5C;">None evidenced.</li>'

    risks = "".join(
        f'<li style="margin-bottom:4px;color:#7A2318;">{_esc(d.driver)}</li>'
        for d in analysis.negative_drivers[:3]
    ) or '<li style="color:#5C5C5C;">None evidenced.</li>'

    most_likely = (
        f"No liquidity event within "
        f"{analysis.primary_horizon.label if analysis.primary_horizon else 'the period assessed'}"
        if analysis.primary_scenario == Scenario.NO_LIQUIDITY
        else f"{analysis.primary_scenario.label}"
        + (f" ({analysis.primary_horizon.label})" if analysis.primary_horizon else "")
    )

    limitations = "".join(
        f'<li style="margin-bottom:4px;">{_esc(item)}</li>'
        for item in analysis.limitations[:4]
    )

    section = (
        'margin:22px 0 8px;font-size:12px;font-weight:700;letter-spacing:.06em;'
        'text-transform:uppercase;color:#1A1A1A;'
    )

    return f"""<!doctype html>
<html><body style="margin:0;padding:0;background:#F2F2F0;">
<table role="presentation" style="width:100%;border-collapse:collapse;background:#F2F2F0;">
<tr><td align="center" style="padding:24px 12px;">
<table role="presentation" style="width:100%;max-width:640px;border-collapse:collapse;
       background:#FFFFFF;font-family:'Open Sans',Helvetica,Arial,sans-serif;color:#1A1A1A;">

  <tr><td style="background:#1A1A1A;padding:16px 22px;">
    <div style="color:#FFFFFF;font-size:17px;font-weight:700;letter-spacing:.02em;">TEN CAPITAL</div>
    <div style="color:#FFFFFF;font-size:12px;letter-spacing:.08em;text-transform:uppercase;
                margin-top:3px;">Venture Exit Scenario Analysis</div>
  </td></tr>

  <tr><td style="padding:22px;">

    <div style="{section}">Deal Summary</div>
    {definition_table(_summary_rows(dossier, analysis))}

    <div style="{section}">Exit Scenario Outlook</div>
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
      <tr>
        <th style="padding:6px 10px;text-align:left;font-size:11px;letter-spacing:.04em;
                   color:#5C5C5C;text-transform:uppercase;border-bottom:1px solid #C9C9C9;">Scenario</th>
        {horizon_headers}
      </tr>
      {scenario_rows}
    </table>
    <div style="margin-top:6px;font-size:10px;letter-spacing:.05em;text-transform:uppercase;
                color:#5C5C5C;">Directional scenario estimate &mdash; not a forecast</div>

    <table style="width:100%;border-collapse:collapse;margin-top:14px;background:#F2F2F0;">
      <tr>
        <td style="padding:10px 12px;font-size:13px;">
          <div style="font-size:10px;letter-spacing:.05em;text-transform:uppercase;
                      color:#5C5C5C;">Most likely path</div>
          <div style="font-weight:700;margin-top:2px;">{_esc(most_likely)}</div>
        </td>
        <td style="padding:10px 12px;font-size:13px;">
          <div style="font-size:10px;letter-spacing:.05em;text-transform:uppercase;
                      color:#5C5C5C;">Analysis confidence</div>
          <div style="font-weight:700;margin-top:2px;">
            {analysis.confidence.score:.0f}/100 ({_esc(analysis.confidence.band)})</div>
        </td>
      </tr>
    </table>

    <div style="{section}">Likely Exit Profile</div>
    <div style="font-size:13px;line-height:1.55;">{_esc(analysis.exit_profile_summary)}</div>

    <div style="{section}">Indicative Ranges</div>
    {definition_table(_valuation_rows(analysis))}

    <div style="{section}">Value Creation Milestones</div>
    <ul style="margin:0;padding-left:18px;font-size:13px;line-height:1.5;">{milestones}</ul>

    <div style="{section}">Valuation Drivers</div>
    <div style="font-size:11px;font-weight:700;color:#5C5C5C;margin-bottom:4px;">UPSIDE</div>
    <ul style="margin:0 0 12px;padding-left:18px;font-size:13px;line-height:1.5;">{upside}</ul>
    <div style="font-size:11px;font-weight:700;color:#5C5C5C;margin-bottom:4px;">RISKS</div>
    <ul style="margin:0;padding-left:18px;font-size:13px;line-height:1.5;">{risks}</ul>

    <div style="{section}">Limitations</div>
    <ul style="margin:0;padding-left:18px;font-size:12px;line-height:1.5;color:#5C5C5C;">
      {limitations}</ul>

    <div style="margin-top:22px;padding-top:12px;border-top:1px solid #C9C9C9;
                font-size:11px;line-height:1.5;color:#5C5C5C;">
      Directional decision-support analysis based on supplied information and stated
      assumptions. It is not an investment recommendation, valuation opinion, or
      prediction of future liquidity. The full one-page report and the structured
      record are attached.
    </div>
    <div style="margin-top:8px;font-size:11px;font-weight:700;color:#5C5C5C;">
      Confidential &mdash; for recipients only.</div>

  </td></tr>
</table>
</td></tr></table>
</body></html>"""


def build_text(dossier: DealDossier, analysis: ExitScenarioAnalysis) -> str:
    """Plain-text alternative. Some clients show it, and all archives index it."""
    lines = [
        "TEN CAPITAL - VENTURE EXIT SCENARIO ANALYSIS",
        "=" * 52,
        "",
        "DEAL SUMMARY",
    ]
    lines += [f"  {k}: {v}" for k, v in _summary_rows(dossier, analysis)]

    lines += ["", "EXIT SCENARIO OUTLOOK"]
    header = "  " + "Scenario".ljust(24) + "".join(
        p.horizon.label.rjust(12) for p in analysis.probabilities
    )
    lines.append(header)
    for scenario in (
        Scenario.STRATEGIC,
        Scenario.FINANCIAL,
        Scenario.IPO,
        Scenario.NO_LIQUIDITY,
    ):
        row = "  " + scenario.label.ljust(24) + "".join(
            f"{p.as_dict()[scenario]:.0f}%".rjust(12) for p in analysis.probabilities
        )
        lines.append(row)
    lines.append("  Directional scenario estimate - not a forecast.")

    most_likely = (
        "No liquidity event"
        if analysis.primary_scenario == Scenario.NO_LIQUIDITY
        else analysis.primary_scenario.label
    )
    lines += [
        "",
        f"  Most likely path: {most_likely}"
        + (f" ({analysis.primary_horizon.label})" if analysis.primary_horizon else ""),
        f"  Analysis confidence: {analysis.confidence.score:.0f}/100 "
        f"({analysis.confidence.band})",
        "",
        "LIKELY EXIT PROFILE",
        f"  {analysis.exit_profile_summary}",
        "",
        "INDICATIVE RANGES",
    ]
    lines += [f"  {k}: {v}" for k, v in _valuation_rows(analysis)]

    lines += ["", "VALUE CREATION MILESTONES"]
    lines += [
        f"  - {m.milestone} ({m.exit_impact} impact)" for m in analysis.milestones[:5]
    ] or ["  None identified."]

    lines += ["", "VALUATION DRIVERS", "  Upside:"]
    lines += [f"    + {d.driver}" for d in analysis.positive_drivers[:3]] or ["    None evidenced."]
    lines += ["  Risks:"]
    lines += [f"    - {d.driver}" for d in analysis.negative_drivers[:3]] or ["    None evidenced."]

    lines += ["", "LIMITATIONS"]
    lines += [f"  - {item}" for item in analysis.limitations[:4]]

    lines += [
        "",
        "-" * 52,
        "Directional decision-support analysis based on supplied information and",
        "stated assumptions. It is not an investment recommendation, valuation",
        "opinion, or prediction of future liquidity.",
        "Confidential - for recipients only.",
    ]
    return "\n".join(lines)


def _attachment(path: Path, label: str) -> Optional[Dict[str, str]]:
    """Base64-encode a file for the Resend payload, or skip it with a warning."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        logger.warning("Could not attach %s (%s): %s", label, path, exc)
        return None
    if len(raw) > MAX_ATTACHMENT_BYTES:
        logger.warning("Skipping %s: %d bytes exceeds the attachment cap.", label, len(raw))
        return None
    return {
        "filename": path.name,
        "content": base64.b64encode(raw).decode("ascii"),
    }


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class EmailService:
    """Sends analysis results through Resend."""

    def __init__(self, config: Optional[EmailConfig] = None, transport: Any = None):
        self.config = config or EmailConfig.from_env()
        self._transport = transport

    @property
    def available(self) -> bool:
        return self.config.is_configured or self._transport is not None

    @property
    def status(self) -> str:
        if self._transport is not None and not self.config.is_configured:
            return "Using an injected transport (test mode)."
        return (
            f"Connected. {self.config.describe()}"
            if self.available
            else self.config.describe()
        )

    def check_consent(self, consent: ConsentBlock) -> None:
        """Raise unless this analysis may be emailed.

        Runs before any payload is built, so an unauthorised send does not even
        reach the point of serialising the company's financials.
        """
        if not consent.analysis_consent:
            raise EmailConsentError(
                "This analysis has no consent to analyse the source document, so its "
                "results cannot be emailed. Nothing has been sent."
            )
        if self.config.require_share_consent and not consent.share_with_ten_capital:
            raise EmailConsentError(
                "EMAIL_REQUIRE_SHARE_CONSENT is set, and this analysis has not been "
                "approved for sharing with TEN Capital. Nothing has been sent."
            )

    def send_analysis(
        self,
        dossier: DealDossier,
        analysis: ExitScenarioAnalysis,
        consent: ConsentBlock,
        pdf_path: Optional[str | Path] = None,
        record: Optional[TenCapitalRecord] = None,
        recipient: Optional[str] = None,
    ) -> EmailResult:
        """Email the analysis results, with the report and record attached."""
        self.check_consent(consent)

        if not self.available:
            raise EmailError(self.config.describe())

        to_address = (recipient or self.config.recipient).strip()
        if not to_address:
            raise EmailError("No recipient address is configured.")

        attachments: List[Dict[str, str]] = []
        names: List[str] = []

        if pdf_path:
            item = _attachment(Path(pdf_path), "report PDF")
            if item:
                attachments.append(item)
                names.append(item["filename"])

        if record is not None:
            safe = "".join(
                ch if ch.isalnum() or ch in "-_" else "_"
                for ch in (record.company.name or "analysis")
            )[:50]
            payload = record.model_dump_json(indent=2).encode("utf-8")
            if len(payload) <= MAX_ATTACHMENT_BYTES:
                filename = f"exit_record_{safe}_{datetime.now():%Y%m%d_%H%M%S}.json"
                attachments.append(
                    {
                        "filename": filename,
                        "content": base64.b64encode(payload).decode("ascii"),
                    }
                )
                names.append(filename)

        body: Dict[str, Any] = {
            "from": self.config.sender,
            "to": [to_address],
            "subject": build_subject(dossier, analysis),
            "html": build_html(dossier, analysis),
            "text": build_text(dossier, analysis),
        }
        if attachments:
            body["attachments"] = attachments

        message_id = self._deliver(body)
        logger.info("Analysis email sent to %s (id=%s)", to_address, message_id)
        return EmailResult(
            success=True,
            detail=(
                f"Results emailed to {to_address}"
                + (f" with {len(names)} attachment(s)." if names else ".")
            ),
            message_id=message_id,
            recipient=to_address,
            timestamp=datetime.now(timezone.utc).isoformat(),
            attachments=names,
        )

    def _deliver(self, body: Dict[str, Any]) -> Optional[str]:
        if self._transport is not None:
            return self._transport(body)

        try:
            import httpx
        except ImportError as exc:
            raise EmailError(
                "The 'httpx' package is required to send email. Run: pip install httpx"
            ) from exc

        try:
            response = httpx.post(
                RESEND_ENDPOINT,
                json=body,
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=self.config.timeout,
            )
        except Exception as exc:
            raise EmailError(f"Could not reach Resend: {exc}") from exc

        if response.status_code >= 400:
            detail = response.text[:300]
            # Resend's most common rejections have specific, actionable causes.
            if response.status_code == 401:
                raise EmailError(
                    f"Resend rejected the API key (HTTP 401). Check {API_KEY_ENV}."
                )
            if response.status_code == 403:
                raise EmailError(
                    f"Resend refused the sender address '{self.config.sender}' (HTTP 403). "
                    f"The domain must be verified in the Resend dashboard. {detail}"
                )
            if response.status_code == 422:
                raise EmailError(
                    f"Resend rejected the message as invalid (HTTP 422). {detail}"
                )
            if response.status_code == 429:
                raise EmailError("Resend rate limit reached (HTTP 429). Try again shortly.")
            raise EmailError(f"Resend returned HTTP {response.status_code}: {detail}")

        try:
            return response.json().get("id")
        except Exception:
            return None


def build_email_service(config: Optional[EmailConfig] = None) -> EmailService:
    """Construct the service from the environment."""
    return EmailService(config or EmailConfig.from_env())

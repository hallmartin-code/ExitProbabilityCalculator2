"""Transmission of records to TEN Capital.

This is the only module in the application that sends data anywhere. It is kept
separate for exactly that reason: the consent gate is one place, it is checked
before anything leaves the process, and it cannot be bypassed by a caller that
forgot to check.

If no endpoint is configured, the mock transport is used and the record is
written to a local outbox directory instead of being sent.
"""

from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..models.record import TenCapitalRecord

logger = logging.getLogger(__name__)

DEFAULT_OUTBOX = Path("outbox")
ENDPOINT_ENV = "TEN_CAPITAL_API_URL"
API_KEY_ENV = "TEN_CAPITAL_API_KEY"
TIMEOUT_ENV = "TEN_CAPITAL_TIMEOUT_SECONDS"


class ConsentError(PermissionError):
    """Raised when transmission is attempted without the required consent.

    Deliberately a hard error, not a warning: sending a company's confidential
    financials without permission is not a recoverable condition.
    """


class TransmissionError(RuntimeError):
    """Raised when a transmission attempt fails."""


@dataclass
class TransmissionResult:
    success: bool
    transport: str
    detail: str
    reference: Optional[str] = None
    timestamp: str = ""


class Transport(ABC):
    """A destination a record can be delivered to."""

    name: str = "transport"

    @abstractmethod
    def deliver(self, record: TenCapitalRecord) -> TransmissionResult:
        ...


class MockTransport(Transport):
    """Writes the record to a local outbox instead of sending it.

    Used when no API endpoint is configured, so the full flow is exercisable --
    and auditable -- without a live integration.
    """

    name = "mock"

    def __init__(self, outbox: Path = DEFAULT_OUTBOX):
        self.outbox = Path(outbox)

    def deliver(self, record: TenCapitalRecord) -> TransmissionResult:
        timestamp = datetime.now(timezone.utc)
        try:
            self.outbox.mkdir(parents=True, exist_ok=True)
            safe_name = "".join(
                ch if ch.isalnum() or ch in "-_" else "_"
                for ch in (record.company.name or "unnamed")
            )[:60]
            path = self.outbox / f"{timestamp:%Y%m%dT%H%M%SZ}_{safe_name}.json"
            path.write_text(
                json.dumps(record.model_dump(mode="json"), indent=2), encoding="utf-8"
            )
        except OSError as exc:
            raise TransmissionError(
                f"Could not write the record to the outbox: {exc}"
            ) from exc

        logger.info("Record written to outbox: %s", path)
        return TransmissionResult(
            success=True,
            transport=self.name,
            detail=(
                f"No TEN Capital endpoint is configured, so the record was written to "
                f"{path} rather than transmitted. Set {ENDPOINT_ENV} to enable live delivery."
            ),
            reference=str(path),
            timestamp=timestamp.isoformat(),
        )


class HttpTransport(Transport):
    """Posts the record to the configured TEN Capital endpoint."""

    name = "https"

    def __init__(self, endpoint: str, api_key: Optional[str] = None, timeout: float = 30.0):
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout = timeout

    def deliver(self, record: TenCapitalRecord) -> TransmissionResult:
        try:
            import requests
        except ImportError as exc:
            raise TransmissionError(
                "The 'requests' package is required for live transmission. "
                "Install it, or unset the endpoint to use the local outbox."
            ) from exc

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            response = requests.post(
                self.endpoint,
                json=record.model_dump(mode="json"),
                headers=headers,
                timeout=self.timeout,
            )
        except Exception as exc:
            raise TransmissionError(f"Could not reach TEN Capital: {exc}") from exc

        if not 200 <= response.status_code < 300:
            raise TransmissionError(
                f"TEN Capital rejected the record with status {response.status_code}: "
                f"{response.text[:300]}"
            )

        reference = None
        try:
            reference = response.json().get("id")
        except Exception:
            logger.debug("Response body was not JSON; no reference recorded.")

        return TransmissionResult(
            success=True,
            transport=self.name,
            detail=f"Record accepted by TEN Capital (HTTP {response.status_code}).",
            reference=reference,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )


def default_transport() -> Transport:
    """Live transport when an endpoint is configured, otherwise the mock."""
    endpoint = os.environ.get(ENDPOINT_ENV, "").strip()
    if not endpoint:
        return MockTransport()
    try:
        timeout = float(os.environ.get(TIMEOUT_ENV, "30"))
    except ValueError:
        timeout = 30.0
    return HttpTransport(endpoint, os.environ.get(API_KEY_ENV) or None, timeout)


def send_to_ten_capital(
    record: TenCapitalRecord, transport: Optional[Transport] = None
) -> TransmissionResult:
    """Transmit a record, but only with explicit consent.

    The consent check happens here, before the transport is even constructed, so
    there is no code path that reaches a network call without it.
    """
    if not record.consent.share_with_ten_capital:
        raise ConsentError(
            "This record has not been approved for sharing with TEN Capital. "
            "The user must explicitly tick 'Share this analysis with TEN Capital' "
            "before it can be transmitted. Nothing has been sent."
        )
    if not record.consent.analysis_consent:
        raise ConsentError(
            "Sharing was requested but consent to analyse the document was never "
            "given. Nothing has been sent."
        )

    transport = transport or default_transport()
    logger.info(
        "Transmitting record for %s via %s transport.",
        record.company.name or "unnamed company",
        transport.name,
    )
    return transport.deliver(record)


def export_record_json(record: TenCapitalRecord, indent: int = 2) -> str:
    """Serialise the record for local download. Always permitted.

    Exporting to the user's own machine is not transmission, so it is not gated
    on the sharing consent.
    """
    return json.dumps(record.model_dump(mode="json"), indent=indent)

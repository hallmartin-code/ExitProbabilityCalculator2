"""Validation and parsing of user-supplied inputs.

Comparable transactions arrive from humans, spreadsheets and exports, so this
module is deliberately forgiving about shape and strict about meaning: it accepts
many column spellings and value formats, and it reports every row it could not
use rather than dropping it quietly.
"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from pydantic import ValidationError

from ..models.comparables import BuyerType, ComparableExit, ComparableSet, TransactionType


class InputValidationError(ValueError):
    """Raised when an uploaded file cannot be parsed at all."""


#: Accepted column spellings, normalised key -> the many ways people write it.
COLUMN_ALIASES: Dict[str, Tuple[str, ...]] = {
    "company": ("company", "target", "company_name", "name", "target_company"),
    "sector": ("sector", "industry", "vertical"),
    "transaction_date": ("transaction_date", "date", "close_date", "announced", "announced_date"),
    "buyer": ("buyer", "acquirer", "purchaser"),
    "buyer_type": ("buyer_type", "acquirer_type", "buyertype"),
    "transaction_type": ("transaction_type", "deal_type", "type", "transactiontype"),
    "transaction_value": (
        "transaction_value", "deal_value", "value", "ev", "enterprise_value", "price",
    ),
    "revenue_at_exit": ("revenue_at_exit", "revenue", "ttm_revenue", "arr_at_exit", "sales"),
    "ebitda_at_exit": ("ebitda_at_exit", "ebitda", "ttm_ebitda"),
    "revenue_multiple": ("revenue_multiple", "ev_revenue", "rev_multiple", "ev/revenue", "x_revenue"),
    "ebitda_multiple": ("ebitda_multiple", "ev_ebitda", "ev/ebitda", "x_ebitda"),
    "growth_rate": ("growth_rate", "growth", "yoy_growth", "revenue_growth"),
    "strategic_rationale": ("strategic_rationale", "rationale", "notes", "comment"),
}

_BUYER_TYPE_HINTS = {
    BuyerType.STRATEGIC: ("strategic", "corporate", "trade", "industry"),
    BuyerType.FINANCIAL: ("financial", "pe", "private equity", "buyout", "sponsor", "growth equity"),
    BuyerType.PUBLIC_MARKET: ("ipo", "public", "listing", "spac", "direct listing"),
}

_TRANSACTION_TYPE_HINTS = {
    TransactionType.IPO: ("ipo", "listing", "spac", "public offering"),
    TransactionType.MERGER: ("merger", "merge"),
    TransactionType.ASSET_SALE: ("asset", "asset sale", "carve"),
    TransactionType.ACQUISITION: ("acquisition", "acquire", "buyout", "takeover", "m&a"),
}

#: Multipliers for the shorthand people write in spreadsheets.
_SCALE_SUFFIXES = {"k": 1e3, "m": 1e6, "mm": 1e6, "b": 1e9, "bn": 1e9, "t": 1e12}


def _normalize_key(key: str) -> Optional[str]:
    cleaned = re.sub(r"[^a-z0-9/]+", "_", str(key).strip().lower()).strip("_")
    for canonical, aliases in COLUMN_ALIASES.items():
        if cleaned == canonical or cleaned in aliases:
            return canonical
    return None


def parse_money(raw: Any) -> Optional[float]:
    """Parse '$1.2B', '450k', '1,200,000' or a number into a float.

    Returns None for blanks and unparseable text so a bad cell never becomes 0.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip().lower().replace("$", "").replace(",", "").replace("usd", "").strip()
    if not text or text in {"-", "n/a", "na", "none", "null", "tbd", "undisclosed"}:
        return None
    match = re.fullmatch(r"(-?\d*\.?\d+)\s*([kmbt]{1,2})?", text)
    if not match:
        return None
    value = float(match.group(1))
    suffix = match.group(2)
    if suffix:
        value *= _SCALE_SUFFIXES.get(suffix, 1.0)
    return value


def parse_percent(raw: Any) -> Optional[float]:
    """Parse '45%', '0.45' or 45 into a decimal fraction.

    A bare number greater than 1 is read as a percentage, which is how people
    almost always write growth rates in a spreadsheet.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        return value / 100.0 if abs(value) > 1.0 else value
    text = str(raw).strip().replace(",", "")
    if not text or text in {"-", "n/a", "na", "none", "null"}:
        return None
    explicit_pct = text.endswith("%")
    text = text.rstrip("%").strip()
    try:
        value = float(text)
    except ValueError:
        return None
    if explicit_pct or abs(value) > 1.0:
        return value / 100.0
    return value


def parse_multiple(raw: Any) -> Optional[float]:
    """Parse '8.0x', '8' or 8.0 into a float."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip().lower().rstrip("x").replace(",", "").strip()
    if not text or text in {"-", "n/a", "na", "none", "null"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_date(raw: Any) -> Optional[date]:
    """Parse the common date spellings; year-only values map to 1 January."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text = str(raw).strip()
    if re.fullmatch(r"\d{4}", text):
        return date(int(text), 1, 1)
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%b-%Y", "%b %Y", "%B %Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_buyer_type(raw: Any) -> BuyerType:
    if raw is None:
        return BuyerType.UNKNOWN
    text = str(raw).strip().lower()
    for buyer_type, hints in _BUYER_TYPE_HINTS.items():
        if any(hint in text for hint in hints):
            return buyer_type
    return BuyerType.UNKNOWN


def parse_transaction_type(raw: Any) -> TransactionType:
    if raw is None:
        return TransactionType.UNKNOWN
    text = str(raw).strip().lower()
    for tx_type, hints in _TRANSACTION_TYPE_HINTS.items():
        if any(hint in text for hint in hints):
            return tx_type
    return TransactionType.UNKNOWN


def _row_to_comparable(row: Dict[str, Any]) -> ComparableExit:
    """Build a ComparableExit from one already key-normalised row."""
    company = str(row.get("company") or "").strip()
    if not company:
        raise ValueError("row has no company name")

    return ComparableExit(
        company=company,
        sector=(str(row["sector"]).strip() if row.get("sector") else None),
        transaction_date=parse_date(row.get("transaction_date")),
        buyer=(str(row["buyer"]).strip() if row.get("buyer") else None),
        buyer_type=parse_buyer_type(row.get("buyer_type")),
        transaction_type=parse_transaction_type(row.get("transaction_type")),
        transaction_value=parse_money(row.get("transaction_value")),
        revenue_at_exit=parse_money(row.get("revenue_at_exit")),
        ebitda_at_exit=parse_money(row.get("ebitda_at_exit")),
        revenue_multiple=parse_multiple(row.get("revenue_multiple")),
        ebitda_multiple=parse_multiple(row.get("ebitda_multiple")),
        growth_rate=parse_percent(row.get("growth_rate")),
        strategic_rationale=(
            str(row["strategic_rationale"]).strip() if row.get("strategic_rationale") else None
        ),
    )


def _normalize_rows(raw_rows: List[Dict[str, Any]]) -> Tuple[List[ComparableExit], List[str]]:
    comparables: List[ComparableExit] = []
    warnings: List[str] = []

    for index, raw in enumerate(raw_rows, start=1):
        if not isinstance(raw, dict):
            warnings.append(f"Row {index}: expected an object, got {type(raw).__name__}.")
            continue
        mapped: Dict[str, Any] = {}
        unrecognised: List[str] = []
        for key, value in raw.items():
            canonical = _normalize_key(key)
            if canonical is None:
                if str(key).strip():
                    unrecognised.append(str(key))
                continue
            if value is not None and str(value).strip() != "":
                mapped[canonical] = value
        try:
            comparables.append(_row_to_comparable(mapped))
        except (ValueError, ValidationError) as exc:
            warnings.append(f"Row {index}: skipped - {exc}")
            continue
        if unrecognised:
            warnings.append(
                f"Row {index}: ignored unrecognised column(s) {', '.join(sorted(set(unrecognised)))}."
            )
    return comparables, warnings


def load_comparables_csv(content: bytes | str) -> Tuple[ComparableSet, List[str]]:
    """Parse a CSV of comparable transactions.

    Returns the usable comparables plus a warning for every row or column that
    could not be interpreted, so nothing is discarded invisibly.
    """
    text = content.decode("utf-8-sig") if isinstance(content, bytes) else content
    if not text.strip():
        raise InputValidationError("The CSV file is empty.")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise InputValidationError("The CSV file has no header row.")
    if not any(_normalize_key(f) == "company" for f in reader.fieldnames if f):
        raise InputValidationError(
            "The CSV needs a company/target column. Recognised headers include: "
            + ", ".join(sorted(COLUMN_ALIASES))
        )
    rows = [dict(r) for r in reader]
    comparables, warnings = _normalize_rows(rows)
    if not comparables:
        raise InputValidationError(
            "No usable comparable rows were found. " + (" ".join(warnings[:3]) if warnings else "")
        )
    return ComparableSet(comparables=comparables), warnings


def load_comparables_json(content: bytes | str) -> Tuple[ComparableSet, List[str]]:
    """Parse a JSON list of comparables, or an object with a 'comparables' key."""
    text = content.decode("utf-8-sig") if isinstance(content, bytes) else content
    if not text.strip():
        raise InputValidationError("The JSON file is empty.")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InputValidationError(
            f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc

    if isinstance(data, dict):
        data = data.get("comparables", data.get("transactions"))
    if not isinstance(data, list):
        raise InputValidationError(
            "Expected a JSON list of comparables, or an object with a 'comparables' list."
        )
    comparables, warnings = _normalize_rows(data)
    if not comparables:
        raise InputValidationError(
            "No usable comparable rows were found. " + (" ".join(warnings[:3]) if warnings else "")
        )
    return ComparableSet(comparables=comparables), warnings


def validate_percentage_input(
    value: Optional[float], field_name: str, allow_above_one: bool = True
) -> Optional[float]:
    """Coerce a UI percentage entry to a decimal fraction, or raise a clear error."""
    if value is None:
        return None
    number = float(value)
    if allow_above_one and number > 1.0:
        number = number / 100.0
    if not -10.0 <= number <= 10.0:
        raise InputValidationError(
            f"{field_name} of {value} is outside a plausible range. Enter a percentage such as 45."
        )
    return number

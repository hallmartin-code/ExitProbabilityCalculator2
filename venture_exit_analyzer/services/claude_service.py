"""Claude API integration.

The AI layer is deliberately bounded. Per the application's design rules it does
three things and no more:

  1. **Extraction** -- reads the deck and reports facts the deterministic parser
     could not find, each with the page it came from and a verbatim quote.
  2. **Classification** -- sector, business model, and other judgements that
     keyword matching does badly.
  3. **Narrative** -- prose for the exit-profile paragraph.

It is never the calculator. Scores, probabilities, valuations and capital
requirements remain deterministic functions of the reviewed dossier, and are not
shown to the model as anything it may revise.

Two safety properties are enforced here rather than trusted:

  * **The model may only fill gaps.** A value the deterministic parser already
    found is never overwritten, so a confident hallucination cannot displace a
    figure that was actually read off the page.
  * **The narrative may not introduce numbers.** Any figure in generated prose
    must appear in the allow-list of computed facts, or the narrative is
    rejected and the deterministic one is used instead.

Every extracted value is marked ``INFERRED`` and surfaced in the review step, so
nothing the model produced reaches a report without a human seeing it first.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..ingestion.base import ExtractedDocument
from ..models.analysis import DealDossier
from ..models.company import Stage
from ..models.evidence import Evidenced
from ..utils.env import env_bool, env_float, env_int, load_dotenv, redact

logger = logging.getLogger(__name__)

API_KEY_ENV = "ANTHROPIC_API_KEY"
MODEL_ENV = "CLAUDE_MODEL"
ENABLED_ENV = "CLAUDE_ENABLED"

#: Sonnet 5 balances extraction accuracy against latency for a deck-sized input.
DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TIMEOUT_SECONDS = 120.0

#: Deck text sent to the model, in characters. A pitch deck is far smaller than
#: this; the cap exists so a pathological document cannot produce a huge bill.
MAX_DECK_CHARS = 120_000


class ClaudeUnavailable(RuntimeError):
    """Raised when an AI operation is attempted without a working client."""


# ---------------------------------------------------------------------------
# Field mapping
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FieldSpec:
    """One field the model is permitted to report."""

    name: str
    kind: str  # money | percent | integer | text | boolean | list
    description: str
    apply: Callable[[DealDossier, Any, Evidenced], None]
    #: Plausibility bounds for numeric fields. A value outside them is discarded
    #: rather than trusted, because a misplaced decimal is the most common and
    #: most damaging extraction error.
    minimum: Optional[float] = None
    maximum: Optional[float] = None


def _set(path: str) -> Callable[[DealDossier, Any, Evidenced], None]:
    """Build a setter that assigns an Evidenced value at a dotted dossier path."""
    section, attribute = path.split(".", 1)

    def apply(dossier: DealDossier, value: Any, evidenced: Evidenced) -> None:
        setattr(getattr(dossier, section), attribute, evidenced)

    return apply


def _set_list(path: str) -> Callable[[DealDossier, Any, Evidenced], None]:
    section, attribute = path.split(".", 1)

    def apply(dossier: DealDossier, value: Any, evidenced: Evidenced) -> None:
        items = value if isinstance(value, list) else [value]
        cleaned = [str(item).strip() for item in items if str(item).strip()]
        if cleaned:
            setattr(getattr(dossier, section), attribute, cleaned)

    return apply


def _set_stage(dossier: DealDossier, value: Any, evidenced: Evidenced) -> None:
    try:
        stage = Stage(str(value).strip().lower().replace(" ", "_").replace("-", "_"))
    except ValueError:
        return
    dossier.company.stage = Evidenced(
        value=stage,
        source=evidenced.source,
        source_page_or_slide=evidenced.source_page_or_slide,
        evidence_class=evidenced.evidence_class,
        confidence=evidenced.confidence,
    )
    dossier.company.current_financing_stage = Evidenced(
        value=stage.label,
        source=evidenced.source,
        source_page_or_slide=evidenced.source_page_or_slide,
        evidence_class=evidenced.evidence_class,
        confidence=evidenced.confidence,
    )


def _set_named_buyers(dossier: DealDossier, value: Any, evidenced: Evidenced) -> None:
    items = value if isinstance(value, list) else [value]
    cleaned = [str(item).strip() for item in items if str(item).strip()]
    if cleaned:
        dossier.named_buyers = sorted(set(dossier.named_buyers) | set(cleaned))


#: Every field the model may report. Anything outside this list is ignored, so a
#: model that invents a field name cannot write anywhere unexpected.
FIELD_SPECS: Tuple[FieldSpec, ...] = (
    # -- company ---------------------------------------------------------
    FieldSpec("company.name", "text", "Legal or trading name of the company.", _set("company.name")),
    FieldSpec("company.website", "text", "Company website URL.", _set("company.website")),
    FieldSpec("company.headquarters", "text", "Headquarters city and country.", _set("company.headquarters")),
    FieldSpec("company.sector", "text", "Broad sector, e.g. Healthcare, Technology, Industrials, Consumer.", _set("company.sector")),
    FieldSpec("company.subsector", "text", "Specific subsector, e.g. Diagnostics, Enterprise Software.", _set("company.subsector")),
    FieldSpec("company.business_model", "text", "How the company charges: SaaS subscription, per-transaction, licensing, hardware.", _set("company.business_model")),
    FieldSpec("company.founding_year", "integer", "Year founded.", _set("company.founding_year"), 1800, 2100),
    FieldSpec("company.stage", "text", "Financing stage. One of: pre_seed, seed, series_a, series_b, series_c, growth.", _set_stage),
    # -- financials ------------------------------------------------------
    FieldSpec("financials.arr", "money", "Annual recurring revenue in USD.", _set("financials.arr"), 0, 1e12),
    FieldSpec("financials.annual_revenue", "money", "Total annual revenue in USD. Do NOT report a percentage here.", _set("financials.annual_revenue"), 0, 1e12),
    FieldSpec("financials.revenue_growth_rate", "percent", "Year-over-year revenue growth as a decimal fraction (1.63 means 163%).", _set("financials.revenue_growth_rate"), -1.0, 100.0),
    FieldSpec("financials.gross_margin", "percent", "Gross margin as a decimal fraction (0.78 means 78%).", _set("financials.gross_margin"), -10.0, 1.0),
    FieldSpec("financials.ebitda_margin", "percent", "EBITDA or operating margin as a decimal fraction. Keep the sign: -0.45 for a 45% loss.", _set("financials.ebitda_margin"), -100.0, 1.0),
    FieldSpec("financials.monthly_burn", "money", "Net monthly cash burn in USD.", _set("financials.monthly_burn"), 0, 1e10),
    FieldSpec("financials.runway_months", "integer", "Months of cash runway remaining.", _set("financials.runway_months"), 0, 600),
    FieldSpec("financials.current_cash", "money", "Cash on hand in USD.", _set("financials.current_cash"), 0, 1e12),
    FieldSpec("financials.capital_raised_to_date", "money", "Total capital raised to date in USD.", _set("financials.capital_raised_to_date"), 0, 1e12),
    FieldSpec("financials.amount_raising", "money", "Amount being raised in the current round, in USD.", _set("financials.amount_raising"), 0, 1e12),
    # -- market ----------------------------------------------------------
    FieldSpec("market.tam", "money", "Total addressable market in USD.", _set("market.tam"), 0, 1e14),
    FieldSpec("market.sam", "money", "Serviceable addressable market in USD.", _set("market.sam"), 0, 1e14),
    FieldSpec("market.som", "money", "Serviceable obtainable market in USD.", _set("market.som"), 0, 1e14),
    FieldSpec("market.market_growth_rate", "percent", "Market growth rate as a decimal fraction.", _set("market.market_growth_rate"), -1.0, 10.0),
    FieldSpec("market.market_share", "percent", "Company market share as a decimal fraction.", _set("market.market_share"), 0, 1.0),
    FieldSpec("market.category_leadership", "boolean", "True only if the deck explicitly claims category or market leadership.", _set("market.category_leadership")),
    FieldSpec("market.differentiation", "text", "One sentence on what makes the product different.", _set("market.differentiation")),
    FieldSpec("market.competitive_advantages", "list", "Distinct competitive advantages claimed.", _set_list("market.competitive_advantages")),
    # -- customers -------------------------------------------------------
    FieldSpec("customers.customer_count", "integer", "Number of customers.", _set("customers.customer_count"), 0, 1_000_000_000),
    FieldSpec("customers.customer_type", "text", "Type of customer, e.g. hospitals, enterprises, consumers.", _set("customers.customer_type")),
    FieldSpec("customers.major_customers", "list", "Named customers disclosed in the deck.", _set_list("customers.major_customers")),
    FieldSpec("customers.customer_concentration", "percent", "Revenue share of the largest customer, as a decimal fraction.", _set("customers.customer_concentration"), 0, 1.0),
    FieldSpec("customers.contract_duration_months", "integer", "Typical contract length in months.", _set("customers.contract_duration_months"), 0, 600),
    FieldSpec("customers.recurring_revenue_pct", "percent", "Share of revenue that is recurring, as a decimal fraction.", _set("customers.recurring_revenue_pct"), 0, 1.0),
    FieldSpec("customers.retention_rate", "percent", "Gross or logo retention as a decimal fraction. NOT net revenue retention.", _set("customers.retention_rate"), 0, 2.0),
    FieldSpec("customers.net_revenue_retention", "percent", "Net revenue retention as a decimal fraction (1.18 means 118%).", _set("customers.net_revenue_retention"), 0, 5.0),
    # -- intellectual property -------------------------------------------
    FieldSpec("ip.patents_granted", "integer", "Number of granted or issued patents.", _set("ip.patents_granted"), 0, 100_000),
    FieldSpec("ip.patent_applications", "integer", "Number of pending patent applications.", _set("ip.patent_applications"), 0, 100_000),
    FieldSpec("ip.regulatory_approvals", "list", "Regulatory approvals or clearances held, e.g. 'FDA 510(k) clearance'.", _set_list("ip.regulatory_approvals")),
    FieldSpec("ip.proprietary_technology", "text", "The core proprietary technology or mechanism.", _set("ip.proprietary_technology")),
    FieldSpec("ip.proprietary_data", "text", "Any proprietary dataset the company holds.", _set("ip.proprietary_data")),
    FieldSpec("ip.clinical_assets", "list", "Clinical programmes or assets.", _set_list("ip.clinical_assets")),
    # -- ownership -------------------------------------------------------
    FieldSpec("ownership.founder_ownership", "percent", "Combined founder ownership as a decimal fraction.", _set("ownership.founder_ownership"), 0, 1.0),
    FieldSpec("ownership.major_investors", "list", "Named existing investors.", _set_list("ownership.major_investors")),
    # -- buyers ----------------------------------------------------------
    FieldSpec("named_buyers", "list", "Companies the deck explicitly names as potential acquirers or exit partners. Only include names the deck actually states.", _set_named_buyers),
)

FIELD_INDEX: Dict[str, FieldSpec] = {spec.name: spec for spec in FIELD_SPECS}


def missing_field_names(dossier: DealDossier) -> List[str]:
    """Field names in FIELD_SPECS that the dossier does not yet populate."""
    missing: List[str] = []
    for spec in FIELD_SPECS:
        if spec.name == "named_buyers":
            if not dossier.named_buyers:
                missing.append(spec.name)
            continue
        section_name, attribute = spec.name.split(".", 1)
        section = getattr(dossier, section_name, None)
        current = getattr(section, attribute, None) if section else None
        if isinstance(current, Evidenced):
            if not current.is_present:
                missing.append(spec.name)
        elif isinstance(current, list):
            if not current:
                missing.append(spec.name)
    return missing


def apply_user_value(dossier: DealDossier, name: str, value: Any) -> None:
    """Write a user-entered value for a FIELD_SPECS field, in canonical units.

    The user is held to the same plausibility bounds as the model, so a typo
    such as a margin of 7800% is refused rather than scored. Raises ValueError
    with a user-facing message when the value is unusable.
    """
    spec = FIELD_INDEX.get(name)
    if spec is None:
        raise ValueError(f"{name} is not a recognised field.")

    if spec.kind == "list":
        items = value if isinstance(value, list) else [value]
        value = [str(item).strip() for item in items if str(item).strip()]
        if not value:
            raise ValueError("enter at least one item.")
    elif spec.kind == "boolean":
        if not isinstance(value, bool):
            raise ValueError("choose yes or no.")
    elif spec.kind == "text":
        value = str(value or "").strip()
        if not value:
            raise ValueError("enter a value.")
        if name == "company.stage":
            Stage(value)  # raises ValueError for an unknown stage
    else:
        number = float(value)
        if spec.minimum is not None and number < spec.minimum:
            raise ValueError("value is below the plausible minimum.")
        if spec.maximum is not None and number > spec.maximum:
            raise ValueError("value is above the plausible maximum.")
        value = int(round(number)) if spec.kind == "integer" else number

    evidenced = Evidenced.from_user(value, note="Entered by the user after extraction.")
    spec.apply(dossier, value, evidenced)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
@dataclass
class ExtractionOutcome:
    """What the model contributed, and what was refused."""

    applied: List[str] = field(default_factory=list)
    skipped_existing: List[str] = field(default_factory=list)
    rejected: List[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""

    @property
    def applied_count(self) -> int:
        return len(self.applied)

    @property
    def rejected_fields(self) -> Dict[str, str]:
        """Recognised field names whose proposed value was refused, with the reason."""
        reasons: Dict[str, str] = {}
        for item in self.rejected:
            name, _, reason = item.partition(": ")
            if name in FIELD_INDEX:
                reasons.setdefault(name, reason)
        return reasons

    def summary(self) -> str:
        if not self.applied and not self.rejected:
            return "The model found nothing the deterministic parser had missed."
        parts = [f"{len(self.applied)} field(s) filled"]
        if self.skipped_existing:
            parts.append(f"{len(self.skipped_existing)} already extracted deterministically")
        if self.rejected:
            parts.append(f"{len(self.rejected)} rejected as implausible")
        return "; ".join(parts) + "."


@dataclass
class NarrativeOutcome:
    text: Optional[str] = None
    accepted: bool = False
    rejection_reason: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclass
class ClaudeConfig:
    api_key: Optional[str] = None
    model: str = DEFAULT_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    enabled: bool = True

    @classmethod
    def from_env(cls, load_file: bool = True) -> "ClaudeConfig":
        if load_file:
            load_dotenv()
        return cls(
            api_key=(os.environ.get(API_KEY_ENV) or "").strip() or None,
            model=(os.environ.get(MODEL_ENV) or DEFAULT_MODEL).strip(),
            max_tokens=env_int("CLAUDE_MAX_TOKENS", DEFAULT_MAX_TOKENS),
            timeout=env_float("CLAUDE_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS),
            enabled=env_bool(ENABLED_ENV, True),
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key) and self.enabled

    def describe(self) -> str:
        if not self.enabled:
            return "AI assistance is switched off (CLAUDE_ENABLED=false)."
        if not self.api_key:
            return (
                f"No API key found. Set {API_KEY_ENV} to enable AI-assisted extraction. "
                f"The application runs fully without it, using deterministic parsing only."
            )
        return f"Model {self.model}, key {redact(self.api_key)}."


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class ClaudeService:
    """Bounded Claude access for extraction and narrative generation."""

    def __init__(self, config: Optional[ClaudeConfig] = None, client: Any = None):
        self.config = config or ClaudeConfig.from_env()
        self._client = client
        self._init_error: Optional[str] = None
        if client is None and self.config.is_configured:
            self._client = self._build_client()

    def _build_client(self) -> Any:
        try:
            import anthropic
        except ImportError:
            self._init_error = (
                "The 'anthropic' package is not installed. Run: pip install anthropic"
            )
            logger.warning(self._init_error)
            return None
        try:
            return anthropic.Anthropic(
                api_key=self.config.api_key, timeout=self.config.timeout
            )
        except Exception as exc:
            self._init_error = f"Could not initialise the Claude client: {exc}"
            logger.warning(self._init_error)
            return None

    @property
    def available(self) -> bool:
        return self._client is not None

    @property
    def status(self) -> str:
        if self._init_error:
            return self._init_error
        if not self.available:
            return self.config.describe()
        return f"Connected. {self.config.describe()}"

    # -- transport -------------------------------------------------------
    def _call_tool(
        self, system: str, prompt: str, tool: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], int, int]:
        """Invoke the model and require it to answer through a tool schema.

        Forcing tool use is what makes the response parseable; free-text JSON
        from a model is a parsing problem waiting to happen.
        """
        if not self.available:
            raise ClaudeUnavailable(self.status)

        response = self._client.messages.create(
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            system=system,
            tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]},
            messages=[{"role": "user", "content": prompt}],
        )
        payload: Dict[str, Any] = {}
        for block in response.content:
            if getattr(block, "type", None) == "tool_use":
                payload = dict(block.input or {})
                break
        usage = getattr(response, "usage", None)
        return (
            payload,
            getattr(usage, "input_tokens", 0) or 0,
            getattr(usage, "output_tokens", 0) or 0,
        )

    # -- extraction ------------------------------------------------------
    def extract(
        self, document: ExtractedDocument, dossier: DealDossier
    ) -> ExtractionOutcome:
        """Fill gaps the deterministic parser left, in place on ``dossier``.

        Values already extracted deterministically are never overwritten.
        """
        outcome = ExtractionOutcome(model=self.config.model)
        if not self.available:
            raise ClaudeUnavailable(self.status)

        missing = self._missing_field_names(dossier)
        if not missing:
            return outcome

        payload, input_tokens, output_tokens = self._call_tool(
            system=_EXTRACTION_SYSTEM,
            prompt=_extraction_prompt(document, missing, dossier),
            tool=_extraction_tool(missing),
        )
        outcome.input_tokens, outcome.output_tokens = input_tokens, output_tokens

        for fact in payload.get("facts", []) or []:
            self._apply_fact(dossier, fact, document, outcome)
        return outcome

    def _missing_field_names(self, dossier: DealDossier) -> List[str]:
        """Field names the deterministic pass did not populate."""
        return missing_field_names(dossier)

    def _apply_fact(
        self,
        dossier: DealDossier,
        fact: Dict[str, Any],
        document: ExtractedDocument,
        outcome: ExtractionOutcome,
    ) -> None:
        name = str(fact.get("field", "")).strip()
        spec = FIELD_INDEX.get(name)
        if spec is None:
            outcome.rejected.append(f"{name or '(unnamed)'}: not a recognised field")
            return

        if name not in self._missing_field_names(dossier):
            outcome.skipped_existing.append(name)
            return

        value = self._coerce(spec, fact, outcome)
        if value is None:
            return

        page = fact.get("source_page")
        page = int(page) if isinstance(page, (int, float)) and page > 0 else None
        quote = str(fact.get("quote", "") or "").strip()[:160]
        confidence = fact.get("confidence")
        confidence = (
            float(confidence) if isinstance(confidence, (int, float)) else 0.55
        )
        # AI-extracted values are capped below the confidence given to a value
        # read directly off the page by the deterministic parser.
        confidence = max(0.1, min(float(confidence), 0.75))

        unit = document.unit_name
        source = f"Claude extraction from {unit} {page}" if page else "Claude extraction"
        if quote:
            source += f': "{quote}"'

        evidenced = Evidenced.inferred(value, source, confidence)
        evidenced.source_page_or_slide = page
        evidenced.note = "Extracted by Claude. Confirm before relying on it."

        spec.apply(dossier, value, evidenced)
        outcome.applied.append(name)

    def _coerce(
        self, spec: FieldSpec, fact: Dict[str, Any], outcome: ExtractionOutcome
    ) -> Any:
        """Convert and sanity-check a reported value, or reject it."""
        if spec.kind == "list":
            items = fact.get("value_list") or []
            if isinstance(items, str):
                items = [items]
            cleaned = [str(i).strip() for i in items if str(i).strip()]
            if not cleaned:
                outcome.rejected.append(f"{spec.name}: empty list")
                return None
            return cleaned

        if spec.kind == "boolean":
            value = fact.get("value_boolean")
            if not isinstance(value, bool):
                outcome.rejected.append(f"{spec.name}: not a boolean")
                return None
            return value

        if spec.kind == "text":
            value = str(fact.get("value_text", "") or "").strip()
            if not value:
                outcome.rejected.append(f"{spec.name}: empty text")
                return None
            return value[:300]

        raw = fact.get("value_numeric")
        if not isinstance(raw, (int, float)):
            outcome.rejected.append(f"{spec.name}: not numeric")
            return None
        number = float(raw)

        if spec.minimum is not None and number < spec.minimum:
            outcome.rejected.append(f"{spec.name}: {number:g} below plausible minimum")
            return None
        if spec.maximum is not None and number > spec.maximum:
            outcome.rejected.append(f"{spec.name}: {number:g} above plausible maximum")
            return None

        return int(number) if spec.kind == "integer" else number

    # -- narrative -------------------------------------------------------
    def narrate(
        self,
        facts: Dict[str, Any],
        allowed_numbers: List[str],
        char_limit: int = 620,
    ) -> NarrativeOutcome:
        """Generate the exit-profile paragraph from computed facts.

        The model is given the conclusions, not the freedom to reach its own. Any
        figure it writes that is not in ``allowed_numbers`` causes the narrative
        to be rejected, so generated prose can never contradict the model-free
        numbers printed beside it.
        """
        if not self.available:
            raise ClaudeUnavailable(self.status)

        payload, input_tokens, output_tokens = self._call_tool(
            system=_NARRATIVE_SYSTEM,
            prompt=_narrative_prompt(facts, char_limit),
            tool=_NARRATIVE_TOOL,
        )
        outcome = NarrativeOutcome(input_tokens=input_tokens, output_tokens=output_tokens)
        text = " ".join(str(payload.get("narrative", "") or "").split())

        if not text:
            outcome.rejection_reason = "The model returned no narrative."
            return outcome
        if len(text) > char_limit * 1.35:
            outcome.rejection_reason = (
                f"Narrative was {len(text)} characters against a {char_limit} budget."
            )
            return outcome

        invented = _unsupported_figures(text, allowed_numbers)
        if invented:
            outcome.rejection_reason = (
                f"Narrative introduced figures absent from the computed analysis: "
                f"{', '.join(invented)}."
            )
            outcome.text = text
            return outcome

        outcome.text = text
        outcome.accepted = True
        return outcome


# ---------------------------------------------------------------------------
# Prompt and tool construction
# ---------------------------------------------------------------------------
_EXTRACTION_SYSTEM = """You are a data extraction engine for a venture capital analysis tool.

You read pitch decks and report ONLY facts the deck actually states. You are not an \
analyst and you are not being asked for judgement about the company's prospects.

Rules you must follow:
- Report a field only if the deck states it or it follows arithmetically from something \
the deck states. If the deck does not say it, omit the field entirely.
- Never estimate, infer from industry norms, or fill a gap with a typical value.
- Always cite the page or slide number and a short verbatim quote for each fact.
- Express all money in whole USD: "$4.2M" becomes 4200000.
- Express all percentages as decimal fractions: "78%" becomes 0.78, "163%" becomes 1.63.
- Preserve signs. "EBITDA margin -45%" is -0.45, not 0.45.
- Do not confuse a percentage with a currency amount.
- Net revenue retention and gross/logo retention are different metrics. Do not merge them.
- Set confidence below 0.5 when the deck is ambiguous.

Critically: you will be shown facts that have ALREADY been captured. Never report an \
already-captured fact again under a different field name. A single statement in the deck \
supports exactly one field. Specifically:
- A stated SAM is not a SOM. A stated TAM is not a SAM. If the deck gives only one \
market size, only one market field is populated.
- A stated ARR is not separately "annual revenue". Report annual revenue only if the \
deck gives a total revenue figure distinct from ARR.
- Gross retention is not net revenue retention.
If the deck does not independently state a field, leave it out. An empty field is \
correct; a re-labelled duplicate is a fabrication.

Returning fewer, correct facts is much better than returning more, uncertain ones."""

_NARRATIVE_SYSTEM = """You write a single paragraph for an institutional venture capital \
report. The analysis has already been completed by a deterministic model; you are \
writing prose for conclusions that are already fixed.

Rules you must follow:
- Use ONLY the facts provided. Introduce no figure, percentage, company name or claim \
that is not in the input.
- Do not soften or strengthen the conclusion. If the most likely outcome is that no \
liquidity event occurs, say so plainly and first.
- Write for a partner who has not seen the deck. Be specific and unsentimental.
- No hedging filler, no marketing language, no recommendation to invest.
- One paragraph of continuous prose. No bullets, no headings."""


def _extraction_tool(missing_fields: List[str]) -> Dict[str, Any]:
    """Build the extraction tool schema, restricted to the fields still needed."""
    descriptions = "\n".join(
        f"- {name} ({FIELD_INDEX[name].kind}): {FIELD_INDEX[name].description}"
        for name in missing_fields
    )
    return {
        "name": "record_extracted_facts",
        "description": (
            "Record facts found in the pitch deck. Only these fields are accepted:\n"
            f"{descriptions}"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "facts": {
                    "type": "array",
                    "description": "One entry per fact actually found in the deck.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "field": {
                                "type": "string",
                                "enum": missing_fields,
                                "description": "Which field this fact populates.",
                            },
                            "value_numeric": {
                                "type": "number",
                                "description": "For money/percent/integer fields. USD whole numbers; percentages as decimal fractions.",
                            },
                            "value_text": {
                                "type": "string",
                                "description": "For text fields.",
                            },
                            "value_boolean": {
                                "type": "boolean",
                                "description": "For boolean fields.",
                            },
                            "value_list": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "For list fields.",
                            },
                            "source_page": {
                                "type": "integer",
                                "description": "Page or slide number the fact appears on.",
                            },
                            "quote": {
                                "type": "string",
                                "description": "Short verbatim quote from the deck supporting this fact.",
                            },
                            "confidence": {
                                "type": "number",
                                "description": "0.0 to 1.0. Below 0.5 when the deck is ambiguous.",
                            },
                        },
                        "required": ["field", "source_page", "quote", "confidence"],
                    },
                }
            },
            "required": ["facts"],
        },
    }


_NARRATIVE_TOOL: Dict[str, Any] = {
    "name": "record_narrative",
    "description": "Record the exit-profile paragraph.",
    "input_schema": {
        "type": "object",
        "properties": {
            "narrative": {
                "type": "string",
                "description": "One paragraph of continuous prose.",
            }
        },
        "required": ["narrative"],
    },
}


def _known_facts_summary(dossier: DealDossier) -> str:
    """What the deterministic parser already captured.

    Shown to the model so it cannot re-map a fact it can see onto an empty field
    of a different name -- the failure mode that turns a stated SAM into a
    fabricated SOM, or an ARR into a duplicated "annual revenue".
    """
    lines: List[str] = []
    for spec in FIELD_SPECS:
        if spec.name == "named_buyers":
            if dossier.named_buyers:
                lines.append(f"  {spec.name} = {', '.join(dossier.named_buyers)}")
            continue
        section_name, attribute = spec.name.split(".", 1)
        section = getattr(dossier, section_name, None)
        current = getattr(section, attribute, None) if section else None
        if isinstance(current, Evidenced) and current.is_present:
            lines.append(f"  {spec.name} = {current.value}")
        elif isinstance(current, list) and current:
            lines.append(f"  {spec.name} = {', '.join(str(i) for i in current)}")
    return "\n".join(lines) if lines else "  (nothing captured yet)"


def _extraction_prompt(
    document: ExtractedDocument, missing_fields: List[str], dossier: DealDossier
) -> str:
    pages = []
    budget = MAX_DECK_CHARS
    for page in document.pages:
        text = page.searchable_text()
        if not text:
            continue
        block = f"--- {document.unit_name.upper()} {page.number} ---\n{text}"
        if budget - len(block) < 0:
            pages.append(f"[Remaining {document.unit_name}s omitted: length limit reached.]")
            break
        pages.append(block)
        budget -= len(block)

    return (
        "Extract facts from this pitch deck.\n\n"
        "ALREADY CAPTURED - do not report these again, and do not satisfy an unknown\n"
        "field by re-labelling one of these statements:\n"
        + _known_facts_summary(dossier)
        + "\n\nSTILL UNKNOWN - the only fields you may report:\n"
        + "\n".join(f"  {name}" for name in missing_fields)
        + "\n\nIf the deck does not independently state one of these, omit it.\n\n"
        f"=== PITCH DECK ({document.filename}) ===\n" + "\n\n".join(pages)
    )


def _narrative_prompt(facts: Dict[str, Any], char_limit: int) -> str:
    return (
        "Write the LIKELY EXIT PROFILE paragraph for this report.\n\n"
        "Use only the facts below. Do not introduce any other figure or claim.\n\n"
        f"{json.dumps(facts, indent=2, default=str)}\n\n"
        f"Write at most {char_limit} characters. Return one paragraph."
    )


#: Figures a narrative might contain: percentages, currency amounts, multiples.
_FIGURE_PATTERN = re.compile(
    r"\$\s?\d[\d,]*(?:\.\d+)?\s*[KMB]?|\d[\d,]*(?:\.\d+)?\s*%|\b\d[\d,]*(?:\.\d+)?\s*x\b",
    re.IGNORECASE,
)


def _normalise_figure(text: str) -> str:
    return re.sub(r"[\s,]", "", text).upper()


def _unsupported_figures(text: str, allowed: List[str]) -> List[str]:
    """Figures in the narrative that do not appear in the computed facts.

    This is the guardrail that stops generated prose inventing a number. It is
    intentionally strict: anything not recognised is treated as unsupported.
    """
    permitted = {_normalise_figure(value) for value in allowed}
    found = {_normalise_figure(match) for match in _FIGURE_PATTERN.findall(text)}
    return sorted(found - permitted)


def build_service(config: Optional[ClaudeConfig] = None) -> ClaudeService:
    """Construct the service from the environment."""
    return ClaudeService(config or ClaudeConfig.from_env())

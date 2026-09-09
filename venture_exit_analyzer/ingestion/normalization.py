"""Normalisation: extracted document text -> a DealDossier with provenance.

This layer is deliberately conservative. It only records a value when a pattern
matches unambiguously, it always records the page or slide the value came from,
and it never fills a gap with a guess. Anything it cannot find is left MISSING for
the user to supply in the review step.

Deck language is promotional, so extracted values are marked EXPLICIT only when
the deck states them directly; anything the parser had to compute is CALCULATED
and anything it had to interpret is INFERRED, each with a lower confidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Pattern, Tuple

from ..models.analysis import DealDossier
from ..models.company import Stage
from ..models.evidence import Evidenced, EvidencedFloat, EvidencedInt, EvidencedStr
from ..models.financials import RevenuePoint
from ..utils.calculations import growth_rate_from_history
from ..utils.validation import parse_money, parse_percent
from .base import ExtractedDocument

#: A currency amount with an optional scale word: "$4.2M", "1.6 million", "450k".
#: The trailing guard stops a percentage ("92%") from being read as an amount.
MONEY = r"\$?\s?\d[\d,]*(?:\.\d+)?\s*(?:million|billion|thousand|mm|bn|[mbk])?\b(?!\s*%)"
#: A percentage, allowing a leading minus for negative margins.
PCT = r"-?\d[\d,]*(?:\.\d+)?\s*%"
#: A plain integer.
NUM = r"\d[\d,]*"
#: Horizontal whitespace only. Metric labels and their values sit on the same
#: line; allowing a newline lets a column header pair with the wrong row of a
#: table, which silently produces a plausible but incorrect figure.
HS = r"[ \t]"
#: Separators between a label and its value. A hyphen is excluded deliberately --
#: consuming it as a separator would turn "-45%" into "45%".
SEP = r"(?:of|is|at|:|=)?"

#: Confidence assigned to a value the deck stated in so many words.
STATED_CONFIDENCE = 0.85
#: Confidence assigned to a value read out of a keyword-matched context.
CONTEXTUAL_CONFIDENCE = 0.7
#: Confidence assigned to a value the normaliser had to interpret.
INFERRED_CONFIDENCE = 0.45


@dataclass(frozen=True)
class Match:
    """A raw regex hit, with the location that produced it."""

    raw: str
    page: int
    context: str


def _compile(patterns: List[str]) -> List[Pattern[str]]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]


def _search_pages(
    document: ExtractedDocument, patterns: List[Pattern[str]]
) -> Optional[Match]:
    """First match across the deck, scanning pages in order.

    Earlier pages win because decks state headline figures up front and repeat
    them later in less precise forms.
    """
    for page in document.pages:
        text = page.searchable_text()
        if not text:
            continue
        for pattern in patterns:
            found = pattern.search(text)
            if found:
                try:
                    raw = found.group("val")
                except (IndexError, KeyError):
                    raw = found.group(0)
                if raw and raw.strip():
                    start = max(0, found.start() - 60)
                    return Match(
                        raw=raw.strip(),
                        page=page.number,
                        context=" ".join(text[start : found.end() + 40].split()),
                    )
    return None


def _all_matches(
    document: ExtractedDocument, patterns: List[Pattern[str]]
) -> List[Match]:
    results: List[Match] = []
    for page in document.pages:
        text = page.searchable_text()
        if not text:
            continue
        for pattern in patterns:
            for found in pattern.finditer(text):
                try:
                    raw = found.group("val")
                except (IndexError, KeyError):
                    raw = found.group(0)
                if raw and raw.strip():
                    results.append(Match(raw=raw.strip(), page=page.number, context=""))
    return results


# ---------------------------------------------------------------------------
# Pattern library
# ---------------------------------------------------------------------------

PATTERNS: Dict[str, List[str]] = {
    "arr": [
        rf"\bARR{HS}*{SEP}{HS}*(?P<val>{MONEY})",
        rf"(?P<val>{MONEY}){HS}*(?:in{HS}+)?ARR\b",
        rf"annual{HS}+recurring{HS}+revenue{HS}*{SEP}{HS}*(?P<val>{MONEY})",
    ],
    "annual_revenue": [
        rf"(?:annual|total|current){HS}+revenue{HS}*{SEP}{HS}*(?P<val>{MONEY})",
        rf"\brevenue{HS}*(?:of|is|at|:|=){HS}*(?P<val>{MONEY})",
        rf"revenue{HS}+run[- ]?rate{HS}*{SEP}{HS}*(?P<val>{MONEY})",
    ],
    "revenue_growth_rate": [
        rf"revenue{HS}+growth{HS}*{SEP}{HS}*(?P<val>{PCT})",
        rf"growth{HS}+(?:rate{HS}+)?(?:of|is|at|:|=){HS}*(?P<val>{PCT})",
        rf"(?P<val>{PCT}){HS}*(?:YoY|year[- ]over[- ]year|annual{HS}+growth|growth)",
        rf"grow(?:ing|n){HS}+(?:at{HS}+)?(?P<val>{PCT})",
    ],
    "gross_margin": [
        rf"gross{HS}+margins?{HS}*{SEP}{HS}*(?P<val>{PCT})",
        rf"(?P<val>{PCT}){HS}+gross{HS}+margins?",
    ],
    "ebitda_margin": [
        rf"EBITDA{HS}+margins?{HS}*{SEP}{HS}*(?P<val>{PCT})",
        rf"operating{HS}+margins?{HS}*{SEP}{HS}*(?P<val>{PCT})",
    ],
    "monthly_burn": [
        rf"(?:monthly{HS}+)?(?:net{HS}+)?burn{HS}*(?:rate)?{HS}*{SEP}{HS}*(?P<val>{MONEY})",
        rf"burning{HS}*(?P<val>{MONEY}){HS}*(?:per|/){HS}*month",
    ],
    "runway_months": [
        rf"runway{HS}*{SEP}{HS}*(?P<val>{NUM}){HS}*months?",
        rf"(?P<val>{NUM}){HS}*months?{HS}+of{HS}+runway",
    ],
    "current_cash": [
        rf"cash{HS}+(?:on{HS}+hand|balance|position|in{HS}+bank){HS}*{SEP}{HS}*(?P<val>{MONEY})",
        rf"(?P<val>{MONEY}){HS}+(?:of{HS}+)?cash{HS}+on{HS}+hand",
    ],
    "capital_raised_to_date": [
        rf"(?:capital{HS}+)?raised{HS}+to{HS}+date{HS}*{SEP}{HS}*(?P<val>{MONEY})",
        rf"(?:total{HS}+)?(?:capital|funding){HS}+raised{HS}*{SEP}{HS}*(?P<val>{MONEY})",
        rf"(?:have{HS}+)?raised{HS}*(?P<val>{MONEY}){HS}+to{HS}+date",
    ],
    "amount_raising": [
        rf"raising{HS}*(?:a{HS}+)?(?P<val>{MONEY})",
        rf"seeking{HS}*(?P<val>{MONEY})",
        rf"(?P<val>{MONEY}){HS}+(?:Series{HS}+[A-D]|seed|round)\b",
    ],
    "tam": [
        rf"\bTAM{HS}*{SEP}{HS}*(?P<val>{MONEY})",
        rf"total{HS}+addressable{HS}+market{HS}*{SEP}{HS}*(?P<val>{MONEY})",
    ],
    "sam": [
        rf"\bSAM{HS}*{SEP}{HS}*(?P<val>{MONEY})",
        rf"serviceable{HS}+(?:available{HS}+|addressable{HS}+)?market{HS}*{SEP}{HS}*(?P<val>{MONEY})",
    ],
    "som": [
        rf"\bSOM{HS}*{SEP}{HS}*(?P<val>{MONEY})",
        rf"serviceable{HS}+obtainable{HS}+market{HS}*{SEP}{HS}*(?P<val>{MONEY})",
    ],
    "market_growth_rate": [
        rf"market{HS}+(?:is{HS}+)?grow(?:ing|th){HS}*(?:at|of|rate{HS}+of)?{HS}*(?P<val>{PCT})",
        rf"(?P<val>{PCT}){HS}+(?:market{HS}+)?CAGR",
        rf"CAGR{HS}*{SEP}{HS}*(?P<val>{PCT})",
    ],
    "customer_count": [
        rf"(?P<val>{NUM}){HS}+(?:\w+{HS}+)?customers\b",
        rf"(?P<val>{NUM}){HS}+(?:paying{HS}+)?clients\b",
        rf"customers{HS}*(?::|=){HS}*(?P<val>{NUM})",
    ],
    "net_revenue_retention": [
        rf"net{HS}+(?:revenue{HS}+|dollar{HS}+)?retention{HS}*{SEP}{HS}*(?P<val>{PCT})",
        rf"\bNRR{HS}*{SEP}{HS}*(?P<val>{PCT})",
    ],
    "retention_rate": [
        # The lookbehinds keep net revenue retention out of this field. NRR and
        # gross retention are different metrics, and reading a 118% NRR as a
        # retention rate would overstate customer loyalty.
        rf"(?<!net{HS}revenue{HS})(?<!net{HS}dollar{HS})(?<!net{HS})"
        rf"(?:gross{HS}+|logo{HS}+|customer{HS}+)?"
        rf"retention{HS}*(?:rate{HS}*)?{SEP}{HS}*(?P<val>{PCT})",
    ],
    "recurring_revenue_pct": [
        rf"recurring{HS}+revenue{HS}+is{HS}*(?P<val>{PCT})",
        rf"(?P<val>{PCT}){HS}+(?:of{HS}+revenue{HS}+is{HS}+)?recurring",
        rf"recurring{HS}+revenue{HS}*(?::|=|of){HS}*(?P<val>{PCT})",
    ],
    "customer_concentration": [
        rf"(?:top|largest){HS}+customer{HS}+(?:represents|is|accounts{HS}+for|=){HS}*(?P<val>{PCT})",
        rf"customer{HS}+concentration{HS}*{SEP}{HS}*(?P<val>{PCT})",
        rf"(?:top|largest){HS}+(?:\d+{HS}+)?customers?{HS}+(?:represent|are){HS}*(?P<val>{PCT})",
    ],
    "patents_granted": [
        rf"(?P<val>{NUM}){HS}+(?:issued|granted){HS}+patents",
        rf"(?P<val>{NUM}){HS}+patents{HS}+(?:issued|granted)",
        rf"(?P<val>{NUM}){HS}+patents\b(?!{HS}+(?:pending|application))",
    ],
    "patent_applications": [
        rf"(?P<val>{NUM}){HS}+patent{HS}+applications",
        rf"(?P<val>{NUM}){HS}+(?:patents{HS}+)?pending",
        rf"(?P<val>{NUM}){HS}+provisional{HS}+(?:patent{HS}+)?(?:applications|filings)",
    ],
    "founding_year": [
        r"founded\s+(?:in\s+)?(?P<val>(?:19|20)\d{2})",
        r"(?:est\.|established|inception)\s*(?:in\s+)?(?P<val>(?:19|20)\d{2})",
    ],
    "website": [
        r"(?P<val>(?:https?://)?www\.[A-Za-z0-9][A-Za-z0-9.\-]*\.[A-Za-z]{2,})",
        r"(?P<val>https?://[A-Za-z0-9][A-Za-z0-9.\-]*\.[A-Za-z]{2,}[^\s]*)",
    ],
}

COMPILED: Dict[str, List[Pattern[str]]] = {k: _compile(v) for k, v in PATTERNS.items()}

#: Historical revenue in a "FY2023: $1.6M" or "2023 revenue $1.6M" shape.
#: Year and amount must sit on the same line. Multi-column tables ("FY23 FY24" on
#: one row, the figures on the next) pair a year with the wrong column otherwise,
#: which yields a confident and completely wrong growth rate.
HISTORY_PATTERNS = _compile(
    [
        rf"(?:FY{HS}?)?(?P<year>20\d{{2}}){HS}*(?:revenue){HS}*[:\-|]?{HS}*(?P<amount>\$?{MONEY})",
        rf"(?:FY{HS}?)?(?P<year>20\d{{2}}){HS}*[:|]{HS}*(?P<amount>\${MONEY})",
    ]
)

#: Sector detection. Ordered most-specific first; the first hit wins.
SECTOR_KEYWORDS: List[Tuple[str, str, Tuple[str, ...]]] = [
    ("Healthcare", "Diagnostics", ("diagnostic", "pathology", "assay", "biomarker")),
    ("Healthcare", "Therapeutics", ("therapeutic", "drug", "clinical trial", "phase i", "preclinical")),
    ("Healthcare", "Medical Devices", ("medical device", "implant", "510(k)", "fda clearance")),
    ("Healthcare", "Digital Health", ("digital health", "telehealth", "patient", "hospital", "clinical")),
    ("Technology", "Artificial Intelligence", ("machine learning", "artificial intelligence", " ai ", "neural", "llm")),
    ("Technology", "Cybersecurity", ("cybersecurity", "threat detection", "zero trust", "endpoint security")),
    ("Technology", "Fintech", ("fintech", "payments", "lending", "banking", "insurtech")),
    ("Technology", "Enterprise Software", ("saas", "enterprise software", "platform", "api", "workflow")),
    ("Industrials", "Climate & Energy", ("carbon", "renewable", "battery", "solar", "grid", "emissions")),
    ("Consumer", "Consumer Products", ("consumer", "retail", "ecommerce", "marketplace", "subscription box")),
]

STAGE_KEYWORDS: List[Tuple[Stage, Tuple[str, ...]]] = [
    (Stage.SERIES_C, ("series c",)),
    (Stage.SERIES_B, ("series b",)),
    (Stage.SERIES_A, ("series a",)),
    (Stage.PRE_SEED, ("pre-seed", "pre seed")),
    (Stage.SEED, ("seed round", "seed financing", "seed stage", "raising seed")),
    (Stage.GROWTH, ("growth round", "series d", "series e", "pre-ipo")),
]

REGULATORY_KEYWORDS = {
    "FDA 510(k) clearance": ("510(k)", "510k"),
    "FDA approval": ("fda approval", "fda-approved", "pma approval"),
    "CE Mark": ("ce mark", "ce-mark"),
    "Breakthrough Device Designation": ("breakthrough device",),
    "Orphan Drug Designation": ("orphan drug",),
    "CLIA certification": ("clia",),
    "SOC 2": ("soc 2", "soc2"),
    "HIPAA compliance": ("hipaa",),
}

BUSINESS_MODEL_KEYWORDS = {
    "SaaS / subscription": ("saas", "subscription", "per seat", "arr"),
    "Transactional / usage-based": ("per test", "per transaction", "usage-based", "per scan"),
    "Licensing": ("licensing", "royalty", "licence fee"),
    "Hardware + service": ("hardware", "instrument", "device sales"),
    "Marketplace": ("marketplace", "take rate", "gmv"),
}


def _percent_value(raw: str) -> Optional[float]:
    return parse_percent(raw)


def _money_value(raw: str) -> Optional[float]:
    """Parse a money string, expanding the long-form scale words parse_money omits."""
    text = raw.lower().strip()
    text = re.sub(r"\bmillion\b", "m", text)
    text = re.sub(r"\bbillion\b", "b", text)
    text = re.sub(r"\bthousand\b", "k", text)
    return parse_money(text.strip())


def _int_value(raw: str) -> Optional[int]:
    try:
        return int(float(raw.replace(",", "").strip()))
    except (ValueError, AttributeError):
        return None


def _extract(
    document: ExtractedDocument,
    key: str,
    parser: Callable[[str], Optional[object]],
    label: str,
    confidence: float = STATED_CONFIDENCE,
) -> Evidenced:
    """Run one pattern group and wrap the result with its provenance."""
    match = _search_pages(document, COMPILED[key])
    if match is None:
        return Evidenced.missing(note=f"{label} was not found in the deck.")
    value = parser(match.raw)
    if value is None:
        return Evidenced.missing(
            note=f"{label} appeared as '{match.raw}' but could not be interpreted."
        )
    unit = document.unit_name
    return Evidenced(
        value=value,
        source=f"Deck {unit} {match.page}: \"{match.raw}\"",
        source_page_or_slide=match.page,
        evidence_class="explicitly_stated",
        confidence=confidence,
    )


def _extract_history(document: ExtractedDocument) -> List[RevenuePoint]:
    """Pull (year, revenue) pairs out of the deck text."""
    points: Dict[int, float] = {}
    for page in document.pages:
        text = page.searchable_text()
        for pattern in HISTORY_PATTERNS:
            for found in pattern.finditer(text):
                try:
                    year = int(found.group("year"))
                    amount = _money_value(found.group("amount"))
                except (ValueError, IndexError):
                    continue
                # Only accept amounts at a plausible revenue scale, so a stray
                # "2024" next to a phone number does not become a revenue point.
                if amount is None or amount < 1000:
                    continue
                if 1990 <= year <= 2100:
                    points.setdefault(year, amount)
    return [RevenuePoint(year=y, revenue=v) for y, v in sorted(points.items())]


def _detect_sector(text: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    lowered = f" {text.lower()} "
    for sector, subsector, keywords in SECTOR_KEYWORDS:
        for keyword in keywords:
            if keyword in lowered:
                return sector, subsector, keyword
    return None, None, None


def _detect_stage(text: str) -> Tuple[Optional[Stage], Optional[str]]:
    lowered = text.lower()
    for stage, keywords in STAGE_KEYWORDS:
        for keyword in keywords:
            if keyword in lowered:
                return stage, keyword
    return None, None


def _detect_business_model(text: str) -> Optional[str]:
    lowered = f" {text.lower()} "
    hits = [
        model
        for model, keywords in BUSINESS_MODEL_KEYWORDS.items()
        if any(k in lowered for k in keywords)
    ]
    return " / ".join(hits[:2]) if hits else None


def _detect_regulatory(text: str) -> List[str]:
    lowered = text.lower()
    return [
        label
        for label, keywords in REGULATORY_KEYWORDS.items()
        if any(k in lowered for k in keywords)
    ]


def _company_name(document: ExtractedDocument) -> Evidenced:
    """The title of the first slide is the company name in almost every deck."""
    for page in document.pages[:2]:
        candidate = (page.title or "").strip()
        if 2 <= len(candidate) <= 60 and not candidate.lower().startswith(
            ("the ", "our ", "why ", "how ", "what ")
        ):
            return Evidenced(
                value=candidate,
                source=f"Deck {document.unit_name} {page.number} title",
                source_page_or_slide=page.number,
                evidence_class="inferred",
                confidence=0.7,
            )
    return Evidenced.missing(note="No company name could be identified from the cover slide.")


def normalize_document(document: ExtractedDocument) -> DealDossier:
    """Build a DealDossier from a parsed deck.

    Values the deck does not state are left MISSING. The caller is expected to put
    the dossier in front of the user for review before any analysis runs.
    """
    dossier = DealDossier()
    dossier.source_files = [document.filename]
    dossier.extraction_warnings = list(document.warnings)
    full_text = document.full_text
    unit = document.unit_name

    # -- company -----------------------------------------------------------
    company = dossier.company
    company.name = _company_name(document)
    company.website = _extract(document, "website", lambda s: s.strip(), "Website", 0.8)
    company.founding_year = _extract(document, "founding_year", _int_value, "Founding year")

    sector, subsector, keyword = _detect_sector(full_text)
    if sector:
        company.sector = Evidenced.inferred(
            sector, f"Keyword '{keyword.strip()}' in deck text", INFERRED_CONFIDENCE
        )
        company.subsector = Evidenced.inferred(
            subsector, f"Keyword '{keyword.strip()}' in deck text", INFERRED_CONFIDENCE
        )
    else:
        company.sector = Evidenced.missing(note="Sector could not be inferred from deck language.")
        company.subsector = Evidenced.missing()

    stage, stage_keyword = _detect_stage(full_text)
    if stage:
        company.stage = Evidenced.inferred(
            stage, f"Deck references '{stage_keyword}'", CONTEXTUAL_CONFIDENCE
        )
        company.current_financing_stage = Evidenced.inferred(
            stage.label, f"Deck references '{stage_keyword}'", CONTEXTUAL_CONFIDENCE
        )
    else:
        company.stage = Evidenced.missing(note="Financing stage was not stated.")

    model = _detect_business_model(full_text)
    if model:
        company.business_model = Evidenced.inferred(
            model, "Business-model keywords in deck text", INFERRED_CONFIDENCE
        )

    # -- financials --------------------------------------------------------
    fin = dossier.financials
    fin.arr = _extract(document, "arr", _money_value, "ARR")
    fin.annual_revenue = _extract(document, "annual_revenue", _money_value, "Annual revenue")
    fin.gross_margin = _extract(document, "gross_margin", _percent_value, "Gross margin")
    fin.ebitda_margin = _extract(document, "ebitda_margin", _percent_value, "EBITDA margin")
    fin.monthly_burn = _extract(document, "monthly_burn", _money_value, "Monthly burn")
    fin.current_cash = _extract(document, "current_cash", _money_value, "Cash on hand")
    fin.capital_raised_to_date = _extract(
        document, "capital_raised_to_date", _money_value, "Capital raised to date"
    )
    fin.amount_raising = _extract(document, "amount_raising", _money_value, "Amount being raised")
    fin.runway_months = _extract(
        document, "runway_months", lambda s: float(_int_value(s) or 0) or None, "Runway"
    )
    fin.historical_revenue = _extract_history(document)

    stated_growth = _extract(
        document, "revenue_growth_rate", _percent_value, "Revenue growth rate"
    )
    if stated_growth.is_present:
        fin.revenue_growth_rate = stated_growth
    else:
        derived = growth_rate_from_history(
            [(p.year, p.revenue) for p in fin.historical_revenue]
        )
        if derived is not None:
            span = f"{fin.historical_revenue[0].year}-{fin.historical_revenue[-1].year}"
            fin.revenue_growth_rate = Evidenced.calculated(
                derived,
                f"CAGR computed from deck revenue history ({span})",
                confidence=0.75,
            )
        else:
            fin.revenue_growth_rate = Evidenced.missing(
                note="Growth rate was neither stated nor derivable from a revenue history."
            )

    # -- market ------------------------------------------------------------
    market = dossier.market
    market.tam = _extract(document, "tam", _money_value, "TAM")
    market.sam = _extract(document, "sam", _money_value, "SAM")
    market.som = _extract(document, "som", _money_value, "SOM")
    market.market_growth_rate = _extract(
        document, "market_growth_rate", _percent_value, "Market growth rate"
    )

    # -- customers ---------------------------------------------------------
    customers = dossier.customers
    customers.customer_count = _extract(document, "customer_count", _int_value, "Customer count")
    customers.net_revenue_retention = _extract(
        document, "net_revenue_retention", _percent_value, "Net revenue retention"
    )
    customers.retention_rate = _extract(
        document, "retention_rate", _percent_value, "Retention rate"
    )
    customers.recurring_revenue_pct = _extract(
        document, "recurring_revenue_pct", _percent_value, "Recurring revenue share"
    )
    customers.customer_concentration = _extract(
        document, "customer_concentration", _percent_value, "Customer concentration"
    )

    # -- intellectual property --------------------------------------------
    ip = dossier.ip
    ip.patents_granted = _extract(document, "patents_granted", _int_value, "Granted patents")
    ip.patent_applications = _extract(
        document, "patent_applications", _int_value, "Patent applications"
    )
    approvals = _detect_regulatory(full_text)
    if approvals:
        ip.regulatory_approvals = approvals

    _record_extraction_notes(dossier, document, unit)
    return dossier


def _record_extraction_notes(
    dossier: DealDossier, document: ExtractedDocument, unit: str
) -> None:
    """Add warnings about coverage and internal inconsistencies.

    Contradictions between stated figures are surfaced rather than resolved -- the
    user is better placed than the parser to say which number is right.
    """
    if document.coverage_ratio() < 0.6:
        dossier.extraction_warnings.append(
            f"Only {document.text_page_count} of {document.page_count} {unit}s yielded "
            f"readable text. Extracted values may be incomplete."
        )
    if document.used_ocr:
        dossier.extraction_warnings.append(
            "Some content was recovered with OCR, which is less reliable than a text "
            "layer. Verify every extracted figure before running the analysis."
        )

    fin = dossier.financials
    cash, burn, runway = fin.current_cash.value, fin.monthly_burn.value, fin.runway_months.value
    if cash and burn and runway and burn > 0:
        implied = cash / burn
        if abs(implied - runway) > max(3.0, runway * 0.35):
            dossier.extraction_warnings.append(
                f"Stated runway ({runway:.0f} months) does not reconcile with cash and burn "
                f"(implies {implied:.0f} months). Confirm which figure is current."
            )

    if fin.arr.is_present and fin.annual_revenue.is_present:
        arr, revenue = fin.arr.value, fin.annual_revenue.value
        if revenue > 0 and arr / revenue > 3.0:
            dossier.extraction_warnings.append(
                "Extracted ARR is far larger than extracted annual revenue. One of the "
                "two figures is likely a mis-read; confirm both before proceeding."
            )

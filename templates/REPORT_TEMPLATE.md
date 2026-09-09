# Venture Exit Scenario Report — Document Structure Template

The canonical structure of every report the application generates. No company data
appears here; this defines the document's sections, fields, value formats, and the
analysis output that populates each slot.

**Companion artefact:** [`venture_exit_scenario_report_template.pdf`](venture_exit_scenario_report_template.pdf) —
the same structure rendered as a blank one-page PDF.

**Source of truth:** [`reporting/report_template.py`](../venture_exit_analyzer/reporting/report_template.py)
builds this structure and renders it through `render_report()`, the same function that
renders real analyses. The template therefore cannot drift from actual output, and a
test asserts the two stay structurally identical.

---

## Document envelope

| Property | Value |
| --- | --- |
| Page count | **Exactly 1** — enforced by content caps, `KeepInFrame` shrink-to-fit, and a post-render page-count check |
| Page size / orientation | Letter, portrait (`report_config.yaml → page`) |
| Margins | 38pt left/right, 34pt top, 30pt bottom |
| Typeface | Open Sans where installed, Helvetica fallback |
| Body / section header | 8.5pt / 10pt (TEN Capital standard) |
| Colour | Monochrome, with green upside and oxblood risk accents |

## Placeholder conventions

| Notation | Meaning |
| --- | --- |
| `[UPPER CASE IN BRACKETS]` | A text value supplied by the analysis |
| `$XX.XM` | Currency, auto-scaled (`$450K`, `$8.4M`, `$1.2B`) |
| `XX%` | Percentage, whole numbers only |
| `XX/100` | A score |
| `Not disclosed` | Printed literally wherever a value was never supplied — **never `0`, never blank** |

---

## Section 1 — Masthead

Fixed furniture. Identical on every report.

```
TEN CAPITAL                    VENTURE EXIT SCENARIO ANALYSIS
```

Reversed-out white on a black band. Brand left, document title right.

---

## Section 2 — Company metric strip

Seven fields in a single row beneath the masthead. **All seven always render** — a
value the analysis lacks prints as `Not disclosed` so the strip never reflows.

| # | Label | Format | Populated from |
| --- | --- | --- | --- |
| 1 | COMPANY | `[COMPANY NAME]` | `company.name` |
| 2 | SECTOR | `[SECTOR / SUBSECTOR]` | `company.sector` + `company.subsector` |
| 3 | STAGE | `[STAGE]` | `company.current_financing_stage`, else `company.stage` |
| 4 | CURRENT REVENUE | `$XX.XM ARR` | `financials.arr` if present, else `annual_revenue`; suffix reads `ARR` or `Revenue` accordingly |
| 5 | GROWTH | `XX%` | `financials.revenue_growth_rate` |
| 6 | CAPITAL RAISED | `$XX.XM` | `financials.capital_raised_to_date` |
| 7 | CURRENT RAISE | `$XX.XM` | `financials.amount_raising` |

---

## Section 3 — EXIT SCENARIO OUTLOOK

### Probability grid

Rows are **fixed and exhaustive**. These four outcomes are mutually exclusive and
collectively exhaustive, and each column sums to exactly 100%.

| Scenario | `[HORIZON 1]` | `[HORIZON 2]` | `[HORIZON 3]` |
| --- | ---: | ---: | ---: |
| Strategic Acquisition | `XX%` | `XX%` | `XX%` |
| Financial Acquisition | `XX%` | `XX%` | `XX%` |
| IPO | `XX%` | `XX%` | `XX%` |
| **No Liquidity** | `XX%` | `XX%` | `XX%` |

- **Columns are variable**: 1 to 3, labelled by the horizons the user selected
  (`1-3 Years`, `3-5 Years`, `5-7 Years`, `7-10 Years`). Capped at
  `limits.max_horizons_shown`; when more are selected the longest are shown and the
  full set goes to the JSON export.
- **No Liquidity is set in bold** and is never omitted. It is floored at 10% and
  capped at 85%.
- All values are rounded to 5% increments — the report never prints a figure like
  `17.43%`.

### Summary pair

| Label | Format | Populated from |
| --- | --- | --- |
| MOST LIKELY PATH | `[MOST LIKELY OUTCOME] ([HORIZON])` | `primary_scenario` read off the longest selected horizon. Where no exit is most likely, reads `No liquidity event within [HORIZON]` |
| EXIT ANALYSIS CONFIDENCE | `XX/100 ([CONFIDENCE BAND])` | `confidence.score` and `confidence.band` (High / Moderate / Low) |

### Caption

Fixed text, always present:

> DIRECTIONAL SCENARIO ESTIMATE — NOT A FORECAST

---

## Section 4 — LIKELY EXIT PROFILE

A single narrative paragraph, capped at `limits.exit_profile_chars` (620) and truncated
on a word boundary. Populated from `exit_profile_summary`.

It states, in order:

1. Where no liquidity is the most likely outcome overall — that fact, first.
2. The most plausible liquidity **route** and its probability.
3. The **horizon** in which it is most likely.
4. The **buyer category** most likely to transact.
5. The evidenced **rationale** (regulatory position, IP, revenue, recurring share).

Where nothing supports a rationale, it says so rather than inventing one.

---

## Section 5 — VALUE CREATION MILESTONES

Left column of a two-column band. Up to `limits.max_milestones` (5) rows, ranked by
impact weight descending.

| MILESTONE | IMPACT |
| --- | --- |
| `[MILESTONE 1]`<br><sub>`[SCENARIO / SCENARIO]`</sub> | `[IMPACT]` |
| … up to 5 | |

- **Milestone** — capped at 58 characters; the applicable scenarios appear beneath in
  6pt grey.
- **Impact** — one of `Very High`, `High`, `Medium`, `Low`.
- Rows are derived from gaps the scoring actually found, so the list is specific to the
  company rather than a generic checklist. Fewer than 5 render when fewer apply.

---

## Section 6 — CAPITAL TO EXIT READINESS

Right column of the same band.

| Label | Format | Notes |
| --- | --- | --- |
| Current runway | `XX mo` | Or `Cash-flow positive` where burn is non-positive |
| Monthly burn | `$XX.XM` | Disclosed, or derived from cash ÷ runway |
| Additional capital | `$XXM-$XXM` | **Or `Not estimable`** |
| Expected financing | `[N] further round(s)` | Optional — omitted when not estimable; reads `None required` when zero |
| Implied dilution | `XX%-XX%` | Optional — compounding, not additive |

Followed by a **basis note** (≤190 chars) stating what the estimate rests on, or why it
could not be made.

---

## Section 7 — EXIT VALUATION DRIVERS

Two columns, bulleted.

| UPSIDE | RISKS |
| --- | --- |
| `[POSITIVE VALUATION DRIVER 1]` | `[NEGATIVE VALUATION DRIVER 1]` |
| `[POSITIVE VALUATION DRIVER 2]` | `[NEGATIVE VALUATION DRIVER 2]` |
| `[POSITIVE VALUATION DRIVER 3]` | `[NEGATIVE VALUATION DRIVER 3]` |

- Capped at `limits.max_upside_drivers` / `max_risk_drivers` (3 each), each ≤62 chars.
- Upside renders green, risks oxblood.
- Where a side has no evidenced drivers it states that explicitly rather than rendering
  empty.

---

## Section 8 — INDICATIVE EXIT RANGE

Shaded band. Three fixed rows.

| Row | Format |
| --- | --- |
| Strategic Exit | `$XXM-$XXM` **or** `Insufficient information for a credible valuation range.` |
| Financial Exit | `$XXM-$XXM` **or** the same insufficiency statement |
| IPO Readiness Threshold | `$XXM+ revenue` — the configured revenue floor, always present |

A range renders only when it is defensible: at least 3 usable comparables **and**
revenue above the configured floor. Otherwise the insufficiency statement prints in
full. The application holds no market data of its own and produces no range without
user-supplied comparables.

---

## Section 9 — ANALYTICAL BASIS

Two columns. Makes the reasoning auditable on the page rather than in an appendix.

| KEY ASSUMPTIONS | LIMITATIONS |
| --- | --- |
| `[KEY ASSUMPTION 1]` | `[LIMITATION 1]` |
| `[KEY ASSUMPTION 2]` | `[LIMITATION 2]` |
| `[KEY ASSUMPTION 3]` | `[LIMITATION 3]` |

- Capped at `limits.max_assumptions` / `max_limitations` (3 each), each ≤165 chars.
- Generic framing already stated elsewhere on the page (the "not a forecast" caption and
  the disclaimer) is filtered out, so these slots carry company-specific content.

---

## Section 10 — Disclaimer and footer

Fixed text, always present, below a hairline rule.

> Directional decision-support analysis based on supplied information and stated
> assumptions. It is not an investment recommendation, valuation opinion, or prediction
> of future liquidity.

Centred page footer:

> **Confidential - for recipients only.**

---

## Regenerating

```bash
python -m venture_exit_analyzer.reporting.report_template
```

Writes `templates/venture_exit_scenario_report_template.pdf`.

## Changing the structure

All furniture — section titles, boilerplate, row caps, typography, geometry — lives in
[`config/report_config.yaml`](../venture_exit_analyzer/config/report_config.yaml).
Adding or removing a **section** requires a change to `_build_flowables()` in
`pdf_generator.py`; the template picks it up automatically, and
`test_template_matches_live_report_structure` fails if the two diverge.

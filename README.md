# TEN Capital — Venture Exit Scenario Analyzer

A decision-support application that reads an investor pitch deck (PDF or PowerPoint),
combines what it finds with user-supplied assumptions and comparable transactions, and
produces a one-page investor-facing PDF describing the plausible paths to liquidity.

It is **not a prediction engine**. Every probability it produces is a directional
estimate derived from a transparent, configurable scoring model, and is labelled as
such on the report itself.

The application exists to help an investor answer four questions:

> What is the most plausible path to liquidity, what must happen before that outcome
> becomes realistic, how much additional capital may be required, and how confident
> should we be in that conclusion?

---

## Contents

- [Quick start](#quick-start)
- [Architecture](#architecture)
- [How the analysis works](#how-the-analysis-works)
  - [Evidence and provenance](#evidence-and-provenance)
  - [Scoring methodology](#scoring-methodology)
  - [Probability methodology](#probability-methodology)
  - [Comparable exit methodology](#comparable-exit-methodology)
  - [Capital-to-exit methodology](#capital-to-exit-methodology)
  - [Confidence, and why it is not probability](#confidence-and-why-it-is-not-probability)
- [Interface and design system](#interface-and-design-system)
- [The AI layer](#the-ai-layer)
- [Result emails](#result-emails)
- [Deployment](#deployment)
- [Report template](#report-template)
- [Configuration](#configuration)
- [Consent and privacy](#consent-and-privacy)
- [JSON record schema](#json-record-schema)
- [Testing](#testing)
- [Known limitations](#known-limitations)

---

## Quick start

**Requirements:** Python 3.10 or newer (developed and tested on 3.14).

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

streamlit run venture_exit_analyzer/app.py
```

The interface opens at `http://localhost:8501`.

### Supported file types

| Format | Extension | Notes |
| --- | --- | --- |
| PDF | `.pdf` | Text layer and tables read directly; OCR only for pages with no text layer |
| PowerPoint | `.pptx`, `.pptm` | Slide text, tables, grouped shapes, chart categories and speaker notes |
| Legacy PowerPoint | `.ppt` | **Not supported.** Detected and reported with instructions to re-save as `.pptx` |

Comparable transactions can be supplied by CSV, JSON, or manual entry.

### Optional extras

OCR is used only for pages that yield no text at all. To enable it, install
`pytesseract` and `Pillow` **and** the [Tesseract binary](https://github.com/tesseract-ocr/tesseract).
Without it, image-only pages are reported as unread rather than silently skipped.

Live transmission to TEN Capital requires `requests` and a configured endpoint
(see [`.env.example`](.env.example)). Without one, records are written to a local
`outbox/` directory instead.

**Fonts.** The report uses Open Sans, the TEN Capital standard, when it can be found in
`assets/fonts/` or the system font directories. It falls back to Helvetica otherwise;
the metrics are close enough that page geometry is unaffected.

---

## Architecture

Extraction, analysis, probability, valuation, rendering and transmission are separate
modules with no reach-through between them. The analysis layer never touches a file,
and the reporting layer never computes a number.

```
venture_exit_analyzer/
├── app.py                      Streamlit interface (upload → review → analyse → report)
├── config/
│   ├── scoring_weights.yaml    Every weight, band, threshold and probability constant
│   ├── report_config.yaml      Typography, geometry, content caps, boilerplate
│   └── loader.py               Loading, caching, and invariant validation
├── models/
│   ├── evidence.py             Evidenced[T] — value + provenance + confidence
│   ├── company.py              Company, market, customer, IP, ownership profiles
│   ├── financials.py           Financial profile and funding history
│   ├── comparables.py          User-supplied comparable transactions
│   ├── analysis.py             Scores, probabilities, dossier, analysis result
│   └── record.py               The exportable TEN Capital JSON record
├── ingestion/
│   ├── base.py                 Format-neutral document structures and errors
│   ├── pdf_parser.py           pdfplumber → pypdf → OCR, in that order
│   ├── ppt_parser.py           python-pptx extraction, plus format dispatch
│   └── normalization.py        Deck text → DealDossier, with page-level provenance
├── analysis/
│   ├── scoring_base.py         Weighted driver builder and its invariants
│   ├── strategic_exit.py       Strategic Acquisition Score (0–100)
│   ├── financial_exit.py       Financial Acquisition Score (0–100)
│   ├── ipo_analysis.py         IPO Readiness Score (0–100)
│   ├── liquidity_risk.py       Liquidity Risk Score (0–100)
│   ├── probability_engine.py   Scores → scenario probabilities per horizon
│   ├── valuation.py            Comparable-derived ranges and valuation drivers
│   ├── capital_requirements.py Capital to exit readiness
│   ├── confidence.py           Analysis confidence (0–100)
│   ├── acquirers.py            Acquirer category profiles
│   ├── milestones.py           Ranked value-creation milestones
│   ├── narrative.py            Facts and figure allow-list for generated prose
│   └── engine.py               Orchestration
├── ui/
│   └── theme.py                Design tokens, brand mark, injected CSS, components
├── static/                     Public assets, served at /app/static/ (favicons, manifest)
├── reporting/
│   ├── report_layout.py        Content preparation (no ReportLab dependency)
│   ├── pdf_generator.py        One-page rendering, with a verified page count
│   └── report_template.py      Blank structural template, rendered via the same path
├── services/
│   ├── claude_service.py       Bounded Claude access: extraction and narrative
│   ├── email_service.py        Result emails via Resend, consent-gated
│   └── ten_capital_service.py  Structured-record transmission, consent-gated
└── utils/
    ├── calculations.py         Pure, total financial functions
    ├── formatting.py           Display formatting
    ├── env.py                  Environment and .env loading
    └── validation.py           CSV/JSON parsing and input validation
```

### Design rules the code enforces

- **The AI layer is never the calculator.** Every figure is computed by a deterministic,
  unit-tested function in `utils/calculations.py`.
- **Missing data is never zero.** An absent value is `None` with an evidence class of
  `MISSING`, distinguishable everywhere from a real measurement of zero.
- **A dimension cannot lose a factor.** `DriverBuilder.build()` raises if any configured
  factor went unscored, so a refactor cannot silently change a score's denominator.
- **The report cannot silently exceed one page.** Content is capped by configuration,
  fitted with `KeepInFrame`, and the generated file's page count is verified.
- **Nothing leaves the process without consent.** Both outbound modules -- the record
  transmitter and the emailer -- check consent in one place, before any payload is built
  or transport constructed.

---

## How the analysis works

### Evidence and provenance

Every material value is wrapped in an `Evidenced[T]` carrying its source, the page or
slide it came from, how it was established, and a confidence weight:

```python
Evidenced(
    value=4_200_000.0,
    source='Deck page 4: "$4.2M"',
    source_page_or_slide=4,
    evidence_class=EvidenceClass.EXPLICIT,
    confidence=0.85,
)
```

Values are classified as **explicitly stated**, **calculated**, **inferred**,
**user supplied**, **comparable-derived**, or **missing**. The classification travels
with the value into the scoring drivers, the report and the JSON record, so any number
can be traced back to the slide it came from — or shown to have no source at all.

Extraction is conservative by design. It reads only what the deck states plainly on a
single line, and it declines several patterns that would produce confident errors:

- a percentage is never read as a currency amount (`"recurring revenue is 92%"` does not
  become $92 of revenue);
- a hyphen is never consumed as a label separator (`"EBITDA margin -45%"` keeps its sign);
- a year is never paired with a figure on a different line, because a `FY23 | FY24`
  column header would otherwise pair with the row beneath it and yield a plausible,
  wrong growth rate;
- net revenue retention is never read as gross retention.

Anything not found is left `MISSING` for the user to supply.

### Scoring methodology

Four independent dimensions are scored 0–100. Each is a weighted sum of factors:

```
dimension_score = Σ (factor_score × 10 × factor_weight)      factor_score ∈ [0, 10]
```

| Dimension | Factors | What it measures |
| --- | ---: | --- |
| Strategic Acquisition | 11 | What a corporate buyer would be paying for that they cannot easily build |
| Financial Acquisition | 11 | What a PE buyer can underwrite: revenue, margin, retention, efficiency |
| IPO Readiness | 12 | Public-market scale, growth, leadership and governance |
| Liquidity Risk | 12 | The likelihood that no exit occurs at all |

Factor weights are defined in `config/scoring_weights.yaml` and validated to sum to
exactly 1.0 per dimension at load time.

**Factors that cannot be assessed score a neutral 5.0/10 and are marked `MISSING`.**
Absence of information is not evidence of strength, and it is not evidence of weakness
either. The cost of missing disclosure is paid through the confidence score, never by
silently penalising or flattering the company.

Every score is fully decomposed. The interface shows each factor, its weight, its
contribution, and the evidence behind it:

```json
{
  "factor": "ip_defensibility",
  "score": 9.0,
  "weight": 0.13,
  "contribution": 11.7,
  "evidence": "7 granted and 4 pending patent(s) of 11 position(s) (Deck page 6 ...)",
  "evidence_class": "explicitly_stated"
}
```

Note that Liquidity Risk is scored in the opposite direction: a **high** factor score
there means **more** risk.

### Probability methodology

For each liquidity route and each selected horizon:

```
weight = base_rate × (score / 100) ^ exponent × horizon_factor × stage_multiplier × damping
```

- **`base_rate`** encodes how often each outcome occurs across venture portfolios at all.
  Strategic acquisition is by far the most common route; IPO is rare.
- **`exponent`** (> 1) sharpens the response so a weak score is punished more than
  linearly. IPO is the most demanding at 2.6.
- **`horizon_factor`** reflects how the opportunity develops over time. Routes requiring
  scale (PE, IPO) strengthen in later windows; the no-liquidity weight decays.
- **`stage_multiplier`** gates the liquidity routes by company maturity. It is *not*
  applied to no-liquidity, so an early-stage company accumulates more weight on "no exit"
  through normalisation alone.
- **`damping`** applies hard scale gates: below the configured revenue floor, IPO weight
  is cut to 25% and financial-acquisition weight to 40%. A strong IPO-readiness score on
  a $4M-revenue company does not make a listing realistic, and the model does not pretend
  otherwise.

The four weights are then normalised to 100%, the no-liquidity bucket is held within its
configured floor and ceiling, and everything is **rounded to 5% increments**. Output like
17.43% would imply precision the inputs cannot support.

Two deliberate conservatism rules:

- **No-liquidity is floored at 10% and never removed.** Venture outcomes are power-law
  distributed; a report that omits the possibility of no exit is advocacy, not analysis.
- **Ties resolve to no-liquidity.** Asserting an exit is the stronger claim, and a tie is
  not evidence for it.

The headline "most likely path" is read off the **longest** selected horizon. Near-term
windows put most of their mass on "no liquidity" for almost every venture-stage company,
so a cross-horizon maximum would return that almost always — true, but close to
tautological.

Every weight is traceable. The interface prints the full derivation:

```
strategic_acquisition   weight 0.4877  (score 69/100, base 1.00, horizon x1.00, stage x0.88)
ipo                     weight 0.0043  (score 60/100, base 0.18, horizon x0.40, stage x0.88,
                                        damped x0.25 (revenue below the $50.0M listing threshold))
```

### Comparable exit methodology

**The application holds no market data and will not invent any.** Valuation ranges are
computed only from comparables the user supplies by CSV, JSON, or manual entry.

- Multiples are derived arithmetically where implied (value ÷ revenue). An EBITDA
  multiple is never computed on non-positive EBITDA, where it is meaningless.
- A range requires at least 3 usable comparables. Below that, the report states
  *"Insufficient information for a credible valuation range."*
- With fewer than 5 comparables the **full observed spread** is used rather than the
  25th–75th percentile, because an interquartile range over 3 points collapses to a band
  far tighter than the evidence justifies.
- A **30% size discount** applies when the subject's revenue is below $10M, since
  published transaction sets are dominated by larger companies.
- Acquirers are named only as *categories* derived from sector. Buyers appearing in the
  user's own comparable set are surfaced as evidence of an active market, attributed to
  that source and explicitly not as a claim of interest in this company. Specific buyers
  are scored only when the user names them.

### Capital-to-exit methodology

1. Determine the revenue scale the likely exit route requires.
2. Compute the years to reach it at the disclosed growth rate — **capped at 100%**, since
   compounding a triple-digit rate forward flatters the timeline and understates the
   capital needed.
3. Add 12 months for the exit process itself. Reaching exit-readiness revenue is not the
   same as being paid; engagement, diligence, negotiation and close must also be funded.
4. Fund the shortfall beyond existing runway at the disclosed burn, widened 0.75×–1.45×
   because burn rises as a company scales.
5. Derive the implied number of rounds and the resulting **compounding** dilution — two
   20% rounds give up 36%, not 40%.

Where burn is unavailable, or the company is not growing and sits below the target, the
result is reported as **not estimable** rather than guessed.

### Confidence, and why it is not probability

A 55% strategic-acquisition probability computed from a deck with no financials and no
comparables is a weak conclusion. The same number from full disclosure and six
comparables is a strong one. The probability is identical; the confidence is not.

The 0–100 confidence score weighs six components:

| Component | Weight | Measures |
| --- | ---: | --- |
| Deck completeness | 28% | Share of scoring weight backed by evidence rather than neutral defaults |
| Financial data quality | 26% | Presence of core financials, discounted by extraction confidence |
| Comparables support | 18% | Usable comparable multiples, saturating at 6 |
| Stage predictability | 12% | Later-stage outcomes are inherently more predictable |
| Market data quality | 10% | Presence of market and customer metrics |
| Assumption consistency | 6% | Penalises internal contradictions found during extraction |

Bands: **High** ≥ 70, **Moderate** ≥ 45, **Low** below.

---

## Interface and design system

TEN Capital Network house style: a dark navy ground with a coral / amber / teal accent
triad echoing the three figures in the network mark. Sora for headings, Inter for body,
JetBrains Mono for labels and technical values.

### Where the styling lives

| Layer | Contains | Why there |
| --- | --- | --- |
| `.streamlit/config.toml` | Colours, fonts, radii, borders, semantic status colours, sidebar palette | Theme configuration is a supported Streamlit API. It styles Streamlit's own widgets correctly and survives upgrades. |
| `ui/theme.py` | Palette tokens, brand mark, injected CSS, components | Only what configuration cannot express: the gradient rule, brand lockup, dropzone, CTA gradient, step rail |

The injected CSS targets `data-testid` attributes, never the hashed
`st-emotion-cache-*` class names, which change between releases. A test asserts this.

`PALETTE` in `theme.py` is the single source of truth, and `config.toml` mirrors it.
Because that is two places, `check_config_matches_palette()` compares them and a test
fails if they drift — including a test that the guard itself detects drift.

### Semantic colour

The triad carries meaning rather than decoration, so a glance reports state:

| Colour | Meaning | Used for |
| --- | --- | --- |
| Teal | Confirmed, complete | Completed steps, success messages, connected services |
| Amber | Attention, gaps | Unreviewed inputs, warnings, missing disclosures |
| Coral | Primary action, risk | CTAs, the active step, errors and risk drivers |

Streamlit's own `greenColor` / `orangeColor` / `redColor` are remapped onto the triad in
config, so built-in alerts inherit the same language.

### The step rail

A four-step rail (Upload → Review → Analyse → Report) sits under the hero and reflects
real session state, with the first incomplete step marked active. It exists because the
review step is a **hard gate** — the report cannot be generated until extracted inputs
are confirmed — and a gate the user cannot see reads as a broken button.

The rail and the sidebar status render *before* the tab bodies, so state-changing actions
call `st.rerun()` once complete. Without it both would report the previous state until
the next click.

### Corrections made to the source mockup

The design was supplied as an HTML mockup. Three things in it were not carried over:

- **The disclosure named a personal Gmail address.** Result emails go to
  `Info@tencapital.group`, and the interface now reads the recipient from configuration
  so the promise on screen cannot drift from where mail actually goes.
- **`.docx` was listed as accepted.** The application cannot read Word documents;
  advertising it produces an upload error. The accepted types are `.pdf` and `.pptx`.
  Streamlit's own uploader hint is hidden for the same reason — it lists `.ppt`, which is
  accepted *only* so it can be rejected with a useful message.
- **The coral arc in the brand mark carried `transform="rotate(180 50 79)"`**, which maps
  it from the bottom-left to the bottom-right, overlapping the teal arc and detaching it
  from the coral circle. The transform is dropped so each arc sits beneath its own
  circle; a test asserts the arc origins and circle centres agree.

---

### Icons and favicon

The TEN Capital Network mark is the browser tab icon, the iOS home-screen icon and the
in-app brand lockup. The master artwork and the generated set live in
`venture_exit_analyzer/static/`, rebuilt with:

```bash
python tools/generate_icons.py
```

| Asset | Purpose |
| --- | --- |
| `favicon-{16,32,48,64,96,192,512}.png` | Browser tabs and the web manifest, transparent |
| `favicon.ico` | Multi-resolution (16/32/48) for legacy `/favicon.ico` requests |
| `apple-touch-icon.png` | 180px iOS home screen, opaque navy |
| `icon-maskable-512.png` | Android adaptive icon, inset to the safe zone |
| `site.webmanifest` | Installable-app metadata |
| `logo-source.png` | Master artwork, committed so the set is reproducible |

Three details that are easy to get wrong:

- **Web icons keep their transparency**; the mark is a coloured ring, so it reads on both
  a light and a dark tab. **Apple and maskable icons are opaque navy** — iOS shows
  transparency as black behind its rounded tile, and launchers crop maskable icons to a
  circle, so the mark is inset to 78% and a test asserts nothing survives outside it.
- **`static/` must sit beside `app.py`, not at the project root.** Streamlit resolves it
  as `Path(entrypoint).parent / "static"` (`streamlit/file_util.py`). At the root it is
  silently never served — every asset 404s. A test pins the location.
- **`<link>` tags must reach `<head>`.** `st.markdown` puts them in the body, where Chrome
  ignores both the manifest and the touch icon. The tab favicon comes from
  `st.set_page_config(page_icon=...)`, which handles `<head>` correctly; the other two are
  appended by a zero-height component whose script reaches `window.parent.document.head`.

---

## The AI layer

The application calls the Claude API, but its role is deliberately bounded. Per the
design rule that **the AI is never the calculator**, it does exactly three things:

1. **Gap-filling extraction** — reads the deck and reports facts the deterministic
   parser could not find, each with a page number and a verbatim quote.
2. **Classification** — sector, business model, customer type: judgements keyword
   matching does badly.
3. **Narrative** — prose for the exit-profile paragraph.

Scores, probabilities, valuations, capital requirements and confidence remain pure
functions of the reviewed dossier. **Turning the AI layer off changes no number in the
report** — there is a test that asserts exactly this.

### Four safety properties, enforced rather than trusted

| Property | How |
| --- | --- |
| The model may only fill gaps | Fields already extracted deterministically are excluded from the tool schema and re-checked before writing. A confident hallucination cannot displace a figure read off the page. |
| It cannot re-label a known fact | Already-captured values are shown in the prompt, with explicit rules that a stated SAM is not a SOM and an ARR is not separately "annual revenue". |
| Implausible values are refused | Every numeric field carries plausibility bounds. A founder-ownership of `45.0` (a misplaced decimal) is rejected, not stored. |
| Generated prose cannot invent a figure | Every percentage, currency amount and multiple in the narrative must appear in an allow-list built from the computed analysis. Otherwise the narrative is discarded and the deterministic text is kept. |

Values the model supplies are classified `INFERRED`, capped at 0.75 confidence — below
anything read directly from the page — and surfaced in the mandatory review step. Nothing
the model produced reaches a report without a human seeing it first.

Failure is always non-fatal. No key, no network, an API error, or a rejected narrative
all leave the application working exactly as it does without AI.

### Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | — | Enables the AI layer. Without it, deterministic parsing only. |
| `CLAUDE_MODEL` | `claude-sonnet-5` | Model used for extraction and narrative |
| `CLAUDE_ENABLED` | `true` | Set `false` to disable AI without removing the key |
| `CLAUDE_MAX_TOKENS` | `4096` | Response budget |
| `CLAUDE_TIMEOUT_SECONDS` | `120` | Request timeout |

Locally these go in `.env` (gitignored). In deployment they are real environment
variables — real environment always wins over the file, which is what lets the same code
run unchanged in both places.

---

## Result emails

Every generated report is emailed automatically to **Info@tencapital.group** through
Resend, with the one-page PDF and the structured JSON record attached.

The message mirrors the report: deal summary, the full scenario grid across every
selected horizon, most likely path, confidence, exit profile, indicative ranges,
milestones, valuation drivers and limitations — followed by the same "not a forecast"
caption and confidentiality footer. Both HTML and plain-text bodies are sent.

### Consent

Email is one of only two places the application sends data anywhere, and it is gated the
same way: the check runs before any payload is composed, so an unauthorised send never
reaches the point of serialising a company's financials.

| Requirement | Default | Why |
| --- | --- | --- |
| `analysis_consent` | **Required** | An analysis you had no right to run is one you have no right to circulate |
| `share_with_ten_capital` | Not required | That flag governs filing the structured record into TEN Capital *systems*. This email is an operational notification to the operator's own inbox — a different act |

Set `EMAIL_REQUIRE_SHARE_CONSENT=true` to take the stricter reading and require both.

### Behaviour

- **Automatic** on each successful report generation, guarded against Streamlit reruns so
  a page refresh cannot send the same analysis twice.
- **Manual re-send** available from the Report tab.
- **Never fatal.** No key, no network, or a Resend rejection leaves the analysis and the
  report untouched; the interface says what happened and the PDF is still downloadable.
- Deck-supplied text is HTML-escaped: a company name is untrusted input and must not
  become markup in a message sent to an inbox.

### Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `RESEND_API_KEY` | — | Enables email. Without it, delivery is skipped and the app runs normally. |
| `EMAIL_TO` | `Info@tencapital.group` | Recipient |
| `EMAIL_FROM` | `TEN Capital Exit Analyzer <exit-analyzer@tencapital.group>` | Sender; the domain must be verified in Resend |
| `EMAIL_ENABLED` | `true` | Set `false` to disable without removing the key |
| `EMAIL_AUTO_SEND` | `true` | Set `false` for manual sending only |
| `EMAIL_REQUIRE_SHARE_CONSENT` | `false` | Require `share_with_ten_capital` as well |
| `EMAIL_TIMEOUT_SECONDS` | `30` | Request timeout |

`tencapital.group` is verified in Resend with sending enabled, which is what allows mail
from `exit-analyzer@tencapital.group`. Sending from an unverified domain returns HTTP 403,
and the service reports that specifically rather than as a generic failure.

---

## Deployment

The app is deployed on Railway as a web service.

**Live:** https://exit-analyzer-production.up.railway.app

### Files

| File | Purpose |
| --- | --- |
| `railway.json` | Builder, start command, healthcheck (`/_stcore/health`), restart policy |
| `Procfile` | Equivalent start command for other platforms |
| `.python-version` | Pins Python 3.12 for the build |
| `.streamlit/config.toml` | Headless mode, 25MB upload cap, theme |
| `.railwayignore` | Keeps `.env`, tests and caches out of the build context |

### Deploying an update

```bash
railway up --service exit-analyzer
```

### Managing secrets

The API key is a Railway environment variable and is never committed. `.env` is listed in
both `.gitignore` and `.railwayignore`, so it is not uploaded to the build context.

```bash
railway variables --service exit-analyzer   --set "ANTHROPIC_API_KEY=sk-ant-..."   --set "RESEND_API_KEY=re_..."
```

To rotate: issue a new key at console.anthropic.com, run the command above, and revoke
the old one.

### A note on Streamlit and CORS

`server.enableCORS = false` is a common suggestion for proxied Streamlit deployments. It
is a no-op here: Streamlit refuses to combine it with XSRF protection and silently
re-enables CORS. The configuration therefore leaves CORS at its default and keeps XSRF
protection on, which is what actually guards the upload endpoint and works correctly
behind Railway's TLS-terminating proxy.

---

## Report template

The report's structure is documented as a standalone, data-free template:

| Artefact | Purpose |
| --- | --- |
| [`templates/REPORT_TEMPLATE.md`](templates/REPORT_TEMPLATE.md) | Field specification — every section, row, value format, and the analysis output that populates it |
| [`templates/venture_exit_scenario_report_template.pdf`](templates/venture_exit_scenario_report_template.pdf) | The same structure rendered as a blank one-page PDF |

Regenerate with:

```bash
python -m venture_exit_analyzer.reporting.report_template
```

The template is built by `reporting/report_template.py` and rendered through
`render_report()` — **the same function that renders real analyses**. It therefore cannot
drift from actual output: add a section to the layout and the template gains it too.
`test_template_matches_live_report_structure` compares the fixed furniture of a blank
template against a populated report and fails if they diverge, and a further set of tests
asserts the template contains no concrete company name, currency amount, percentage or
score.

Use it as the reference when changing the report, and as the specification when
reviewing what the application is expected to produce.

---

## Configuration

Nothing that shapes a score or a probability is hard-coded.

**`config/scoring_weights.yaml`** — factor weights, scoring bands, probability base rates,
exponents, horizon factors, stage multipliers, revenue gates, rounding increment,
no-liquidity bounds, confidence weights, capital assumptions, valuation rules.

**`config/report_config.yaml`** — page geometry, typography (Open Sans, body 8.5pt and
section headers 10pt per the TEN Capital standard), colours, content caps, boilerplate.

Validated at load: factor weights must sum to 1.0 per dimension, the rounding increment
must divide 100 evenly, and scoring bands must be ordered. A mis-weighted dimension fails
loudly at startup rather than producing scores that look plausible and mean nothing.

The interface has a **Reload configuration** control, so weights can be tuned and
re-applied without restarting.

---

## Consent and privacy

Consent is captured as three independent decisions, and none is inferred from another:

| Flag | Meaning |
| --- | --- |
| `analysis_consent` | The user has the right to analyse the document, and consents to it |
| `data_storage_consent` | The analysis may be retained locally |
| `share_with_ten_capital` | The record may be transmitted to TEN Capital |

**Uploading a deck is not consent to analyse it.** Extraction is blocked until
`analysis_consent` is given.

**Nothing is transmitted unless `share_with_ten_capital` is `True`.** The check lives in
`send_to_ten_capital()` — the only function in the application that sends data anywhere —
and runs before the transport is even constructed, so there is no code path to a network
call that skips it. Attempting to transmit without consent raises `ConsentError` and
writes nothing.

Exporting JSON to the user's own machine is not transmission and is not gated on sharing.

If no endpoint is configured, `MockTransport` writes the record to `outbox/` instead of
sending it, so the full flow is exercisable and auditable without a live integration.

---

## JSON record schema

```json
{
  "analysis_type": "venture_exit_scenario",
  "schema_version": "1.0",
  "user": { "user_id": "", "name": "", "email": "" },
  "company": {
    "name": "", "sector": "", "subsector": "", "stage": "",
    "revenue": null, "growth_rate": null
  },
  "analysis": {
    "analysis_date": "", "confidence_score": null, "confidence_band": "",
    "primary_scenario": "", "primary_horizon": null,
    "time_horizons": {
      "3_5_years": {
        "strategic_acquisition": null, "financial_acquisition": null,
        "ipo": null, "no_liquidity": null
      }
    },
    "dimension_scores": {}
  },
  "capital": {
    "raised_to_date": null, "current_raise": null, "current_cash": null,
    "runway_months": null, "monthly_burn": null,
    "estimated_additional_capital_required": { "low": null, "high": null },
    "expected_financing_rounds": null
  },
  "exit_scenarios": [],
  "likely_acquirer_profiles": [],
  "milestones": [],
  "valuation_drivers": [],
  "valuation_ranges": [],
  "risks": [],
  "assumptions": [],
  "limitations": [],
  "evidence_trail": [],
  "source_files": [],
  "consent": {
    "analysis_consent": false,
    "data_storage_consent": false,
    "share_with_ten_capital": false,
    "consent_timestamp": null
  }
}
```

`evidence_trail` carries the provenance of every value that was actually found, so a
recipient can audit any figure back to its page or slide. Absent values serialise as
`null`, never `0`.

The schema is defined and validated with Pydantic in `models/record.py`.

---

## Testing

```bash
pytest tests/ -q          # 288 tests
```

Test fixtures are generated on first run — synthetic PDF and PPTX decks for a fictional
company, plus deliberately broken files. To rebuild them:

```bash
python tests/fixtures/make_fixtures.py
```

Coverage spans PDF and PowerPoint extraction, the error paths (corrupt, image-only,
legacy `.ppt`, unsupported extensions), normalisation and its regression guards, all
deterministic calculations, probability normalisation invariants under randomised input,
score decomposition, valuation, capital requirements, confidence, one-page PDF rendering,
JSON schema round-tripping, consent enforcement, the structural template, and the AI layer's safety properties, email composition, consent and failure handling, and the design system's palette/config agreement, brand-mark geometry, and the generated icon set.

The AI and email tests use fake transports throughout: the suite makes no network calls, sends no mail, and passes with no API keys configured.

The suite was checked by mutation: deliberately reintroducing the extraction bugs,
removing the no-liquidity floor and ceiling, bypassing the consent gate, or allowing an
incomplete dimension to build all cause tests to fail.

---

## Known limitations

- **Extraction is pattern-based, not semantic.** It reads what a deck states plainly and
  declines ambiguity. Charts, infographics and figures embedded in images are not read
  without OCR, and complex multi-column tables are deliberately not parsed into a revenue
  history. **The review step exists because of this** and is mandatory: the report cannot
  be generated until extracted values are confirmed.
- **Base rates are judgement, not a fitted model.** The probability constants encode
  reasonable priors about venture outcomes. They are not estimated from a proprietary
  transaction database, and they are configurable precisely so a firm can substitute its
  own experience.
- **No market data is included.** Valuation ranges exist only when the user supplies
  comparables. Sector multiples, buyer appetite and market conditions are not known to the
  application.
- **Acquirer categories are sector heuristics.** They are not derived from research into
  any specific buyer's strategy or corporate development pipeline.
- **Sector detection is keyword-based** and covers healthcare, technology, industrials and
  consumer. Companies outside these fall back to generic acquirer categories; the sector
  can be corrected in the review step.
- **The model does not consider deal structure.** Liquidation preferences, participation,
  ratchets and other terms can mean a headline exit value returns little to common
  holders. The report addresses whether an exit occurs and at roughly what enterprise
  value, not how proceeds are distributed.
- **Single-company analysis only.** There is no portfolio construction, correlation or
  aggregation across deals.
- **Confidence measures evidence quality, not correctness.** A high-confidence analysis of
  a deck full of accurate but misleading claims will still be wrong. Deck language is
  promotional by nature, and the scoring weights disclosed financials far more heavily
  than narrative — but it cannot audit the underlying truth of what a founder states.

---

*Directional decision-support analysis based on supplied information and stated
assumptions. It is not an investment recommendation, valuation opinion, or prediction of
future liquidity.*

**Confidential — for recipients only.**

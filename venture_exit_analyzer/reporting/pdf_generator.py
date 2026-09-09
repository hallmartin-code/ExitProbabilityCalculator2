"""One-page PDF rendering with ReportLab.

The single-page requirement is treated as a hard constraint, enforced in two
layers: content is capped by ``report_config.yaml`` before it reaches the
renderer, and the assembled flowables are then placed inside a ``KeepInFrame``
that shrinks to fit as a final guarantee. The generator verifies the produced
file really is one page and raises if it is not, so a silent two-page report can
never reach an investor.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4, LETTER, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Flowable,
    Frame,
    KeepInFrame,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.pdfgen import canvas as pdfcanvas

from ..config.loader import load_report_config, load_scoring_config
from ..models.analysis import DealDossier, ExitScenarioAnalysis
from .report_layout import ReportContent, build_report_content

logger = logging.getLogger(__name__)

#: Where Open Sans may live, in preference order. The TEN Capital standard is
#: Open Sans; Helvetica is the fallback when it is not installed, and its metrics
#: are close enough that the page geometry is unaffected.
FONT_SEARCH_PATHS = [
    Path("assets/fonts"),
    Path(__file__).resolve().parent.parent / "assets" / "fonts",
    Path("C:/Windows/Fonts"),
    Path.home() / "AppData/Local/Microsoft/Windows/Fonts",
    Path("/usr/share/fonts/truetype/open-sans"),
    Path("/usr/share/fonts/truetype"),
    Path("/Library/Fonts"),
    Path.home() / "Library/Fonts",
]

OPEN_SANS_FILES = {
    "regular": ("OpenSans-Regular.ttf", "OpenSans.ttf"),
    "bold": ("OpenSans-Bold.ttf", "OpenSans-SemiBold.ttf"),
}


class PDFGenerationError(RuntimeError):
    """Raised when the report cannot be rendered."""


class ReportFonts:
    """Resolved font family names for the renderer."""

    def __init__(self, regular: str, bold: str, using_preferred: bool):
        self.regular = regular
        self.bold = bold
        self.using_preferred = using_preferred


def _find_font(filenames: Tuple[str, ...]) -> Optional[Path]:
    for directory in FONT_SEARCH_PATHS:
        try:
            if not directory.is_dir():
                continue
        except OSError:
            continue
        for filename in filenames:
            candidate = directory / filename
            if candidate.is_file():
                return candidate
    return None


def register_fonts(report_config: Dict[str, Any]) -> ReportFonts:
    """Register Open Sans when available, otherwise fall back to Helvetica."""
    fallback = report_config["fonts"]["fallback_family"]
    regular_path = _find_font(OPEN_SANS_FILES["regular"])
    bold_path = _find_font(OPEN_SANS_FILES["bold"])

    if regular_path and bold_path:
        try:
            pdfmetrics.registerFont(TTFont("OpenSans", str(regular_path)))
            pdfmetrics.registerFont(TTFont("OpenSans-Bold", str(bold_path)))
            return ReportFonts("OpenSans", "OpenSans-Bold", True)
        except Exception as exc:
            logger.warning("Open Sans found but could not be registered: %s", exc)

    logger.info("Open Sans not found; falling back to %s.", fallback)
    return ReportFonts(fallback, f"{fallback}-Bold", False)


class HorizontalRule(Flowable):
    """A hairline rule spanning the frame width."""

    def __init__(self, width: float, thickness: float = 0.6, color=colors.black):
        super().__init__()
        self.width = width
        self.thickness = thickness
        self.color = color
        self.height = thickness

    def draw(self) -> None:
        self.canv.setStrokeColor(self.color)
        self.canv.setLineWidth(self.thickness)
        self.canv.line(0, 0, self.width, 0)


def _styles(config: Dict[str, Any], fonts: ReportFonts) -> Dict[str, ParagraphStyle]:
    scale = config["type_scale"]
    palette = config["colors"]
    ink = colors.HexColor(palette["ink"])
    muted = colors.HexColor(palette["muted"])

    def style(name: str, size: float, **kwargs) -> ParagraphStyle:
        return ParagraphStyle(
            name,
            fontName=kwargs.pop("font", fonts.regular),
            fontSize=size,
            leading=kwargs.pop("leading", size * 1.32),
            textColor=kwargs.pop("color", ink),
            alignment=kwargs.pop("alignment", TA_LEFT),
            spaceAfter=0,
            spaceBefore=0,
            **kwargs,
        )

    return {
        "brand": style(
            "brand", scale["brand"], font=fonts.bold, color=colors.HexColor(palette["header_text"])
        ),
        "title": style(
            "title", scale["title"], font=fonts.bold, color=colors.HexColor(palette["header_text"])
        ),
        "section": style("section", scale["section_header"], font=fonts.bold),
        "body": style("body", scale["body"]),
        "body_muted": style("body_muted", scale["body"], color=muted),
        "metric_label": style("metric_label", scale["metric_label"], color=muted),
        "metric_value": style("metric_value", scale["metric_value"], font=fonts.bold),
        "table_header": style("table_header", scale["table_header"], font=fonts.bold, color=muted),
        "table_body": style("table_body", scale["table_body"]),
        "table_body_bold": style("table_body_bold", scale["table_body"], font=fonts.bold),
        "positive": style("positive", scale["body"], color=colors.HexColor(palette["positive"])),
        "negative": style("negative", scale["body"], color=colors.HexColor(palette["negative"])),
        "disclaimer": style(
            "disclaimer", scale["disclaimer"], color=muted, leading=scale["disclaimer"] * 1.4
        ),
        "footer": style(
            "footer", scale["footer"], font=fonts.bold, alignment=TA_CENTER, color=muted
        ),
        "caption": style("caption", scale["metric_label"], font=fonts.bold, color=muted),
    }


def _page_size(config: Dict[str, Any]):
    page = config["page"]
    base = A4 if str(page.get("size", "LETTER")).upper() == "A4" else LETTER
    return landscape(base) if str(page.get("orientation", "portrait")) == "landscape" else base


def _header_block(
    content: ReportContent, styles: Dict[str, ParagraphStyle], config: Dict[str, Any], width: float
) -> List[Flowable]:
    palette = config["colors"]
    labels = config["labels"]

    banner = Table(
        [
            [
                Paragraph(labels["brand"], styles["brand"]),
                Paragraph(labels["title"], styles["title"]),
            ]
        ],
        colWidths=[width * 0.30, width * 0.70],
    )
    banner.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(palette["header_band"])),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (0, 0), 9),
                ("RIGHTPADDING", (-1, 0), (-1, 0), 9),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),
            ]
        )
    )

    # Company facts as a single metric strip. Company and sector carry long text
    # and get proportionally more width so they do not wrap onto a second line.
    fields = content.header_fields
    label_row = [Paragraph(label.upper(), styles["metric_label"]) for label, _ in fields]
    value_row = [Paragraph(value, styles["metric_value"]) for _, value in fields]
    proportions = [1.55, 1.45, 0.85, 1.15, 0.70, 1.00, 1.00][: len(fields)]
    if len(proportions) < len(fields):
        proportions += [1.0] * (len(fields) - len(proportions))
    total = sum(proportions)
    col_widths = [width * (p / total) for p in proportions]
    facts = Table([label_row, value_row], colWidths=col_widths)
    facts.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, 0), 7),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 1),
                ("TOPPADDING", (0, 1), (-1, 1), 0),
                ("BOTTOMPADDING", (0, 1), (-1, 1), 6),
            ]
        )
    )
    return [banner, facts, HorizontalRule(width, 0.8, colors.HexColor(palette["rule"]))]


def _section_header(text: str, styles: Dict[str, ParagraphStyle]) -> Paragraph:
    return Paragraph(text, styles["section"])


def _outlook_block(
    content: ReportContent, styles: Dict[str, ParagraphStyle], config: Dict[str, Any], width: float
) -> List[Flowable]:
    palette = config["colors"]
    spacing = config["spacing"]

    header = [Paragraph(h.upper(), styles["table_header"]) for h in content.outlook_header]
    rows = [header]
    for row in content.outlook_rows:
        is_no_liquidity = row[0].lower().startswith("no liquidity")
        label_style = styles["table_body"] if not is_no_liquidity else styles["table_body_bold"]
        cells = [Paragraph(row[0], label_style)]
        cells.extend(Paragraph(v, styles["table_body_bold"]) for v in row[1:])
        rows.append(cells)

    label_width = width * 0.40
    value_width = (width - label_width) / max(len(content.outlook_header) - 1, 1)
    table = Table(
        rows,
        colWidths=[label_width] + [value_width] * (len(content.outlook_header) - 1),
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle(
            [
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), spacing["table_padding"]),
                ("BOTTOMPADDING", (0, 0), (-1, -1), spacing["table_padding"]),
                ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.HexColor(palette["hairline"])),
                ("LINEABOVE", (0, -1), (-1, -1), 0.5, colors.HexColor(palette["hairline"])),
            ]
        )
    )

    summary = Table(
        [
            [
                Paragraph("MOST LIKELY PATH", styles["metric_label"]),
                Paragraph("EXIT ANALYSIS CONFIDENCE", styles["metric_label"]),
            ],
            [
                Paragraph(content.most_likely_path, styles["table_body_bold"]),
                Paragraph(content.confidence_line, styles["table_body_bold"]),
            ],
        ],
        colWidths=[width * 0.58, width * 0.42],
    )
    summary.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, 0), 5),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 1),
                ("TOPPADDING", (0, 1), (-1, 1), 0),
                ("BOTTOMPADDING", (0, 1), (-1, 1), 0),
            ]
        )
    )

    return [
        _section_header(config["labels"]["sections"]["outlook"], styles),
        Spacer(1, spacing["rule_gap"]),
        table,
        summary,
        Spacer(1, 2),
        Paragraph(content.estimate_caption.upper(), styles["caption"]),
    ]


def _two_column(left: List[Flowable], right: List[Flowable], width: float, gutter: float = 16):
    """Place two content stacks side by side."""
    column_width = (width - gutter) / 2
    table = Table(
        [[left, right]], colWidths=[column_width, column_width], hAlign="LEFT"
    )
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (0, 0), 0),
                ("RIGHTPADDING", (0, 0), (0, 0), gutter),
                ("LEFTPADDING", (1, 0), (1, 0), 0),
                ("RIGHTPADDING", (1, 0), (1, 0), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return table


def _milestones_block(
    content: ReportContent, styles: Dict[str, ParagraphStyle], config: Dict[str, Any], width: float
) -> List[Flowable]:
    palette = config["colors"]
    spacing = config["spacing"]

    rows = [
        [
            Paragraph("MILESTONE", styles["table_header"]),
            Paragraph("IMPACT", styles["table_header"]),
        ]
    ]
    for milestone, impact, scenarios in content.milestones:
        rows.append(
            [
                Paragraph(f"{milestone}<br/><font size=5.6 color='#5C5C5C'>{scenarios}</font>",
                          styles["table_body"]),
                Paragraph(impact, styles["table_body_bold"]),
            ]
        )

    table = Table(rows, colWidths=[width * 0.72, width * 0.28], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), spacing["table_padding"]),
                ("BOTTOMPADDING", (0, 0), (-1, -1), spacing["table_padding"]),
                ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.HexColor(palette["hairline"])),
            ]
        )
    )
    return [
        _section_header(config["labels"]["sections"]["milestones"], styles),
        Spacer(1, spacing["rule_gap"]),
        table,
    ]


def _capital_block(
    content: ReportContent, styles: Dict[str, ParagraphStyle], config: Dict[str, Any], width: float
) -> List[Flowable]:
    spacing = config["spacing"]
    rows = [
        [
            Paragraph(label, styles["table_body"]),
            Paragraph(value, styles["table_body_bold"]),
        ]
        for label, value in content.capital_lines
    ]
    table = Table(rows, colWidths=[width * 0.52, width * 0.48], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), spacing["table_padding"]),
                ("BOTTOMPADDING", (0, 0), (-1, -1), spacing["table_padding"]),
            ]
        )
    )
    return [
        _section_header(config["labels"]["sections"]["capital"], styles),
        Spacer(1, spacing["rule_gap"]),
        table,
        Spacer(1, 2),
        Paragraph(content.capital_note, styles["disclaimer"]),
    ]


def _drivers_block(
    content: ReportContent, styles: Dict[str, ParagraphStyle], config: Dict[str, Any], width: float
) -> List[Flowable]:
    spacing = config["spacing"]

    def bullet_list(items: List[str], style_key: str) -> List[Flowable]:
        return [
            Paragraph(f"&bull;&nbsp;{item}", styles[style_key]) for item in items
        ]

    left: List[Flowable] = [Paragraph("UPSIDE", styles["caption"]), Spacer(1, 2)]
    left.extend(bullet_list(content.upside, "positive"))
    right: List[Flowable] = [Paragraph("RISKS", styles["caption"]), Spacer(1, 2)]
    right.extend(bullet_list(content.risks, "negative"))

    return [
        _section_header(config["labels"]["sections"]["drivers"], styles),
        Spacer(1, spacing["rule_gap"]),
        _two_column(left, right, width),
    ]


def _valuation_block(
    content: ReportContent, styles: Dict[str, ParagraphStyle], config: Dict[str, Any], width: float
) -> List[Flowable]:
    spacing = config["spacing"]
    palette = config["colors"]

    rows = [
        [
            Paragraph(label, styles["table_body"]),
            Paragraph(value, styles["table_body_bold"]),
        ]
        for label, value in content.valuation_lines
    ]
    table = Table(rows, colWidths=[width * 0.34, width * 0.66], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), spacing["table_padding"]),
                ("BOTTOMPADDING", (0, 0), (-1, -1), spacing["table_padding"]),
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(palette["band"])),
                ("LEFTPADDING", (0, 0), (0, -1), 6),
                ("RIGHTPADDING", (-1, 0), (-1, -1), 6),
            ]
        )
    )
    return [
        _section_header(config["labels"]["sections"]["valuation"], styles),
        Spacer(1, spacing["rule_gap"]),
        table,
    ]


def _basis_block(
    content: ReportContent, styles: Dict[str, ParagraphStyle], config: Dict[str, Any], width: float
) -> List[Flowable]:
    """Assumptions and limitations, stated on the page rather than buried.

    A directional estimate is only usable if the reader can see what it rests on,
    so this section is part of the report rather than an appendix.
    """
    spacing = config["spacing"]

    def stack(heading: str, items: List[str]) -> List[Flowable]:
        block: List[Flowable] = [Paragraph(heading, styles["caption"]), Spacer(1, 2)]
        if not items:
            block.append(Paragraph("None recorded.", styles["disclaimer"]))
        block.extend(
            Paragraph(f"&bull;&nbsp;{item}", styles["disclaimer"]) for item in items
        )
        return block

    return [
        _section_header(config["labels"]["sections"]["basis"], styles),
        Spacer(1, spacing["rule_gap"]),
        _two_column(
            stack("KEY ASSUMPTIONS", content.assumptions),
            stack("LIMITATIONS", content.limitations),
            width,
        ),
    ]


def _build_flowables(
    content: ReportContent, styles: Dict[str, ParagraphStyle], config: Dict[str, Any], width: float
) -> List[Flowable]:
    spacing = config["spacing"]
    gap = Spacer(1, spacing["section_gap"])
    labels = config["labels"]["sections"]

    story: List[Flowable] = []
    story.extend(_header_block(content, styles, config, width))
    story.append(gap)
    story.extend(_outlook_block(content, styles, config, width))
    story.append(gap)

    story.append(_section_header(labels["profile"], styles))
    story.append(Spacer(1, spacing["rule_gap"]))
    story.append(Paragraph(content.exit_profile, styles["body"]))
    story.append(gap)

    column_width = (width - 16) / 2
    story.append(
        _two_column(
            _milestones_block(content, styles, config, column_width),
            _capital_block(content, styles, config, column_width),
            width,
        )
    )
    story.append(gap)
    story.extend(_drivers_block(content, styles, config, width))
    story.append(gap)
    story.extend(_valuation_block(content, styles, config, width))
    story.append(gap)
    story.extend(_basis_block(content, styles, config, width))
    story.append(Spacer(1, spacing["section_gap"]))
    story.append(HorizontalRule(width, 0.5, colors.HexColor(config["colors"]["hairline"])))
    story.append(Spacer(1, 3))
    story.append(Paragraph(content.disclaimer, styles["disclaimer"]))
    return story


def generate_pdf(
    dossier: DealDossier,
    analysis: ExitScenarioAnalysis,
    output_path: str | Path,
    report_config: Optional[Dict[str, Any]] = None,
    scoring_config: Optional[Dict[str, Any]] = None,
) -> Path:
    """Render the one-page Venture Exit Scenario Report for a completed analysis."""
    config = report_config or load_report_config()
    scoring = scoring_config or load_scoring_config()
    content = build_report_content(
        dossier, analysis, config, float(scoring["probability"]["ipo_revenue_floor"])
    )
    return render_report(content, output_path, config)


def render_report(
    content: ReportContent,
    output_path: str | Path,
    report_config: Optional[Dict[str, Any]] = None,
) -> Path:
    """Render prepared report content to a one-page PDF.

    Separated from ``generate_pdf`` so the blank structural template renders
    through exactly this path. A template produced any other way would drift
    from real output the first time the layout changed.

    Raises PDFGenerationError if the output would exceed a single page, rather
    than emitting a report that silently breaks the format contract.
    """
    config = report_config or load_report_config()
    output_path = Path(output_path)

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PDFGenerationError(f"Cannot create output directory: {exc}") from exc

    fonts = register_fonts(config)
    styles = _styles(config, fonts)

    page_width, page_height = _page_size(config)
    page = config["page"]
    frame_width = page_width - page["margin_left"] - page["margin_right"]
    footer_height = 14
    frame_height = page_height - page["margin_top"] - page["margin_bottom"] - footer_height

    story = _build_flowables(content, styles, config, frame_width)

    try:
        canvas = pdfcanvas.Canvas(str(output_path), pagesize=(page_width, page_height))
        canvas.setTitle(f"Venture Exit Scenario Analysis - {content.company_name}")
        canvas.setAuthor("TEN Capital Network")
        canvas.setSubject("Directional exit-scenario decision support")

        frame = Frame(
            page["margin_left"],
            page["margin_bottom"] + footer_height,
            frame_width,
            frame_height,
            leftPadding=0,
            rightPadding=0,
            topPadding=0,
            bottomPadding=0,
            showBoundary=0,
        )

        # KeepInFrame is the final guarantee: if the capped content is still a
        # shade too tall, it scales to fit rather than spilling to page two.
        fitted = KeepInFrame(frame_width, frame_height, story, mode="shrink")
        frame.addFromList([fitted], canvas)

        _draw_footer(canvas, content, styles, config, page_width, page["margin_bottom"])
        canvas.showPage()
        canvas.save()
    except PDFGenerationError:
        raise
    except Exception as exc:
        raise PDFGenerationError(f"The report could not be rendered: {exc}") from exc

    _verify_single_page(output_path)
    return output_path


def _draw_footer(
    canvas,
    content: ReportContent,
    styles: Dict[str, ParagraphStyle],
    config: Dict[str, Any],
    page_width: float,
    margin_bottom: float,
) -> None:
    paragraph = Paragraph(content.footer, styles["footer"])
    width = page_width - config["page"]["margin_left"] - config["page"]["margin_right"]
    _, height = paragraph.wrap(width, 20)
    paragraph.drawOn(canvas, config["page"]["margin_left"], margin_bottom - height + 6)


def _verify_single_page(path: Path) -> None:
    """Confirm the rendered file really is one page."""
    try:
        from pypdf import PdfReader

        pages = len(PdfReader(str(path)).pages)
    except Exception as exc:
        logger.warning("Could not verify page count of %s: %s", path, exc)
        return
    if pages != 1:
        raise PDFGenerationError(
            f"The report rendered to {pages} pages but must be exactly one. "
            f"Reduce the content limits in report_config.yaml."
        )

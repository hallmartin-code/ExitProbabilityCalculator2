"""PDF ingestion.

Strategy, in order:
  1. pdfplumber -- best text-with-layout fidelity, and native table detection.
  2. pypdf -- lighter fallback when pdfplumber cannot open the file.
  3. OCR -- only for pages that yielded no text by either deterministic route, and
     only when an OCR stack is actually installed.

OCR is last by design: it is slow, lossy, and a deck with a real text layer should
never be read through it.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import List, Optional

from .base import (
    DocumentExtractionError,
    ExtractedDocument,
    ExtractedPage,
    ExtractedTable,
    ExtractionMethod,
    PasswordProtectedError,
    SourceKind,
)

logger = logging.getLogger(__name__)

#: A page with fewer characters than this is treated as having no usable text
#: layer -- typically a full-bleed image slide with only a stray artefact on it.
MIN_CHARS_FOR_TEXT_LAYER = 25

#: Cap on OCR work so one scanned deck cannot hang the interface indefinitely.
MAX_OCR_PAGES = 40


def _clean_text(text: Optional[str]) -> str:
    """Normalise extracted text: collapse runaway whitespace, keep line structure."""
    if not text:
        return ""
    lines = []
    for line in text.splitlines():
        collapsed = re.sub(r"[ \t ]+", " ", line).strip()
        if collapsed:
            lines.append(collapsed)
    return "\n".join(lines)


def _guess_title(text: str) -> Optional[str]:
    """Take the first substantial line as the page/slide title.

    Deck titles are short and sit at the top; anything long is body copy.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if 3 <= len(stripped) <= 90 and not stripped.replace(".", "").isdigit():
            return stripped
    return None


def _tables_from_plumber(page, page_number: int) -> List[ExtractedTable]:
    try:
        raw_tables = page.extract_tables() or []
    except Exception as exc:  # pdfplumber raises a variety of layout errors
        logger.debug("Table extraction failed on page %s: %s", page_number, exc)
        return []
    tables: List[ExtractedTable] = []
    for raw in raw_tables:
        rows = [[(cell or "").strip() for cell in row] for row in raw if row]
        table = ExtractedTable(page_or_slide=page_number, rows=rows)
        if table.is_meaningful:
            tables.append(table)
    return tables


def _extract_with_pdfplumber(path: Path) -> ExtractedDocument:
    import pdfplumber

    doc = ExtractedDocument(filename=path.name, kind=SourceKind.PDF)
    try:
        pdf = pdfplumber.open(str(path))
    except Exception as exc:
        message = str(exc).lower()
        if "password" in message or "encrypt" in message:
            raise PasswordProtectedError(path.name) from exc
        raise DocumentExtractionError(
            f"{path.name} could not be opened as a PDF ({exc}).",
            "Check that the file is a valid, uncorrupted PDF.",
        ) from exc

    with pdf:
        if not pdf.pages:
            raise DocumentExtractionError(
                f"{path.name} contains no pages.", "Upload a deck with at least one page."
            )
        for index, page in enumerate(pdf.pages, start=1):
            try:
                raw_text = page.extract_text() or ""
            except Exception as exc:
                logger.debug("Text extraction failed on page %s: %s", index, exc)
                raw_text = ""
                doc.warnings.append(f"Page {index}: text extraction failed ({exc}).")

            text = _clean_text(raw_text)
            tables = _tables_from_plumber(page, index)
            method = (
                ExtractionMethod.TEXT_LAYER
                if len(text) >= MIN_CHARS_FOR_TEXT_LAYER
                else (ExtractionMethod.TABLE if tables else ExtractionMethod.NONE)
            )
            doc.pages.append(
                ExtractedPage(
                    number=index,
                    title=_guess_title(text),
                    text=text,
                    tables=tables,
                    method=method,
                )
            )
    return doc


def _extract_with_pypdf(path: Path) -> ExtractedDocument:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    doc = ExtractedDocument(filename=path.name, kind=SourceKind.PDF)
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            # An empty user password is common and legitimately decryptable.
            try:
                if reader.decrypt("") == 0:
                    raise PasswordProtectedError(path.name)
            except NotImplementedError as exc:
                raise PasswordProtectedError(path.name) from exc
    except PasswordProtectedError:
        raise
    except (PdfReadError, OSError, ValueError) as exc:
        raise DocumentExtractionError(
            f"{path.name} could not be read ({exc}).",
            "The file may be corrupted. Try re-exporting the deck as a PDF.",
        ) from exc

    for index, page in enumerate(reader.pages, start=1):
        try:
            text = _clean_text(page.extract_text())
        except Exception as exc:
            logger.debug("pypdf text extraction failed on page %s: %s", index, exc)
            text = ""
        doc.pages.append(
            ExtractedPage(
                number=index,
                title=_guess_title(text),
                text=text,
                method=(
                    ExtractionMethod.TEXT_LAYER
                    if len(text) >= MIN_CHARS_FOR_TEXT_LAYER
                    else ExtractionMethod.NONE
                ),
            )
        )
    return doc


def _ocr_available() -> bool:
    try:
        import pytesseract  # noqa: F401
        import pymupdf  # noqa: F401
    except ImportError:
        return False
    try:
        import pytesseract

        pytesseract.get_tesseract_version()
        return True
    except Exception:
        # The Python binding is installed but the Tesseract binary is not.
        return False


def _ocr_pages(path: Path, doc: ExtractedDocument, page_numbers: List[int]) -> None:
    """OCR the given pages in place. Best-effort: failures downgrade, never raise."""
    import pymupdf
    import pytesseract
    from PIL import Image

    try:
        pdf = pymupdf.open(str(path))
    except Exception as exc:
        doc.warnings.append(f"OCR could not open the document ({exc}).")
        return

    with pdf:
        for number in page_numbers[:MAX_OCR_PAGES]:
            try:
                page = pdf[number - 1]
                # 2x scale materially improves recognition of deck-sized type.
                pixmap = page.get_pixmap(matrix=pymupdf.Matrix(2, 2))
                image = Image.frombytes(
                    "RGB", (pixmap.width, pixmap.height), pixmap.samples
                )
                text = _clean_text(pytesseract.image_to_string(image))
            except Exception as exc:
                doc.warnings.append(f"OCR failed on page {number} ({exc}).")
                continue

            if len(text) >= MIN_CHARS_FOR_TEXT_LAYER:
                target = doc.page(number)
                if target is not None:
                    target.text = text
                    target.title = _guess_title(text)
                    target.method = ExtractionMethod.OCR
                    doc.used_ocr = True

    if len(page_numbers) > MAX_OCR_PAGES:
        doc.warnings.append(
            f"Only the first {MAX_OCR_PAGES} image-only pages were processed with OCR."
        )


def parse_pdf(path: str | Path, allow_ocr: bool = True) -> ExtractedDocument:
    """Extract text, tables and structure from a PDF deck.

    Raises DocumentExtractionError (or a subclass) when the file cannot be opened
    or yields nothing at all, so the interface can present a specific remedy.
    """
    path = Path(path)
    if not path.exists():
        raise DocumentExtractionError(
            f"{path.name} was not found.", "Upload the file again."
        )
    if path.stat().st_size == 0:
        raise DocumentExtractionError(
            f"{path.name} is empty (0 bytes).", "Upload a complete PDF file."
        )

    try:
        doc = _extract_with_pdfplumber(path)
    except PasswordProtectedError:
        raise
    except DocumentExtractionError as primary:
        logger.info("pdfplumber failed on %s, falling back to pypdf: %s", path.name, primary)
        doc = _extract_with_pypdf(path)
        doc.warnings.append(
            "Primary PDF parser failed; a simpler fallback parser was used and "
            "table detection was unavailable."
        )

    image_only = [p.number for p in doc.pages if not p.has_text]
    if image_only and allow_ocr:
        if _ocr_available():
            _ocr_pages(path, doc, image_only)
        else:
            doc.warnings.append(
                f"{len(image_only)} page(s) contain no text layer and OCR is not "
                "installed, so their content was not read. Install pytesseract and "
                "the Tesseract binary to enable OCR, or enter those values manually."
            )
    elif image_only:
        doc.warnings.append(
            f"{len(image_only)} page(s) contain no text layer; OCR was disabled."
        )

    if doc.is_empty:
        raise DocumentExtractionError(
            f"No text could be extracted from {path.name}.",
            "The deck appears to be image-only. Enable OCR, or enter the company "
            "details manually in the Company Inputs tab.",
        )
    return doc

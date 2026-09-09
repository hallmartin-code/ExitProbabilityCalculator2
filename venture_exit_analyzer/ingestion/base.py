"""Common structures for extracted documents.

Both parsers produce the same shape, so downstream normalisation never needs to
know whether the deck arrived as a PDF or a PowerPoint file. Page and slide
numbers are preserved throughout so every extracted value can cite its origin.
"""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class SourceKind(str, Enum):
    PDF = "pdf"
    PPTX = "pptx"
    UNKNOWN = "unknown"


class ExtractionMethod(str, Enum):
    """How the text on a page was recovered, in descending order of reliability."""

    TEXT_LAYER = "text_layer"
    TABLE = "table"
    SPEAKER_NOTES = "speaker_notes"
    OCR = "ocr"
    NONE = "none"


class ExtractedTable(BaseModel):
    page_or_slide: int
    rows: List[List[str]] = Field(default_factory=list)

    def as_text(self) -> str:
        return "\n".join(" | ".join(cell or "" for cell in row) for row in self.rows)

    @property
    def is_meaningful(self) -> bool:
        """A table needs at least two rows and one populated cell to be worth keeping."""
        if len(self.rows) < 2:
            return False
        return any(cell and cell.strip() for row in self.rows for cell in row)


class ExtractedPage(BaseModel):
    """One page of a PDF or one slide of a deck."""

    number: int
    title: Optional[str] = None
    text: str = ""
    notes: str = ""
    tables: List[ExtractedTable] = Field(default_factory=list)
    method: ExtractionMethod = ExtractionMethod.NONE

    @property
    def has_text(self) -> bool:
        return bool(self.text.strip())

    def searchable_text(self, include_notes: bool = True) -> str:
        """All text on the page, including tables and optionally speaker notes."""
        parts = [self.text]
        parts.extend(t.as_text() for t in self.tables)
        if include_notes and self.notes:
            parts.append(self.notes)
        return "\n".join(p for p in parts if p and p.strip())


class ExtractedDocument(BaseModel):
    """A parsed deck, page by page."""

    filename: str
    kind: SourceKind = SourceKind.UNKNOWN
    pages: List[ExtractedPage] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    used_ocr: bool = False

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def text_page_count(self) -> int:
        return sum(1 for p in self.pages if p.has_text)

    @property
    def full_text(self) -> str:
        return "\n\n".join(p.searchable_text() for p in self.pages if p.searchable_text())

    @property
    def is_empty(self) -> bool:
        return not self.full_text.strip()

    @property
    def unit_name(self) -> str:
        """What to call a page in user-facing messages: 'slide' or 'page'."""
        return "slide" if self.kind == SourceKind.PPTX else "page"

    def page(self, number: int) -> Optional[ExtractedPage]:
        return next((p for p in self.pages if p.number == number), None)

    def coverage_ratio(self) -> float:
        """Share of pages that yielded any text -- a proxy for extraction quality."""
        if not self.pages:
            return 0.0
        return self.text_page_count / len(self.pages)


class DocumentExtractionError(Exception):
    """Raised when a document cannot be opened or contains no recoverable content.

    Carries a remedy string so the UI can tell the user what to actually do.
    """

    def __init__(self, message: str, remedy: str = ""):
        super().__init__(message)
        self.message = message
        self.remedy = remedy

    def __str__(self) -> str:
        return f"{self.message} {self.remedy}".strip()


class PasswordProtectedError(DocumentExtractionError):
    def __init__(self, filename: str):
        super().__init__(
            f"{filename} is password-protected and cannot be read.",
            "Remove the password protection and upload the file again.",
        )


class UnsupportedFormatError(DocumentExtractionError):
    def __init__(self, filename: str, detail: str = ""):
        super().__init__(
            f"{filename} is not a supported document format. {detail}".strip(),
            "Upload a PDF, PPTX, or PPT file.",
        )

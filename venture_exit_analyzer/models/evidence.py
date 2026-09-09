"""Evidence-tracked values.

Every materially important extracted value carries its provenance: where it came
from, how it was established, and how much confidence we place in it. This is the
foundation of the application's explainability requirement -- no number reaches the
report without a traceable origin.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Generic, Optional, TypeVar

from pydantic import BaseModel, Field, field_validator

T = TypeVar("T")


class EvidenceClass(str, Enum):
    """How a value came to be known."""

    EXPLICIT = "explicitly_stated"
    CALCULATED = "calculated"
    INFERRED = "inferred"
    USER_SUPPLIED = "user_supplied"
    COMPARABLE_DERIVED = "comparable_derived"
    MISSING = "missing"


#: Evidence classes we treat as hard fact when scoring. Inferred and comparable-derived
#: values still count, but the confidence attached to them carries the discount.
FACTUAL_CLASSES = frozenset(
    {EvidenceClass.EXPLICIT, EvidenceClass.CALCULATED, EvidenceClass.USER_SUPPLIED}
)


class Evidenced(BaseModel, Generic[T]):
    """A value plus its provenance.

    A missing value is represented by ``value=None`` with
    ``evidence_class=MISSING`` and ``confidence=0.0`` -- never by a zero or an
    empty string, which would be indistinguishable from a real measurement.
    """

    value: Optional[T] = None
    source: Optional[str] = Field(
        default=None, description="Human-readable origin, e.g. 'Deck p.12 revenue table'."
    )
    source_page_or_slide: Optional[int] = None
    evidence_class: EvidenceClass = EvidenceClass.MISSING
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    note: Optional[str] = Field(
        default=None, description="Assumption or caveat attached to this value."
    )

    @field_validator("confidence")
    @classmethod
    def _confidence_matches_presence(cls, v: float, info) -> float:
        # A present value with zero confidence, or an absent value with positive
        # confidence, is almost always a construction bug. Clamp rather than raise so
        # ingestion of a messy deck never hard-fails.
        if info.data.get("value") is None:
            return 0.0
        return v

    @property
    def is_present(self) -> bool:
        return self.value is not None

    @property
    def is_factual(self) -> bool:
        """True when the value is stated, calculated, or user-supplied."""
        return self.is_present and self.evidence_class in FACTUAL_CLASSES

    def or_default(self, default: Any) -> Any:
        """Return the value, or ``default`` when absent. Never coerces None to zero."""
        return self.value if self.is_present else default

    def describe(self) -> str:
        """One-line provenance string for report footnotes and the audit trail."""
        if not self.is_present:
            return "not stated in source materials"
        where = ""
        if self.source_page_or_slide is not None:
            where = f" (p./slide {self.source_page_or_slide})"
        src = self.source or self.evidence_class.value.replace("_", " ")
        return f"{src}{where} [{self.evidence_class.value}, conf {self.confidence:.2f}]"

    # -- constructors -------------------------------------------------------
    @classmethod
    def missing(cls, note: Optional[str] = None) -> "Evidenced[T]":
        return cls(value=None, evidence_class=EvidenceClass.MISSING, confidence=0.0, note=note)

    @classmethod
    def from_deck(
        cls,
        value: T,
        source: str,
        page: Optional[int] = None,
        confidence: float = 0.8,
    ) -> "Evidenced[T]":
        return cls(
            value=value,
            source=source,
            source_page_or_slide=page,
            evidence_class=EvidenceClass.EXPLICIT,
            confidence=confidence,
        )

    @classmethod
    def from_user(cls, value: T, note: Optional[str] = None) -> "Evidenced[T]":
        return cls(
            value=value,
            source="User supplied",
            evidence_class=EvidenceClass.USER_SUPPLIED,
            confidence=0.95,
            note=note,
        )

    @classmethod
    def calculated(
        cls, value: T, source: str, confidence: float = 0.85, note: Optional[str] = None
    ) -> "Evidenced[T]":
        return cls(
            value=value,
            source=source,
            evidence_class=EvidenceClass.CALCULATED,
            confidence=confidence,
            note=note,
        )

    @classmethod
    def inferred(
        cls, value: T, source: str, confidence: float = 0.5, note: Optional[str] = None
    ) -> "Evidenced[T]":
        return cls(
            value=value,
            source=source,
            evidence_class=EvidenceClass.INFERRED,
            confidence=confidence,
            note=note,
        )


# Convenient concrete aliases -- Pydantic v2 resolves the generic at these sites.
EvidencedFloat = Evidenced[float]
EvidencedInt = Evidenced[int]
EvidencedStr = Evidenced[str]
EvidencedBool = Evidenced[bool]

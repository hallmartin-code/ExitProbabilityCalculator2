"""Shared scoring machinery.

Every dimension is built the same way: declare a factor, supply the evidence, and
let the builder attach the configured weight. The builder enforces the rules that
keep scores honest --

  * a factor with no evidence scores the configured neutral value and is marked
    MISSING, so it lowers confidence without inventing a strength or a weakness;
  * a factor may only be scored once, and only if it exists in the config;
  * every factor in the config must be accounted for before the score is built.

That last rule is what stops a dimension from quietly losing a factor during a
refactor and returning a score computed over the wrong denominator.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..config.loader import factor_weights, load_scoring_config, neutral_score, score_from_band
from ..models.analysis import DimensionScore, ScoreDriver
from ..models.evidence import Evidenced, EvidenceClass
from ..utils.calculations import clamp


class ScoringError(RuntimeError):
    """Raised when a dimension is assembled inconsistently with its configuration."""


class DriverBuilder:
    """Accumulates weighted drivers for one dimension."""

    def __init__(self, dimension: str, config: Optional[Dict[str, Any]] = None):
        self.config = config or load_scoring_config()
        self.dimension = dimension
        self.weights = factor_weights(dimension, self.config)
        self.neutral = neutral_score(self.config)
        self._drivers: Dict[str, ScoreDriver] = {}

    # -- internals ---------------------------------------------------------
    def _weight(self, factor: str) -> float:
        if factor not in self.weights:
            raise ScoringError(
                f"Factor '{factor}' is not defined for dimension '{self.dimension}' "
                f"in scoring_weights.yaml."
            )
        if factor in self._drivers:
            raise ScoringError(
                f"Factor '{factor}' was scored twice in dimension '{self.dimension}'."
            )
        return self.weights[factor]

    def _add(
        self, factor: str, score: float, evidence: str, evidence_class: EvidenceClass
    ) -> "DriverBuilder":
        weight = self._weight(factor)
        self._drivers[factor] = ScoreDriver(
            factor=factor,
            score=clamp(float(score), 0.0, 10.0),
            weight=weight,
            evidence=evidence,
            evidence_class=evidence_class,
        )
        return self

    # -- public API --------------------------------------------------------
    def score(
        self,
        factor: str,
        value: float,
        evidence: str,
        evidence_class: EvidenceClass = EvidenceClass.INFERRED,
    ) -> "DriverBuilder":
        """Record a directly assessed 0-10 factor score."""
        return self._add(factor, value, evidence, evidence_class)

    def missing(self, factor: str, reason: str) -> "DriverBuilder":
        """Record a factor that could not be assessed.

        Scores the neutral value so the dimension is not distorted, and marks the
        driver MISSING so the confidence model can see the gap.
        """
        return self._add(
            factor,
            self.neutral,
            f"Not disclosed - {reason}. Scored neutral ({self.neutral:.0f}/10).",
            EvidenceClass.MISSING,
        )

    def banded(
        self,
        factor: str,
        evidenced: Evidenced,
        band: str,
        label: str,
        formatter=lambda v: f"{v}",
        invert: bool = False,
    ) -> "DriverBuilder":
        """Score a factor from an evidenced numeric value using a config band.

        ``invert`` is for factors where the band already expresses the desirable
        direction of a metric whose raw value runs the other way.
        """
        if not evidenced.is_present:
            return self.missing(factor, f"{label} was not stated")

        banded_score = score_from_band(float(evidenced.value), band, self.config)
        if banded_score is None:
            return self.missing(factor, f"{label} could not be scored")
        if invert:
            banded_score = 10.0 - banded_score

        return self._add(
            factor,
            banded_score,
            f"{label} of {formatter(evidenced.value)} ({evidenced.describe()}).",
            evidenced.evidence_class,
        )

    def flag(
        self,
        factor: str,
        condition: Optional[bool],
        true_score: float,
        false_score: float,
        true_evidence: str,
        false_evidence: str,
        missing_reason: str,
        evidence_class: EvidenceClass = EvidenceClass.INFERRED,
    ) -> "DriverBuilder":
        """Score a yes/no factor, treating None as unknown rather than as 'no'."""
        if condition is None:
            return self.missing(factor, missing_reason)
        return self._add(
            factor,
            true_score if condition else false_score,
            true_evidence if condition else false_evidence,
            evidence_class,
        )

    def fill_remaining(self, reason: str = "not addressed in the source materials") -> "DriverBuilder":
        """Mark every unscored factor as missing.

        Called at the end of each dimension so the driver set always covers the
        full configured weight, whatever the deck happened to contain.
        """
        for factor in self.weights:
            if factor not in self._drivers:
                self.missing(factor, reason)
        return self

    def build(self, summary: str = "") -> DimensionScore:
        missing_factors = set(self.weights) - set(self._drivers)
        if missing_factors:
            raise ScoringError(
                f"Dimension '{self.dimension}' left {sorted(missing_factors)} unscored. "
                f"Call fill_remaining() before build()."
            )

        drivers: List[ScoreDriver] = [self._drivers[f] for f in self.weights]
        total = sum(d.contribution for d in drivers)
        return DimensionScore(
            dimension=self.dimension,
            score=clamp(total, 0.0, 100.0),
            drivers=drivers,
            summary=summary,
        )


def keyword_signal(text: str, keywords: tuple, minimum_hits: int = 1) -> Optional[bool]:
    """Look for supporting language in the deck.

    Returns True on a hit, and None -- not False -- when nothing matches. Absence
    of a keyword is weak evidence of absence in a document that is a summary by
    nature, so the caller treats it as unknown.
    """
    if not text:
        return None
    lowered = f" {text.lower()} "
    hits = sum(1 for keyword in keywords if keyword in lowered)
    return True if hits >= minimum_hits else None

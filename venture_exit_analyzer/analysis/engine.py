"""Analysis orchestration.

Runs the scoring dimensions, the probability engine, and the supporting analyses
in order, then assembles the narrative, assumptions and limitations that make the
result readable. This is the single entry point the UI and the tests call.
"""

from __future__ import annotations

import logging

from typing import Any, Dict, List, Optional

from ..config.loader import load_scoring_config
from ..models.analysis import (
    DealDossier,
    ExitScenarioAnalysis,
    Scenario,
    TimeHorizon,
)
from ..utils.formatting import money, months, percent, sentence
from .acquirers import build_acquirer_profiles
from .capital_requirements import estimate_capital_to_exit
from .confidence import assess_confidence
from .financial_exit import score_financial_acquisition
from .ipo_analysis import score_ipo_readiness
from .liquidity_risk import score_liquidity_risk
from .milestones import build_milestones
from .probability_engine import (
    HorizonComputation,
    compute_probabilities,
    select_primary_liquidity_path,
    select_primary_scenario,
)
from .strategic_exit import score_strategic_acquisition
from .valuation import compute_valuation_ranges, identify_valuation_drivers

logger = logging.getLogger(__name__)

DEFAULT_HORIZONS = [TimeHorizon.Y3_5, TimeHorizon.Y5_7]


class AnalysisResult:
    """The analysis plus the intermediate traces the UI uses for explainability."""

    def __init__(
        self,
        analysis: ExitScenarioAnalysis,
        computations: List[HorizonComputation],
        narrative_outcome: Any = None,
    ):
        self.analysis = analysis
        self.computations = computations
        #: Set when AI narrative generation ran, whether or not it was accepted.
        self.narrative_outcome = narrative_outcome


def run_analysis(
    dossier: DealDossier,
    horizons: Optional[List[TimeHorizon]] = None,
    deck_text: str = "",
    config: Optional[Dict[str, Any]] = None,
    claude_service: Any = None,
) -> AnalysisResult:
    """Run the complete exit-scenario analysis over a reviewed dossier.

    ``claude_service`` is optional and affects only prose. Every score,
    probability, valuation and capital figure below is computed deterministically
    and is unchanged by its presence or absence.
    """
    cfg = config or load_scoring_config()
    horizons = horizons or list(DEFAULT_HORIZONS)

    strategic = score_strategic_acquisition(dossier, deck_text, cfg)
    financial = score_financial_acquisition(dossier, deck_text, cfg)
    ipo = score_ipo_readiness(dossier, deck_text, cfg)
    liquidity_risk = score_liquidity_risk(dossier, deck_text, cfg)

    computations = compute_probabilities(
        horizons, strategic, financial, ipo, liquidity_risk, dossier, cfg
    )
    primary_scenario, primary_horizon = select_primary_scenario(computations)
    liquidity_path, liquidity_horizon, liquidity_probability = select_primary_liquidity_path(
        computations
    )

    milestones = build_milestones(dossier, strategic, financial, ipo, liquidity_risk, cfg)
    acquirers = build_acquirer_profiles(
        dossier, strategic, financial, liquidity_risk, [m.milestone for m in milestones]
    )
    capital = estimate_capital_to_exit(dossier, liquidity_path, cfg)
    valuation_ranges = compute_valuation_ranges(dossier, cfg)
    valuation_drivers = identify_valuation_drivers(
        dossier, strategic, financial, liquidity_risk
    )
    confidence = assess_confidence(
        dossier, [strategic, financial, ipo, liquidity_risk], cfg
    )

    analysis = ExitScenarioAnalysis(
        strategic_score=strategic,
        financial_score=financial,
        ipo_score=ipo,
        liquidity_risk_score=liquidity_risk,
        probabilities=[c.probabilities for c in computations],
        primary_scenario=primary_scenario,
        primary_horizon=primary_horizon,
        confidence=confidence,
        acquirer_profiles=acquirers,
        milestones=milestones,
        capital_requirement=capital,
        valuation_ranges=valuation_ranges,
        valuation_drivers=valuation_drivers,
        exit_profile_summary=_build_exit_profile(
            dossier, liquidity_path, liquidity_horizon, liquidity_probability, acquirers,
            primary_scenario,
        ),
        assumptions=_collect_assumptions(dossier, capital, computations, cfg),
        limitations=_collect_limitations(dossier, confidence, valuation_ranges),
        risks=_collect_risks(dossier, liquidity_risk),
    )

    narrative_outcome = _apply_ai_narrative(
        dossier, analysis, claude_service, liquidity_path, liquidity_horizon,
        liquidity_probability,
    )
    return AnalysisResult(analysis, computations, narrative_outcome)


def _apply_ai_narrative(
    dossier: DealDossier,
    analysis: ExitScenarioAnalysis,
    claude_service: Any,
    liquidity_path: Scenario,
    liquidity_horizon: Optional[TimeHorizon],
    liquidity_probability: float,
) -> Any:
    """Replace the templated exit profile with generated prose, if it is safe to.

    The deterministic narrative is already in place before this runs, so any
    failure -- no service, an API error, or a rejected narrative -- simply leaves
    the report as it was. Generated prose is an improvement in readability, never
    a dependency.
    """
    if claude_service is None or not getattr(claude_service, "available", False):
        return None

    from .narrative import build_narrative_facts

    try:
        facts, allowed = build_narrative_facts(
            dossier, analysis, liquidity_path, liquidity_horizon, liquidity_probability
        )
        outcome = claude_service.narrate(facts, allowed)
    except Exception as exc:  # noqa: BLE001 - never let prose break the analysis
        logger.warning("Narrative generation failed, keeping deterministic text: %s", exc)
        return None

    if outcome.accepted and outcome.text:
        analysis.exit_profile_summary = outcome.text
    elif outcome.rejection_reason:
        logger.info("Generated narrative rejected: %s", outcome.rejection_reason)
    return outcome


def _build_exit_profile(
    dossier: DealDossier,
    path: Scenario,
    horizon: Optional[TimeHorizon],
    probability: float,
    acquirers: List,
    primary_scenario: Scenario,
) -> str:
    """The narrative paragraph describing the most plausible exit."""
    company = dossier.company.display_name
    timing = horizon.label.lower() if horizon else "the period assessed"
    buyer = next(
        (a.category for a in acquirers if not a.is_named_buyer and a.strategic_fit is not None),
        "industry incumbents",
    )

    if primary_scenario == Scenario.NO_LIQUIDITY:
        lead = (
            f"On the evidence supplied, the single most likely outcome for {company} across "
            f"the period assessed is that no liquidity event occurs. "
        )
    else:
        lead = ""

    body = (
        f"The most plausible liquidity route is a {path.label.lower()} at {probability:.0f}% "
        f"within {timing}, most likely to {buyer.lower()}. "
    )

    rationale_parts = []
    if dossier.ip.regulatory_approvals:
        rationale_parts.append(
            f"regulatory clearance already held ({dossier.ip.regulatory_approvals[0]})"
        )
    positions = dossier.ip.total_patent_positions
    if positions:
        rationale_parts.append(f"{positions} patent position(s)")
    revenue = dossier.financials.best_revenue
    if revenue:
        label = dossier.financials.best_revenue_label
        rationale_parts.append(
            f"{money(revenue)} of {label if label == 'ARR' else label.lower()}"
        )
    if dossier.customers.recurring_revenue_pct.is_present:
        rationale_parts.append(
            f"{percent(dossier.customers.recurring_revenue_pct.value)} recurring revenue"
        )

    rationale = (
        f"The rationale rests on {', '.join(rationale_parts)}."
        if rationale_parts
        else "The disclosures do not establish a specific strategic rationale."
    )
    return sentence(lead + body + rationale)


def _collect_assumptions(
    dossier: DealDossier,
    capital,
    computations: List[HorizonComputation],
    cfg: Dict[str, Any],
) -> List[str]:
    assumptions: List[str] = [
        "Probabilities are directional estimates derived from a transparent weighted "
        "scoring model, not forecasts, and are rounded to "
        f"{int(cfg['probability']['rounding_increment'])}% increments to avoid implying "
        "precision the inputs cannot support.",
        "Factors not disclosed in the source materials are scored neutrally rather than "
        "favourably or unfavourably; the resulting uncertainty is carried by the "
        "confidence score instead.",
    ]
    assumptions.extend(capital.assumptions)

    for computation in computations:
        for note in computation.notes:
            assumptions.append(f"{computation.horizon.label}: {note}")

    if dossier.comparables.comparables:
        assumptions.append(
            f"Valuation ranges use the {len(dossier.comparables)} comparable transaction(s) "
            f"supplied by the user. The application holds no market data of its own."
        )
    if dossier.named_buyers:
        assumptions.append(
            "Named buyers were supplied by the user. Their strategic fit is scored from "
            "this company's characteristics only, not from any knowledge of those buyers' "
            "acquisition intentions."
        )
    return assumptions


def _collect_limitations(dossier: DealDossier, confidence, valuation_ranges) -> List[str]:
    limitations: List[str] = [
        "This analysis is decision support, not an investment recommendation, a valuation "
        "opinion, or a prediction of future liquidity.",
    ]
    if confidence.score < 45:
        limitations.append(
            f"Analysis confidence is {confidence.score:.0f}/100 ({confidence.band}). The "
            f"conclusions should be treated as provisional pending better disclosure."
        )
    if not dossier.comparables.comparables:
        limitations.append(
            "No comparable transactions were supplied, so no valuation range could be "
            "triangulated against market evidence."
        )
    for valuation in valuation_ranges:
        if not valuation.is_defensible and valuation.insufficient_reason:
            limitations.append(
                f"{valuation.scenario.label} valuation range omitted: "
                f"{valuation.insufficient_reason}."
            )
    limitations.extend(dossier.extraction_warnings)

    if dossier.financials.best_revenue is None:
        limitations.append(
            "No revenue was disclosed, which materially limits every scale-dependent "
            "conclusion in this report."
        )
    return limitations


def _collect_risks(dossier: DealDossier, liquidity_risk) -> List[str]:
    """The highest-scoring liquidity risks, stated as the evidence supports them."""
    risks = [
        f"{driver.factor.replace('_', ' ').capitalize()}: {driver.evidence}"
        for driver in liquidity_risk.top_drivers(4)
        if driver.score >= 5.5
    ]
    if not risks:
        risks.append(
            "No individual risk factor scored above the reporting threshold on the "
            "evidence disclosed."
        )
    return risks

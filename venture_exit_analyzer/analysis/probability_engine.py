"""Translation of dimension scores into directional scenario probabilities.

The model is deliberately simple and fully inspectable. For each liquidity route
and each time horizon:

    weight = base_rate * (score / 100) ** exponent * horizon_factor * gates

The four weights are normalised to 100%, the no-liquidity bucket is held within
its configured floor and ceiling, and the result is rounded to a coarse increment
so the output never implies precision the inputs cannot support.

Every constant lives in ``scoring_weights.yaml``. Nothing here is a forecast: the
output is a directional distribution conditioned on the evidence supplied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..config.loader import load_scoring_config
from ..models.analysis import (
    DealDossier,
    DimensionScore,
    HorizonProbabilities,
    Scenario,
    TimeHorizon,
)
from ..models.company import Stage
from ..utils.calculations import (
    apply_floor_and_renormalize,
    normalize_to_percent,
    round_to_increment,
)

#: Config key for each scenario, matching the YAML structure.
SCENARIO_KEYS: Dict[Scenario, str] = {
    Scenario.STRATEGIC: "strategic_acquisition",
    Scenario.FINANCIAL: "financial_acquisition",
    Scenario.IPO: "ipo",
    Scenario.NO_LIQUIDITY: "no_liquidity",
}


@dataclass
class WeightTrace:
    """The full derivation of one scenario weight, kept for explainability."""

    scenario: Scenario
    dimension_score: float
    base_rate: float
    exponent: float
    horizon_factor: float
    stage_multiplier: float
    damping: float
    damping_reason: Optional[str]
    weight: float

    def explain(self) -> str:
        parts = [
            f"score {self.dimension_score:.0f}/100",
            f"base {self.base_rate:.2f}",
            f"horizon x{self.horizon_factor:.2f}",
        ]
        if self.stage_multiplier != 1.0:
            parts.append(f"stage x{self.stage_multiplier:.2f}")
        if self.damping != 1.0:
            parts.append(f"damped x{self.damping:.2f} ({self.damping_reason})")
        return ", ".join(parts)


@dataclass
class HorizonComputation:
    """Probabilities for one horizon plus the traces that produced them."""

    horizon: TimeHorizon
    probabilities: HorizonProbabilities
    traces: List[WeightTrace] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def _stage_multiplier(stage: Stage, cfg: Dict[str, Any]) -> float:
    table = cfg["probability"]["stage_liquidity_multiplier"]
    return float(table.get(stage.ordinal, table.get(1, 1.0)))


def _damping(
    scenario: Scenario, revenue: Optional[float], cfg: Dict[str, Any]
) -> tuple[float, Optional[str]]:
    """Hard gates that reflect the scale each route actually requires.

    A strong IPO-readiness score on a $4M-revenue company still does not make a
    listing a realistic outcome, and the model should not pretend otherwise.
    """
    prob = cfg["probability"]
    if scenario == Scenario.IPO:
        floor = float(prob["ipo_revenue_floor"])
        if revenue is None or revenue < floor:
            from ..utils.formatting import money

            return (
                float(prob["ipo_below_floor_damping"]),
                f"revenue below the {money(floor)} listing threshold",
            )
    if scenario == Scenario.FINANCIAL:
        floor = float(prob["financial_revenue_floor"])
        if revenue is None or revenue < floor:
            from ..utils.formatting import money

            return (
                float(prob["financial_below_floor_damping"]),
                f"revenue below the {money(floor)} level a financial buyer underwrites",
            )
    return 1.0, None


def compute_horizon(
    horizon: TimeHorizon,
    strategic: DimensionScore,
    financial: DimensionScore,
    ipo: DimensionScore,
    liquidity_risk: DimensionScore,
    dossier: DealDossier,
    config: Optional[Dict[str, Any]] = None,
) -> HorizonComputation:
    """Compute the scenario distribution for a single time horizon."""
    cfg = config or load_scoring_config()
    prob_cfg = cfg["probability"]
    horizon_factors = prob_cfg["horizon_factor"][horizon.value]

    scores = {
        Scenario.STRATEGIC: strategic.score,
        Scenario.FINANCIAL: financial.score,
        Scenario.IPO: ipo.score,
        Scenario.NO_LIQUIDITY: liquidity_risk.score,
    }

    stage = dossier.company.stage_value
    stage_mult = _stage_multiplier(stage, cfg)
    revenue = dossier.financials.best_revenue

    traces: List[WeightTrace] = []
    weights: Dict[str, float] = {}

    for scenario, score in scores.items():
        key = SCENARIO_KEYS[scenario]
        base = float(prob_cfg["base_rate"][key])
        exponent = float(prob_cfg["exponent"][key])
        horizon_factor = float(horizon_factors[key])

        # Stage maturity gates the liquidity routes only. The no-liquidity bucket
        # is not multiplied by it, so an early-stage company ends up with more
        # weight on "no exit" purely through normalisation.
        applied_stage = stage_mult if scenario != Scenario.NO_LIQUIDITY else 1.0
        damping, damping_reason = (
            _damping(scenario, revenue, cfg)
            if scenario != Scenario.NO_LIQUIDITY
            else (1.0, None)
        )

        normalized_score = max(score, 0.0) / 100.0
        weight = (
            base * (normalized_score**exponent) * horizon_factor * applied_stage * damping
        )
        weights[key] = weight
        traces.append(
            WeightTrace(
                scenario=scenario,
                dimension_score=score,
                base_rate=base,
                exponent=exponent,
                horizon_factor=horizon_factor,
                stage_multiplier=applied_stage,
                damping=damping,
                damping_reason=damping_reason,
                weight=weight,
            )
        )

    notes: List[str] = []

    percentages = normalize_to_percent(weights)
    if sum(weights.values()) <= 0:
        notes.append(
            "All scenario weights evaluated to zero, so an even distribution was used. "
            "Treat this result as uninformative."
        )

    no_liquidity_key = SCENARIO_KEYS[Scenario.NO_LIQUIDITY]
    bounded = apply_floor_and_renormalize(
        percentages,
        no_liquidity_key,
        floor=float(prob_cfg["min_no_liquidity"]),
        ceiling=float(prob_cfg["max_no_liquidity"]),
    )
    if abs(bounded[no_liquidity_key] - percentages[no_liquidity_key]) > 0.01:
        notes.append(
            f"No-liquidity probability was held to its configured bound of "
            f"{bounded[no_liquidity_key]:.0f}%."
        )

    rounded = round_to_increment(bounded, float(prob_cfg["rounding_increment"]))

    return HorizonComputation(
        horizon=horizon,
        probabilities=HorizonProbabilities(
            horizon=horizon,
            strategic_acquisition=rounded[SCENARIO_KEYS[Scenario.STRATEGIC]],
            financial_acquisition=rounded[SCENARIO_KEYS[Scenario.FINANCIAL]],
            ipo=rounded[SCENARIO_KEYS[Scenario.IPO]],
            no_liquidity=rounded[no_liquidity_key],
        ),
        traces=traces,
        notes=notes,
    )


def compute_probabilities(
    horizons: List[TimeHorizon],
    strategic: DimensionScore,
    financial: DimensionScore,
    ipo: DimensionScore,
    liquidity_risk: DimensionScore,
    dossier: DealDossier,
    config: Optional[Dict[str, Any]] = None,
) -> List[HorizonComputation]:
    """Compute distributions across every selected horizon, in chronological order."""
    if not horizons:
        raise ValueError("At least one time horizon must be selected.")
    ordered = sorted(set(horizons), key=lambda h: h.midpoint_years)
    return [
        compute_horizon(h, strategic, financial, ipo, liquidity_risk, dossier, config)
        for h in ordered
    ]


def select_primary_scenario(
    computations: List[HorizonComputation],
) -> tuple[Scenario, Optional[TimeHorizon]]:
    """The most probable outcome over the full period under consideration.

    Read off the longest selected horizon rather than as an argmax across all of
    them. Near-term windows put most of their mass on "no liquidity" for almost
    every venture-stage company, so a cross-horizon argmax would almost always
    return that, which is close to tautological and tells the reader nothing.
    The terminal horizon answers the question actually being asked: across the
    whole period, what is the single most likely outcome?

    No-liquidity remains eligible to win, and when it does the report says so.
    """
    if not computations:
        return Scenario.NO_LIQUIDITY, None

    terminal = max(computations, key=lambda c: c.horizon.midpoint_years)
    return terminal.probabilities.most_likely, terminal.horizon


def select_primary_liquidity_path(
    computations: List[HorizonComputation],
) -> tuple[Scenario, Optional[TimeHorizon], float]:
    """The most probable actual exit route, ignoring the no-liquidity case.

    Used for the report's exit-profile narrative, which describes what an exit
    would look like if one occurs.
    """
    best = (Scenario.STRATEGIC, None, -1.0)
    for computation in computations:
        for scenario, value in computation.probabilities.as_dict().items():
            if scenario == Scenario.NO_LIQUIDITY:
                continue
            if value > best[2]:
                best = (scenario, computation.horizon, value)
    return best

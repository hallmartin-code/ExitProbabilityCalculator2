"""Scoring, probability, valuation, capital and confidence."""

from __future__ import annotations

import pytest

from venture_exit_analyzer.analysis.capital_requirements import estimate_capital_to_exit
from venture_exit_analyzer.analysis.confidence import assess_confidence
from venture_exit_analyzer.analysis.engine import run_analysis
from venture_exit_analyzer.analysis.financial_exit import score_financial_acquisition
from venture_exit_analyzer.analysis.ipo_analysis import score_ipo_readiness
from venture_exit_analyzer.analysis.liquidity_risk import score_liquidity_risk
from venture_exit_analyzer.analysis.probability_engine import (
    compute_probabilities,
    select_primary_scenario,
)
from venture_exit_analyzer.analysis.scoring_base import DriverBuilder, ScoringError
from venture_exit_analyzer.analysis.strategic_exit import score_strategic_acquisition
from venture_exit_analyzer.analysis.valuation import compute_valuation_ranges
from venture_exit_analyzer.config.loader import (
    ConfigError,
    factor_weights,
    load_scoring_config,
    score_from_band,
    validate_scoring_config,
)
from venture_exit_analyzer.models.analysis import Scenario, TimeHorizon
from venture_exit_analyzer.models.company import Stage
from venture_exit_analyzer.models.evidence import Evidenced, EvidenceClass


class TestConfiguration:
    def test_every_dimension_sums_to_one(self):
        cfg = load_scoring_config()
        for dimension in cfg["dimensions"]:
            assert sum(factor_weights(dimension, cfg).values()) == pytest.approx(1.0)

    def test_mis_weighted_config_is_rejected(self):
        bad = {
            "dimensions": {"x": {"factors": {"a": 0.5, "b": 0.2}}},
            "probability": {"rounding_increment": 5},
        }
        with pytest.raises(ConfigError) as exc:
            validate_scoring_config(bad)
        assert "sum to" in str(exc.value)

    def test_band_lookup(self):
        assert score_from_band(25_000_000, "revenue_scale") == 8.0
        assert score_from_band(25_000_000, "ipo_revenue_scale") == 5.5

    def test_absent_value_scores_none_not_zero(self):
        assert score_from_band(None, "revenue_scale") is None


class TestDriverBuilder:
    def test_unknown_factor_rejected(self):
        builder = DriverBuilder("strategic_acquisition")
        with pytest.raises(ScoringError):
            builder.score("not_a_real_factor", 5, "evidence")

    def test_double_scoring_rejected(self):
        builder = DriverBuilder("strategic_acquisition")
        builder.score("ip_defensibility", 5, "e")
        with pytest.raises(ScoringError):
            builder.score("ip_defensibility", 6, "e")

    def test_incomplete_dimension_cannot_be_built(self):
        """A dimension missing factors would score against the wrong denominator."""
        builder = DriverBuilder("strategic_acquisition")
        builder.score("ip_defensibility", 5, "e")
        with pytest.raises(ScoringError) as exc:
            builder.build()
        assert "unscored" in str(exc.value)

    def test_missing_factors_score_neutral_and_are_flagged(self):
        builder = DriverBuilder("strategic_acquisition").fill_remaining()
        dimension = builder.build()
        assert dimension.score == pytest.approx(50.0)
        assert dimension.evidence_coverage == 0.0


class TestDimensionScores:
    def test_all_dimensions_in_range(self, dossier, parsed_pdf):
        text = parsed_pdf.full_text
        for score in (
            score_strategic_acquisition(dossier, text),
            score_financial_acquisition(dossier, text),
            score_ipo_readiness(dossier, text),
            score_liquidity_risk(dossier, text),
        ):
            assert 0.0 <= score.score <= 100.0
            assert score.drivers

    def test_score_equals_sum_of_driver_contributions(self, dossier, parsed_pdf):
        score = score_strategic_acquisition(dossier, parsed_pdf.full_text)
        assert score.score == pytest.approx(sum(d.contribution for d in score.drivers))

    def test_regulatory_clearance_lifts_strategic_score(self, dossier, parsed_pdf):
        text = parsed_pdf.full_text
        with_clearance = score_strategic_acquisition(dossier, text).score
        stripped = dossier.model_copy(deep=True)
        stripped.ip.regulatory_approvals = []
        assert score_strategic_acquisition(stripped, text).score < with_clearance

    def test_ipo_uses_the_stricter_revenue_band(self, dossier, parsed_pdf):
        """A $4.2M company must not score well on IPO revenue scale."""
        score = score_ipo_readiness(dossier, parsed_pdf.full_text)
        revenue_driver = next(d for d in score.drivers if d.factor == "revenue_scale")
        assert revenue_driver.score <= 2.0

    def test_every_driver_carries_evidence_text(self, dossier, parsed_pdf):
        score = score_financial_acquisition(dossier, parsed_pdf.full_text)
        assert all(d.evidence.strip() for d in score.drivers)

    def test_empty_dossier_scores_neutral_with_no_coverage(self, empty_dossier):
        score = score_strategic_acquisition(empty_dossier, "")
        assert score.evidence_coverage == 0.0
        assert score.score == pytest.approx(50.0)


class TestProbabilityEngine:
    def test_probabilities_sum_to_one_hundred(self, dossier, parsed_pdf):
        text = parsed_pdf.full_text
        computations = compute_probabilities(
            list(TimeHorizon),
            score_strategic_acquisition(dossier, text),
            score_financial_acquisition(dossier, text),
            score_ipo_readiness(dossier, text),
            score_liquidity_risk(dossier, text),
            dossier,
        )
        for computation in computations:
            assert computation.probabilities.total == pytest.approx(100.0)

    def test_probabilities_use_coarse_increments(self, analysis):
        for probabilities in analysis.probabilities:
            for value in probabilities.as_dict().values():
                assert value % 5 == 0, "probabilities must avoid false precision"

    def test_no_liquidity_declines_over_longer_horizons(self, dossier, parsed_pdf):
        text = parsed_pdf.full_text
        computations = compute_probabilities(
            list(TimeHorizon),
            score_strategic_acquisition(dossier, text),
            score_financial_acquisition(dossier, text),
            score_ipo_readiness(dossier, text),
            score_liquidity_risk(dossier, text),
            dossier,
        )
        values = [c.probabilities.no_liquidity for c in computations]
        assert values == sorted(values, reverse=True)

    def test_ipo_is_damped_below_the_revenue_floor(self, analysis):
        """A $4.2M-revenue company is not an IPO candidate at any score."""
        for probabilities in analysis.probabilities:
            assert probabilities.ipo <= 5

    def test_no_liquidity_never_disappears(self, dossier, parsed_pdf):
        cfg = load_scoring_config()
        floor = cfg["probability"]["min_no_liquidity"]
        text = parsed_pdf.full_text
        computations = compute_probabilities(
            list(TimeHorizon),
            score_strategic_acquisition(dossier, text),
            score_financial_acquisition(dossier, text),
            score_ipo_readiness(dossier, text),
            score_liquidity_risk(dossier, text),
            dossier,
        )
        assert all(c.probabilities.no_liquidity >= floor for c in computations)

    def test_no_liquidity_floor_binds_for_a_flawless_company(self, dossier):
        """The floor must actually hold, not merely never be reached.

        A company scoring perfectly on every exit route with zero liquidity risk
        would otherwise be shown as a certainty. Venture outcomes are power-law
        distributed and the possibility of no exit is never zero.
        """
        from venture_exit_analyzer.models.analysis import DimensionScore

        cfg = load_scoring_config()
        floor = float(cfg["probability"]["min_no_liquidity"])
        perfect = DimensionScore(dimension="d", score=100.0, drivers=[])
        spotless = DimensionScore(dimension="d", score=0.0, drivers=[])

        flawless = dossier.model_copy(deep=True)
        flawless.company.stage = Evidenced.from_user(Stage.GROWTH)
        flawless.financials.arr = Evidenced.from_user(500_000_000.0)

        computations = compute_probabilities(
            list(TimeHorizon), perfect, perfect, perfect, spotless, flawless
        )
        for computation in computations:
            assert computation.probabilities.no_liquidity >= floor
            assert computation.probabilities.total == pytest.approx(100.0)

    def test_no_liquidity_ceiling_binds_for_a_hopeless_company(self, dossier):
        """The ceiling keeps the model from asserting certain failure."""
        from venture_exit_analyzer.models.analysis import DimensionScore

        cfg = load_scoring_config()
        ceiling = float(cfg["probability"]["max_no_liquidity"])
        hopeless = DimensionScore(dimension="d", score=0.0, drivers=[])
        maximal_risk = DimensionScore(dimension="d", score=100.0, drivers=[])

        computations = compute_probabilities(
            list(TimeHorizon), hopeless, hopeless, hopeless, maximal_risk, dossier
        )
        for computation in computations:
            assert computation.probabilities.no_liquidity <= ceiling
            assert computation.probabilities.total == pytest.approx(100.0)

    def test_earlier_stage_raises_no_liquidity(self, dossier, parsed_pdf):
        text = parsed_pdf.full_text
        early = dossier.model_copy(deep=True)
        early.company.stage = Evidenced.from_user(Stage.PRE_SEED)

        def no_liquidity(target):
            return compute_probabilities(
                [TimeHorizon.Y3_5],
                score_strategic_acquisition(target, text),
                score_financial_acquisition(target, text),
                score_ipo_readiness(target, text),
                score_liquidity_risk(target, text),
                target,
            )[0].probabilities.no_liquidity

        assert no_liquidity(early) > no_liquidity(dossier)

    def test_traces_explain_every_weight(self, dossier, parsed_pdf):
        text = parsed_pdf.full_text
        computation = compute_probabilities(
            [TimeHorizon.Y3_5],
            score_strategic_acquisition(dossier, text),
            score_financial_acquisition(dossier, text),
            score_ipo_readiness(dossier, text),
            score_liquidity_risk(dossier, text),
            dossier,
        )[0]
        assert len(computation.traces) == 4
        assert all(t.explain() for t in computation.traces)

    def test_primary_scenario_read_from_terminal_horizon(self, dossier, parsed_pdf):
        text = parsed_pdf.full_text
        computations = compute_probabilities(
            [TimeHorizon.Y1_3, TimeHorizon.Y5_7],
            score_strategic_acquisition(dossier, text),
            score_financial_acquisition(dossier, text),
            score_ipo_readiness(dossier, text),
            score_liquidity_risk(dossier, text),
            dossier,
        )
        _, horizon = select_primary_scenario(computations)
        assert horizon == TimeHorizon.Y5_7

    def test_no_horizons_is_an_error(self, dossier):
        with pytest.raises(ValueError):
            compute_probabilities([], *([None] * 4), dossier)

    def test_ties_resolve_to_no_liquidity(self):
        from venture_exit_analyzer.models.analysis import HorizonProbabilities

        tied = HorizonProbabilities(
            horizon=TimeHorizon.Y5_7,
            strategic_acquisition=45,
            financial_acquisition=5,
            ipo=5,
            no_liquidity=45,
        )
        assert tied.most_likely == Scenario.NO_LIQUIDITY


class TestValuation:
    def test_range_derived_from_supplied_comparables(self, dossier):
        strategic = compute_valuation_ranges(dossier)[0]
        assert strategic.is_defensible
        assert strategic.low < strategic.high
        assert "comparable" in strategic.basis

    def test_no_comparables_means_no_range(self, dossier):
        from venture_exit_analyzer.models.comparables import ComparableSet

        bare = dossier.model_copy(deep=True)
        bare.comparables = ComparableSet()
        for valuation in compute_valuation_ranges(bare):
            assert not valuation.is_defensible
            assert valuation.insufficient_reason

    def test_no_revenue_means_no_range(self, empty_dossier):
        for valuation in compute_valuation_ranges(empty_dossier):
            assert not valuation.is_defensible
            assert "revenue" in valuation.insufficient_reason

    def test_size_discount_applied_to_small_companies(self, dossier):
        strategic = compute_valuation_ranges(dossier)[0]
        assert "size discount" in strategic.basis


class TestCapitalRequirements:
    def test_estimates_from_burn_and_target_scale(self, dossier):
        capital = estimate_capital_to_exit(dossier, Scenario.STRATEGIC)
        assert capital.is_estimable
        assert capital.additional_capital_low < capital.additional_capital_high
        assert capital.assumptions

    def test_includes_time_for_the_exit_process_itself(self, dossier):
        """Reaching exit-readiness revenue is not the same as being paid."""
        capital = estimate_capital_to_exit(dossier, Scenario.STRATEGIC)
        assert capital.additional_capital_low > 0
        assert any("exit process" in a for a in capital.assumptions)

    def test_growth_is_capped_for_projection(self, dossier):
        capital = estimate_capital_to_exit(dossier, Scenario.STRATEGIC)
        assert any("capped" in a for a in capital.assumptions)

    def test_not_estimable_without_burn(self, empty_dossier):
        capital = estimate_capital_to_exit(empty_dossier)
        assert not capital.is_estimable
        assert capital.additional_capital_low is None

    def test_flat_company_below_target_is_not_estimable(self, dossier):
        stalled = dossier.model_copy(deep=True)
        stalled.financials.revenue_growth_rate = Evidenced.from_user(0.0)
        capital = estimate_capital_to_exit(stalled, Scenario.STRATEGIC)
        assert not capital.is_estimable
        assert any("not reachable" in a for a in capital.assumptions)


class TestConfidence:
    def test_full_disclosure_scores_higher_than_empty(self, dossier, empty_dossier, parsed_pdf):
        text = parsed_pdf.full_text

        def confidence_for(target, deck_text):
            return assess_confidence(
                target,
                [
                    score_strategic_acquisition(target, deck_text),
                    score_financial_acquisition(target, deck_text),
                    score_ipo_readiness(target, deck_text),
                    score_liquidity_risk(target, deck_text),
                ],
            ).score

        assert confidence_for(dossier, text) > confidence_for(empty_dossier, "") + 30

    def test_empty_dossier_is_low_confidence(self, empty_dossier):
        assessment = assess_confidence(
            empty_dossier, [score_strategic_acquisition(empty_dossier, "")]
        )
        assert assessment.band == "Low Confidence"
        assert assessment.rationale

    def test_confidence_is_bounded(self, dossier, parsed_pdf):
        assessment = assess_confidence(
            dossier, [score_strategic_acquisition(dossier, parsed_pdf.full_text)]
        )
        assert 0.0 <= assessment.score <= 100.0

    def test_confidence_is_independent_of_probability(self, analysis):
        """A high probability must not imply a high confidence, or vice versa."""
        assert analysis.confidence.score != max(
            p.strategic_acquisition for p in analysis.probabilities
        )


class TestEngine:
    def test_produces_a_complete_analysis(self, analysis):
        assert analysis.probabilities
        assert analysis.milestones
        assert analysis.acquirer_profiles
        assert analysis.assumptions
        assert analysis.limitations
        assert analysis.exit_profile_summary

    def test_never_invents_named_acquirers(self, analysis):
        """Named buyers must come from the user, never from the application."""
        assert all(not profile.is_named_buyer for profile in analysis.acquirer_profiles)

    def test_user_supplied_buyers_are_scored(self, dossier, parsed_pdf):
        dossier.named_buyers = ["Roche Diagnostics"]
        result = run_analysis(dossier, [TimeHorizon.Y3_5], parsed_pdf.full_text)
        named = [p for p in result.analysis.acquirer_profiles if p.is_named_buyer]
        assert len(named) == 1
        assert named[0].category == "Roche Diagnostics"

    def test_empty_dossier_still_produces_a_usable_analysis(self, empty_dossier):
        result = run_analysis(empty_dossier, [TimeHorizon.Y3_5])
        assert result.analysis.probabilities[0].total == pytest.approx(100.0)
        assert result.analysis.confidence.band == "Low Confidence"

    def test_milestones_ranked_by_impact(self, analysis):
        weights = [m.impact_weight for m in analysis.milestones]
        assert weights == sorted(weights, reverse=True)

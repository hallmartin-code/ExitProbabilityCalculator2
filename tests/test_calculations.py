"""Deterministic calculation and validation utilities."""

from __future__ import annotations

import math

import pytest

from venture_exit_analyzer.utils.calculations import (
    CalculationError,
    apply_floor_and_renormalize,
    cagr,
    capital_efficiency,
    compound_dilution,
    ebitda_multiple,
    growth_rate_from_history,
    normalize_to_percent,
    percentile,
    revenue_multiple,
    round_to_increment,
    runway_months,
    years_to_target_revenue,
)
from venture_exit_analyzer.utils.formatting import money, money_range, months, percent, truncate
from venture_exit_analyzer.utils.validation import (
    InputValidationError,
    load_comparables_csv,
    load_comparables_json,
    parse_money,
    parse_percent,
)


class TestGrowth:
    def test_cagr(self):
        assert cagr(1.0, 4.0, 2) == pytest.approx(1.0)
        assert cagr(100, 121, 2) == pytest.approx(0.10)

    def test_cagr_from_zero_is_undefined_not_infinite(self):
        assert cagr(0, 5, 2) is None
        assert cagr(-1, 5, 2) is None

    def test_cagr_requires_positive_period(self):
        with pytest.raises(CalculationError):
            cagr(1, 2, 0)

    def test_growth_from_history_uses_endpoints(self):
        assert growth_rate_from_history([(2022, 1e6), (2024, 4e6)]) == pytest.approx(1.0)

    def test_growth_from_single_point_is_none(self):
        assert growth_rate_from_history([(2024, 1e6)]) is None


class TestMultiples:
    def test_revenue_multiple(self):
        assert revenue_multiple(200e6, 25e6) == pytest.approx(8.0)

    def test_revenue_multiple_undefined_without_revenue(self):
        assert revenue_multiple(200e6, 0) is None
        assert revenue_multiple(200e6, None) is None

    def test_ebitda_multiple_undefined_for_negative_ebitda(self):
        """A multiple on negative EBITDA is meaningless, not negative."""
        assert ebitda_multiple(200e6, -1e6) is None
        assert ebitda_multiple(200e6, 0) is None
        assert ebitda_multiple(200e6, 5e6) == pytest.approx(40.0)

    def test_capital_efficiency(self):
        assert capital_efficiency(4.2e6, 11.5e6) == pytest.approx(0.365, abs=1e-3)
        assert capital_efficiency(4.2e6, 0) is None


class TestRunwayAndCapital:
    def test_runway(self):
        assert runway_months(1.2e6, 100_000) == pytest.approx(12.0)

    def test_no_burn_means_infinite_runway(self):
        assert math.isinf(runway_months(1e6, 0))

    def test_dilution_compounds_multiplicatively(self):
        """Two 20% rounds give up 36%, not 40%."""
        assert compound_dilution(2, 0.20) == pytest.approx(0.36)
        assert compound_dilution(0, 0.20) == 0.0

    def test_dilution_rejects_impossible_inputs(self):
        with pytest.raises(CalculationError):
            compound_dilution(-1, 0.2)
        with pytest.raises(CalculationError):
            compound_dilution(2, 1.5)

    def test_years_to_target(self):
        assert years_to_target_revenue(2e6, 10e6, 1.0, 3.5) == pytest.approx(2.32, abs=0.01)

    def test_already_at_target_needs_no_time(self):
        assert years_to_target_revenue(20e6, 10e6, 0.5, 3.5) == 0.0

    def test_no_growth_never_reaches_a_higher_target(self):
        assert years_to_target_revenue(2e6, 10e6, 0.0, 3.5) is None

    def test_unknown_growth_falls_back_to_default(self):
        assert years_to_target_revenue(2e6, 10e6, None, 3.5) == 3.5


class TestPercentiles:
    def test_interpolated_percentiles(self):
        values = [2, 4, 6, 8, 10]
        assert percentile(values, 25) == pytest.approx(4.0)
        assert percentile(values, 50) == pytest.approx(6.0)
        assert percentile(values, 75) == pytest.approx(8.0)

    def test_single_value(self):
        assert percentile([7.0], 25) == 7.0

    def test_empty_is_none(self):
        assert percentile([], 50) is None


class TestProbabilityNormalisation:
    def test_normalises_to_one_hundred(self):
        result = normalize_to_percent({"a": 1, "b": 3})
        assert sum(result.values()) == pytest.approx(100.0)
        assert result["b"] == pytest.approx(75.0)

    def test_all_zero_weights_fall_back_to_uniform(self):
        result = normalize_to_percent({"a": 0, "b": 0, "c": 0, "d": 0})
        assert all(v == pytest.approx(25.0) for v in result.values())

    def test_negative_weights_rejected(self):
        with pytest.raises(CalculationError):
            normalize_to_percent({"a": -1, "b": 2})

    def test_rounding_preserves_total_and_increment(self):
        rounded = round_to_increment({"a": 37.2, "b": 22.9, "c": 4.1, "d": 35.8}, 5)
        assert sum(rounded.values()) == pytest.approx(100.0)
        assert all(v % 5 == 0 for v in rounded.values())

    @pytest.mark.parametrize("seed", range(25))
    def test_rounding_invariants_hold_for_arbitrary_inputs(self, seed):
        import random

        rng = random.Random(seed)
        weights = {k: rng.random() for k in "abcd"}
        rounded = round_to_increment(normalize_to_percent(weights), 5)
        assert sum(rounded.values()) == pytest.approx(100.0)
        assert all(v >= 0 for v in rounded.values())

    def test_increment_must_divide_one_hundred(self):
        with pytest.raises(CalculationError):
            round_to_increment({"a": 100.0}, 7)

    def test_floor_is_applied_and_others_rebalance(self):
        result = apply_floor_and_renormalize(
            {"a": 60.0, "b": 30.0, "c": 5.0, "d": 5.0}, "d", floor=20.0
        )
        assert result["d"] == pytest.approx(20.0)
        assert sum(result.values()) == pytest.approx(100.0)

    def test_ceiling_is_applied(self):
        result = apply_floor_and_renormalize(
            {"a": 5.0, "b": 5.0, "c": 5.0, "d": 85.0}, "d", floor=10.0, ceiling=70.0
        )
        assert result["d"] == pytest.approx(70.0)
        assert sum(result.values()) == pytest.approx(100.0)


class TestFormatting:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (8.4e6, "$8.4M"),
            (250e6, "$250M"),
            (1.24e9, "$1.2B"),
            (450_000, "$450K"),
            (-2.5e6, "-$2.5M"),
            (None, "Not disclosed"),
        ],
    )
    def test_money(self, value, expected):
        assert money(value) == expected

    def test_money_range_collapses_when_equal(self):
        assert money_range(10e6, 10e6) == "$10.0M"
        assert money_range(150e6, 250e6) == "$150M-$250M"

    def test_percent(self):
        assert percent(0.185) == "18%"
        assert percent(42.0, already_percent=True) == "42%"
        assert percent(None) == "Not disclosed"

    def test_infinite_runway_reads_as_cash_positive(self):
        assert months(float("inf")) == "Cash-flow positive"

    def test_truncate_breaks_on_word_boundary(self):
        assert truncate("The company has a strong position", 20).endswith("...")
        assert len(truncate("The company has a strong position", 20)) <= 20


class TestInputParsing:
    @pytest.mark.parametrize(
        "raw,expected",
        [("$1.2B", 1.2e9), ("450k", 450_000), ("1,200,000", 1.2e6), ("undisclosed", None), ("", None)],
    )
    def test_parse_money(self, raw, expected):
        assert parse_money(raw) == (pytest.approx(expected) if expected else expected)

    @pytest.mark.parametrize("raw,expected", [("45%", 0.45), (0.45, 0.45), (45, 0.45), ("n/a", None)])
    def test_parse_percent(self, raw, expected):
        assert parse_percent(raw) == (pytest.approx(expected) if expected else expected)

    def test_csv_load_with_aliased_headers(self, comparables_csv_path):
        comparables, warnings = load_comparables_csv(comparables_csv_path.read_bytes())
        assert len(comparables) == 5
        assert comparables.comparables[0].revenue_multiple == pytest.approx(8.0)

    def test_csv_without_company_column_is_rejected(self):
        with pytest.raises(InputValidationError) as exc:
            load_comparables_csv("value,revenue\n100,10\n")
        assert "company" in str(exc.value).lower()

    def test_bad_rows_are_reported_not_dropped_silently(self):
        _, warnings = load_comparables_csv(
            "Company,Value,Revenue\nAcme,100M,10M\n,50M,5M\n"
        )
        assert any("skipped" in w for w in warnings)

    def test_invalid_json_reports_position(self):
        with pytest.raises(InputValidationError) as exc:
            load_comparables_json("{not json")
        assert "line" in str(exc.value).lower()

    def test_json_accepts_wrapped_or_bare_list(self):
        wrapped, _ = load_comparables_json(
            '{"comparables":[{"company":"Gamma","value":"1.2B","revenue":"200M"}]}'
        )
        bare, _ = load_comparables_json('[{"company":"Gamma","value":"1.2B","revenue":"200M"}]')
        assert len(wrapped) == len(bare) == 1
        assert wrapped.comparables[0].revenue_multiple == pytest.approx(6.0)

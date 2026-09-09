"""Claude integration.

Every test here uses a fake transport. The suite must never make a network call:
it has to run in CI without a key, and a test whose result depends on a live
model is not a test.

The properties under test are the safety ones -- that the model can only fill
gaps, that implausible values are refused, and that generated prose cannot
introduce a figure the analysis did not compute.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

import pytest

from venture_exit_analyzer.analysis.engine import run_analysis
from venture_exit_analyzer.analysis.narrative import build_narrative_facts
from venture_exit_analyzer.models.analysis import Scenario, TimeHorizon
from venture_exit_analyzer.models.evidence import Evidenced, EvidenceClass
from venture_exit_analyzer.services.claude_service import (
    ClaudeConfig,
    ClaudeService,
    ClaudeUnavailable,
    _unsupported_figures,
    build_service,
)
from venture_exit_analyzer.utils.env import env_bool, env_int, load_dotenv, redact


# ---------------------------------------------------------------------------
# Fake transport
# ---------------------------------------------------------------------------
@dataclass
class _Usage:
    input_tokens: int = 100
    output_tokens: int = 50


@dataclass
class _ToolBlock:
    input: Dict[str, Any]
    type: str = "tool_use"


@dataclass
class _Response:
    content: List[_ToolBlock]
    usage: _Usage


class FakeMessages:
    def __init__(self, payload: Dict[str, Any]):
        self.payload = payload
        self.calls: List[Dict[str, Any]] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _Response([_ToolBlock(input=self.payload)], _Usage())


class FakeClient:
    def __init__(self, payload: Dict[str, Any]):
        self.messages = FakeMessages(payload)


def service_returning(payload: Dict[str, Any]) -> ClaudeService:
    config = ClaudeConfig(api_key="test-key", model="test-model", enabled=True)
    return ClaudeService(config=config, client=FakeClient(payload))


def fact(field: str, **kwargs) -> Dict[str, Any]:
    base = {"field": field, "source_page": 3, "quote": "quoted from deck", "confidence": 0.7}
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# Configuration and availability
# ---------------------------------------------------------------------------
class TestConfiguration:
    def test_no_key_means_unavailable_not_broken(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        config = ClaudeConfig(api_key=None)
        service = ClaudeService(config=config)
        assert not service.available
        assert "No API key" in service.status

    def test_explicitly_disabled_is_unavailable_even_with_a_key(self):
        service = ClaudeService(config=ClaudeConfig(api_key="k", enabled=False))
        assert not service.available
        assert "switched off" in service.status

    def test_operations_raise_clearly_when_unavailable(self, parsed_pdf, empty_dossier):
        service = ClaudeService(config=ClaudeConfig(api_key=None))
        with pytest.raises(ClaudeUnavailable):
            service.extract(parsed_pdf, empty_dossier)
        with pytest.raises(ClaudeUnavailable):
            service.narrate({}, [])

    def test_key_is_never_exposed_in_status(self):
        service = ClaudeService(
            config=ClaudeConfig(api_key="sk-ant-supersecretvalue123456"), client=object()
        )
        assert "supersecretvalue" not in service.status

    def test_redact_keeps_secrets_unusable(self):
        assert redact("sk-ant-abcdefghijklmnop") == "sk-ant...mnop"
        assert redact(None) == "(not set)"

    def test_env_helpers_are_tolerant(self, monkeypatch):
        monkeypatch.setenv("X_FLAG", "yes")
        monkeypatch.setenv("X_NUM", "not-a-number")
        assert env_bool("X_FLAG") is True
        assert env_bool("X_ABSENT", True) is True
        assert env_int("X_NUM", 7) == 7

    def test_dotenv_does_not_override_real_environment(self, tmp_path, monkeypatch):
        """Railway supplies real env vars; a stray .env must not win."""
        env_file = tmp_path / ".env"
        env_file.write_text("ANTHROPIC_API_KEY=from-file\n", encoding="utf-8")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "from-environment")
        load_dotenv(env_file)
        import os

        assert os.environ["ANTHROPIC_API_KEY"] == "from-environment"

    def test_missing_dotenv_is_not_an_error(self, tmp_path):
        assert load_dotenv(tmp_path / "nope.env") == 0

    def test_build_service_never_raises(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("CLAUDE_ENABLED", "false")
        assert build_service() is not None


# ---------------------------------------------------------------------------
# Extraction safety
# ---------------------------------------------------------------------------
class TestExtractionFillsGapsOnly:
    def test_fills_a_missing_field(self, parsed_pdf, dossier):
        assert not dossier.company.headquarters.is_present
        service = service_returning(
            {"facts": [fact("company.headquarters", value_text="San Diego, CA")]}
        )
        outcome = service.extract(parsed_pdf, dossier)
        assert "company.headquarters" in outcome.applied
        assert dossier.company.headquarters.value == "San Diego, CA"

    def test_never_overwrites_a_deterministic_value(self, parsed_pdf, dossier):
        """A confident hallucination must not displace a figure read off the page."""
        original = dossier.financials.arr.value
        assert original == pytest.approx(4_200_000)
        service = service_returning(
            {"facts": [fact("financials.arr", value_numeric=99_000_000)]}
        )
        outcome = service.extract(parsed_pdf, dossier)
        assert dossier.financials.arr.value == pytest.approx(original)
        assert "financials.arr" in outcome.skipped_existing
        assert "financials.arr" not in outcome.applied

    def test_extracted_values_are_marked_inferred_not_explicit(self, parsed_pdf, dossier):
        service = service_returning(
            {"facts": [fact("company.headquarters", value_text="Austin, TX")]}
        )
        service.extract(parsed_pdf, dossier)
        assert dossier.company.headquarters.evidence_class == EvidenceClass.INFERRED

    def test_extracted_values_carry_page_and_quote(self, parsed_pdf, dossier):
        service = service_returning(
            {
                "facts": [
                    fact("company.headquarters", value_text="Austin, TX",
                         source_page=1, quote="Austin, TX | Founded 2021")
                ]
            }
        )
        service.extract(parsed_pdf, dossier)
        field_value = dossier.company.headquarters
        assert field_value.source_page_or_slide == 1
        assert "Austin, TX | Founded 2021" in field_value.source

    def test_ai_confidence_is_capped_below_deterministic(self, parsed_pdf, dossier):
        service = service_returning(
            {"facts": [fact("company.headquarters", value_text="Austin", confidence=0.99)]}
        )
        service.extract(parsed_pdf, dossier)
        assert dossier.company.headquarters.confidence <= 0.75

    def test_only_missing_fields_are_offered_to_the_model(self, parsed_pdf, dossier):
        service = service_returning({"facts": []})
        service.extract(parsed_pdf, dossier)
        tool = service._client.messages.calls[0]["tools"][0]
        offered = tool["input_schema"]["properties"]["facts"]["items"]["properties"]["field"]["enum"]
        assert "financials.arr" not in offered, "already-extracted field was offered"
        assert "company.headquarters" in offered

    def test_prompt_shows_already_captured_facts(self, parsed_pdf, dossier):
        """Context that stops the model re-labelling a known fact into a free slot."""
        service = service_returning({"facts": []})
        service.extract(parsed_pdf, dossier)
        prompt = service._client.messages.calls[0]["messages"][0]["content"]
        assert "ALREADY CAPTURED" in prompt
        assert "financials.arr" in prompt.split("STILL UNKNOWN")[0]


class TestExtractionRejectsBadValues:
    def test_unknown_field_names_are_ignored(self, parsed_pdf, dossier):
        service = service_returning(
            {"facts": [fact("financials.__evil__", value_numeric=1)]}
        )
        outcome = service.extract(parsed_pdf, dossier)
        assert not outcome.applied
        assert any("not a recognised field" in r for r in outcome.rejected)

    def test_out_of_range_values_are_refused(self, parsed_pdf, dossier):
        """A misplaced decimal is the most damaging extraction error."""
        service = service_returning(
            {"facts": [fact("ownership.founder_ownership", value_numeric=45.0)]}
        )
        outcome = service.extract(parsed_pdf, dossier)
        assert not dossier.ownership.founder_ownership.is_present
        assert any("above plausible maximum" in r for r in outcome.rejected)

    def test_wrong_value_type_is_refused(self, parsed_pdf, dossier):
        service = service_returning(
            {"facts": [fact("ownership.founder_ownership", value_text="a lot")]}
        )
        outcome = service.extract(parsed_pdf, dossier)
        assert not dossier.ownership.founder_ownership.is_present
        assert any("not numeric" in r for r in outcome.rejected)

    def test_empty_text_is_refused(self, parsed_pdf, dossier):
        service = service_returning(
            {"facts": [fact("company.headquarters", value_text="   ")]}
        )
        outcome = service.extract(parsed_pdf, dossier)
        assert not dossier.company.headquarters.is_present
        assert any("empty text" in r for r in outcome.rejected)

    def test_negative_margin_sign_is_preserved(self, parsed_pdf, empty_dossier):
        service = service_returning(
            {"facts": [fact("financials.ebitda_margin", value_numeric=-0.45)]}
        )
        service.extract(parsed_pdf, empty_dossier)
        assert empty_dossier.financials.ebitda_margin.value == pytest.approx(-0.45)

    def test_empty_response_is_handled(self, parsed_pdf, dossier):
        service = service_returning({})
        outcome = service.extract(parsed_pdf, dossier)
        assert outcome.applied == []


# ---------------------------------------------------------------------------
# Narrative guardrail
# ---------------------------------------------------------------------------
class TestNarrativeGuardrail:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("A strategic acquisition at 55% is most plausible.", []),
            ("A strategic acquisition at 73% is most plausible.", ["73%"]),
            ("Indicative value of $310M on exit.", ["$310M"]),
            ("Trading at 12.5x revenue.", ["12.5X"]),
            ("Revenue of $4.2M and 55% probability.", []),
        ],
    )
    def test_detects_figures_absent_from_the_analysis(self, text, expected):
        assert _unsupported_figures(text, ["55%", "$4.2M", "20%"]) == expected

    def test_accepts_a_narrative_using_only_computed_figures(self):
        service = service_returning(
            {"narrative": "A strategic acquisition at 55% within 5-7 years is most plausible."}
        )
        outcome = service.narrate({}, ["55%"])
        assert outcome.accepted
        assert outcome.rejection_reason is None

    def test_rejects_a_narrative_that_invents_a_figure(self):
        service = service_returning(
            {"narrative": "A strategic acquisition at 88% is near certain."}
        )
        outcome = service.narrate({}, ["55%"])
        assert not outcome.accepted
        assert "88%" in outcome.rejection_reason

    def test_rejects_an_overlong_narrative(self):
        service = service_returning({"narrative": "word " * 400})
        outcome = service.narrate({}, [], char_limit=620)
        assert not outcome.accepted
        assert "characters" in outcome.rejection_reason

    def test_rejects_an_empty_narrative(self):
        service = service_returning({"narrative": "   "})
        outcome = service.narrate({}, [])
        assert not outcome.accepted


class TestNarrativeFacts:
    def test_allow_list_covers_every_computed_probability(self, dossier, analysis):
        _, allowed = build_narrative_facts(
            dossier, analysis, Scenario.STRATEGIC, TimeHorizon.Y5_7, 55.0
        )
        for probabilities in analysis.probabilities:
            for value in probabilities.as_dict().values():
                assert f"{value:.0f}%" in allowed

    def test_facts_state_when_no_liquidity_leads(self, empty_dossier):
        result = run_analysis(empty_dossier, [TimeHorizon.Y1_3])
        facts, _ = build_narrative_facts(
            empty_dossier, result.analysis, Scenario.STRATEGIC, TimeHorizon.Y1_3, 25.0
        )
        assert facts["no_liquidity_is_the_single_most_likely_outcome"] is True


# ---------------------------------------------------------------------------
# Engine integration
# ---------------------------------------------------------------------------
class TestEngineIntegration:
    def test_analysis_is_identical_with_and_without_ai(self, dossier, parsed_pdf):
        """AI touches prose only. Every number must be unchanged."""
        text = parsed_pdf.full_text
        service = service_returning({"narrative": "Strategic acquisition leads at 55%."})

        without = run_analysis(dossier, [TimeHorizon.Y3_5, TimeHorizon.Y5_7], text).analysis
        with_ai = run_analysis(
            dossier, [TimeHorizon.Y3_5, TimeHorizon.Y5_7], text, claude_service=service
        ).analysis

        assert with_ai.strategic_score.score == without.strategic_score.score
        assert with_ai.confidence.score == without.confidence.score
        assert [p.as_dict() for p in with_ai.probabilities] == [
            p.as_dict() for p in without.probabilities
        ]
        assert (
            with_ai.capital_requirement.additional_capital_low
            == without.capital_requirement.additional_capital_low
        )

    def test_rejected_narrative_leaves_deterministic_text_in_place(self, dossier, parsed_pdf):
        text = parsed_pdf.full_text
        deterministic = run_analysis(
            dossier, [TimeHorizon.Y3_5], text
        ).analysis.exit_profile_summary

        service = service_returning({"narrative": "Certain exit at 99% for $900M."})
        result = run_analysis(dossier, [TimeHorizon.Y3_5], text, claude_service=service)

        assert result.analysis.exit_profile_summary == deterministic
        assert not result.narrative_outcome.accepted

    def test_api_failure_does_not_break_the_analysis(self, dossier, parsed_pdf):
        class ExplodingClient:
            class messages:
                @staticmethod
                def create(**kwargs):
                    raise RuntimeError("connection reset")

        service = ClaudeService(
            config=ClaudeConfig(api_key="k"), client=ExplodingClient()
        )
        result = run_analysis(
            dossier, [TimeHorizon.Y3_5], parsed_pdf.full_text, claude_service=service
        )
        assert result.analysis.exit_profile_summary
        assert result.narrative_outcome is None

    def test_no_service_is_the_default_and_works(self, dossier, parsed_pdf):
        result = run_analysis(dossier, [TimeHorizon.Y3_5], parsed_pdf.full_text)
        assert result.narrative_outcome is None
        assert result.analysis.exit_profile_summary

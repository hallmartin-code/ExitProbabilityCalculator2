"""Shared fixtures.

The sample decks are generated on demand so a fresh checkout can run the suite
without a separate build step.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"

from venture_exit_analyzer.analysis.engine import run_analysis  # noqa: E402
from venture_exit_analyzer.ingestion.normalization import normalize_document  # noqa: E402
from venture_exit_analyzer.ingestion.ppt_parser import parse_document  # noqa: E402
from venture_exit_analyzer.models.analysis import DealDossier, TimeHorizon  # noqa: E402
from venture_exit_analyzer.utils.validation import load_comparables_csv  # noqa: E402


def _ensure_fixtures() -> None:
    """Build the sample decks if they are not already present."""
    required = [
        "sample_deck.pdf",
        "sample_deck.pptx",
        "image_only.pdf",
        "corrupt.pdf",
        "legacy.ppt",
        "comparables.csv",
    ]
    if all((FIXTURE_DIR / name).exists() for name in required):
        return
    sys.path.insert(0, str(FIXTURE_DIR))
    import make_fixtures

    make_fixtures.main()


@pytest.fixture(scope="session", autouse=True)
def fixtures_built() -> None:
    _ensure_fixtures()


@pytest.fixture(scope="session")
def sample_pdf_path() -> Path:
    return FIXTURE_DIR / "sample_deck.pdf"


@pytest.fixture(scope="session")
def sample_pptx_path() -> Path:
    return FIXTURE_DIR / "sample_deck.pptx"


@pytest.fixture(scope="session")
def comparables_csv_path() -> Path:
    return FIXTURE_DIR / "comparables.csv"


@pytest.fixture(scope="session")
def parsed_pdf(sample_pdf_path):
    return parse_document(sample_pdf_path)


@pytest.fixture
def dossier(parsed_pdf, comparables_csv_path) -> DealDossier:
    """A fully populated dossier from the sample deck plus comparables."""
    built = normalize_document(parsed_pdf)
    built.comparables, _ = load_comparables_csv(comparables_csv_path.read_bytes())
    return built


@pytest.fixture
def analysis(dossier, parsed_pdf):
    return run_analysis(
        dossier, [TimeHorizon.Y3_5, TimeHorizon.Y5_7], parsed_pdf.full_text
    ).analysis


@pytest.fixture
def empty_dossier() -> DealDossier:
    return DealDossier()

"""Configuration loading and validation.

Scoring weights and report styling are data, not code. This module loads them,
validates the invariants the analysis layer depends on, and caches the result.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

CONFIG_DIR = Path(__file__).resolve().parent
SCORING_PATH = CONFIG_DIR / "scoring_weights.yaml"
REPORT_PATH = CONFIG_DIR / "report_config.yaml"

#: Tolerance for the "factor weights sum to 1.0" invariant.
WEIGHT_SUM_TOLERANCE = 1e-6


class ConfigError(ValueError):
    """Raised when a configuration file is missing, malformed, or inconsistent."""


def _read_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Configuration file not found: {path}")
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Could not parse {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} must contain a top-level mapping.")
    return data


def validate_scoring_config(cfg: Dict[str, Any]) -> None:
    """Check the invariants the scoring engine relies on.

    A silently mis-weighted dimension would produce scores that look plausible but
    mean nothing, so this fails loudly at startup rather than at report time.
    """
    dims = cfg.get("dimensions")
    if not isinstance(dims, dict) or not dims:
        raise ConfigError("scoring_weights.yaml: 'dimensions' section is missing or empty.")

    for name, spec in dims.items():
        factors = (spec or {}).get("factors")
        if not isinstance(factors, dict) or not factors:
            raise ConfigError(f"Dimension '{name}' has no factors.")
        total = sum(float(v) for v in factors.values())
        if abs(total - 1.0) > WEIGHT_SUM_TOLERANCE:
            raise ConfigError(
                f"Dimension '{name}' factor weights sum to {total:.6f}, expected 1.0. "
                f"Adjust the weights in scoring_weights.yaml."
            )
        for factor, weight in factors.items():
            if not 0.0 <= float(weight) <= 1.0:
                raise ConfigError(
                    f"Dimension '{name}' factor '{factor}' has weight {weight}, "
                    f"which is outside the range 0-1."
                )

    prob = cfg.get("probability", {})
    increment = prob.get("rounding_increment", 5)
    if increment <= 0 or 100 % increment != 0:
        raise ConfigError(
            f"probability.rounding_increment must divide 100 evenly; got {increment}."
        )

    for band_name, band in (cfg.get("bands") or {}).items():
        if not isinstance(band, list) or not band:
            raise ConfigError(f"Band '{band_name}' must be a non-empty list.")
        mins = [float(entry["min"]) for entry in band]
        if mins != sorted(mins, reverse=True):
            raise ConfigError(
                f"Band '{band_name}' must be ordered from highest 'min' to lowest."
            )


@lru_cache(maxsize=1)
def load_scoring_config(path: Optional[str] = None) -> Dict[str, Any]:
    cfg = _read_yaml(Path(path) if path else SCORING_PATH)
    validate_scoring_config(cfg)
    return cfg


@lru_cache(maxsize=1)
def load_report_config(path: Optional[str] = None) -> Dict[str, Any]:
    return _read_yaml(Path(path) if path else REPORT_PATH)


def clear_config_cache() -> None:
    """Drop cached configuration. Used by tests and by the UI's reload control."""
    load_scoring_config.cache_clear()
    load_report_config.cache_clear()


def factor_weights(dimension: str, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, float]:
    """Factor -> weight mapping for one dimension."""
    cfg = cfg or load_scoring_config()
    try:
        return {k: float(v) for k, v in cfg["dimensions"][dimension]["factors"].items()}
    except KeyError as exc:
        raise ConfigError(f"Unknown scoring dimension: {dimension}") from exc


def score_from_band(
    value: Optional[float], band_name: str, cfg: Optional[Dict[str, Any]] = None
) -> Optional[float]:
    """Map a measured value onto its 0-10 band score.

    Returns None when the value is absent, so callers can distinguish "no data"
    from "scored zero" -- the two must never be conflated.
    """
    if value is None:
        return None
    cfg = cfg or load_scoring_config()
    bands: List[Dict[str, float]] = (cfg.get("bands") or {}).get(band_name)
    if not bands:
        raise ConfigError(f"Unknown scoring band: {band_name}")
    for entry in bands:
        if value >= float(entry["min"]):
            return float(entry["score"])
    # Value sits below the lowest band floor; award the lowest score rather than
    # failing, since the bands are open-ended downwards by intent.
    return float(bands[-1]["score"])


def neutral_score(cfg: Optional[Dict[str, Any]] = None) -> float:
    cfg = cfg or load_scoring_config()
    return float(cfg.get("neutral_factor_score", 5.0))

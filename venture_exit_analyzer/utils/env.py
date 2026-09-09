"""Environment loading.

A minimal ``.env`` reader so local development does not need an extra
dependency. Real environment variables always win over the file, which is what
makes the same code work unchanged on Railway, where configuration arrives as
process environment rather than as a file.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_ENV_FILENAME = ".env"


def load_dotenv(path: Optional[str | Path] = None, override: bool = False) -> int:
    """Load KEY=VALUE pairs from a .env file into the process environment.

    Returns the number of variables set. Missing files are not an error: in a
    deployed environment there is no .env file and there should not be one.
    """
    env_path = Path(path) if path else Path(DEFAULT_ENV_FILENAME)
    if not env_path.is_file():
        return 0

    loaded = 0
    try:
        content = env_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not read %s: %s", env_path, exc)
        return 0

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        if override or key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


def env_bool(name: str, default: bool = False) -> bool:
    """Read a boolean environment variable tolerantly."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "enabled"}


def env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        return default


def redact(secret: Optional[str], keep: int = 6) -> str:
    """Render a secret safe for logs and the interface.

    Never returns enough of the value to be usable.
    """
    if not secret:
        return "(not set)"
    if len(secret) <= keep * 2:
        return "*" * len(secret)
    return f"{secret[:keep]}...{secret[-4:]}"

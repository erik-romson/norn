"""Env file loader with precedence rules.

Load order (later overrides earlier, ``os.environ.setdefault`` preserves explicit vars):
1. ``~/.norn/env`` — global defaults (API keys, default org)
2. ``.norn.env`` in CWD — project-specific overrides
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

GLOBAL_ENV_FILE = Path.home() / ".norn" / "env"
PROJECT_ENV_FILE = Path(".norn.env")


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE pairs from an env file.

    Skips blank lines and ``#`` comments. Strips optional surrounding quotes
    (single or double) from values. No shell expansion.

    Returns an empty dict if the file does not exist.
    """
    if not path.is_file():
        return {}

    result: dict[str, str] = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        # Strip matching surrounding quotes
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        result[key] = value
    return result


def apply_env_files() -> None:
    """Load global then project env files into ``os.environ``.

    Uses ``os.environ.setdefault`` so that explicit env vars always win.
    Project values override global values for the same key.
    """
    global_vars = _parse_env_file(GLOBAL_ENV_FILE)
    project_vars = _parse_env_file(PROJECT_ENV_FILE)

    # Merge: project overrides global
    merged = {**global_vars, **project_vars}

    for key, value in merged.items():
        os.environ.setdefault(key, value)
        log.debug("env setdefault %s", key)

"""CodexBar quota sampling — optional, opt-in.

``sample_quota`` shells out to ``codexbar usage --provider <p> --format json``
and returns a dict with safe fields only (no identity/account).

Raises ``QuotaProbeError`` on failure so the caller can decide whether to
abort or continue.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess

log = logging.getLogger(__name__)

# Provider name mapping: norn's provider names -> codexbar's
_PROVIDER_MAP: dict[str, str] = {
    "claude-code": "claude",
    "opencode": "opencode",
}

# Fields to keep from codexbar output (no identity/account fields)
_SAFE_FIELDS = frozenset({
    "provider",
    "remaining_pct",
    "remaining_tokens",
    "window",
    "pace",
    "limit",
    "used",
    "reset_at",
})


class QuotaProbeError(Exception):
    """Raised when quota probing fails."""


def sample_quota(provider: str) -> dict:
    """Run ``codexbar usage`` and return a dict of safe fields.

    Args:
        provider: norn provider name (e.g. ``"claude-code"``).

    Returns:
        Dict of safe quota fields.

    Raises:
        QuotaProbeError: if ``codexbar`` is not found or the command fails.
    """
    cb_provider = _PROVIDER_MAP.get(provider)
    if cb_provider is None:
        raise QuotaProbeError(
            f"No codexbar mapping for provider {provider!r}; "
            f"supported: {', '.join(sorted(_PROVIDER_MAP))}"
        )

    binary = shutil.which("codexbar")
    if binary is None:
        raise QuotaProbeError("codexbar binary not found on PATH")

    try:
        result = subprocess.run(
            [binary, "usage", "--provider", cb_provider, "--format", "json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as e:
        raise QuotaProbeError(f"codexbar timed out: {e}") from e
    except OSError as e:
        raise QuotaProbeError(f"codexbar execution failed: {e}") from e

    if result.returncode != 0:
        raise QuotaProbeError(
            f"codexbar exited {result.returncode}: {result.stderr.strip()}"
        )

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise QuotaProbeError(f"codexbar returned invalid JSON: {e}") from e

    # Keep only safe fields
    return {k: v for k, v in data.items() if k in _SAFE_FIELDS}

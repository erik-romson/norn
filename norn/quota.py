"""CodexBar quota sampling — optional, opt-in.

``sample_quota`` shells out to ``codexbar usage --provider <p> --format json``
and returns a dict with safe fields only (no identity/account).

Raises ``QuotaProbeError`` on failure so the caller can decide whether to
abort or continue.

``check_quota_guard`` is the gating half: it shells out to
``codexbar guard --min-remaining <pct> --window <session|weekly>`` before a
run and returns a ``GuardDecision`` saying whether the run may start.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass

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


# ---------------------------------------------------------------------------
# Threshold guard — ``codexbar guard``
# ---------------------------------------------------------------------------

# Exit codes returned by ``codexbar guard``
GUARD_ALLOW = 0
GUARD_BLOCK = 1
GUARD_INVALID_ARGS = 64
GUARD_UNKNOWN = 69

# Deadline handed to codexbar, kept under our own subprocess timeout so the
# guard reports "unavailable" (69) rather than being killed mid-flight.
_GUARD_TIMEOUT_S = 25

_WINDOWS = frozenset({"session", "weekly"})
_DEFAULT_WINDOW = "session"


@dataclass(frozen=True)
class GuardDecision:
    """Outcome of a ``codexbar guard`` threshold check.

    Attributes:
        allowed: Whether the run may start.
        exit_code: Raw ``codexbar guard`` exit code (``69`` = could not
            determine usage).
        threshold_pct: The percentage the guard was asked to enforce.
        window: Quota window the threshold applies to.
        message: Human-readable decision text from codexbar, or from norn
            when codexbar could not be reached at all.
    """

    allowed: bool
    exit_code: int
    threshold_pct: float
    window: str
    message: str


def parse_quota_threshold(spec: str) -> tuple[float, str]:
    """Parse a ``--require-quota`` value of the form ``<pct>[:session|weekly]``.

    Args:
        spec: Threshold spec, e.g. ``"20"`` or ``"20:weekly"``.

    Returns:
        ``(percentage, window)``; the window defaults to ``"session"``.

    Raises:
        QuotaProbeError: if the percentage or the window is malformed.
    """
    pct_text, _, window_text = spec.partition(":")
    window = window_text or _DEFAULT_WINDOW
    if window not in _WINDOWS:
        raise QuotaProbeError(
            f"Invalid quota window {window!r}; "
            f"expected one of {', '.join(sorted(_WINDOWS))}"
        )
    try:
        pct = float(pct_text)
    except ValueError as e:
        raise QuotaProbeError(
            f"Invalid quota threshold {pct_text!r}; expected a percentage"
        ) from e
    if not 0 <= pct <= 100:
        raise QuotaProbeError(f"Quota threshold {pct:g} out of range; expected 0-100")
    return pct, window


def check_quota_guard(
    provider: str,
    spec: str,
    *,
    fail_open: bool = False,
) -> GuardDecision:
    """Run ``codexbar guard`` and decide whether the run may start.

    Exit ``0`` allows the run and exit ``1`` blocks it. Exit ``69`` means
    codexbar could not determine usage; that blocks the run too unless
    ``fail_open`` is set. A missing binary, a timeout, or a failed spawn are
    treated the same way as exit ``69``. Exit ``64`` means norn built a bad
    command line and raises.

    Args:
        provider: norn provider name (e.g. ``"claude-code"``).
        spec: Threshold spec accepted by :func:`parse_quota_threshold`.
        fail_open: Start the run anyway when usage cannot be determined.

    Returns:
        The guard decision; ``allowed`` says whether to start the run.

    Raises:
        QuotaProbeError: if the spec is malformed, the provider has no
            codexbar mapping, or codexbar rejected the command line — all
            caller mistakes, not quota states.
    """
    pct, window = parse_quota_threshold(spec)

    cb_provider = _PROVIDER_MAP.get(provider)
    if cb_provider is None:
        raise QuotaProbeError(
            f"No codexbar mapping for provider {provider!r}; "
            f"supported: {', '.join(sorted(_PROVIDER_MAP))}"
        )

    binary = shutil.which("codexbar")
    if binary is None:
        return _undetermined(pct, window, "codexbar binary not found on PATH", fail_open)

    try:
        result = subprocess.run(
            [
                binary, "guard",
                "--provider", cb_provider,
                "--min-remaining", f"{pct:g}",
                "--window", window,
                "--timeout", str(_GUARD_TIMEOUT_S),
            ],
            capture_output=True,
            text=True,
            timeout=_GUARD_TIMEOUT_S + 5,
        )
    except subprocess.TimeoutExpired as e:
        return _undetermined(pct, window, f"codexbar guard timed out: {e}", fail_open)
    except OSError as e:
        return _undetermined(
            pct, window, f"codexbar guard execution failed: {e}", fail_open
        )

    message = (result.stdout or "").strip() or (result.stderr or "").strip()

    if result.returncode == GUARD_ALLOW:
        return GuardDecision(
            allowed=True,
            exit_code=GUARD_ALLOW,
            threshold_pct=pct,
            window=window,
            message=message or f"quota is above {pct:g}% ({window})",
        )

    if result.returncode == GUARD_INVALID_ARGS:
        raise QuotaProbeError(f"codexbar guard rejected its arguments: {message}")

    if result.returncode == GUARD_UNKNOWN:
        return _undetermined(
            pct,
            window,
            message or "codexbar guard could not determine quota",
            fail_open,
        )

    return GuardDecision(
        allowed=False,
        exit_code=result.returncode,
        threshold_pct=pct,
        window=window,
        message=message or f"quota is below {pct:g}% ({window})",
    )


def _undetermined(
    pct: float, window: str, message: str, fail_open: bool
) -> GuardDecision:
    """Build the decision for "codexbar could not tell us" (exit 69)."""
    return GuardDecision(
        allowed=fail_open,
        exit_code=GUARD_UNKNOWN,
        threshold_pct=pct,
        window=window,
        message=message,
    )

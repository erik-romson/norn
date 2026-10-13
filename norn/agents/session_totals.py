"""Running cost and API time per agent session, for providers that report session totals.

The Claude Code CLI persists a session's cost state and restores it on
``--resume`` (and copies it on ``--fork-session``), so ``ResultMessage``'s
``total_cost_usd`` and ``duration_api_ms`` are running totals for the whole
session, not for the call that produced them. A stage that resumes a session —
a closeout after its implement step, a revise after its draft, a retry after
``max_turns`` — would otherwise be charged everything the session spent before
it as well.

The ledger records the totals each session last reported, so the provider can
turn the next report into a per-call delta. It is process-wide because session
ids are unique and a session crosses run boundaries (an isolated include forks
its parent's session in a fresh context). ``--resume``/``--continue`` start a
new process, so the checkpoint carries the resumed session's totals and the
runner seeds them back with :func:`seed`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SessionTotals:
    """What a session had spent when it last reported.

    Attributes:
        cost_usd: Running cost of the session in USD.
        duration_api_ms: Running API time of the session in milliseconds.
    """

    cost_usd: float = 0.0
    duration_api_ms: int = 0


_ledger: dict[str, SessionTotals] = {}


def get(session_id: str | None) -> SessionTotals:
    """Return the totals *session_id* last reported, or zero totals for an unknown or absent session."""
    if not session_id:
        return SessionTotals()
    return _ledger.get(session_id, SessionTotals())


def record(session_id: str, totals: SessionTotals) -> None:
    """Store the running totals *session_id* just reported."""
    _ledger[session_id] = totals


def seed(session_id: str | None, totals: SessionTotals | None) -> None:
    """Restore a session's totals saved in a checkpoint, keeping any newer in-process value."""
    if not session_id or totals is None or session_id in _ledger:
        return
    _ledger[session_id] = totals


def clear() -> None:
    """Forget every session (test isolation)."""
    _ledger.clear()

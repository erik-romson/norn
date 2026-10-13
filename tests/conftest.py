from __future__ import annotations

import pytest

from norn.agents import session_totals
from norn.testing import reset_call_counter


@pytest.fixture(autouse=True)
def _reset_global_call_counter():
    reset_call_counter()
    yield
    reset_call_counter()


@pytest.fixture(autouse=True)
def _reset_session_totals():
    """The session-cost ledger is process-wide; keep tests from seeing each other's sessions."""
    session_totals.clear()
    yield
    session_totals.clear()

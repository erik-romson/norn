"""Tests for how ClaudeCodeProvider turns ResultMessages into one call's usage.

The CLI reports tokens, turns and wall time per round but cost and API time as
running totals for the whole session, which a resume or fork carries over.
The claude_agent_sdk is mocked entirely offline; no real Claude calls are made.
"""
from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from norn.agents import session_totals
from norn.agents.base import AgentUsage
from norn.agents.claude_code import ClaudeCodeProvider
from norn.agents.session_totals import SessionTotals
from tests.test_claude_code_blocks import _make_request, _make_sdk


class _Result:
    """Stand-in for claude_agent_sdk.ResultMessage with per-instance values."""

    structured_output = None
    subtype = "success"
    stop_reason = None
    is_error = False

    def __init__(
        self,
        *,
        session_id: str,
        total_cost_usd: float,
        duration_api_ms: int,
        duration_ms: int = 1000,
        num_turns: int = 1,
        cache_read: int = 0,
        output_tokens: int = 0,
    ) -> None:
        self.session_id = session_id
        self.total_cost_usd = total_cost_usd
        self.duration_api_ms = duration_api_ms
        self.duration_ms = duration_ms
        self.num_turns = num_turns
        self.usage = {
            "input_tokens": 1,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": 0,
        }


async def _final_usage(messages: list, **request: object) -> AgentUsage:
    """Run the provider over *messages* and return the last usage it emitted."""
    sdk, sdk_types = _make_sdk(messages)
    sdk.ResultMessage = _Result
    with patch.dict(sys.modules, {"claude_agent_sdk": sdk, "claude_agent_sdk.types": sdk_types}):
        events = [e async for e in ClaudeCodeProvider().run(_make_request(**request))]
    return [e.usage for e in events if e.usage is not None][-1]


@pytest.mark.asyncio
async def test_rounds_woken_by_background_tasks_all_count_toward_the_call():
    """Regression: a background build's notification starts a new round with its
    own ResultMessage, and only the last round's tokens and turns were kept."""
    usage = await _final_usage([
        _Result(session_id="s", total_cost_usd=6.0, duration_api_ms=500_000,
                duration_ms=900_000, num_turns=90, cache_read=17_000_000, output_tokens=130_000),
        _Result(session_id="s", total_cost_usd=8.5, duration_api_ms=600_000,
                duration_ms=60_000, num_turns=3, cache_read=850_000, output_tokens=2_000),
    ])
    assert usage.num_turns == 93
    assert usage.cache_read_input_tokens == 17_850_000
    assert usage.output_tokens == 132_000
    assert usage.input_tokens == 2
    assert usage.duration_ms == 960_000
    # Cost and API time are already running totals: the last one is the call's.
    assert usage.total_cost_usd == pytest.approx(8.5)
    assert usage.duration_api_ms == 600_000


@pytest.mark.asyncio
async def test_resumed_session_is_charged_only_for_the_new_call():
    """Regression: a closeout resuming its implement session reported the
    implement step's cost again on top of its own."""
    await _final_usage([_Result(session_id="s", total_cost_usd=0.44, duration_api_ms=84_482)])
    usage = await _final_usage(
        [_Result(session_id="s", total_cost_usd=0.47, duration_api_ms=87_164)],
        session_id="s",
    )
    assert usage.total_cost_usd == pytest.approx(0.03)
    assert usage.duration_api_ms == 2_682
    assert session_totals.get("s") == SessionTotals(cost_usd=0.47, duration_api_ms=87_164)


@pytest.mark.asyncio
async def test_fork_is_charged_from_its_parent_session_totals():
    """A forked session starts from the parent's running totals, not from zero."""
    session_totals.record("parent", SessionTotals(cost_usd=2.0, duration_api_ms=10_000))
    usage = await _final_usage(
        [_Result(session_id="child", total_cost_usd=2.25, duration_api_ms=12_000)],
        session_id="parent",
        fork_session=True,
    )
    assert usage.session_id == "child"
    assert usage.total_cost_usd == pytest.approx(0.25)
    assert usage.duration_api_ms == 2_000
    assert session_totals.get("parent") == SessionTotals(cost_usd=2.0, duration_api_ms=10_000)


@pytest.mark.asyncio
async def test_fresh_session_is_charged_its_full_reported_cost():
    usage = await _final_usage([_Result(session_id="new", total_cost_usd=1.25, duration_api_ms=4_000)])
    assert usage.total_cost_usd == pytest.approx(1.25)
    assert usage.duration_api_ms == 4_000


def test_seed_keeps_totals_already_recorded_in_this_process():
    session_totals.record("s", SessionTotals(cost_usd=3.0, duration_api_ms=30))
    session_totals.seed("s", SessionTotals(cost_usd=1.0, duration_api_ms=10))
    assert session_totals.get("s") == SessionTotals(cost_usd=3.0, duration_api_ms=30)

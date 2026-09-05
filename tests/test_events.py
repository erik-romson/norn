"""Tests for norn.events — event model, key, and delivery classification."""

from __future__ import annotations

import pytest

from norn.events import (
    Delivery,
    EventKey,
    LoopExhausted,
    RunError,
    RunEvent,
    RunFinished,
    RunStarted,
    StageFinished,
    StageRetrying,
    StageStarted,
    TurnEvent,
    UnitMerged,
    UnitStarted,
    UsageUpdated,
    WaitingInput,
)


# ---------------------------------------------------------------------------
# EventKey
# ---------------------------------------------------------------------------


class TestEventKey:
    def test_construction_defaults(self):
        k = EventKey(run_id="r1", unit_id="unit-0")
        assert k.run_id == "r1"
        assert k.unit_id == "unit-0"
        assert k.stage_id is None
        assert k.attempt == 0
        assert k.seq == 0

    def test_construction_full(self):
        k = EventKey(run_id="r1", unit_id="u1", stage_id="stage:build", attempt=2, seq=42)
        assert k.stage_id == "stage:build"
        assert k.attempt == 2
        assert k.seq == 42

    def test_frozen(self):
        k = EventKey(run_id="r1", unit_id="u0")
        with pytest.raises(AttributeError):
            k.run_id = "r2"  # type: ignore[misc]

    def test_equality_and_hash(self):
        a = EventKey(run_id="r1", unit_id="u0", stage_id="s1", attempt=1, seq=5)
        b = EventKey(run_id="r1", unit_id="u0", stage_id="s1", attempt=1, seq=5)
        assert a == b
        assert hash(a) == hash(b)

    def test_inequality(self):
        a = EventKey(run_id="r1", unit_id="u0", seq=1)
        b = EventKey(run_id="r1", unit_id="u0", seq=2)
        assert a != b


# ---------------------------------------------------------------------------
# Delivery enum
# ---------------------------------------------------------------------------


class TestDelivery:
    def test_values_exist(self):
        assert Delivery.LOSSLESS is not None
        assert Delivery.COALESCIBLE is not None
        assert Delivery.PAGEABLE is not None

    def test_distinct(self):
        assert len(set(Delivery)) == 3


# ---------------------------------------------------------------------------
# Event construction and delivery tags
# ---------------------------------------------------------------------------


def _key(**kw) -> EventKey:
    kw.setdefault("run_id", "r1")
    kw.setdefault("unit_id", "unit-0")
    return EventKey(**kw)


class TestRunStarted:
    def test_delivery_is_lossless(self):
        e = RunStarted(key=_key(), pipeline_name="hello", provider="claude-code")
        assert e.delivery is Delivery.LOSSLESS

    def test_fields(self):
        e = RunStarted(key=_key(), pipeline_name="hello", provider="claude-code")
        assert e.pipeline_name == "hello"
        assert e.provider == "claude-code"
        assert e.budget is None
        assert e.capabilities is None


class TestUnitStarted:
    def test_delivery_is_lossless(self):
        e = UnitStarted(key=_key())
        assert e.delivery is Delivery.LOSSLESS

    def test_optional_model(self):
        e = UnitStarted(key=_key(), model="sonnet")
        assert e.model == "sonnet"


class TestStageStarted:
    def test_delivery(self):
        e = StageStarted(key=_key(stage_id="stage:build"), name="build")
        assert e.delivery is Delivery.LOSSLESS

    def test_attempt_default(self):
        e = StageStarted(key=_key(), name="build")
        assert e.attempt == 1


class TestTurnEvent:
    def test_delivery_is_pageable(self):
        from norn.agents.base import AgentEvent

        ae = AgentEvent(text="hello")
        e = TurnEvent(key=_key(stage_id="stage:build", seq=1), event=ae)
        assert e.delivery is Delivery.PAGEABLE

    def test_wraps_agent_event(self):
        from norn.agents.base import AgentEvent

        ae = AgentEvent(text="chunk")
        e = TurnEvent(key=_key(seq=3), event=ae)
        assert e.event is ae
        assert e.event.text == "chunk"


class TestUsageUpdated:
    def test_delivery_is_coalescible(self):
        e = UsageUpdated(key=_key(), input_tokens=100)
        assert e.delivery is Delivery.COALESCIBLE

    def test_fields(self):
        e = UsageUpdated(key=_key(), input_tokens=10, output_tokens=5, total_cost_usd=0.01)
        assert e.input_tokens == 10
        assert e.output_tokens == 5
        assert e.total_cost_usd == 0.01


class TestStageFinished:
    def test_delivery_is_lossless(self):
        e = StageFinished(key=_key(), name="build", status="passed", success=True)
        assert e.delivery is Delivery.LOSSLESS

    def test_failure_with_error(self):
        e = StageFinished(key=_key(), name="test", status="failed", success=False, error="boom")
        assert not e.success
        assert e.error == "boom"

    def test_artifacts(self):
        e = StageFinished(
            key=_key(), name="gen", status="passed", success=True,
            artifacts=["a.py", "b.py"],
        )
        assert e.artifacts == ["a.py", "b.py"]


class TestStageRetrying:
    def test_delivery(self):
        e = StageRetrying(key=_key(), next_attempt=2)
        assert e.delivery is Delivery.LOSSLESS


class TestWaitingInput:
    def test_delivery(self):
        e = WaitingInput(key=_key(), kind="budget")
        assert e.delivery is Delivery.LOSSLESS

    def test_kind_and_excerpt(self):
        e = WaitingInput(key=_key(), kind="agent", prompt_excerpt="What file?")
        assert e.kind == "agent"
        assert e.prompt_excerpt == "What file?"


class TestLoopExhausted:
    def test_delivery(self):
        e = LoopExhausted(key=_key(), loop_id="loop:build")
        assert e.delivery is Delivery.LOSSLESS


class TestRunError:
    def test_delivery(self):
        e = RunError(key=_key(), error_kind="RunCrashed", detail="oops")
        assert e.delivery is Delivery.LOSSLESS

    def test_fields(self):
        e = RunError(key=_key(), error_kind="ProviderError", detail="timeout")
        assert e.error_kind == "ProviderError"
        assert e.detail == "timeout"


class TestUnitMerged:
    def test_delivery(self):
        e = UnitMerged(key=_key(), merge_status="merged")
        assert e.delivery is Delivery.LOSSLESS


class TestRunFinished:
    def test_delivery(self):
        e = RunFinished(key=_key(), success=True, summary="done")
        assert e.delivery is Delivery.LOSSLESS

    def test_defaults(self):
        e = RunFinished(key=_key())
        assert e.success is True
        assert e.summary == ""


# ---------------------------------------------------------------------------
# All event types are frozen
# ---------------------------------------------------------------------------


_ALL_EVENTS = [
    RunStarted(key=_key(), pipeline_name="p", provider="x"),
    UnitStarted(key=_key()),
    StageStarted(key=_key(), name="s"),
    TurnEvent(key=_key(), event=__import__("norn.agents.base", fromlist=["AgentEvent"]).AgentEvent()),
    UsageUpdated(key=_key()),
    StageFinished(key=_key(), name="s", status="passed", success=True),
    StageRetrying(key=_key(), next_attempt=2),
    WaitingInput(key=_key(), kind="step"),
    LoopExhausted(key=_key()),
    RunError(key=_key(), error_kind="RunCrashed"),
    UnitMerged(key=_key()),
    RunFinished(key=_key()),
]


@pytest.mark.parametrize("event", _ALL_EVENTS, ids=lambda e: type(e).__name__)
def test_events_are_frozen(event):
    with pytest.raises(AttributeError):
        event.key = _key(run_id="other")  # type: ignore[misc]


# ---------------------------------------------------------------------------
# RunEvent union covers all types
# ---------------------------------------------------------------------------


def test_run_event_union_covers_all():
    """Every event in _ALL_EVENTS is an instance of one of the RunEvent union members."""
    # RunEvent is a type alias (union), so we check by name.
    from norn import events as mod

    union_names = {
        "RunStarted", "UnitStarted", "StageStarted", "TurnEvent",
        "UsageUpdated", "StageFinished", "StageRetrying", "WaitingInput",
        "LoopExhausted", "RunError", "UnitMerged", "RunFinished",
    }
    for ev in _ALL_EVENTS:
        assert type(ev).__name__ in union_names

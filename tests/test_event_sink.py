"""Tests for norn.event_sink — delivery guarantees, redaction, and consumer API."""

from __future__ import annotations

import asyncio

import pytest

from norn.agents.base import AgentEvent, ThinkingBlock, ToolResultBlock, ToolUseBlock
from norn.event_sink import EventSink, NullSink, _redact_event
from norn.events import (
    Delivery,
    EventKey,
    RunFinished,
    RunStarted,
    StageFinished,
    StageStarted,
    TurnEvent,
    UsageUpdated,
    WaitingInput,
    RunError,
)


def _key(**kw) -> EventKey:
    kw.setdefault("run_id", "r1")
    kw.setdefault("unit_id", "unit-0")
    return EventKey(**kw)


# ---------------------------------------------------------------------------
# Lossless delivery
# ---------------------------------------------------------------------------


class TestLossless:
    def test_lossless_events_stored_in_order(self):
        sink = EventSink()
        e1 = RunStarted(key=_key(), pipeline_name="p", provider="x")
        e2 = StageStarted(key=_key(stage_id="s1"), name="build")
        e3 = RunFinished(key=_key())
        sink.emit(e1)
        sink.emit(e2)
        sink.emit(e3)
        assert sink.lossless_events == [e1, e2, e3]

    def test_lossless_survives_many_events(self):
        """Lossless events are never dropped even when many are emitted."""
        sink = EventSink()
        events = [
            StageStarted(key=_key(stage_id=f"s{i}"), name=f"stage-{i}")
            for i in range(200)
        ]
        for e in events:
            sink.emit(e)
        assert len(sink.lossless_events) == 200

    def test_lossless_snapshot_is_independent(self):
        sink = EventSink()
        sink.emit(RunStarted(key=_key(), pipeline_name="p", provider="x"))
        snap1 = sink.lossless_events
        sink.emit(RunFinished(key=_key()))
        snap2 = sink.lossless_events
        assert len(snap1) == 1
        assert len(snap2) == 2


# ---------------------------------------------------------------------------
# Coalescible delivery
# ---------------------------------------------------------------------------


class TestCoalescible:
    def test_latest_wins(self):
        sink = EventSink()
        k = _key(stage_id="s1", attempt=1)
        sink.emit(UsageUpdated(key=k, input_tokens=100))
        sink.emit(UsageUpdated(key=k, input_tokens=200))
        sink.emit(UsageUpdated(key=k, input_tokens=300))
        latest = sink.latest_coalescible(stage_id="s1", attempt=1)
        assert latest is not None
        assert latest.input_tokens == 300  # type: ignore[attr-defined]

    def test_different_keys_independent(self):
        sink = EventSink()
        sink.emit(UsageUpdated(key=_key(stage_id="s1", attempt=1), input_tokens=10))
        sink.emit(UsageUpdated(key=_key(stage_id="s2", attempt=1), input_tokens=20))
        r1 = sink.latest_coalescible(stage_id="s1", attempt=1)
        r2 = sink.latest_coalescible(stage_id="s2", attempt=1)
        assert r1 is not None and r1.input_tokens == 10  # type: ignore[attr-defined]
        assert r2 is not None and r2.input_tokens == 20  # type: ignore[attr-defined]

    def test_missing_key_returns_none(self):
        sink = EventSink()
        assert sink.latest_coalescible(stage_id="nope", attempt=1) is None

    def test_all_coalescible(self):
        sink = EventSink()
        sink.emit(UsageUpdated(key=_key(stage_id="s1", attempt=1), input_tokens=10))
        sink.emit(UsageUpdated(key=_key(stage_id="s2", attempt=1), input_tokens=20))
        all_c = sink.all_coalescible()
        assert len(all_c) == 2


# ---------------------------------------------------------------------------
# Pageable / transcript spool
# ---------------------------------------------------------------------------


class TestPageable:
    def test_transcript_appended_in_order(self):
        sink = EventSink()
        for seq in range(5):
            ae = AgentEvent(text=f"chunk-{seq}")
            sink.emit(TurnEvent(key=_key(stage_id="s1", attempt=1, seq=seq), event=ae))
        t = sink.transcript(stage_id="s1", attempt=1)
        assert len(t) == 5
        assert t[0].event.text == "chunk-0"  # type: ignore[attr-defined]
        assert t[4].event.text == "chunk-4"  # type: ignore[attr-defined]

    def test_paging_after_seq(self):
        sink = EventSink()
        for seq in range(10):
            ae = AgentEvent(text=f"t{seq}")
            sink.emit(TurnEvent(key=_key(stage_id="s1", attempt=1, seq=seq), event=ae))
        page = sink.transcript(stage_id="s1", attempt=1, after_seq=6)
        assert len(page) == 3
        seqs = [e.key.seq for e in page]
        assert seqs == [7, 8, 9]

    def test_different_stages_independent(self):
        sink = EventSink()
        sink.emit(TurnEvent(
            key=_key(stage_id="s1", attempt=1, seq=0),
            event=AgentEvent(text="a"),
        ))
        sink.emit(TurnEvent(
            key=_key(stage_id="s2", attempt=1, seq=0),
            event=AgentEvent(text="b"),
        ))
        assert len(sink.transcript(stage_id="s1", attempt=1)) == 1
        assert len(sink.transcript(stage_id="s2", attempt=1)) == 1

    def test_empty_transcript_returns_empty_list(self):
        sink = EventSink()
        assert sink.transcript(stage_id="nope", attempt=1) == []

    def test_never_blocks_producer(self):
        """Emitting many pageable events doesn't block (no bounded queue)."""
        sink = EventSink()
        for seq in range(1000):
            ae = AgentEvent(text=f"x{seq}")
            sink.emit(TurnEvent(key=_key(stage_id="s1", attempt=1, seq=seq), event=ae))
        assert len(sink.transcript(stage_id="s1", attempt=1)) == 1000


# ---------------------------------------------------------------------------
# Redaction at the seam
# ---------------------------------------------------------------------------


class TestRedaction:
    def test_secret_in_turn_event_text_is_masked(self):
        from norn.ui import register_secrets
        register_secrets(["SUPER_SECRET_TOKEN"])
        try:
            ae = AgentEvent(text="Using SUPER_SECRET_TOKEN for auth")
            event = TurnEvent(key=_key(seq=0), event=ae)
            sink = EventSink()
            sink.emit(event)
            stored = sink.transcript()[0]
            assert "SUPER_SECRET_TOKEN" not in stored.event.text  # type: ignore[attr-defined]
            assert "***" in stored.event.text  # type: ignore[attr-defined]
        finally:
            from norn.ui import _masked_secrets
            _masked_secrets.discard("SUPER_SECRET_TOKEN")

    def test_secret_in_tool_result_summary_is_masked(self):
        from norn.ui import register_secrets
        register_secrets(["MY_API_KEY"])
        try:
            block = ToolResultBlock(ok=True, summary="key=MY_API_KEY returned ok")
            ae = AgentEvent(block=block)
            event = TurnEvent(key=_key(seq=0), event=ae)
            sink = EventSink()
            sink.emit(event)
            stored = sink.transcript()[0]
            result_block = stored.event.block  # type: ignore[attr-defined]
            assert "MY_API_KEY" not in result_block.summary
            assert "***" in result_block.summary
        finally:
            from norn.ui import _masked_secrets
            _masked_secrets.discard("MY_API_KEY")

    def test_secret_in_tool_use_input_summary_is_masked(self):
        from norn.ui import register_secrets
        register_secrets(["SECRET123"])
        try:
            block = ToolUseBlock(name="Bash", input_summary="echo SECRET123")
            ae = AgentEvent(block=block)
            event = TurnEvent(key=_key(seq=0), event=ae)
            redacted = _redact_event(event)
            assert "SECRET123" not in redacted.event.block.input_summary  # type: ignore[attr-defined]
        finally:
            from norn.ui import _masked_secrets
            _masked_secrets.discard("SECRET123")

    def test_secret_in_stage_finished_error_is_masked(self):
        from norn.ui import register_secrets
        register_secrets(["DB_PASS"])
        try:
            event = StageFinished(
                key=_key(), name="migrate", status="failed",
                success=False, error="connection failed with DB_PASS",
            )
            redacted = _redact_event(event)
            assert "DB_PASS" not in redacted.error  # type: ignore[attr-defined]
            assert "***" in redacted.error  # type: ignore[attr-defined]
        finally:
            from norn.ui import _masked_secrets
            _masked_secrets.discard("DB_PASS")

    def test_secret_in_run_error_detail_is_masked(self):
        from norn.ui import register_secrets
        register_secrets(["TOKEN_XYZ"])
        try:
            event = RunError(key=_key(), error_kind="ProviderError", detail="bad TOKEN_XYZ")
            redacted = _redact_event(event)
            assert "TOKEN_XYZ" not in redacted.detail  # type: ignore[attr-defined]
        finally:
            from norn.ui import _masked_secrets
            _masked_secrets.discard("TOKEN_XYZ")

    def test_secret_in_waiting_input_excerpt_is_masked(self):
        from norn.ui import register_secrets
        register_secrets(["PRIV_KEY"])
        try:
            event = WaitingInput(key=_key(), kind="agent", prompt_excerpt="Enter PRIV_KEY")
            redacted = _redact_event(event)
            assert "PRIV_KEY" not in redacted.prompt_excerpt  # type: ignore[attr-defined]
        finally:
            from norn.ui import _masked_secrets
            _masked_secrets.discard("PRIV_KEY")

    def test_secret_in_thinking_block_text_is_masked(self):
        """Extended thinking that echoes a secret must be redacted at the seam."""
        from norn.ui import register_secrets
        register_secrets(["THINK_SECRET"])
        try:
            block = ThinkingBlock(text="The auth token THINK_SECRET failed with 403")
            ae = AgentEvent(block=block)
            event = TurnEvent(key=_key(seq=0), event=ae)
            sink = EventSink()
            sink.emit(event)
            stored = sink.transcript()[0]
            result_block = stored.event.block  # type: ignore[attr-defined]
            assert isinstance(result_block, ThinkingBlock)
            assert "THINK_SECRET" not in result_block.text
            assert "***" in result_block.text
        finally:
            from norn.ui import _masked_secrets
            _masked_secrets.discard("THINK_SECRET")

    def test_no_secret_no_copy(self):
        """When no secrets match, the original event object is returned."""
        ae = AgentEvent(text="nothing secret here")
        event = TurnEvent(key=_key(seq=0), event=ae)
        redacted = _redact_event(event)
        assert redacted is event


# ---------------------------------------------------------------------------
# on_event callback
# ---------------------------------------------------------------------------


class TestCallback:
    def test_on_event_called_for_every_emit(self):
        received = []
        sink = EventSink(on_event=received.append)
        sink.emit(RunStarted(key=_key(), pipeline_name="p", provider="x"))
        sink.emit(UsageUpdated(key=_key(), input_tokens=10))
        sink.emit(TurnEvent(key=_key(seq=0), event=AgentEvent(text="hi")))
        assert len(received) == 3

    def test_callback_receives_redacted_events(self):
        from norn.ui import register_secrets
        register_secrets(["REDACT_ME"])
        received = []
        try:
            sink = EventSink(on_event=received.append)
            sink.emit(TurnEvent(
                key=_key(seq=0),
                event=AgentEvent(text="token REDACT_ME here"),
            ))
            assert "REDACT_ME" not in received[0].event.text  # type: ignore[attr-defined]
        finally:
            from norn.ui import _masked_secrets
            _masked_secrets.discard("REDACT_ME")


# ---------------------------------------------------------------------------
# Async drain notification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_wakes_on_emit():
    sink = EventSink()
    sink.bind_loop()

    woken = False

    async def emitter():
        await asyncio.sleep(0.01)
        sink.emit(RunStarted(key=_key(), pipeline_name="p", provider="x"))

    async def waiter():
        nonlocal woken
        await sink.drain()
        woken = True

    await asyncio.gather(emitter(), waiter())
    assert woken


@pytest.mark.asyncio
async def test_drain_without_bind_raises():
    sink = EventSink()
    with pytest.raises(RuntimeError, match="bind_loop"):
        await sink.drain()


# ---------------------------------------------------------------------------
# NullSink
# ---------------------------------------------------------------------------


class TestNullSink:
    def test_emit_is_noop(self):
        sink = NullSink()
        sink.emit(RunStarted(key=_key(), pipeline_name="p", provider="x"))
        assert sink.lossless_events == []

    def test_consumer_methods_return_empty(self):
        sink = NullSink()
        assert sink.latest_coalescible() is None
        assert sink.all_coalescible() == {}
        assert sink.transcript() == []

    def test_bind_loop_is_noop(self):
        sink = NullSink()
        sink.bind_loop()  # should not raise


# ---------------------------------------------------------------------------
# Mixed delivery classification
# ---------------------------------------------------------------------------


class TestMixedDelivery:
    def test_events_routed_to_correct_stores(self):
        sink = EventSink()
        lossless_ev = StageStarted(key=_key(stage_id="s1"), name="build")
        coal_ev = UsageUpdated(key=_key(stage_id="s1", attempt=1), input_tokens=50)
        page_ev = TurnEvent(
            key=_key(stage_id="s1", attempt=1, seq=0),
            event=AgentEvent(text="hi"),
        )

        sink.emit(lossless_ev)
        sink.emit(coal_ev)
        sink.emit(page_ev)

        assert len(sink.lossless_events) == 1
        assert sink.latest_coalescible(stage_id="s1", attempt=1) is not None
        assert len(sink.transcript(stage_id="s1", attempt=1)) == 1

    def test_coalescible_not_in_lossless(self):
        sink = EventSink()
        sink.emit(UsageUpdated(key=_key(stage_id="s1", attempt=1), input_tokens=10))
        assert len(sink.lossless_events) == 0

    def test_pageable_not_in_lossless(self):
        sink = EventSink()
        sink.emit(TurnEvent(
            key=_key(stage_id="s1", attempt=1, seq=0),
            event=AgentEvent(text="x"),
        ))
        assert len(sink.lossless_events) == 0

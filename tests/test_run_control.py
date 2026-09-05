"""Tests for run lifecycle control: pause, cancel, answer, state machine.

All tests are offline — no real Claude/agent calls.  The test contract
validates:

* State-machine transitions (paused, cancelled distinct from failed)
* Cooperative pause gates the next stage start
* Cancel marks the run cancelled and stops further stages
* AnswerInput resolves a pending WaitingInput via the responder
* TUI key bindings respect capability gating
"""
from __future__ import annotations

import asyncio
import contextlib

import pytest

from norn.dsl import Budget, Loop, OnFailure, Pipeline, Stage
from norn.event_sink import EventSink
from norn.events import (
    EventKey,
    RunCancelled,
    RunFinished,
    RunPaused,
    RunResumed,
    RunStarted,
    StageFinished,
    StageStarted,
    WaitingInput,
)
from norn.models import PipelineContext, StageResult, UsageRecord
from norn.responder import InputResponder, TUIResponder
from norn.run_control import CancelledError, RunController
from norn.runner import PipelineError, run_pipeline
from norn.stages.base import BaseStage


# ---------------------------------------------------------------------------
# Test stage helpers
# ---------------------------------------------------------------------------


class _SuccessStage(BaseStage):
    """Always succeeds."""

    def __init__(self, output: str = "ok") -> None:
        self._output = output

    async def run(self, ctx: PipelineContext) -> StageResult:
        return StageResult(name="", success=True, output=self._output)


class _FailStage(BaseStage):
    """Always fails."""

    async def run(self, ctx: PipelineContext) -> StageResult:
        return StageResult(name="", success=False, error="boom")


class _SlowStage(BaseStage):
    """Succeeds after a delay, allowing cancel to interrupt."""

    def __init__(self, delay: float = 5.0) -> None:
        self._delay = delay

    async def run(self, ctx: PipelineContext) -> StageResult:
        try:
            await asyncio.sleep(self._delay)
        except asyncio.CancelledError:
            return StageResult(name="", success=False, error="cancelled")
        return StageResult(name="", success=True, output="slow-done")


class _TrackingStage(BaseStage):
    """Tracks whether it was called."""

    def __init__(self) -> None:
        self.called = False

    async def run(self, ctx: PipelineContext) -> StageResult:
        self.called = True
        return StageResult(name="", success=True, output="tracked")


class _PauseCheckStage(BaseStage):
    """Succeeds after yielding control, used to verify pause happens between stages."""

    def __init__(self, label: str = "") -> None:
        self.label = label
        self.called = False

    async def run(self, ctx: PipelineContext) -> StageResult:
        self.called = True
        # Yield control so background tasks (cancel, etc.) can run
        await asyncio.sleep(0)
        return StageResult(name="", success=True, output=self.label)


def _events_of_type(sink: EventSink, cls: type) -> list:
    return [e for e in sink.lossless_events if isinstance(e, cls)]


# ---------------------------------------------------------------------------
# RunController unit tests
# ---------------------------------------------------------------------------


class TestRunControllerState:
    """Test RunController state machine basics."""

    def test_initial_state(self):
        ctrl = RunController()
        assert not ctrl.is_paused
        assert not ctrl.is_cancelled

    def test_pause_sets_paused(self):
        ctrl = RunController()
        ctrl.pause()
        assert ctrl.is_paused

    def test_resume_clears_paused(self):
        ctrl = RunController()
        ctrl.pause()
        ctrl.resume()
        assert not ctrl.is_paused

    def test_cancel_sets_cancelled(self):
        ctrl = RunController()
        ctrl.cancel()
        assert ctrl.is_cancelled

    def test_cancel_unblocks_pause(self):
        ctrl = RunController()
        ctrl.pause()
        ctrl.cancel()
        # resume_event should be set so wait_if_paused doesn't block
        assert ctrl._resume_event.is_set()

    def test_check_cancelled_raises(self):
        ctrl = RunController()
        ctrl.cancel()
        with pytest.raises(CancelledError):
            ctrl.check_cancelled()

    def test_check_cancelled_noop_when_not_cancelled(self):
        ctrl = RunController()
        ctrl.check_cancelled()  # should not raise


class TestRunControllerAsync:
    """Async tests for RunController cooperative operations."""

    @pytest.mark.asyncio
    async def test_wait_if_paused_returns_immediately_when_not_paused(self):
        ctrl = RunController()
        await ctrl.wait_if_paused()  # should not block

    @pytest.mark.asyncio
    async def test_wait_if_paused_blocks_then_resumes(self):
        ctrl = RunController()
        ctrl.pause()

        resumed = False

        async def resume_after_delay():
            nonlocal resumed
            await asyncio.sleep(0.05)
            ctrl.resume()
            resumed = True

        asyncio.create_task(resume_after_delay())
        await ctrl.wait_if_paused()
        assert resumed

    @pytest.mark.asyncio
    async def test_wait_if_paused_raises_on_cancel(self):
        ctrl = RunController()
        ctrl.pause()

        async def cancel_after_delay():
            await asyncio.sleep(0.05)
            ctrl.cancel()

        asyncio.create_task(cancel_after_delay())
        with pytest.raises(CancelledError):
            await ctrl.wait_if_paused()

    @pytest.mark.asyncio
    async def test_cancel_cancels_active_task(self):
        ctrl = RunController()
        task = asyncio.create_task(asyncio.sleep(100))
        ctrl.set_active_task(task, "my-stage")
        ctrl.cancel()
        # Let the event loop process the cancellation
        await asyncio.sleep(0)
        assert task.cancelled()

    @pytest.mark.asyncio
    async def test_set_active_task_none_clears_registration(self):
        """After set_active_task(None, None), cancel() must not touch the old task."""
        ctrl = RunController()
        task = asyncio.create_task(asyncio.sleep(100))
        ctrl.set_active_task(task, "done-stage")
        # Deregister before cancel — simulates the finally block in _run_stage.
        ctrl.set_active_task(None, None)
        ctrl.cancel()
        await asyncio.sleep(0)
        # The task should NOT be cancelled — it was deregistered first.
        assert not task.cancelled()
        task.cancel()  # clean up
        with contextlib.suppress(asyncio.CancelledError):
            await task


class TestRunControllerAnswer:
    """Test AnswerInput via RunController future."""

    @pytest.mark.asyncio
    async def test_answer_resolves_future(self):
        ctrl = RunController()
        future = ctrl.create_answer_future()
        ctrl.answer_input("c")
        result = await future
        assert result == "c"

    @pytest.mark.asyncio
    async def test_answer_noop_when_no_future(self):
        ctrl = RunController()
        ctrl.answer_input("c")  # no future pending — should not raise

    @pytest.mark.asyncio
    async def test_answer_noop_when_future_done(self):
        ctrl = RunController()
        future = ctrl.create_answer_future()
        ctrl.answer_input("c")
        ctrl.answer_input("a")  # second call is no-op
        result = await future
        assert result == "c"


# ---------------------------------------------------------------------------
# TUIResponder tests
# ---------------------------------------------------------------------------


class TestTUIResponder:
    """Test the TUI responder's delegation to RunController."""

    @pytest.mark.asyncio
    async def test_ask_budget_awaits_answer(self):
        ctrl = RunController()
        responder = TUIResponder(ctrl)

        async def answer_later():
            await asyncio.sleep(0.02)
            ctrl.answer_input("a")

        asyncio.create_task(answer_later())
        result = await responder.ask_budget(None, None)
        assert result == "a"

    @pytest.mark.asyncio
    async def test_ask_failure_awaits_answer(self):
        ctrl = RunController()
        responder = TUIResponder(ctrl)

        async def answer_later():
            await asyncio.sleep(0.02)
            ctrl.answer_input("c")

        asyncio.create_task(answer_later())
        result = await responder.ask_failure("stage1", "error")
        assert result == "c"

    @pytest.mark.asyncio
    async def test_ask_step_awaits_answer(self):
        ctrl = RunController()
        responder = TUIResponder(ctrl)

        async def answer_later():
            await asyncio.sleep(0.02)
            ctrl.answer_input("r")

        asyncio.create_task(answer_later())
        # We pass None for stage/ctx since the TUIResponder doesn't use them
        result = await responder.ask_step(None, None)
        assert result == "r"


# ---------------------------------------------------------------------------
# Pause integration with runner
# ---------------------------------------------------------------------------


class TestPauseInRunner:
    """Test that the cooperative pause check gates stage execution."""

    @pytest.mark.asyncio
    async def test_pause_then_resume_completes_pipeline(self):
        """Pipeline with pause: stages run only after resume."""
        ctrl = RunController()
        sink = EventSink()
        s1 = _PauseCheckStage("first")
        s2 = _PauseCheckStage("second")

        p = Pipeline("test").stage("s1", s1).stage("s2", s2)

        # Pause before running; resume after a short delay
        ctrl.pause()

        async def resume_later():
            await asyncio.sleep(0.05)
            ctrl.resume()

        asyncio.create_task(resume_later())

        ctx = await run_pipeline(p, event_sink=sink, run_controller=ctrl)
        assert s1.called
        assert s2.called
        assert ctx.get("s1") == "first"
        assert ctx.get("s2") == "second"

    @pytest.mark.asyncio
    async def test_pause_emits_events(self):
        """RunPaused and RunResumed events emitted when pause/resume happens."""
        ctrl = RunController()
        sink = EventSink()

        p = Pipeline("test").stage("s1", _SuccessStage()).stage("s2", _SuccessStage())

        # Pause and auto-resume
        ctrl.pause()

        async def resume_later():
            await asyncio.sleep(0.05)
            ctrl.resume()

        asyncio.create_task(resume_later())

        await run_pipeline(p, event_sink=sink, run_controller=ctrl)

        paused = _events_of_type(sink, RunPaused)
        resumed = _events_of_type(sink, RunResumed)
        assert len(paused) >= 1
        assert len(resumed) >= 1

    @pytest.mark.asyncio
    async def test_no_controller_runs_normally(self):
        """Pipeline without a controller runs stages without blocking."""
        sink = EventSink()
        p = Pipeline("test").stage("s1", _SuccessStage("ok"))
        ctx = await run_pipeline(p, event_sink=sink)
        assert ctx.get("s1") == "ok"


# ---------------------------------------------------------------------------
# Cancel integration with runner
# ---------------------------------------------------------------------------


class TestCancelInRunner:
    """Test that cancel stops the pipeline and emits RunCancelled."""

    @pytest.mark.asyncio
    async def test_cancel_before_first_stage(self):
        """Cancel before the run starts — raises CancelledError."""
        ctrl = RunController()
        ctrl.cancel()
        sink = EventSink()
        s1 = _TrackingStage()

        p = Pipeline("test").stage("s1", s1)

        with pytest.raises(CancelledError):
            await run_pipeline(p, event_sink=sink, run_controller=ctrl)

        assert not s1.called
        cancelled_events = _events_of_type(sink, RunCancelled)
        assert len(cancelled_events) == 1

    @pytest.mark.asyncio
    async def test_cancel_between_stages(self):
        """Cancel after first stage — second stage should not run."""
        ctrl = RunController()
        sink = EventSink()

        call_order: list[str] = []

        class _S1(BaseStage):
            async def run(self, ctx: PipelineContext) -> StageResult:
                call_order.append("s1")
                # Yield so the cancel task can run before cooperative check
                await asyncio.sleep(0.05)
                return StageResult(name="", success=True, output="first")

        class _S2(BaseStage):
            async def run(self, ctx: PipelineContext) -> StageResult:
                call_order.append("s2")
                return StageResult(name="", success=True, output="second")

        p = Pipeline("test").stage("s1", _S1()).stage("s2", _S2())

        async def cancel_after_s1():
            while "s1" not in call_order:
                await asyncio.sleep(0.01)
            # Small extra delay so s1 finishes and cooperative check sees cancel
            await asyncio.sleep(0.01)
            ctrl.cancel()

        asyncio.create_task(cancel_after_s1())

        with pytest.raises(CancelledError):
            await run_pipeline(p, event_sink=sink, run_controller=ctrl)

        assert "s1" in call_order
        assert "s2" not in call_order

    @pytest.mark.asyncio
    async def test_cancel_emits_cancelled_and_finished(self):
        """RunCancelled is followed by RunFinished(success=False)."""
        ctrl = RunController()
        ctrl.cancel()
        sink = EventSink()

        p = Pipeline("test").stage("s1", _SuccessStage())

        with pytest.raises(CancelledError):
            await run_pipeline(p, event_sink=sink, run_controller=ctrl)

        cancelled = _events_of_type(sink, RunCancelled)
        finished = _events_of_type(sink, RunFinished)
        assert len(cancelled) == 1
        assert len(finished) == 1
        assert finished[0].success is False

    @pytest.mark.asyncio
    async def test_cancel_in_loop_body(self):
        """Cancel in a loop body stops further stages."""
        ctrl = RunController()
        sink = EventSink()
        call_order: list[str] = []

        class _LoopS1(BaseStage):
            async def run(self, ctx: PipelineContext) -> StageResult:
                call_order.append("s1")
                await asyncio.sleep(0.05)
                return StageResult(name="", success=True, output="loop-s1")

        class _LoopS2(BaseStage):
            async def run(self, ctx: PipelineContext) -> StageResult:
                call_order.append("s2")
                return StageResult(name="", success=True, output="loop-s2")

        p = Pipeline("test").loop(
            "retry",
            max_retries=3,
            on_exhaust=OnFailure.FAIL,
            stages=[Stage("s1", _LoopS1()), Stage("s2", _LoopS2())],
        )

        async def cancel_after_s1():
            while "s1" not in call_order:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.01)
            ctrl.cancel()

        asyncio.create_task(cancel_after_s1())

        with pytest.raises(CancelledError):
            await run_pipeline(p, event_sink=sink, run_controller=ctrl)

        assert "s1" in call_order
        assert "s2" not in call_order

    @pytest.mark.asyncio
    async def test_cancel_mid_stage_ends_promptly(self):
        """cancel() during a running stage ends the run quickly, not after the
        stage's full duration.  This is the test that would have caught the
        original defect (set_active_task never called)."""
        ctrl = RunController()
        sink = EventSink()

        stage_sleep = 5.0  # seconds — the stage "wants" to sleep this long

        class _LongStage(BaseStage):
            async def run(self, ctx: PipelineContext) -> StageResult:
                await asyncio.sleep(stage_sleep)
                return StageResult(name="", success=True, output="long-done")

        p = Pipeline("test").stage("slow", _LongStage())

        async def cancel_soon():
            await asyncio.sleep(0.05)
            ctrl.cancel()

        asyncio.create_task(cancel_soon())

        import time

        t0 = time.monotonic()
        with pytest.raises(CancelledError) as exc_info:
            await run_pipeline(p, event_sink=sink, run_controller=ctrl)
        elapsed = time.monotonic() - t0

        # Must finish well before the stage's sleep duration.
        assert elapsed < 1.0, f"Cancel took {elapsed:.1f}s — stage was not interrupted"
        assert exc_info.value.stage_name == "slow"

        # A StageFinished event was emitted for the cancelled stage.
        finished = _events_of_type(sink, StageFinished)
        assert any(f.name == "slow" and f.status == "cancelled" for f in finished)

    @pytest.mark.asyncio
    async def test_cancel_mid_stage_raises_cancelled_not_normal_result(self):
        """cancel() raises run_control.CancelledError, never returns a normal
        StageResult — the run is aborted, not quietly passed.

        Uses a stage that does NOT swallow asyncio.CancelledError (unlike
        _SlowStage), which is the common case for agent-backed stages.
        """
        ctrl = RunController()
        sink = EventSink()

        class _UnguardedSleep(BaseStage):
            async def run(self, ctx: PipelineContext) -> StageResult:
                await asyncio.sleep(5.0)
                return StageResult(name="", success=True, output="done")

        p = Pipeline("test").stage("sleeper", _UnguardedSleep())

        async def cancel_soon():
            await asyncio.sleep(0.05)
            ctrl.cancel()

        asyncio.create_task(cancel_soon())

        with pytest.raises(CancelledError):
            await run_pipeline(p, event_sink=sink, run_controller=ctrl)

        # RunCancelled must have been emitted.
        cancelled_events = _events_of_type(sink, RunCancelled)
        assert len(cancelled_events) == 1


# ---------------------------------------------------------------------------
# ViewModel state machine tests
# ---------------------------------------------------------------------------


class TestViewModelStateMachine:
    """Test that the ViewModel projects pause/cancel/finished correctly."""

    def test_paused_status(self):
        from norn.tui.viewmodel import RunViewModel

        vm = RunViewModel()
        vm.apply(RunStarted(
            key=EventKey(run_id="r1", unit_id="unit-0"),
            pipeline_name="test",
            provider="claude-code",
        ))
        assert vm.header.status == "running"

        vm.apply(RunPaused(key=EventKey(run_id="r1", unit_id="unit-0")))
        assert vm.header.status == "paused"

    def test_resumed_status(self):
        from norn.tui.viewmodel import RunViewModel

        vm = RunViewModel()
        vm.apply(RunStarted(
            key=EventKey(run_id="r1", unit_id="unit-0"),
            pipeline_name="test",
            provider="claude-code",
        ))
        vm.apply(RunPaused(key=EventKey(run_id="r1", unit_id="unit-0")))
        vm.apply(RunResumed(key=EventKey(run_id="r1", unit_id="unit-0")))
        assert vm.header.status == "running"

    def test_cancelled_status(self):
        from norn.tui.viewmodel import RunViewModel

        vm = RunViewModel()
        vm.apply(RunStarted(
            key=EventKey(run_id="r1", unit_id="unit-0"),
            pipeline_name="test",
            provider="claude-code",
        ))
        vm.apply(RunCancelled(key=EventKey(run_id="r1", unit_id="unit-0")))
        assert vm.header.status == "cancelled"

    def test_finished_after_cancel(self):
        from norn.tui.viewmodel import RunViewModel

        vm = RunViewModel()
        vm.apply(RunStarted(
            key=EventKey(run_id="r1", unit_id="unit-0"),
            pipeline_name="test",
            provider="claude-code",
        ))
        vm.apply(RunCancelled(key=EventKey(run_id="r1", unit_id="unit-0")))
        vm.apply(RunFinished(key=EventKey(run_id="r1", unit_id="unit-0"), success=False))
        assert vm.header.status == "failed"

    def test_full_lifecycle(self):
        """running -> paused -> running -> passed."""
        from norn.tui.viewmodel import RunViewModel

        vm = RunViewModel()
        ekey = EventKey(run_id="r1", unit_id="unit-0")

        vm.apply(RunStarted(key=ekey, pipeline_name="test", provider="claude-code"))
        assert vm.header.status == "running"

        vm.apply(RunPaused(key=ekey))
        assert vm.header.status == "paused"

        vm.apply(RunResumed(key=ekey))
        assert vm.header.status == "running"

        vm.apply(RunFinished(key=ekey, success=True))
        assert vm.header.status == "passed"


# ---------------------------------------------------------------------------
# Status glyphs
# ---------------------------------------------------------------------------


class TestStatusGlyphs:
    """Test that paused/cancelled have distinct glyphs."""

    def test_paused_glyph(self):
        from norn.tui.widgets import STATUS_GLYPHS

        assert "paused" in STATUS_GLYPHS
        assert STATUS_GLYPHS["paused"] != STATUS_GLYPHS["running"]
        assert STATUS_GLYPHS["paused"] != STATUS_GLYPHS["failed"]

    def test_cancelled_glyph(self):
        from norn.tui.widgets import STATUS_GLYPHS

        assert "cancelled" in STATUS_GLYPHS
        assert STATUS_GLYPHS["cancelled"] != STATUS_GLYPHS["failed"]


# ---------------------------------------------------------------------------
# TUI key binding capability gating (Pilot tests)
# ---------------------------------------------------------------------------


class TestTUIBindings:
    """Test that key bindings are gated by capabilities."""

    @pytest.mark.asyncio
    async def test_pause_action_always_enabled(self):
        from norn.tui.app import NornApp

        app = NornApp()
        async with app.run_test():
            assert app._is_action_enabled("toggle_pause") is True

    @pytest.mark.asyncio
    async def test_cancel_disabled_when_no_controller(self):
        from norn.tui.app import NornApp

        app = NornApp()
        async with app.run_test():
            assert app._is_action_enabled("cancel_run") is False

    @pytest.mark.asyncio
    async def test_cancel_disabled_when_finished(self):
        from norn.tui.app import NornApp

        app = NornApp()
        app.run_finished = True
        async with app.run_test():
            assert app._is_action_enabled("cancel_run") is False

    @pytest.mark.asyncio
    async def test_answer_disabled_when_no_waiting_input(self):
        from norn.tui.app import NornApp

        app = NornApp()
        async with app.run_test():
            assert app._is_action_enabled("answer_input") is False

    @pytest.mark.asyncio
    async def test_answer_enabled_when_waiting_input(self):
        from norn.tui.app import NornApp

        app = NornApp()
        app._vm.waiting_input = WaitingInput(
            key=EventKey(run_id="r1", unit_id="unit-0"),
            kind="budget",
        )
        async with app.run_test():
            assert app._is_action_enabled("answer_input") is True

    @pytest.mark.asyncio
    async def test_model_switch_disabled_without_capability(self):
        from norn.tui.app import NornApp

        app = NornApp(capabilities=None)
        async with app.run_test():
            assert app._is_action_enabled("set_model") is False

    @pytest.mark.asyncio
    async def test_attach_disabled_without_capability(self):
        from norn.tui.app import NornApp

        app = NornApp(capabilities=None)
        async with app.run_test():
            assert app._is_action_enabled("attach") is False

    @pytest.mark.asyncio
    async def test_check_action_returns_false_for_disabled(self):
        """check_action() returns False for disabled bindings."""
        from norn.tui.app import NornApp

        app = NornApp()
        async with app.run_test():
            # cancel_run is disabled when no controller
            result = app.check_action("cancel_run", ())
            assert result is False

    @pytest.mark.asyncio
    async def test_check_action_returns_true_for_enabled(self):
        """check_action() returns True for enabled bindings."""
        from norn.tui.app import NornApp

        app = NornApp()
        async with app.run_test():
            result = app.check_action("toggle_pause", ())
            assert result is True

    @pytest.mark.asyncio
    async def test_quit_always_enabled(self):
        from norn.tui.app import NornApp

        app = NornApp()
        async with app.run_test():
            assert app._is_action_enabled("quit_app") is True

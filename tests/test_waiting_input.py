"""Tests for the WaitingInput event seam and pluggable InputResponder.

All prompts are resolved by a fake responder that never touches stdin.  The
CLIResponder tests use monkeypatching so the existing norn.ui functions are
exercised without blocking on a real terminal.
"""
from __future__ import annotations

import pytest
import unittest.mock as mock

from norn.dsl import Budget, Loop, OnFailure, Pipeline, Stage
from norn.event_sink import EventSink
from norn.events import WaitingInput
from norn.models import PipelineContext, StageResult, UsageRecord
from norn.responder import CLIResponder, InputResponder
from norn.runner import BudgetExceededError, PipelineError, RetriesExhaustedError, run_pipeline
from norn.stages.base import BaseStage


# ---------------------------------------------------------------------------
# Shared helpers
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


class _CostStage(BaseStage):
    """Succeeds and reports usage so budget checks fire."""

    def __init__(self, cost_usd: float = 0.0, tokens: int = 0) -> None:
        self._cost = cost_usd
        self._tokens = tokens

    async def run(self, ctx: PipelineContext) -> StageResult:
        usage = UsageRecord(
            stage_name="",
            total_cost_usd=self._cost,
            input_tokens=self._tokens,
            output_tokens=0,
        )
        return StageResult(name="", success=True, output="ok", usage=usage)


class FakeResponder(InputResponder):
    """Never touches stdin. Records all calls for assertion."""

    def __init__(
        self,
        budget_choice: str = "c",
        failure_choice: str = "c",
        step_choice: str = "r",
    ) -> None:
        self._budget_choice = budget_choice
        self._failure_choice = failure_choice
        self._step_choice = step_choice
        self.calls: list[tuple] = []

    async def ask_budget(self, tracker, budget) -> str:
        self.calls.append(("budget",))
        return self._budget_choice

    async def ask_failure(self, name: str, error: str | None) -> str:
        self.calls.append(("failure", name))
        return self._failure_choice

    async def ask_step(self, stage, ctx, *, session_id=None) -> str:
        self.calls.append(("step", stage.name))
        return self._step_choice


def _sink_and_responder(responder: InputResponder):
    """Return an EventSink + inject both into the pipeline via ctx overrides."""
    sink = EventSink()
    return sink, responder


def _waiting_events(sink: EventSink) -> list[WaitingInput]:
    """Extract WaitingInput events from a sink's lossless list."""
    return [e for e in sink.lossless_events if isinstance(e, WaitingInput)]


# ---------------------------------------------------------------------------
# Budget-exceeded prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_waiting_input_emitted():
    """WaitingInput(kind='budget') is emitted before the responder is called."""
    responder = FakeResponder(budget_choice="c")
    sink = EventSink()
    p = (
        Pipeline("t")
        .budget(max_cost_usd=1.00, on_exceed=OnFailure.ASK_USER)
        .stage("s1", _CostStage(cost_usd=2.00))
    )
    await run_pipeline(p, event_sink=sink, input_responder=responder)
    # The pipeline succeeded (responder chose 'c')
    waiting = _waiting_events(sink)
    assert len(waiting) == 1
    assert waiting[0].kind == "budget"


@pytest.mark.asyncio
async def test_budget_fake_responder_continues_no_stdin():
    """Fake responder continues without any stdin interaction."""
    responder = FakeResponder(budget_choice="c")
    sink = EventSink()
    p = (
        Pipeline("t")
        .budget(max_cost_usd=1.00, on_exceed=OnFailure.ASK_USER)
        .stage("s1", _CostStage(cost_usd=2.00))
    )
    ctx = await run_pipeline(p, event_sink=sink, input_responder=responder)
    assert ctx.get("s1") == "ok"
    assert responder.calls == [("budget",)]


@pytest.mark.asyncio
async def test_budget_fake_responder_aborts():
    """Fake responder abort raises BudgetExceededError without touching stdin."""
    responder = FakeResponder(budget_choice="a")
    sink = EventSink()
    p = (
        Pipeline("t")
        .budget(max_cost_usd=1.00, on_exceed=OnFailure.ASK_USER)
        .stage("s1", _CostStage(cost_usd=2.00))
    )
    with pytest.raises(BudgetExceededError):
        await run_pipeline(p, event_sink=sink, input_responder=responder)
    assert responder.calls == [("budget",)]
    waiting = _waiting_events(sink)
    assert waiting and waiting[0].kind == "budget"


# ---------------------------------------------------------------------------
# Failure-recovery prompt (stage on_failure=ASK_USER)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failure_recovery_waiting_input_emitted():
    """WaitingInput(kind='failure_recovery') is emitted on stage failure."""
    responder = FakeResponder(failure_choice="c")
    sink = EventSink()
    p = Pipeline("t").stage("s1", _FailStage(), on_failure=OnFailure.ASK_USER)
    await run_pipeline(p, event_sink=sink, input_responder=responder)
    waiting = _waiting_events(sink)
    assert len(waiting) == 1
    assert waiting[0].kind == "failure_recovery"
    assert waiting[0].prompt_excerpt == "boom"


@pytest.mark.asyncio
async def test_failure_recovery_fake_responder_continues():
    """Fake responder continues after failure without stdin."""
    responder = FakeResponder(failure_choice="c")
    sink = EventSink()
    p = Pipeline("t").stage("s1", _FailStage(), on_failure=OnFailure.ASK_USER)
    await run_pipeline(p, event_sink=sink, input_responder=responder)
    assert responder.calls == [("failure", "s1")]


@pytest.mark.asyncio
async def test_failure_recovery_fake_responder_aborts():
    """Fake responder abort raises PipelineError without stdin."""
    responder = FakeResponder(failure_choice="a")
    sink = EventSink()
    p = Pipeline("t").stage("s1", _FailStage(), on_failure=OnFailure.ASK_USER)
    with pytest.raises(PipelineError) as exc_info:
        await run_pipeline(p, event_sink=sink, input_responder=responder)
    assert exc_info.value.stage_name == "s1"
    assert responder.calls == [("failure", "s1")]


# ---------------------------------------------------------------------------
# Failure-recovery prompt (loop exhaustion with on_exhaust=ASK_USER)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_exhaustion_waiting_input_emitted():
    """WaitingInput(kind='failure_recovery') is emitted when a loop is exhausted."""
    responder = FakeResponder(failure_choice="c")
    sink = EventSink()
    p = Pipeline("t").loop(
        "lp",
        max_retries=2,
        on_exhaust=OnFailure.ASK_USER,
        stages=[Stage("s1", _FailStage())],
    )
    await run_pipeline(p, event_sink=sink, input_responder=responder)
    waiting = _waiting_events(sink)
    assert len(waiting) == 1
    assert waiting[0].kind == "failure_recovery"


@pytest.mark.asyncio
async def test_loop_exhaustion_fake_responder_aborts():
    """Fake responder abort on loop exhaustion raises RetriesExhaustedError."""
    responder = FakeResponder(failure_choice="a")
    sink = EventSink()
    p = Pipeline("t").loop(
        "lp",
        max_retries=1,
        on_exhaust=OnFailure.ASK_USER,
        stages=[Stage("s1", _FailStage())],
    )
    with pytest.raises(RetriesExhaustedError):
        await run_pipeline(p, event_sink=sink, input_responder=responder)
    assert responder.calls == [("failure", "lp")]


# ---------------------------------------------------------------------------
# Step-mode prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_waiting_input_emitted():
    """WaitingInput(kind='step') is emitted before each stage in step mode."""
    responder = FakeResponder(step_choice="r")
    sink = EventSink()
    p = Pipeline("t").stage("s1", _SuccessStage())
    await run_pipeline(p, step_mode=True, event_sink=sink, input_responder=responder)
    waiting = _waiting_events(sink)
    assert len(waiting) == 1
    assert waiting[0].kind == "step"


@pytest.mark.asyncio
async def test_step_fake_responder_runs_stage():
    """Step responder 'r' runs the stage normally."""
    responder = FakeResponder(step_choice="r")
    sink = EventSink()
    p = Pipeline("t").stage("s1", _SuccessStage("hello"))
    ctx = await run_pipeline(p, step_mode=True, event_sink=sink, input_responder=responder)
    assert ctx.get("s1") == "hello"
    assert responder.calls == [("step", "s1")]


@pytest.mark.asyncio
async def test_step_fake_responder_skips_stage():
    """Step responder 's' skips without executing."""
    responder = FakeResponder(step_choice="s")
    sink = EventSink()
    fail = _FailStage()
    p = Pipeline("t").stage("s1", fail)
    ctx = await run_pipeline(p, step_mode=True, event_sink=sink, input_responder=responder)
    result = ctx.results["s1"]
    assert result.success
    assert result.output is None


@pytest.mark.asyncio
async def test_step_fake_responder_aborts():
    """Step responder 'a' raises PipelineError."""
    responder = FakeResponder(step_choice="a")
    sink = EventSink()
    p = Pipeline("t").stage("s1", _SuccessStage())
    with pytest.raises(PipelineError) as exc_info:
        await run_pipeline(p, step_mode=True, event_sink=sink, input_responder=responder)
    assert exc_info.value.stage_name == "s1"


@pytest.mark.asyncio
async def test_step_loop_waiting_input_emitted():
    """WaitingInput(kind='step') is emitted for stages inside a loop."""
    responder = FakeResponder(step_choice="r")
    sink = EventSink()
    p = Pipeline("t").loop(
        "lp", max_retries=1, stages=[Stage("s1", _SuccessStage())]
    )
    await run_pipeline(p, step_mode=True, event_sink=sink, input_responder=responder)
    waiting = _waiting_events(sink)
    assert any(w.kind == "step" for w in waiting)
    assert responder.calls == [("step", "s1")]


# ---------------------------------------------------------------------------
# CLIResponder delegates correctly (monkeypatched norn.ui)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cli_responder_budget_delegates_to_ui(monkeypatch):
    """CLIResponder.ask_budget() drives the same outcome as patching norn.ui."""
    import norn.ui as ui_mod

    monkeypatch.setattr(ui_mod, "ask_budget_exceeded", lambda tracker, budget: "c")
    p = (
        Pipeline("t")
        .budget(max_cost_usd=1.00, on_exceed=OnFailure.ASK_USER)
        .stage("s1", _CostStage(cost_usd=2.00))
    )
    # Default CLIResponder — no explicit responder
    ctx = await run_pipeline(p)
    assert ctx.get("s1") == "ok"


@pytest.mark.asyncio
async def test_cli_responder_failure_delegates_to_ui(monkeypatch):
    """CLIResponder.ask_failure() drives the same outcome as patching norn.ui."""
    import norn.ui as ui_mod

    monkeypatch.setattr(ui_mod, "ask_user_continue", lambda name, error: "c")
    p = Pipeline("t").stage("s1", _FailStage(), on_failure=OnFailure.ASK_USER)
    ctx = await run_pipeline(p)
    # Stage failed but user continued — result stored in ctx
    assert ctx.results["s1"].success is False


@pytest.mark.asyncio
async def test_cli_responder_step_delegates_to_ui(monkeypatch):
    """CLIResponder.ask_step() drives the same outcome as patching norn.ui."""
    import norn.ui as ui_mod

    monkeypatch.setattr(ui_mod, "step_prompt", lambda stage, ctx, session_id=None: "r")
    p = Pipeline("t").stage("s1", _SuccessStage("hello"))
    ctx = await run_pipeline(p, step_mode=True)
    assert ctx.get("s1") == "hello"

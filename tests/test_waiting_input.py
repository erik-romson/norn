"""Tests for the WaitingInput event seam and pluggable InputResponder.

All prompts are resolved by a fake responder that never touches stdin.  The
CLIResponder tests use monkeypatching so the existing norn.ui functions are
exercised without blocking on a real terminal.
"""
from __future__ import annotations

import asyncio

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

    async def ask_failure(self, name: str, error: str | None, *, timed_out: bool = False) -> str:
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


class _SlowOnceStage(BaseStage):
    """Takes *delay* seconds; records how many times it ran."""

    def __init__(self, delay: float) -> None:
        self._delay = delay
        self.runs = 0

    async def run(self, ctx: PipelineContext) -> StageResult:
        self.runs += 1
        await asyncio.sleep(self._delay)
        return StageResult(name="", success=True, output="done")


class _ScriptedResponder(FakeResponder):
    """Answers failure prompts from a list and records ``timed_out``."""

    def __init__(self, answers: list[str]) -> None:
        super().__init__()
        self._answers = list(answers)
        self.timed_out_flags: list[bool] = []

    async def ask_failure(self, name: str, error: str | None, *, timed_out: bool = False) -> str:
        self.timed_out_flags.append(timed_out)
        return self._answers.pop(0)


@pytest.mark.asyncio
async def test_timeout_prompt_is_marked_timed_out():
    """A stage timeout emits kind='timeout_recovery' and tells the responder."""
    responder = _ScriptedResponder(["c"])
    sink = EventSink()
    p = Pipeline("t").stage("slow", _SlowOnceStage(delay=1.0), timeout=0.05, on_failure=OnFailure.ASK_USER)
    await run_pipeline(p, event_sink=sink, input_responder=responder)
    assert [w.kind for w in _waiting_events(sink)] == ["timeout_recovery"]
    assert responder.timed_out_flags == [True]


@pytest.mark.asyncio
async def test_plain_failure_prompt_is_not_timed_out():
    responder = _ScriptedResponder(["c"])
    p = Pipeline("t").stage("s1", _FailStage(), on_failure=OnFailure.ASK_USER)
    await run_pipeline(p, input_responder=responder)
    assert responder.timed_out_flags == [False]


@pytest.mark.asyncio
async def test_retry_with_extended_timeout_lets_stage_finish():
    """'t' doubles the timeout on each retry until the stage fits."""
    stage = _SlowOnceStage(delay=0.3)
    responder = _ScriptedResponder(["t", "t", "t"])
    p = Pipeline("t").stage("slow", stage, timeout=0.1, on_failure=OnFailure.ASK_USER)
    ctx = await run_pipeline(p, input_responder=responder)
    # 0.1s and 0.2s time out; 0.4s is enough.
    assert ctx.get("slow") == "done"
    assert stage.runs == 3
    assert responder.timed_out_flags == [True, True]


@pytest.mark.asyncio
async def test_retry_without_timeout_lets_stage_finish():
    stage = _SlowOnceStage(delay=0.3)
    responder = _ScriptedResponder(["n"])
    p = Pipeline("t").stage("slow", stage, timeout=0.05, on_failure=OnFailure.ASK_USER)
    ctx = await run_pipeline(p, input_responder=responder)
    assert ctx.get("slow") == "done"
    assert p.items[0].timeout == 0.05  # the pipeline definition is untouched


@pytest.mark.asyncio
async def test_plain_retry_keeps_the_timeout():
    stage = _SlowOnceStage(delay=1.0)
    responder = _ScriptedResponder(["r", "a"])
    p = Pipeline("t").stage("slow", stage, timeout=0.05, on_failure=OnFailure.ASK_USER)
    with pytest.raises(PipelineError):
        await run_pipeline(p, input_responder=responder)
    assert stage.runs == 2


class _SessionAgentStage(BaseStage):
    """Agent stage that starts session 'sess-1' and hangs on its first run only."""

    needs_agent = True

    def __init__(self, hang: float) -> None:
        self._hang = hang
        self.sessions: list[str | None] = []
        self.resumes: list[str | None] = []

    async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
        self.sessions.append(kwargs.get("session_id"))
        self.resumes.append(kwargs.get("resume_reason"))
        if len(self.sessions) == 1:
            kwargs["on_session_id"]("sess-1")
            await asyncio.sleep(self._hang)
        return StageResult(name="", success=True, output="done")


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["r", "t", "n"])
async def test_timeout_retry_resumes_the_cut_off_session(answer):
    stage = _SessionAgentStage(hang=1.0)
    responder = _ScriptedResponder([answer])
    p = Pipeline("t").stage("agent", stage, timeout=0.05, on_failure=OnFailure.ASK_USER)
    ctx = await run_pipeline(p, input_responder=responder)
    assert ctx.get("agent") == "done"
    assert stage.sessions == [None, "sess-1"]
    assert stage.resumes == [None, "timeout"]


@pytest.mark.asyncio
async def test_exhausted_loop_retry_resumes_the_cut_off_session():
    stage = _SessionAgentStage(hang=1.0)
    responder = _ScriptedResponder(["t"])
    p = Pipeline("t").loop(
        "lp",
        max_retries=1,
        on_exhaust=OnFailure.ASK_USER,
        stages=[Stage("agent", stage, timeout=0.05)],
    )
    await run_pipeline(p, input_responder=responder)
    assert stage.sessions == [None, "sess-1"]
    assert stage.resumes == [None, "timeout"]


class _AgentProbeStage(BaseStage):
    needs_agent = True

    def __init__(self) -> None:
        self.resumes: list[str | None] = []

    async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
        self.resumes.append(kwargs.get("resume_reason"))
        return StageResult(name="", success=True)


@pytest.mark.asyncio
async def test_loop_continue_prompt_skipped_once_another_agent_spoke():
    """An earlier agent stage in the retried round moves the conversation on."""
    first = _AgentProbeStage()
    slow = _SessionAgentStage(hang=1.0)
    responder = _ScriptedResponder(["n"])
    p = Pipeline("t").loop(
        "lp",
        max_retries=1,
        on_exhaust=OnFailure.ASK_USER,
        stages=[Stage("first", first), Stage("agent", slow, timeout=0.05)],
    )
    await run_pipeline(p, input_responder=responder)
    assert first.resumes == [None, None]
    assert slow.resumes == [None, None]


@pytest.mark.asyncio
async def test_retry_after_plain_failure_sends_the_stage_prompt():
    responder = _ScriptedResponder(["r"])

    class _FailOnce(_AgentProbeStage):
        async def run(self, ctx, **kwargs):
            await super().run(ctx, **kwargs)
            return StageResult(name="", success=len(self.resumes) > 1, error="boom")

    stage = _FailOnce()
    p = Pipeline("t").stage("agent", stage, on_failure=OnFailure.ASK_USER)
    await run_pipeline(p, input_responder=responder)
    assert stage.resumes == [None, None]


class _MaxTurnsOnceStage(_AgentProbeStage):
    """Agent stage that runs out of turns in session 'sess-1' on its first run."""

    def __init__(self) -> None:
        super().__init__()
        self.sessions: list[str | None] = []

    async def run(self, ctx, **kwargs):
        await super().run(ctx, **kwargs)
        self.sessions.append(kwargs.get("session_id"))
        if len(self.sessions) > 1:
            return StageResult(name="", success=True, output="done")
        return StageResult(
            name="", success=False, error="max turns",
            usage=UsageRecord(stage_name="", session_id="sess-1", result_subtype="error_max_turns"),
            details={"max_turns_exhausted": True},
        )


@pytest.mark.asyncio
async def test_max_turns_retry_resumes_the_cut_off_session():
    stage = _MaxTurnsOnceStage()
    responder = _ScriptedResponder(["r"])
    p = Pipeline("t").stage("agent", stage, on_failure=OnFailure.ASK_USER)
    ctx = await run_pipeline(p, input_responder=responder)
    assert ctx.get("agent") == "done"
    assert stage.sessions == [None, "sess-1"]
    assert stage.resumes == [None, "max_turns"]
    assert responder.timed_out_flags == [False]


@pytest.mark.asyncio
async def test_exhausted_loop_retry_resumes_the_max_turns_session():
    stage = _MaxTurnsOnceStage()
    responder = _ScriptedResponder(["r"])
    p = Pipeline("t").loop(
        "lp",
        max_retries=1,
        on_exhaust=OnFailure.ASK_USER,
        stages=[Stage("agent", stage)],
    )
    await run_pipeline(p, input_responder=responder)
    assert stage.sessions == [None, "sess-1"]
    assert stage.resumes == [None, "max_turns"]


@pytest.mark.asyncio
async def test_exhausted_loop_retry_extends_timed_out_stage():
    stage = _SlowOnceStage(delay=0.3)
    responder = _ScriptedResponder(["n"])
    p = Pipeline("t").loop(
        "lp",
        max_retries=1,
        on_exhaust=OnFailure.ASK_USER,
        stages=[Stage("slow", stage, timeout=0.05)],
    )
    ctx = await run_pipeline(p, input_responder=responder)
    assert ctx.get("slow") == "done"
    assert responder.timed_out_flags == [True]


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

    monkeypatch.setattr(ui_mod, "ask_user_continue", lambda name, error, timed_out=False: "c")
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

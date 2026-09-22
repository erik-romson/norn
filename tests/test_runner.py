import asyncio
import unittest.mock as mock

import pytest

from norn.dsl import Loop, OnFailure, Parallel, Pipeline, Stage, file_exists, output_contains, stage_failed, stage_succeeded
from norn.models import PipelineContext, StageResult, UsageRecord
from norn.runner import BudgetExceededError, PipelineError, RetriesExhaustedError, run_pipeline
from norn.stages.base import BaseStage


class SuccessStage(BaseStage):
    """Always succeeds, returning a fixed output."""

    def __init__(self, output: str = "ok") -> None:
        self._output = output

    async def run(self, ctx: PipelineContext) -> StageResult:
        return StageResult(name="", success=True, output=self._output)


class FailStage(BaseStage):
    """Always fails."""

    async def run(self, ctx: PipelineContext) -> StageResult:
        return StageResult(name="", success=False, error="boom")


class FailThenSucceedStage(BaseStage):
    """Fails N times, then succeeds."""

    def __init__(self, fail_count: int = 1) -> None:
        self._fail_count = fail_count
        self._calls = 0

    async def run(self, ctx: PipelineContext) -> StageResult:
        self._calls += 1
        if self._calls <= self._fail_count:
            return StageResult(name="", success=False, error=f"fail #{self._calls}")
        return StageResult(name="", success=True, output="recovered")


class TrackingStage(BaseStage):
    """Records each call for hook order verification."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.calls: list[str] = []

    async def run(self, ctx: PipelineContext) -> StageResult:
        self.calls.append(self.label)
        return StageResult(name="", success=True, output=self.label)


@pytest.mark.asyncio
async def test_simple_pipeline():
    p = Pipeline("test").stage("s1", SuccessStage("hello"))
    ctx = await run_pipeline(p)
    assert ctx.get("s1") == "hello"


@pytest.mark.asyncio
async def test_no_config_path_sets_no_metrics():
    """When config_path is None, run_metrics is None and findings is empty."""
    p = Pipeline("test").stage("s1", SuccessStage("ok"))
    ctx = await run_pipeline(p)
    assert ctx.run_metrics is None
    assert ctx.findings == []


@pytest.mark.asyncio
async def test_stage_failure_raises():
    p = Pipeline("test").stage("s1", FailStage(), on_failure=OnFailure.FAIL)
    with pytest.raises(PipelineError):
        await run_pipeline(p)


@pytest.mark.asyncio
async def test_multiple_stages():
    p = (
        Pipeline("test")
        .stage("s1", SuccessStage("a"))
        .stage("s2", SuccessStage("b"))
    )
    ctx = await run_pipeline(p)
    assert ctx.get("s1") == "a"
    assert ctx.get("s2") == "b"


@pytest.mark.asyncio
async def test_loop_retries():
    flaky = FailThenSucceedStage(fail_count=2)
    p = Pipeline("test").loop(
        "retry",
        max_retries=3,
        on_exhaust=OnFailure.FAIL,
        stages=[Stage("s1", flaky)],
    )
    ctx = await run_pipeline(p)
    assert ctx.get("s1") == "recovered"
    assert flaky._calls == 3


@pytest.mark.asyncio
async def test_loop_exhausted():
    p = Pipeline("test").loop(
        "retry",
        max_retries=2,
        on_exhaust=OnFailure.FAIL,
        stages=[Stage("s1", FailStage())],
    )
    with pytest.raises(RetriesExhaustedError):
        await run_pipeline(p)


@pytest.mark.asyncio
async def test_loop_exhausted_draft_pr_continues():
    """on_exhaust=draft_pr does not raise — pipeline continues after loop."""
    p = (
        Pipeline("test")
        .loop("retry", max_retries=1, on_exhaust=OnFailure.DRAFT_PR, stages=[Stage("s1", FailStage())])
        .stage("s2", SuccessStage("shipped"))
    )
    ctx = await run_pipeline(p)
    assert ctx.get("s2") == "shipped"


@pytest.mark.asyncio
async def test_loop_success_first_try():
    p = Pipeline("test").loop(
        "ok",
        max_retries=3,
        stages=[Stage("s1", SuccessStage("done"))],
    )
    ctx = await run_pipeline(p)
    assert ctx.get("s1") == "done"


@pytest.mark.asyncio
async def test_clear_context_does_not_discard_results():
    p = (
        Pipeline("test")
        .stage("s1", SuccessStage("kept"))
        .clear_context()
        .stage("s2", SuccessStage("after"))
    )
    ctx = await run_pipeline(p)
    assert ctx.get("s1") == "kept"
    assert ctx.get("s2") == "after"


@pytest.mark.asyncio
async def test_loop_with_multiple_stages():
    """Second stage in loop fails, causing retry from top."""
    counter = FailThenSucceedStage(fail_count=1)
    p = Pipeline("test").loop(
        "multi",
        max_retries=3,
        stages=[
            Stage("s1", SuccessStage("ok")),
            Stage("s2", counter),
        ],
    )
    ctx = await run_pipeline(p)
    assert ctx.get("s2") == "recovered"


@pytest.mark.asyncio
async def test_skip_top_level_stage():
    """A skipped top-level stage is not executed and counts as success."""
    fail = FailStage()
    p = Pipeline("test").stage("s1", fail)
    ctx = await run_pipeline(p, params={"skip": {"s1"}})
    result = ctx.results["s1"]
    assert result.success
    assert result.output is None


@pytest.mark.asyncio
async def test_skip_stage_in_loop():
    """A skipped stage inside a loop is not executed and does not block the loop."""
    fail = FailStage()
    p = Pipeline("test").loop(
        "lp",
        max_retries=1,
        stages=[
            Stage("s1", SuccessStage("ok")),
            Stage("s2", fail),
        ],
    )
    ctx = await run_pipeline(p, params={"skip": {"s2"}})
    assert ctx.get("s1") == "ok"
    result = ctx.results["s2"]
    assert result.success


@pytest.mark.asyncio
async def test_skip_does_not_affect_other_stages():
    """Skipping one stage leaves others unaffected."""
    p = (
        Pipeline("test")
        .stage("s1", SuccessStage("a"))
        .stage("s2", SuccessStage("b"))
        .stage("s3", SuccessStage("c"))
    )
    ctx = await run_pipeline(p, params={"skip": {"s2"}})
    assert ctx.get("s1") == "a"
    assert ctx.results["s2"].success
    assert ctx.results["s2"].output is None
    assert ctx.get("s3") == "c"


# ---------------------------------------------------------------------------
# Pipeline-level hooks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_and_post_stage_hooks_fire_around_each_stage():
    """pre_stage fires before and post_stage fires after every successful stage."""
    log: list[str] = []

    class LogStage(BaseStage):
        def __init__(self, label: str) -> None:
            self.label = label

        async def run(self, ctx: PipelineContext) -> StageResult:
            log.append(self.label)
            return StageResult(name="", success=True)

    p = (
        Pipeline("test")
        .hook("pre_stage", LogStage("pre"))
        .hook("post_stage", LogStage("post"))
        .stage("s1", LogStage("s1"))
        .stage("s2", LogStage("s2"))
    )
    await run_pipeline(p)
    assert log == ["pre", "s1", "post", "pre", "s2", "post"]


@pytest.mark.asyncio
async def test_on_failure_hook_fires_when_stage_fails():
    """on_failure hook runs when a stage fails."""
    fired: list[bool] = []

    class FlagStage(BaseStage):
        async def run(self, ctx: PipelineContext) -> StageResult:
            fired.append(True)
            return StageResult(name="", success=True)

    p = (
        Pipeline("test")
        .hook("on_failure", FlagStage())
        .stage("s1", FailStage(), on_failure=OnFailure.ASK_USER)
    )
    # ASK_USER with no UI available will prompt — patch ui to auto-continue
    with mock.patch("norn.ui.ask_user_continue", return_value="c"):
        await run_pipeline(p)

    assert fired == [True]


@pytest.mark.asyncio
async def test_ask_user_retry_reruns_failed_stage():
    """on_failure=ASK_USER with choice 'r' re-runs the stage; once it passes
    the pipeline proceeds normally."""
    flaky = FailThenSucceedStage(fail_count=1)
    p = Pipeline("test").stage("s1", flaky, on_failure=OnFailure.ASK_USER)

    # First failure → 'r' (retry). The retry succeeds, so no second prompt.
    with mock.patch("norn.ui.ask_user_continue", return_value="r") as ask:
        ctx = await run_pipeline(p)

    assert flaky._calls == 2  # ran twice: initial fail + retry
    assert ask.call_count == 1
    assert ctx.results["s1"].success is True


@pytest.mark.asyncio
async def test_ask_user_retry_then_continue_stops_retrying():
    """A persistently failing stage: 'r' retries, then 'c' continues past it
    without aborting the run."""
    p = Pipeline("test").stage("s1", FailStage(), on_failure=OnFailure.ASK_USER)

    # Retry once, then give up and continue.
    with mock.patch("norn.ui.ask_user_continue", side_effect=["r", "c"]) as ask:
        await run_pipeline(p)  # must not raise

    assert ask.call_count == 2


@pytest.mark.asyncio
async def test_loop_exhaust_ask_user_retry_reruns_loop():
    """on_exhaust=ASK_USER with 'r' runs the whole loop again; the second
    round succeeds so the run completes."""
    # Fails the first 2 attempts (round 1 exhausts at max_retries=2), then
    # succeeds on the 3rd call (round 2, first attempt).
    flaky = FailThenSucceedStage(fail_count=2)
    p = Pipeline("test").loop(
        "lp", max_retries=2, on_exhaust=OnFailure.ASK_USER, stages=[Stage("s1", flaky)]
    )

    with mock.patch("norn.ui.ask_user_continue", return_value="r") as ask:
        await run_pipeline(p)  # must not raise

    assert ask.call_count == 1  # one exhaustion prompt, then the rerun passes
    assert flaky._calls == 3


@pytest.mark.asyncio
async def test_post_stage_hook_does_not_fire_on_failure():
    """post_stage must NOT fire when a stage fails."""
    post_called: list[bool] = []

    class PostFlag(BaseStage):
        async def run(self, ctx: PipelineContext) -> StageResult:
            post_called.append(True)
            return StageResult(name="", success=True)

    p = (
        Pipeline("test")
        .hook("post_stage", PostFlag())
        .stage("s1", FailStage(), on_failure=OnFailure.ASK_USER)
    )
    with mock.patch("norn.ui.ask_user_continue", return_value="c"):
        await run_pipeline(p)

    assert post_called == []


@pytest.mark.asyncio
async def test_on_retry_hook_fires_before_loop_retry():
    """on_retry hook fires once per retry (not on the first attempt)."""
    retry_count: list[int] = []

    class CountRetry(BaseStage):
        async def run(self, ctx: PipelineContext) -> StageResult:
            retry_count.append(1)
            return StageResult(name="", success=True)

    flaky = FailThenSucceedStage(fail_count=2)
    p = (
        Pipeline("test")
        .hook("on_retry", CountRetry())
        .loop("lp", max_retries=3, on_exhaust=OnFailure.FAIL, stages=[Stage("s1", flaky)])
    )
    await run_pipeline(p)
    # 3 attempts → 2 retries → on_retry fires twice
    assert len(retry_count) == 2


@pytest.mark.asyncio
async def test_pre_and_post_hooks_fire_inside_loop():
    """pre_stage and post_stage hooks fire for stages inside a loop too."""
    log: list[str] = []

    class LogStage(BaseStage):
        def __init__(self, label: str) -> None:
            self.label = label

        async def run(self, ctx: PipelineContext) -> StageResult:
            log.append(self.label)
            return StageResult(name="", success=True)

    p = (
        Pipeline("test")
        .hook("pre_stage", LogStage("pre"))
        .hook("post_stage", LogStage("post"))
        .loop("lp", max_retries=1, stages=[Stage("s1", LogStage("s1"))])
    )
    await run_pipeline(p)
    assert log == ["pre", "s1", "post"]


@pytest.mark.asyncio
async def test_hook_failure_raises_pipeline_error():
    """A failing hook raises PipelineError and aborts the pipeline."""
    p = (
        Pipeline("test")
        .hook("pre_stage", FailStage())
        .stage("s1", SuccessStage())
    )
    with pytest.raises(PipelineError, match="hook:pre_stage"):
        await run_pipeline(p)


# ---------------------------------------------------------------------------
# Parallel stages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parallel_all_succeed():
    """All parallel stages run and their results are stored in context."""
    p = Pipeline("test").parallel(
        "par",
        stages=[
            Stage("a", SuccessStage("result-a")),
            Stage("b", SuccessStage("result-b")),
        ],
    )
    ctx = await run_pipeline(p)
    assert ctx.get("a") == "result-a"
    assert ctx.get("b") == "result-b"


@pytest.mark.asyncio
async def test_parallel_failure_raises_pipeline_error():
    """A failing stage inside a Parallel block raises PipelineError."""
    p = Pipeline("test").parallel(
        "par",
        stages=[
            Stage("ok", SuccessStage("fine")),
            Stage("bad", FailStage()),
        ],
    )
    with pytest.raises(PipelineError):
        await run_pipeline(p)


@pytest.mark.asyncio
async def test_parallel_stages_run_concurrently():
    """Stages in a Parallel block execute concurrently, not sequentially."""
    started: list[str] = []
    barrier = asyncio.Event()

    class WaitStage(BaseStage):
        def __init__(self, label: str) -> None:
            self.label = label

        async def run(self, ctx: PipelineContext) -> StageResult:
            started.append(self.label)
            await barrier.wait()
            return StageResult(name="", success=True, output=self.label)

    # Release barrier once both stages have started
    async def releaser() -> None:
        while len(started) < 2:
            await asyncio.sleep(0)
        barrier.set()

    p = Pipeline("test").parallel(
        "par",
        stages=[
            Stage("x", WaitStage("x")),
            Stage("y", WaitStage("y")),
        ],
    )
    await asyncio.gather(run_pipeline(p), releaser())
    assert set(started) == {"x", "y"}


@pytest.mark.asyncio
async def test_parallel_results_available_to_downstream_stage():
    """Downstream sequential stage can access outputs from parallel stages."""
    p = (
        Pipeline("test")
        .parallel(
            "par",
            stages=[
                Stage("p1", SuccessStage("from-p1")),
                Stage("p2", SuccessStage("from-p2")),
            ],
        )
        .stage("after", SuccessStage("sequential"))
    )
    ctx = await run_pipeline(p)
    assert ctx.get("p1") == "from-p1"
    assert ctx.get("p2") == "from-p2"
    assert ctx.get("after") == "sequential"


# ---------------------------------------------------------------------------
# Budget enforcement
# ---------------------------------------------------------------------------


class CostStage(BaseStage):
    """Succeeds and records a fixed cost in usage."""

    def __init__(self, cost_usd: float = 0.0, tokens: int = 0) -> None:
        self._cost = cost_usd
        self._tokens = tokens

    async def run(self, ctx: PipelineContext) -> StageResult:
        usage = UsageRecord(stage_name="", total_cost_usd=self._cost, input_tokens=self._tokens)
        return StageResult(name="", success=True, output="ok", usage=usage)


@pytest.mark.asyncio
async def test_budget_cost_exceeded_raises():
    """Pipeline raises BudgetExceededError when cost exceeds max_cost_usd."""
    p = Pipeline("test").budget(max_cost_usd=1.00).stage("s1", CostStage(cost_usd=2.00))
    with pytest.raises(BudgetExceededError):
        await run_pipeline(p)


@pytest.mark.asyncio
async def test_budget_tokens_exceeded_raises():
    """Pipeline raises BudgetExceededError when tokens exceed max_tokens."""
    p = Pipeline("test").budget(max_tokens=100).stage("s1", CostStage(tokens=200))
    with pytest.raises(BudgetExceededError):
        await run_pipeline(p)


@pytest.mark.asyncio
async def test_budget_not_exceeded_passes():
    """Pipeline completes normally when usage stays under the budget."""
    p = Pipeline("test").budget(max_cost_usd=10.00).stage("s1", CostStage(cost_usd=1.00))
    ctx = await run_pipeline(p)
    assert ctx.get("s1") == "ok"


@pytest.mark.asyncio
async def test_budget_ask_user_continue():
    """on_exceed=ASK_USER continues the pipeline when user chooses 'c'."""
    p = (
        Pipeline("test")
        .budget(max_cost_usd=1.00, on_exceed=OnFailure.ASK_USER)
        .stage("s1", CostStage(cost_usd=2.00))
    )
    with mock.patch("norn.ui.ask_budget_exceeded", return_value="c"):
        ctx = await run_pipeline(p)
    assert ctx.get("s1") == "ok"


@pytest.mark.asyncio
async def test_budget_ask_user_continue_extends_instead_of_reprompting():
    """'c' raises the limit above what was spent, so later stages run unasked."""
    p = (
        Pipeline("test")
        .budget(max_cost_usd=1.00, on_exceed=OnFailure.ASK_USER)
        .stage("s1", CostStage(cost_usd=2.00))
        .stage("s2", CostStage(cost_usd=0.50))
        .stage("s3", CostStage(cost_usd=0.10))
    )
    with mock.patch("norn.ui.ask_budget_exceeded", return_value="c") as ask:
        ctx = await run_pipeline(p)
    assert ctx.get("s3") == "ok"
    assert ask.call_count == 1
    # 2.00 spent, +50% headroom.
    assert p.budgets[0].max_cost_usd == 3.00


@pytest.mark.asyncio
async def test_budget_ask_user_prompts_again_once_the_new_limit_is_hit():
    """Extending is headroom, not a blank cheque: the next overrun asks again."""
    p = (
        Pipeline("test")
        .budget(max_cost_usd=1.00, on_exceed=OnFailure.ASK_USER)
        .stage("s1", CostStage(cost_usd=2.00))
        .stage("s2", CostStage(cost_usd=2.00))
    )
    with mock.patch("norn.ui.ask_budget_exceeded", return_value="c") as ask:
        await run_pipeline(p)
    assert ask.call_count == 2


@pytest.mark.asyncio
async def test_budget_ask_user_no_limit_never_asks_again():
    """'u' lifts the limit for the rest of the run."""
    p = (
        Pipeline("test")
        .budget(max_cost_usd=1.00, on_exceed=OnFailure.ASK_USER)
        .stage("s1", CostStage(cost_usd=2.00))
        .stage("s2", CostStage(cost_usd=50.00))
    )
    with mock.patch("norn.ui.ask_budget_exceeded", return_value="u") as ask:
        await run_pipeline(p)
    assert ask.call_count == 1
    assert p.budgets[0].max_cost_usd is None


@pytest.mark.asyncio
async def test_budget_waiting_input_carries_the_overrun_detail():
    """The TUI modal's excerpt comes from the event, so it must say the numbers."""
    from norn.event_sink import EventSink

    sink = EventSink()
    p = (
        Pipeline("test")
        .budget(max_cost_usd=1.00, on_exceed=OnFailure.ASK_USER)
        .stage("s1", CostStage(cost_usd=2.00))
    )
    with mock.patch("norn.ui.ask_budget_exceeded", return_value="c"):
        await run_pipeline(p, event_sink=sink)
    waiting = [
        e for e in sink.lossless_events
        if type(e).__name__ == "WaitingInput" and e.kind == "budget"
    ]
    assert waiting and waiting[0].prompt_excerpt == "$2.0000 > $1.00 limit"


@pytest.mark.asyncio
async def test_budget_ask_user_abort():
    """on_exceed=ASK_USER raises BudgetExceededError when user chooses 'a'."""
    p = (
        Pipeline("test")
        .budget(max_cost_usd=1.00, on_exceed=OnFailure.ASK_USER)
        .stage("s1", CostStage(cost_usd=2.00))
    )
    with mock.patch("norn.ui.ask_budget_exceeded", return_value="a"):
        with pytest.raises(BudgetExceededError):
            await run_pipeline(p)


# ---------------------------------------------------------------------------
# Conditional stages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conditional_stage_runs_when_condition_true():
    """Stage with when=True runs normally."""
    p = Pipeline("test").stage("s1", SuccessStage("yes"), when=lambda ctx: True)
    ctx = await run_pipeline(p)
    assert ctx.get("s1") == "yes"


@pytest.mark.asyncio
async def test_conditional_stage_skipped_when_condition_false():
    """Stage with when=False is skipped; result is success with no output."""
    p = Pipeline("test").stage("s1", FailStage(), when=lambda ctx: False)
    ctx = await run_pipeline(p)
    result = ctx.results["s1"]
    assert result.success
    assert result.output is None


@pytest.mark.asyncio
async def test_conditional_stage_receives_context():
    """The when predicate receives the live PipelineContext."""
    p = (
        Pipeline("test")
        .stage("s1", SuccessStage("hello"))
        .stage("s2", SuccessStage("ran"), when=lambda ctx: ctx.get("s1") == "hello")
        .stage("s3", SuccessStage("skipped"), when=lambda ctx: ctx.get("s1") == "nope")
    )
    ctx = await run_pipeline(p)
    assert ctx.get("s2") == "ran"
    assert ctx.results["s3"].output is None


@pytest.mark.asyncio
async def test_stage_succeeded_predicate():
    """`stage_succeeded` is true after a successful stage."""
    p = (
        Pipeline("test")
        .stage("s1", SuccessStage("ok"))
        .stage("s2", SuccessStage("ran"), when=stage_succeeded("s1"))
    )
    ctx = await run_pipeline(p)
    assert ctx.get("s2") == "ran"


@pytest.mark.asyncio
async def test_stage_failed_predicate():
    """`stage_failed` is true after a failed stage (with on_failure=ASK_USER to continue)."""
    p = (
        Pipeline("test")
        .stage("s1", FailStage(), on_failure=OnFailure.ASK_USER)
        .stage("s2", SuccessStage("ran"), when=stage_failed("s1"))
    )
    with mock.patch("norn.ui.ask_user_continue", return_value="c"):
        ctx = await run_pipeline(p)
    assert ctx.get("s2") == "ran"


@pytest.mark.asyncio
async def test_output_contains_predicate():
    """`output_contains` matches text in a prior stage's output."""
    p = (
        Pipeline("test")
        .stage("s1", SuccessStage("foo bar baz"))
        .stage("s2", SuccessStage("ran"), when=output_contains("s1", "bar"))
        .stage("s3", SuccessStage("skipped"), when=output_contains("s1", "qux"))
    )
    ctx = await run_pipeline(p)
    assert ctx.get("s2") == "ran"
    assert ctx.results["s3"].output is None


@pytest.mark.asyncio
async def test_file_exists_predicate(tmp_path):
    """`file_exists` is true when the file exists, false when it does not."""
    existing = tmp_path / "present.txt"
    existing.write_text("hi")
    missing = tmp_path / "absent.txt"

    p = (
        Pipeline("test")
        .stage("s1", SuccessStage("ran"), when=file_exists(str(existing)))
        .stage("s2", SuccessStage("skipped"), when=file_exists(str(missing)))
    )
    ctx = await run_pipeline(p)
    assert ctx.get("s1") == "ran"
    assert ctx.results["s2"].output is None


@pytest.mark.asyncio
async def test_conditional_stage_in_loop():
    """A conditional stage inside a loop is skipped when condition is false."""
    p = Pipeline("test").loop(
        "lp",
        max_retries=1,
        stages=[
            Stage("s1", SuccessStage("ok")),
            Stage("s2", FailStage(), when=lambda ctx: False),
        ],
    )
    ctx = await run_pipeline(p)
    assert ctx.get("s1") == "ok"
    assert ctx.results["s2"].success
    assert ctx.results["s2"].output is None


# ---------------------------------------------------------------------------
# Stage timeouts
# ---------------------------------------------------------------------------


class SlowStage(BaseStage):
    """Sleeps for a given duration before succeeding."""

    def __init__(self, delay: float = 1.0) -> None:
        self._delay = delay

    async def run(self, ctx: PipelineContext) -> StageResult:
        await asyncio.sleep(self._delay)
        return StageResult(name="", success=True, output="done")


@pytest.mark.asyncio
async def test_stage_timeout_fires():
    """A stage that exceeds its timeout raises PipelineError."""
    p = Pipeline("test").stage("slow", SlowStage(delay=10), timeout=0.05)
    with pytest.raises(PipelineError) as exc_info:
        await run_pipeline(p)
    assert "slow" in exc_info.value.stage_name
    assert "Timed out" in exc_info.value.result.error


@pytest.mark.asyncio
async def test_stage_timeout_not_exceeded():
    """A stage that finishes within its timeout succeeds normally."""
    p = Pipeline("test").stage("fast", SuccessStage("ok"), timeout=5.0)
    ctx = await run_pipeline(p)
    assert ctx.get("fast") == "ok"


@pytest.mark.asyncio
async def test_loop_timeout_fires():
    """A loop that exceeds its timeout raises PipelineError."""
    p = Pipeline("test").loop(
        "slow_loop",
        max_retries=3,
        timeout=0.05,
        stages=[Stage("slow", SlowStage(delay=10))],
    )
    with pytest.raises(PipelineError) as exc_info:
        await run_pipeline(p)
    assert "slow_loop" in exc_info.value.stage_name
    assert "Timed out" in exc_info.value.result.error


@pytest.mark.asyncio
async def test_outer_cancellation_is_not_recorded_as_success():
    """A run task cancelled from outside (app shutdown) must not finish as a success."""
    from norn.event_sink import EventSink
    from norn.events import RunCancelled, RunFinished

    sink = EventSink()
    p = Pipeline("test").stage("slow", SlowStage(delay=10))
    task = asyncio.create_task(run_pipeline(p, event_sink=sink))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    events = sink.lossless_events
    assert any(isinstance(e, RunCancelled) for e in events)
    assert [e.success for e in events if isinstance(e, RunFinished)] == [False]


# ---------------------------------------------------------------------------
# Interactive stepping mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_mode_run_executes_stage():
    """step_mode=True with action 'r' runs the stage normally."""
    p = Pipeline("test").stage("s1", SuccessStage("hello"))
    with mock.patch("norn.ui.step_prompt", return_value="r"):
        ctx = await run_pipeline(p, step_mode=True)
    assert ctx.get("s1") == "hello"


@pytest.mark.asyncio
async def test_step_mode_skip_marks_success_no_output():
    """step_mode=True with action 's' skips the stage: success with no output."""
    fail = FailStage()
    p = Pipeline("test").stage("s1", fail)
    with mock.patch("norn.ui.step_prompt", return_value="s"):
        ctx = await run_pipeline(p, step_mode=True)
    result = ctx.results["s1"]
    assert result.success
    assert result.output is None


@pytest.mark.asyncio
async def test_step_mode_abort_raises_pipeline_error():
    """step_mode=True with action 'a' raises PipelineError."""
    p = Pipeline("test").stage("s1", SuccessStage())
    with mock.patch("norn.ui.step_prompt", return_value="a"):
        with pytest.raises(PipelineError) as exc_info:
            await run_pipeline(p, step_mode=True)
    assert exc_info.value.stage_name == "s1"


@pytest.mark.asyncio
async def test_step_mode_run_in_loop():
    """step_mode=True with 'r' inside a loop runs stages normally."""
    p = Pipeline("test").loop(
        "lp",
        max_retries=1,
        stages=[Stage("s1", SuccessStage("ok"))],
    )
    with mock.patch("norn.ui.step_prompt", return_value="r"):
        ctx = await run_pipeline(p, step_mode=True)
    assert ctx.get("s1") == "ok"


@pytest.mark.asyncio
async def test_step_mode_skip_in_loop():
    """step_mode=True with 's' inside a loop skips the stage."""
    p = Pipeline("test").loop(
        "lp",
        max_retries=1,
        stages=[Stage("s1", FailStage())],
    )
    with mock.patch("norn.ui.step_prompt", return_value="s"):
        ctx = await run_pipeline(p, step_mode=True)
    result = ctx.results["s1"]
    assert result.success
    assert result.output is None


# ---------------------------------------------------------------------------
# Preflight: _check_model_labels
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_missing_label_raises():
    """Preflight raises ModelLabelError when a Generate stage has no model and no default_model."""
    from norn.model_labels import ModelLabelError
    from norn.stages.generate import Generate

    p = Pipeline("test").stage("gen", Generate(prompt="hello"))
    with pytest.raises(ModelLabelError, match="has no model label"):
        await run_pipeline(p, agent_provider="claude-code")


@pytest.mark.asyncio
async def test_preflight_unknown_label_raises():
    """Preflight raises ModelLabelError when the model label is not defined for the provider."""
    from norn.model_labels import ModelLabelError
    from norn.stages.generate import Generate

    p = Pipeline("test").stage("gen", Generate(prompt="hello", model="nonexistent-label"))
    with pytest.raises(ModelLabelError, match="unknown label"):
        await run_pipeline(p, agent_provider="claude-code")


@pytest.mark.asyncio
async def test_preflight_valid_label_passes():
    """Preflight passes (no error) for a known label; only the stage impl is not executed here
    since the stage has no agent — non-Generate stages are unaffected by the preflight."""
    # Use a SuccessStage (no model attr) so the pipeline runs without agent calls.
    p = Pipeline("test").stage("s1", SuccessStage("ok"))
    ctx = await run_pipeline(p, agent_provider="claude-code")
    assert ctx.get("s1") == "ok"


def test_preflight_default_model_satisfies_missing_stage_model():
    """A pipeline with default_model covers Generate stages that don't set model explicitly."""
    from norn.stages.generate import Generate
    from norn.runner import _check_model_labels

    p = Pipeline("test", default_model="standard").stage("gen", Generate(prompt="hello"))
    # Should not raise any exception — "standard" is a known label for claude-code.
    _check_model_labels(p, "claude-code")


@pytest.mark.asyncio
async def test_preflight_walks_loop():
    """Preflight recurses into Loop stages and catches a missing model."""
    from norn.model_labels import ModelLabelError
    from norn.stages.generate import Generate

    loop = Loop("myloop", stages=[Stage("gen", Generate(prompt="hello"))])
    p = Pipeline("test")
    p.items.append(loop)
    with pytest.raises(ModelLabelError, match="has no model label"):
        await run_pipeline(p, agent_provider="claude-code")


@pytest.mark.asyncio
async def test_preflight_walks_parallel():
    """Preflight recurses into Parallel stages and catches an unknown label."""
    from norn.model_labels import ModelLabelError
    from norn.stages.generate import Generate

    par = Parallel("mypar", stages=[Stage("gen", Generate(prompt="hello", model="bad-label"))])
    p = Pipeline("test")
    p.items.append(par)
    with pytest.raises(ModelLabelError, match="unknown label"):
        await run_pipeline(p, agent_provider="claude-code")


@pytest.mark.asyncio
async def test_preflight_walks_include(tmp_path):
    """Preflight recurses into Include items (loads sub-pipeline) and catches a missing label."""
    from norn.model_labels import ModelLabelError

    sub_file = tmp_path / "sub.py"
    sub_file.write_text(
        "from norn.dsl import Pipeline\n"
        "from norn.stages.generate import Generate\n"
        "config = Pipeline('sub').stage('gen', Generate(prompt='hello'))\n"
    )
    from norn.dsl import Include

    p = Pipeline("test")
    p.items.append(Include(path=str(sub_file)))
    with pytest.raises(ModelLabelError, match="has no model label"):
        await run_pipeline(p, agent_provider="claude-code")


@pytest.mark.asyncio
async def test_preflight_rejects_effort_env_process(monkeypatch):
    """Preflight rejects CLAUDE_CODE_EFFORT_LEVEL set in process env."""
    from norn.model_labels import ModelLabelError
    from norn.stages.generate import Generate

    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "high")
    p = Pipeline("test").stage("gen", Generate(prompt="hello", model="standard"))
    with pytest.raises(ModelLabelError, match="CLAUDE_CODE_EFFORT_LEVEL"):
        await run_pipeline(p, agent_provider="claude-code")


@pytest.mark.asyncio
async def test_preflight_rejects_effort_env_pipeline():
    """Preflight rejects CLAUDE_CODE_EFFORT_LEVEL set in pipeline env_vars."""
    from norn.model_labels import ModelLabelError
    from norn.stages.generate import Generate

    p = (
        Pipeline("test")
        .env("CLAUDE_CODE_EFFORT_LEVEL", "medium")
        .stage("gen", Generate(prompt="hello", model="standard"))
    )
    with pytest.raises(ModelLabelError, match="CLAUDE_CODE_EFFORT_LEVEL"):
        await run_pipeline(p, agent_provider="claude-code")


@pytest.mark.asyncio
async def test_preflight_rejects_effort_env_stage():
    """Preflight rejects CLAUDE_CODE_EFFORT_LEVEL set in a Generate stage's env."""
    from norn.model_labels import ModelLabelError
    from norn.stages.generate import Generate

    p = Pipeline("test").stage(
        "gen",
        Generate(prompt="hello", model="standard", env={"CLAUDE_CODE_EFFORT_LEVEL": "low"}),
    )
    with pytest.raises(ModelLabelError, match="CLAUDE_CODE_EFFORT_LEVEL"):
        await run_pipeline(p, agent_provider="claude-code")


@pytest.mark.asyncio
async def test_preflight_rejects_effort_env_profile():
    """Preflight rejects CLAUDE_CODE_EFFORT_LEVEL set in the pipeline session profile."""
    from norn.model_labels import ModelLabelError
    from norn.profiles import SessionProfile
    from norn.stages.generate import Generate

    profile = SessionProfile(
        name="test-profile",
        permission_mode="plan",
        env={"CLAUDE_CODE_EFFORT_LEVEL": "high"},
    )
    p = (
        Pipeline("test")
        .session_profile(profile)
        .stage("gen", Generate(prompt="hello", model="standard"))
    )
    with pytest.raises(ModelLabelError, match="CLAUDE_CODE_EFFORT_LEVEL"):
        await run_pipeline(p, agent_provider="claude-code")


def test_preflight_effort_env_ignored_for_opencode():
    """CLAUDE_CODE_EFFORT_LEVEL in pipeline env is NOT flagged for opencode (it's claude-code only)."""
    from norn.stages.generate import Generate
    from norn.runner import _check_model_labels

    p = (
        Pipeline("test")
        .env("CLAUDE_CODE_EFFORT_LEVEL", "high")
        .stage("gen", Generate(prompt="hello", model="standard"))
    )
    # For opencode the effort env check is skipped; only the label check runs.
    # "standard" is known for opencode so the preflight should pass without raising.
    _check_model_labels(p, "opencode")


# ---------------------------------------------------------------------------
# always_run — guards that must not be restored from a checkpoint
#
# A resumed run that skips its own preconditions inherits a verdict about the
# repo as it stood when the earlier run passed. If the repo moved in between
# (a staged change, a vanished toolchain), the run sails past the guard and
# dies later at a postcondition, blaming a stage that did nothing wrong.
# ---------------------------------------------------------------------------


class CountingStage(BaseStage):
    """Records how many times it ran."""

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, ctx: PipelineContext) -> StageResult:
        self.calls += 1
        return StageResult(name="", success=True, output="ok")


def _checkpoint(*completed: str):
    from norn.checkpoint import Checkpoint

    return Checkpoint(
        pipeline="p",
        timestamp="2026-01-01T00:00:00Z",
        session_id=None,
        completed_stages=list(completed),
        results={name: "ok" for name in completed},
        usage=[],
    )


@pytest.mark.asyncio
async def test_always_run_stage_reruns_on_resume():
    guard, work = CountingStage(), CountingStage()
    pipeline = Pipeline("p")
    pipeline.stage("preflight", guard, always_run=True)
    pipeline.stage("work", work)

    await run_pipeline(pipeline, resume_checkpoint=_checkpoint("preflight", "work"))

    assert guard.calls == 1, "an always_run guard must re-execute on resume"
    assert work.calls == 0, "ordinary stages must still be restored from the checkpoint"


@pytest.mark.asyncio
async def test_always_run_stage_can_still_fail_the_resumed_run():
    """The point of re-running the guard is that it gets to stop the run."""
    pipeline = Pipeline("p")
    pipeline.stage("preflight", FailStage(), always_run=True)

    with pytest.raises(PipelineError):
        await run_pipeline(pipeline, resume_checkpoint=_checkpoint("preflight"))


@pytest.mark.asyncio
async def test_always_run_stage_inside_a_loop_reruns_on_resume():
    guard = CountingStage()
    pipeline = Pipeline("p")
    pipeline.loop(
        "body",
        stages=[Stage(name="preflight", impl=guard, always_run=True)],
        max_retries=1,
    )

    await run_pipeline(pipeline, resume_checkpoint=_checkpoint("preflight"))

    assert guard.calls == 1


@pytest.mark.asyncio
async def test_stages_are_cached_by_default():
    work = CountingStage()
    pipeline = Pipeline("p")
    pipeline.stage("work", work)

    await run_pipeline(pipeline, resume_checkpoint=_checkpoint("work"))

    assert work.calls == 0

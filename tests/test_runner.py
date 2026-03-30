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

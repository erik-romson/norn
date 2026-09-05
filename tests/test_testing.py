"""Tests for norn/testing.py — test infrastructure."""

from __future__ import annotations

from typing import Any

import pytest

from norn.dsl import Pipeline, Stage, Loop, OnFailure, stage_succeeded
from norn.models import PipelineContext, StageResult, UsageRecord
from norn.runner import run_pipeline
from norn.stages.base import BaseStage
from norn.stages.generate import Generate
from norn.stages.run_command import RunCommand
from norn.testing import (
    CallRecord,
    CallVerifier,
    MockStage,
    PipelineTestResult,
    PipelineTestRunner,
    StageResultView,
    Verifier,
    reset_call_counter,
    verify,
    _global_call_counter,
)


def _make_ctx(**results: StageResult) -> PipelineContext:
    """Build a PipelineContext with the given stage results."""
    ctx = PipelineContext()
    ctx.results.update(results)
    return ctx


def _dummy_result(name: str = "s1", success: bool = True) -> StageResult:
    return StageResult(name=name, success=success, output="ok")


class TestCallRecord:
    """Tests for the CallRecord dataclass."""

    def test_basic_fields(self) -> None:
        ctx = _make_ctx()
        result = _dummy_result()
        rec = CallRecord(
            index=0, ctx=ctx, kwargs={}, result=result, timestamp=1.0,
        )
        assert rec.index == 0
        assert rec.ctx is ctx
        assert rec.result is result
        assert rec.timestamp == 1.0
        assert rec.original_impl is None

    def test_session_id_present(self) -> None:
        rec = CallRecord(
            index=0, ctx=PipelineContext(), kwargs={"session_id": "sess-42"},
            result=_dummy_result(), timestamp=1.0,
        )
        assert rec.session_id == "sess-42"

    def test_session_id_absent(self) -> None:
        rec = CallRecord(
            index=0, ctx=PipelineContext(), kwargs={},
            result=_dummy_result(), timestamp=1.0,
        )
        assert rec.session_id is None

    def test_attempt_present(self) -> None:
        rec = CallRecord(
            index=0, ctx=PipelineContext(), kwargs={"attempt": 3},
            result=_dummy_result(), timestamp=1.0,
        )
        assert rec.attempt == 3

    def test_attempt_default(self) -> None:
        rec = CallRecord(
            index=0, ctx=PipelineContext(), kwargs={},
            result=_dummy_result(), timestamp=1.0,
        )
        assert rec.attempt == 1

    def test_succeeded_true(self) -> None:
        rec = CallRecord(
            index=0, ctx=PipelineContext(), kwargs={},
            result=_dummy_result(success=True), timestamp=1.0,
        )
        assert rec.succeeded is True

    def test_succeeded_false(self) -> None:
        rec = CallRecord(
            index=0, ctx=PipelineContext(), kwargs={},
            result=_dummy_result(success=False), timestamp=1.0,
        )
        assert rec.succeeded is False

    def test_context_had_present(self) -> None:
        ctx = _make_ctx(build=_dummy_result("build"))
        rec = CallRecord(
            index=0, ctx=ctx, kwargs={}, result=_dummy_result(), timestamp=1.0,
        )
        assert rec.context_had("build") is True

    def test_context_had_absent(self) -> None:
        rec = CallRecord(
            index=0, ctx=PipelineContext(), kwargs={},
            result=_dummy_result(), timestamp=1.0,
        )
        assert rec.context_had("build") is False


class TestVerifyAPI:
    """Placeholder — verify API is defined but exercised in a later step."""

    def test_verify_classes_importable(self) -> None:
        assert verify is not None
        assert Verifier is not None
        assert CallVerifier is not None

    def test_verify_rejects_non_mock(self) -> None:
        with pytest.raises(TypeError, match="verify\\(\\) requires a MockStage"):
            verify("not a mock")  # type: ignore[arg-type]

    def test_verify_returns_verifier(self) -> None:
        m = MockStage()
        v = verify(m)
        assert isinstance(v, Verifier)


class TestGlobalCallCounter:
    """Tests for the global call counter and reset."""

    def test_counter_increments(self) -> None:
        # The autouse fixture resets the counter before each test
        from norn.testing import _global_call_counter
        assert next(_global_call_counter) == 0
        assert next(_global_call_counter) == 1

    def test_reset_restarts_at_zero(self) -> None:
        from norn.testing import _global_call_counter
        next(_global_call_counter)  # consume 0
        reset_call_counter()
        from norn.testing import _global_call_counter as fresh
        assert next(fresh) == 0


class TestStageResultView:
    """Tests for the StageResultView wrapper."""

    def test_assert_success_passes(self) -> None:
        view = StageResultView(StageResult(name="s1", success=True, output="ok"))
        view.assert_success()  # should not raise

    def test_assert_success_fails(self) -> None:
        view = StageResultView(StageResult(name="s1", success=False, error="boom"))
        with pytest.raises(AssertionError, match="succeed.*boom"):
            view.assert_success()

    def test_assert_failed_passes(self) -> None:
        view = StageResultView(StageResult(name="s1", success=False, error="boom"))
        view.assert_failed()  # should not raise

    def test_assert_failed_fails(self) -> None:
        view = StageResultView(StageResult(name="s1", success=True, output="ok"))
        with pytest.raises(AssertionError, match="fail.*succeeded"):
            view.assert_failed()

    def test_assert_output_passes(self) -> None:
        view = StageResultView(StageResult(name="s1", success=True, output="hello"))
        view.assert_output("hello")

    def test_assert_output_fails(self) -> None:
        view = StageResultView(StageResult(name="s1", success=True, output="hello"))
        with pytest.raises(AssertionError, match="Expected output 'world'"):
            view.assert_output("world")

    def test_assert_output_contains_passes(self) -> None:
        view = StageResultView(StageResult(name="s1", success=True, output="hello world"))
        view.assert_output_contains("world")

    def test_assert_output_contains_fails(self) -> None:
        view = StageResultView(StageResult(name="s1", success=True, output="hello"))
        with pytest.raises(AssertionError, match="contain 'xyz'"):
            view.assert_output_contains("xyz")


class TestPipelineTestResult:
    """Tests for the PipelineTestResult wrapper."""

    def _make_result(self) -> PipelineTestResult:
        ctx = PipelineContext()
        ctx.results["step1"] = StageResult(name="step1", success=True, output="ok")
        ctx.results["step2"] = StageResult(name="step2", success=False, error="fail")
        mocks = {"step1": MockStage(), "step2": MockStage()}
        return PipelineTestResult(ctx, mocks)

    def test_stage_returns_view(self) -> None:
        result = self._make_result()
        view = result.stage("step1")
        assert isinstance(view, StageResultView)
        view.assert_success()

    def test_stage_missing_raises(self) -> None:
        result = self._make_result()
        with pytest.raises(KeyError, match="nope"):
            result.stage("nope")

    def test_mock_returns_mock(self) -> None:
        result = self._make_result()
        m = result.mock("step1")
        assert isinstance(m, MockStage)

    def test_mock_missing_raises(self) -> None:
        result = self._make_result()
        with pytest.raises(KeyError, match="nope"):
            result.mock("nope")

    def test_assert_completed_passes(self) -> None:
        ctx = PipelineContext()
        ctx.results["a"] = StageResult(name="a", success=True, output="ok")
        PipelineTestResult(ctx, {}).assert_completed()

    def test_assert_completed_fails(self) -> None:
        result = self._make_result()
        with pytest.raises(AssertionError, match="failed stages.*step2"):
            result.assert_completed()

    def test_assert_failed_at_passes(self) -> None:
        result = self._make_result()
        result.assert_failed_at("step2")

    def test_assert_failed_at_stage_missing(self) -> None:
        result = self._make_result()
        with pytest.raises(AssertionError, match="not found"):
            result.assert_failed_at("nope")

    def test_assert_failed_at_stage_succeeded(self) -> None:
        result = self._make_result()
        with pytest.raises(AssertionError, match="succeeded"):
            result.assert_failed_at("step1")


class TestPipelineTestRunner:
    """Tests for PipelineTestRunner fluent builder."""

    def test_fluent_chaining(self) -> None:
        from norn.dsl import Pipeline, Stage
        pipeline = Pipeline("test").stage("s1", MockStage())
        runner = PipelineTestRunner(pipeline)
        result = runner.patch("s1", MockStage()).with_param("k", "v").with_resume("sess-1")
        assert isinstance(result, PipelineTestRunner)

    @pytest.mark.asyncio
    async def test_run_with_pipeline_object(self) -> None:
        from norn.dsl import Pipeline, Stage
        m = MockStage().returns("done")
        pipeline = Pipeline("test").stage("s1", MockStage())
        result = await PipelineTestRunner(pipeline).patch("s1", m).run()
        assert isinstance(result, PipelineTestResult)
        result.stage("s1").assert_success()
        result.stage("s1").assert_output("done")

    @pytest.mark.asyncio
    async def test_run_with_params(self) -> None:
        from norn.dsl import Pipeline, Stage
        m = MockStage().returns("ok")
        pipeline = Pipeline("test").stage("s1", MockStage())
        result = await (
            PipelineTestRunner(pipeline)
            .patch("s1", m)
            .with_param("foo", "bar")
            .run()
        )
        result.assert_completed()
        assert result.ctx.params.get("foo") == "bar"


# ---------------------------------------------------------------------------
# Step 6: MockStage basics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mock_stage_returns_output():
    m = MockStage().returns("hello")
    ctx = PipelineContext()
    result = await m.run(ctx)
    assert result.success
    assert result.output == "hello"
    assert m.call_count == 1


@pytest.mark.asyncio
async def test_mock_stage_fails_then_succeeds():
    m = MockStage().fails(times=2, error="boom").then_returns("recovered")
    ctx = PipelineContext()

    r1 = await m.run(ctx)
    assert not r1.success
    assert r1.error == "boom"

    r2 = await m.run(ctx)
    assert not r2.success

    r3 = await m.run(ctx)
    assert r3.success
    assert r3.output == "recovered"

    assert m.call_count == 3


@pytest.mark.asyncio
async def test_mock_stage_always_fails():
    m = MockStage().always_fails(error="permanent")
    ctx = PipelineContext()
    for _ in range(5):
        r = await m.run(ctx)
        assert not r.success
        assert r.error == "permanent"


@pytest.mark.asyncio
async def test_mock_stage_as_agent_sets_needs_agent():
    m = MockStage().returns("code").as_agent()
    assert m.needs_agent is True
    ctx = PipelineContext()
    result = await m.run(ctx, session_id="sess-1", attempt=1)
    assert result.success
    assert result.usage is not None


@pytest.mark.asyncio
async def test_mock_stage_with_side_effect():
    results = []

    async def effect(ctx: PipelineContext, **kwargs: Any) -> StageResult:
        results.append(kwargs.get("attempt"))
        return StageResult(name="", success=True, output="side-effect")

    m = MockStage().with_side_effect(effect)
    ctx = PipelineContext()
    r = await m.run(ctx, attempt=3)
    assert r.output == "side-effect"
    assert results == [3]


@pytest.mark.asyncio
async def test_mock_stage_with_sync_side_effect():
    def effect(ctx: PipelineContext, **kwargs: Any) -> StageResult:
        return StageResult(name="", success=True, output="sync")

    m = MockStage().with_side_effect(effect)
    ctx = PipelineContext()
    r = await m.run(ctx)
    assert r.output == "sync"


# ---------------------------------------------------------------------------
# Step 6: CallRecord and context snapshots
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_record_captures_context_snapshot():
    """Context snapshot reflects state at call time, not after later stages."""
    m = MockStage().returns("ok")
    ctx = PipelineContext()
    ctx.results["prior"] = StageResult(name="prior", success=True, output="data")

    await m.run(ctx)

    # Add a result after the call
    ctx.results["later"] = StageResult(name="later", success=True, output="new")

    call = m.last_call
    assert call.context_had("prior")
    assert not call.context_had("later")


@pytest.mark.asyncio
async def test_call_record_global_ordering():
    """CallRecords across different mocks have globally ordered indices."""
    a = MockStage().returns("a")
    b = MockStage().returns("b")
    ctx = PipelineContext()

    await a.run(ctx)
    await b.run(ctx)
    await a.run(ctx)

    assert a.calls[0].index < b.calls[0].index
    assert b.calls[0].index < a.calls[1].index


@pytest.mark.asyncio
async def test_call_record_kwargs():
    m = MockStage().returns("ok").as_agent()
    ctx = PipelineContext()
    await m.run(ctx, session_id="s1", attempt=2)

    call = m.last_call
    assert call.session_id == "s1"
    assert call.attempt == 2


# ---------------------------------------------------------------------------
# Step 6: verify() API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_called_times():
    m = MockStage().returns("ok")
    ctx = PipelineContext()
    await m.run(ctx)
    await m.run(ctx)

    verify(m).called(times=2)

    with pytest.raises(AssertionError, match="Expected 3"):
        verify(m).called(times=3)


@pytest.mark.asyncio
async def test_verify_never_called():
    m = MockStage().returns("ok")
    verify(m).never_called()


@pytest.mark.asyncio
async def test_verify_at_least_at_most():
    m = MockStage().returns("ok")
    ctx = PipelineContext()
    await m.run(ctx)
    await m.run(ctx)

    verify(m).called(at_least=1)
    verify(m).called(at_most=3)
    verify(m).called(at_least=2, at_most=2)

    with pytest.raises(AssertionError, match="at least 5"):
        verify(m).called(at_least=5)


@pytest.mark.asyncio
async def test_verify_called_before_and_after():
    a = MockStage().returns("a")
    b = MockStage().returns("b")
    ctx = PipelineContext()

    await a.run(ctx)
    await b.run(ctx)

    verify(a).called_before(b)
    verify(b).called_after(a)

    with pytest.raises(AssertionError):
        verify(b).called_before(a)


@pytest.mark.asyncio
async def test_verify_received_context():
    m = MockStage().returns("ok")
    ctx = PipelineContext()
    ctx.results["spec"] = StageResult(name="spec", success=True, output="data")

    await m.run(ctx)

    verify(m).received_context(lambda c: c.get("spec") == "data")

    with pytest.raises(AssertionError, match="No call matched"):
        verify(m).received_context(lambda c: c.get("spec") == "wrong")


@pytest.mark.asyncio
async def test_verify_on_attempt():
    m = MockStage().fails(times=1, error="oops").then_returns("fixed")
    ctx = PipelineContext()

    await m.run(ctx)
    await m.run(ctx)

    verify(m).on_attempt(1).failed()
    verify(m).on_attempt(2).succeeded()


@pytest.mark.asyncio
async def test_verify_on_attempt_had_context():
    m = MockStage().returns("ok")
    ctx = PipelineContext()
    ctx.results["a"] = StageResult(name="a", success=True, output="x")
    await m.run(ctx)

    verify(m).on_attempt(1).had_context("a")
    verify(m).on_attempt(1).had_output("a", expected="x")

    with pytest.raises(AssertionError, match="Expected .* in context"):
        verify(m).on_attempt(1).had_context("missing")


# ---------------------------------------------------------------------------
# Step 6: Integration with Pipeline runner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mock_stage_in_pipeline():
    """MockStage works as a stage impl in a real pipeline run."""
    a = MockStage().returns("result-a")
    b = MockStage().returns("result-b")

    p = (
        Pipeline("test")
        .stage("a", a)
        .stage("b", b)
    )
    ctx = await run_pipeline(p)

    assert ctx.get("a") == "result-a"
    assert ctx.get("b") == "result-b"
    verify(a).called(times=1)
    verify(b).called(times=1)
    verify(a).called_before(b)

    # b should see a's result in its context
    verify(b).on_attempt(1).had_context("a")
    verify(b).on_attempt(1).had_output("a", expected="result-a")


@pytest.mark.asyncio
async def test_mock_stage_in_loop_with_retry():
    """MockStage correctly handles loop retries."""
    gen = MockStage().returns("code").as_agent()
    check = MockStage().fails(times=1, error="compile error").then_returns(
        {"stdout": "", "returncode": 0}
    )

    p = (
        Pipeline("test")
        .loop("build", max_retries=3, on_exhaust=OnFailure.FAIL, stages=[
            Stage("gen", gen),
            Stage("check", check),
        ])
    )
    ctx = await run_pipeline(p)

    verify(gen).called(times=2)
    verify(check).called(times=2)
    verify(check).on_attempt(1).failed()
    verify(check).on_attempt(2).succeeded()


@pytest.mark.asyncio
async def test_mock_stage_original_impl_via_patch():
    """patch_stages stashes the original impl on MockStage."""
    from norn.loader import load_pipeline
    from norn.testing import patch_stages

    pipeline = load_pipeline("norn/pipelines/hello.py")
    gen_mock = MockStage().returns("code").as_agent()
    patch_stages(pipeline, {"generate": gen_mock})

    assert gen_mock.original_impl is not None
    assert isinstance(gen_mock.original_impl, Generate)
    assert "{read_spec.output}" in gen_mock.original_impl.prompt


# ---------------------------------------------------------------------------
# Step 6: PipelineTestRunner integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_test_runner_inline():
    a = MockStage().returns("output-a")
    b = MockStage().returns("output-b")

    result = await (
        PipelineTestRunner(
            Pipeline("test")
            .stage("a", a)
            .stage("b", b)
        )
        .run()
    )

    result.assert_completed()
    result.stage("a").assert_success()
    result.stage("a").assert_output("output-a")
    result.stage("b").assert_output_contains("b")


@pytest.mark.asyncio
async def test_pipeline_test_runner_with_config():
    result = await (
        PipelineTestRunner("norn/pipelines/hello.py")
        .patch("read_spec", MockStage().returns("spec text"))
        .patch("generate", MockStage().returns("code").as_agent())
        .patch("generate_test", MockStage().returns("test code").as_agent())
        .patch("check", MockStage().returns({"stdout": "", "stderr": "", "returncode": 0}))
        .patch("test", MockStage().returns({"stdout": "1 passed", "stderr": "", "returncode": 0}))
        .run()
    )

    result.assert_completed()
    result.stage("generate").assert_success()

    # Access the mock and verify original impl
    gen_mock = result.mock("generate")
    assert gen_mock.original_impl is not None
    assert isinstance(gen_mock.original_impl, Generate)

    # Verify context flow
    verify(gen_mock).received_context(
        lambda ctx: ctx.get("read_spec") == "spec text"
    )


@pytest.mark.asyncio
async def test_pipeline_test_result_assert_failed_at():
    a = MockStage().always_fails(error="boom")

    with pytest.raises(Exception):
        await (
            PipelineTestRunner(
                Pipeline("test")
                .stage("a", a)
            )
            .run()
        )

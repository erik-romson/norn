"""Tests for norn/testing.py — test infrastructure."""

from __future__ import annotations

from typing import Any

import pytest

from norn.models import PipelineContext, StageResult, UsageRecord
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

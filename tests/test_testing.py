"""Tests for norn/testing.py — test infrastructure."""

from __future__ import annotations

from typing import Any

import pytest

from norn.models import PipelineContext, StageResult, UsageRecord
from norn.testing import (
    CallRecord,
    CallVerifier,
    MockStage,
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

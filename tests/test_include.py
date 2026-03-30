from __future__ import annotations

from unittest.mock import patch

import pytest

from norn.dsl import Include, Pipeline, Stage
from norn.models import PipelineContext, StageResult, UsageRecord
from norn.runner import PipelineError, run_pipeline
from norn.stages.base import BaseStage


class SuccessStage(BaseStage):
    def __init__(self, output: str = "ok") -> None:
        self._output = output

    async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
        return StageResult(name="", success=True, output=self._output)


class FailStage(BaseStage):
    async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
        return StageResult(name="", success=False, error="boom")


class ParamStage(BaseStage):
    """Returns the value of ctx.params['key'] as output."""

    async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
        return StageResult(name="", success=True, output=ctx.params.get("key"))


class UsageStage(BaseStage):
    needs_agent = True

    async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
        rec = UsageRecord(stage_name="", input_tokens=10, output_tokens=5, session_id="sub-session")
        return StageResult(name="", success=True, output="used", usage=rec)


# ---------------------------------------------------------------------------
# Inline include tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inline_include_stages_run_in_parent_context():
    sub = Pipeline("sub").stage("sub_s1", SuccessStage("from_sub"))

    with patch("norn.runner.load_pipeline", return_value=sub):
        p = Pipeline("parent").include("sub.py").stage("s2", SuccessStage("parent"))
        ctx = await run_pipeline(p)

    assert ctx.get("sub_s1") == "from_sub"
    assert ctx.get("s2") == "parent"


@pytest.mark.asyncio
async def test_inline_include_with_args_merges_into_params():
    sub = Pipeline("sub").stage("sub_s1", ParamStage())

    with patch("norn.runner.load_pipeline", return_value=sub):
        p = Pipeline("parent").include("sub.py", args={"key": "injected"})
        ctx = await run_pipeline(p)

    assert ctx.get("sub_s1") == "injected"


@pytest.mark.asyncio
async def test_inline_include_shares_session():
    """Inline include stages receive the same session_id as the parent."""
    received_sessions: list[str | None] = []

    class SessionCaptureStage(BaseStage):
        needs_agent = True

        async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
            received_sessions.append(kwargs.get("session_id"))
            rec = UsageRecord(stage_name="", session_id="sess-123")
            return StageResult(name="", success=True, output="ok", usage=rec)

    sub = Pipeline("sub").stage("sub_s1", SessionCaptureStage())

    with patch("norn.runner.load_pipeline", return_value=sub):
        p = (
            Pipeline("parent")
            .stage("s0", SessionCaptureStage())
            .include("sub.py")
        )
        await run_pipeline(p)

    # Both s0 and sub_s1 should have shared the captured session
    assert received_sessions[0] is None  # first call has no session yet
    assert received_sessions[1] == "sess-123"  # inline include reuses parent session


@pytest.mark.asyncio
async def test_inline_include_failure_propagates():
    sub = Pipeline("sub").stage("sub_fail", FailStage())

    with patch("norn.runner.load_pipeline", return_value=sub):
        p = Pipeline("parent").include("sub.py")
        with pytest.raises(PipelineError):
            await run_pipeline(p)


# ---------------------------------------------------------------------------
# Isolated include tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_isolated_include_sub_stages_run():
    sub = Pipeline("sub").stage("sub_s1", SuccessStage("isolated"))

    with patch("norn.runner.load_pipeline", return_value=sub):
        p = Pipeline("parent").include("sub.py", isolated=True, outputs=["sub_s1"])
        ctx = await run_pipeline(p)

    assert ctx.get("sub_s1") == "isolated"


@pytest.mark.asyncio
async def test_isolated_include_outputs_copied_to_parent():
    sub = Pipeline("sub").stage("sub_s1", SuccessStage("result")).stage("sub_s2", SuccessStage("other"))

    with patch("norn.runner.load_pipeline", return_value=sub):
        p = Pipeline("parent").include("sub.py", isolated=True, outputs=["sub_s1"])
        ctx = await run_pipeline(p)

    assert ctx.get("sub_s1") == "result"
    assert "sub_s2" not in ctx.results  # not in outputs, not copied


@pytest.mark.asyncio
async def test_isolated_include_without_outputs_nothing_copied():
    sub = Pipeline("sub").stage("sub_s1", SuccessStage("hidden"))

    with patch("norn.runner.load_pipeline", return_value=sub):
        p = Pipeline("parent").include("sub.py", isolated=True)
        ctx = await run_pipeline(p)

    assert "sub_s1" not in ctx.results


@pytest.mark.asyncio
async def test_isolated_include_merges_usage_records():
    sub = Pipeline("sub").stage("sub_s1", UsageStage())

    with patch("norn.runner.load_pipeline", return_value=sub):
        p = Pipeline("parent").include("sub.py", isolated=True)
        ctx = await run_pipeline(p)

    assert ctx.usage_tracker.total_input_tokens == 10
    assert ctx.usage_tracker.total_output_tokens == 5


@pytest.mark.asyncio
async def test_isolated_include_failure_propagates():
    sub = Pipeline("sub").stage("sub_fail", FailStage())

    with patch("norn.runner.load_pipeline", return_value=sub):
        p = Pipeline("parent").include("sub.py", isolated=True)
        with pytest.raises(PipelineError):
            await run_pipeline(p)


@pytest.mark.asyncio
async def test_isolated_include_with_args():
    sub = Pipeline("sub").stage("sub_s1", ParamStage())

    with patch("norn.runner.load_pipeline", return_value=sub):
        p = Pipeline("parent").include("sub.py", isolated=True, outputs=["sub_s1"], args={"key": "arg_value"})
        ctx = await run_pipeline(p)

    assert ctx.get("sub_s1") == "arg_value"


@pytest.mark.asyncio
async def test_isolated_include_does_not_pollute_parent_context():
    """Stages in isolated sub-pipeline that are not in outputs don't appear in parent ctx."""
    sub = (
        Pipeline("sub")
        .stage("sub_s1", SuccessStage("a"))
        .stage("sub_s2", SuccessStage("b"))
    )

    with patch("norn.runner.load_pipeline", return_value=sub):
        p = (
            Pipeline("parent")
            .stage("p1", SuccessStage("p_val"))
            .include("sub.py", isolated=True)
            .stage("p2", SuccessStage("p_val2"))
        )
        ctx = await run_pipeline(p)

    assert ctx.get("p1") == "p_val"
    assert ctx.get("p2") == "p_val2"
    assert "sub_s1" not in ctx.results
    assert "sub_s2" not in ctx.results


# ---------------------------------------------------------------------------
# DSL tests
# ---------------------------------------------------------------------------


def test_pipeline_include_adds_include_item():
    p = Pipeline("test").include("sub.py")
    assert len(p.items) == 1
    item = p.items[0]
    assert isinstance(item, Include)
    assert item.path == "sub.py"
    assert item.isolated is False
    assert item.outputs == []
    assert item.args == {}


def test_pipeline_include_isolated_with_options():
    p = Pipeline("test").include("sub.py", isolated=True, outputs=["s1"], args={"k": "v"})
    item = p.items[0]
    assert isinstance(item, Include)
    assert item.isolated is True
    assert item.outputs == ["s1"]
    assert item.args == {"k": "v"}


def test_pipeline_include_is_fluent():
    p = Pipeline("test").include("a.py").stage("s1", SuccessStage()).include("b.py")
    assert len(p.items) == 3

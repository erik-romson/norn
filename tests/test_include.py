from __future__ import annotations

from unittest.mock import patch

import pytest

from norn.dsl import Include, Pipeline, Stage
from norn.event_sink import EventSink
from norn.events import StageFinished, StageStarted
from norn.models import PipelineContext, StageResult, UsageRecord
from norn.run_control import CancelledError as RunCancelledError, RunController
from norn.runner import PipelineError, run_pipeline
from norn.stages.base import BaseStage


class WorkingDirCaptureStage(BaseStage):
    """Records the working_dir from the context it was called with."""

    captured_working_dir: str | None = None

    async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
        WorkingDirCaptureStage.captured_working_dir = ctx.working_dir
        return StageResult(name="", success=True, output="captured")


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


@pytest.mark.asyncio
async def test_isolated_include_forwards_working_dir():
    """Isolated includes receive and run under the parent's working_dir."""
    WorkingDirCaptureStage.captured_working_dir = "sentinel"  # ensure it was written
    sub = Pipeline("sub").stage("sub_wd", WorkingDirCaptureStage())

    with patch("norn.runner.load_pipeline", return_value=sub):
        p = Pipeline("parent").include("sub.py", isolated=True, outputs=["sub_wd"])
        ctx = await run_pipeline(p, working_dir="/some/worktree/path")

    assert WorkingDirCaptureStage.captured_working_dir == "/some/worktree/path"


@pytest.mark.asyncio
async def test_isolated_include_forwards_run_id():
    """Isolated includes share the parent's run_id."""
    captured_run_id: list[str | None] = []

    class RunIdCaptureStage(BaseStage):
        async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
            captured_run_id.append(ctx.run_id)
            return StageResult(name="", success=True, output="ok")

    sub = Pipeline("sub").stage("sub_rid", RunIdCaptureStage())

    with patch("norn.runner.load_pipeline", return_value=sub):
        p = Pipeline("parent").include("sub.py", isolated=True)
        await run_pipeline(p, run_id="my-run-42")

    assert captured_run_id == ["my-run-42"]


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


# ---------------------------------------------------------------------------
# Frontend seam forwarding tests (event_sink, input_responder, run_controller)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_isolated_include_events_arrive_in_parent_sink():
    """StageStarted/StageFinished events from the sub-pipeline arrive in the parent sink.

    Without forwarding event_sink the sub-run builds its own CLIRenderer and writes
    Rich escape sequences to stdout while the TUI owns the terminal.  With the fix the
    sub-pipeline emits into the same sink and every consumer (TUI, CLIRenderer) sees
    the sub-stage events.
    """
    sink = EventSink()
    sub = Pipeline("sub").stage("sub_stage", SuccessStage("sub_out"))

    with patch("norn.runner.load_pipeline", return_value=sub):
        p = Pipeline("parent").stage("par_stage", SuccessStage()).include("sub.py", isolated=True)
        await run_pipeline(p, event_sink=sink)

    stage_names_started = {e.name for e in sink.lossless_events if isinstance(e, StageStarted)}
    stage_names_finished = {e.name for e in sink.lossless_events if isinstance(e, StageFinished)}
    assert "sub_stage" in stage_names_started, (
        f"Expected sub_stage in StageStarted events, got: {stage_names_started}"
    )
    assert "sub_stage" in stage_names_finished, (
        f"Expected sub_stage in StageFinished events, got: {stage_names_finished}"
    )


@pytest.mark.asyncio
async def test_isolated_include_forwards_run_controller_and_responder():
    """The sub-pipeline context receives the same run_controller and input_responder.

    Without forwarding these, pause/cancel and ask_user calls inside the isolated
    include use a discarded private responder/controller, silently bypassing the UI.
    """
    parent_sink = EventSink()
    parent_controller = RunController()
    # Sentinel object — no responder methods are called in this simple test
    parent_responder = object()

    class SeamCaptureStage(BaseStage):
        """Captures the three frontend seams from whatever context it runs in."""

        captured_sink: object = None
        captured_controller: object = None
        captured_responder: object = None

        async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
            SeamCaptureStage.captured_sink = ctx.event_sink
            SeamCaptureStage.captured_controller = ctx.run_controller
            SeamCaptureStage.captured_responder = ctx.input_responder
            return StageResult(name="", success=True, output="ok")

    SeamCaptureStage.captured_sink = None
    SeamCaptureStage.captured_controller = None
    SeamCaptureStage.captured_responder = None

    sub = Pipeline("sub").stage("cap", SeamCaptureStage())

    with patch("norn.runner.load_pipeline", return_value=sub):
        p = Pipeline("parent").include("sub.py", isolated=True)
        await run_pipeline(
            p,
            event_sink=parent_sink,
            run_controller=parent_controller,
            input_responder=parent_responder,
        )

    assert SeamCaptureStage.captured_sink is parent_sink, (
        "Sub-pipeline did not inherit the parent's event_sink"
    )
    assert SeamCaptureStage.captured_controller is parent_controller, (
        "Sub-pipeline did not inherit the parent's run_controller"
    )
    assert SeamCaptureStage.captured_responder is parent_responder, (
        "Sub-pipeline did not inherit the parent's input_responder"
    )


@pytest.mark.asyncio
async def test_isolated_include_cancel_propagates_to_sub_run():
    """A cancel requested during an isolated include stops the remaining sub-stages.

    Without forwarding run_controller, ``_cooperative_pause`` in the sub-run sees
    ``ctrl=None`` and returns immediately — every sub-stage executes even after
    cancel().  With the fix the same controller is shared, so cancel() halts the
    sub-run at the next inter-stage pause checkpoint.
    """
    controller = RunController()
    ran_stages: list[str] = []

    class CancelStage(BaseStage):
        """Runs, then cancels the shared run_controller."""

        async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
            ran_stages.append("cancel_stage")
            # Cancel via the shared controller — the next sub-stage must NOT run.
            ctx.run_controller.cancel()
            return StageResult(name="", success=True, output="ok")

    class AfterCancelStage(BaseStage):
        """Must not execute after cancel_stage cancels the run."""

        async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
            ran_stages.append("after_stage")
            return StageResult(name="", success=True, output="ok")

    sub = (
        Pipeline("sub")
        .stage("s1", CancelStage())
        .stage("s2", AfterCancelStage())
    )

    with patch("norn.runner.load_pipeline", return_value=sub):
        p = Pipeline("parent").include("sub.py", isolated=True)
        with pytest.raises(RunCancelledError):
            await run_pipeline(p, run_controller=controller)

    assert "cancel_stage" in ran_stages, "CancelStage never ran"
    assert "after_stage" not in ran_stages, (
        "Cancel did not stop the sub-run — run_controller was not forwarded"
    )

import pytest

from norn.dsl import Loop, OnFailure, Pipeline, Stage
from norn.models import PipelineContext, StageResult, UsageRecord
from norn.runner import run_pipeline
from norn.stages.base import BaseStage
from typing import Any


class FakeGenerate(BaseStage):
    """Simulates a Generate stage that produces usage records with session tracking."""

    needs_agent = True

    def __init__(self, *, fail_count: int = 0, session_id: str = "fake-session") -> None:
        self._fail_count = fail_count
        self._calls = 0
        self._session_id = session_id
        self.received_sessions: list[str | None] = []
        self.received_attempts: list[int] = []

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        self._calls += 1
        session = kwargs.get("session_id")
        attempt = kwargs.get("attempt", 1)
        self.received_sessions.append(session)
        self.received_attempts.append(attempt)

        usage = UsageRecord(
            stage_name="",
            session_id=self._session_id,
            input_tokens=100 * self._calls,
            output_tokens=50 * self._calls,
            total_cost_usd=0.001 * self._calls,
            duration_ms=1000,
            duration_api_ms=800,
            num_turns=1,
            attempt=attempt,
        )

        if self._calls <= self._fail_count:
            return StageResult(name="", success=False, error=f"fail #{self._calls}", usage=usage)
        return StageResult(name="", success=True, output="generated code", usage=usage)


class SuccessStage(BaseStage):
    """Always succeeds, no usage."""

    def __init__(self, output: str = "ok") -> None:
        self._output = output

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        return StageResult(name="", success=True, output=self._output)


@pytest.mark.asyncio
async def test_usage_tracked_in_simple_pipeline():
    gen = FakeGenerate()
    p = Pipeline("test").stage("gen", gen)
    ctx = await run_pipeline(p)

    assert len(ctx.usage_tracker.records) == 1
    assert ctx.usage_tracker.total_input_tokens == 100
    assert ctx.usage_tracker.total_output_tokens == 50
    assert ctx.usage_tracker.total_cost_usd == pytest.approx(0.001)


@pytest.mark.asyncio
async def test_usage_accumulated_across_loop_retries():
    gen = FakeGenerate(fail_count=2)
    p = Pipeline("test").loop(
        "retry",
        max_retries=3,
        on_exhaust=OnFailure.FAIL,
        stages=[Stage("gen", gen)],
    )
    ctx = await run_pipeline(p)

    # 3 calls: 2 failures + 1 success
    assert len(ctx.usage_tracker.records) == 3
    assert ctx.usage_tracker.total_input_tokens == 100 + 200 + 300
    assert ctx.usage_tracker.total_cost_usd == pytest.approx(0.001 + 0.002 + 0.003)


@pytest.mark.asyncio
async def test_attempt_numbers_passed_to_generate():
    gen = FakeGenerate(fail_count=1)
    p = Pipeline("test").loop(
        "retry",
        max_retries=3,
        stages=[Stage("gen", gen)],
    )
    await run_pipeline(p)

    assert gen.received_attempts == [1, 2]


@pytest.mark.asyncio
async def test_clear_context_preserves_usage():
    gen1 = FakeGenerate(session_id="session-1")
    gen2 = FakeGenerate(session_id="session-2")
    p = (
        Pipeline("test")
        .stage("gen1", gen1)
        .clear_context()
        .stage("gen2", gen2)
    )
    ctx = await run_pipeline(p)

    assert len(ctx.usage_tracker.records) == 2
    assert ctx.usage_tracker.unique_sessions == 2
    assert ctx.usage_tracker.total_input_tokens == 200  # 100 + 100


@pytest.mark.asyncio
async def test_non_agent_stages_no_usage():
    p = (
        Pipeline("test")
        .stage("s1", SuccessStage("hello"))
        .stage("s2", SuccessStage("world"))
    )
    ctx = await run_pipeline(p)

    assert len(ctx.usage_tracker.records) == 0
    assert ctx.usage_tracker.total_tokens == 0


@pytest.mark.asyncio
async def test_loop_mixed_stages_usage():
    """Only the Generate stage produces usage records, not RunCommand-like stages."""
    gen = FakeGenerate()
    p = Pipeline("test").loop(
        "build",
        max_retries=2,
        stages=[
            Stage("gen", gen),
            Stage("check", SuccessStage("compiled")),
        ],
    )
    ctx = await run_pipeline(p)

    assert len(ctx.usage_tracker.records) == 1
    assert ctx.usage_tracker.records[0].stage_name == "gen"


@pytest.mark.asyncio
async def test_stage_result_carries_usage():
    gen = FakeGenerate()
    p = Pipeline("test").stage("gen", gen)
    ctx = await run_pipeline(p)

    result = ctx.results["gen"]
    assert result.usage is not None
    assert result.usage.session_id == "fake-session"
    assert result.usage.input_tokens == 100


@pytest.mark.asyncio
async def test_continue_session_reruns_all_stages():
    """--continue passes resume_session but no checkpoint, so all stages re-run."""
    gen = FakeGenerate(session_id="continued-session")
    p = Pipeline("test").stage("gen", gen)

    # First run
    ctx1 = await run_pipeline(p)
    session_id = ctx1.usage_tracker.last_session_id
    assert session_id == "continued-session"

    # Second run with --continue (resume_session only, no checkpoint)
    gen2 = FakeGenerate(session_id="continued-session-2")
    p2 = Pipeline("test").stage("gen", gen2)
    ctx2 = await run_pipeline(p2, resume_session=session_id)

    # Stage re-ran (not skipped) and received the prior session_id
    assert gen2.received_sessions == [session_id]
    assert ctx2.get("gen") == "generated code"
    assert len(ctx2.usage_tracker.records) == 1


@pytest.mark.asyncio
async def test_resume_checkpoint_skips_completed_stages():
    """--resume with checkpoint skips completed stages."""
    from norn.checkpoint import Checkpoint

    gen = FakeGenerate(session_id="new-session")
    p = Pipeline("test").stage("gen", gen)

    checkpoint = Checkpoint(
        pipeline="test",
        timestamp="2026-01-01T00:00:00Z",
        session_id="old-session",
        completed_stages=["gen"],
        results={"gen": "cached-output"},
        usage=[],
    )
    ctx = await run_pipeline(p, resume_session="old-session", resume_checkpoint=checkpoint)

    # Stage was skipped (cached), gen never ran
    assert gen._calls == 0
    assert ctx.get("gen") == "cached-output"


@pytest.mark.asyncio
async def test_resume_drops_partial_loop_cache():
    """A loop that crashed mid-attempt must not have its body stages
    restored from the checkpoint — they need to re-run with fresh
    outputs. The whole-loop cache (all body stages present) is still
    honored.
    """
    from norn.checkpoint import Checkpoint

    class CountingStage(BaseStage):
        def __init__(self, *, fail: bool = False) -> None:
            self.calls = 0
            self._fail = fail

        async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
            self.calls += 1
            return StageResult(name="", success=not self._fail, output="ran")

    compress = CountingStage()
    fix = CountingStage()
    test_stage = CountingStage(fail=True)
    p = (
        Pipeline("test")
        .loop(
            "build_loop",
            max_retries=1,
            on_exhaust=OnFailure.FAIL,
            stages=[
                Stage("compress", compress),
                Stage("fix", fix),
                Stage("test", test_stage),
            ],
        )
    )

    # Prior run got partway through the loop: compress + fix passed,
    # then test failed and retries were exhausted. The partial successes
    # leaked into the checkpoint — exactly the bug we're fixing.
    partial_checkpoint = Checkpoint(
        pipeline="test",
        timestamp="2026-01-01T00:00:00Z",
        session_id=None,
        completed_stages=["compress", "fix"],
        results={"compress": "stale", "fix": "stale"},
        usage=[],
    )

    from norn.runner import RetriesExhaustedError

    with pytest.raises(RetriesExhaustedError):
        await run_pipeline(p, resume_checkpoint=partial_checkpoint)

    # All three loop body stages re-ran fresh — the partial cache was dropped.
    assert compress.calls == 1, "compress must re-run, not be cached"
    assert fix.calls == 1, "fix must re-run, not be cached"
    assert test_stage.calls == 1


@pytest.mark.asyncio
async def test_resume_keeps_complete_loop_cache():
    """When every body stage of a loop is in the checkpoint, the loop
    is treated as fully cached (all stages skipped, loop succeeds on
    attempt 1 without running anything)."""
    from norn.checkpoint import Checkpoint

    class CountingStage(BaseStage):
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
            self.calls += 1
            return StageResult(name="", success=True, output="ran")

    a, b, c = CountingStage(), CountingStage(), CountingStage()
    p = Pipeline("test").loop(
        "build_loop",
        max_retries=2,
        on_exhaust=OnFailure.FAIL,
        stages=[Stage("a", a), Stage("b", b), Stage("c", c)],
    )

    full_checkpoint = Checkpoint(
        pipeline="test",
        timestamp="2026-01-01T00:00:00Z",
        session_id=None,
        completed_stages=["a", "b", "c"],
        results={"a": "ok", "b": "ok", "c": "ok"},
        usage=[],
    )
    await run_pipeline(p, resume_checkpoint=full_checkpoint)

    assert a.calls == 0 and b.calls == 0 and c.calls == 0


# ---------------------------------------------------------------------------
# MCP tools — runner creates MCP server and passes mcp_servers kwarg
# ---------------------------------------------------------------------------


class McpToolStage(BaseStage):
    """Agent stage that declares mcp_tools and records the mcp_tools kwarg."""

    needs_agent = True
    mcp_tools: list = []  # will be set per-test

    def __init__(self, tools: list) -> None:
        self.mcp_tools = tools
        self.received_mcp_tools: list | None = None

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        self.received_mcp_tools = kwargs.get("mcp_tools")
        return StageResult(name="", success=True, output="ok")


class NoMcpToolStage(BaseStage):
    """Agent stage with no mcp_tools."""

    needs_agent = True

    def __init__(self) -> None:
        self.received_mcp_tools: list | None = None

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        self.received_mcp_tools = kwargs.get("mcp_tools")
        return StageResult(name="", success=True, output="ok")


@pytest.mark.asyncio
async def test_runner_passes_mcp_tools_when_mcp_tools_set():
    """Runner passes mcp_tools kwarg to the stage without importing Claude SDK."""
    from unittest.mock import MagicMock

    fake_tool = MagicMock()
    stage = McpToolStage(tools=[fake_tool])
    p = Pipeline("test").stage("stage_name", stage)

    ctx = await run_pipeline(p)

    assert ctx.get("stage_name") == "ok"
    assert stage.received_mcp_tools == [fake_tool]


@pytest.mark.asyncio
async def test_runner_no_mcp_tools_kwarg_when_no_mcp_tools():
    """Runner does not pass mcp_tools when a stage has no mcp_tools."""
    stage = NoMcpToolStage()
    p = Pipeline("test").stage("s1", stage)

    ctx = await run_pipeline(p)

    assert ctx.get("s1") == "ok"
    assert stage.received_mcp_tools is None


@pytest.mark.asyncio
async def test_runner_fails_mcp_tools_with_non_claude_provider():
    """Runner returns a clear stage failure when mcp_tools are used with a non-claude-code provider."""
    from unittest.mock import MagicMock
    from norn.runner import PipelineError

    fake_tool = MagicMock()
    stage = McpToolStage(tools=[fake_tool])
    p = Pipeline("test").stage("stage_name", stage)

    with pytest.raises(PipelineError) as exc_info:
        await run_pipeline(p, agent_provider="opencode")

    result = exc_info.value.result
    assert not result.success
    assert "opencode" in result.error
    assert "MCP" in result.error or "mcp_tools" in result.error

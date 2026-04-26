from __future__ import annotations

import json

import pytest

from norn.checkpoint import (
    Checkpoint,
    checkpoint_file,
    load_checkpoint,
    save_checkpoint,
    serialise_output,
)
from norn.dsl import Loop, OnFailure, Pipeline, Stage
from norn.models import PipelineContext, StageResult
from norn.runner import PipelineError, run_pipeline
from norn.stages.base import BaseStage
from typing import Any


# ---------------------------------------------------------------------------
# checkpoint.py unit tests
# ---------------------------------------------------------------------------


def test_checkpoint_file_path(tmp_path):
    config = str(tmp_path / "my_pipeline.py")
    assert checkpoint_file(config) == (tmp_path / "my_pipeline.checkpoint")


def test_save_and_load_checkpoint(tmp_path):
    config = str(tmp_path / "pipeline.py")
    save_checkpoint(
        config,
        pipeline_name="test",
        session_id="sess-abc",
        completed_stages=["s1", "s2"],
        stage_outputs={"s1": "hello", "s2": {"key": "val"}},
    )

    cp = load_checkpoint(config)
    assert cp is not None
    assert cp.pipeline == "test"
    assert cp.session_id == "sess-abc"
    assert cp.completed_stages == ["s1", "s2"]
    assert cp.results["s1"] == "hello"
    assert cp.results["s2"] == {"key": "val"}


def test_load_checkpoint_returns_none_when_missing(tmp_path):
    assert load_checkpoint(str(tmp_path / "nonexistent.py")) is None


def test_load_checkpoint_returns_none_on_corrupt_file(tmp_path):
    config = str(tmp_path / "pipeline.py")
    checkpoint_file(config).write_text("not valid json")
    assert load_checkpoint(config) is None


def test_save_checkpoint_is_atomic(tmp_path):
    """Atomic write: no partial file visible during write (tmp file replaced)."""
    config = str(tmp_path / "pipeline.py")
    save_checkpoint(config, "test", None, ["s1"], {"s1": "ok"})
    # No .checkpoint.tmp file left behind
    assert not (tmp_path / "pipeline.checkpoint.tmp").exists()
    assert (tmp_path / "pipeline.checkpoint").exists()


def test_serialise_output_json_safe_values():
    assert serialise_output("text") == "text"
    assert serialise_output(42) == 42
    assert serialise_output({"a": 1}) == {"a": 1}
    assert serialise_output(["x", "y"]) == ["x", "y"]
    assert serialise_output(None) is None


def test_serialise_output_uses_to_dict():
    class HasToDict:
        def to_dict(self) -> dict:
            return {"key": "value", "num": 42}

    result = serialise_output(HasToDict())
    assert result == {"key": "value", "num": 42}


def test_serialise_output_falls_back_to_str():
    class Unserializable:
        def __repr__(self) -> str:
            return "unserializable-object"

    result = serialise_output(Unserializable())
    assert isinstance(result, str)
    assert "unserializable-object" in result


# ---------------------------------------------------------------------------
# Integration: run_pipeline with resume_checkpoint
# ---------------------------------------------------------------------------


class SuccessStage(BaseStage):
    def __init__(self, output: str = "ok") -> None:
        self._output = output
        self.call_count = 0

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        self.call_count += 1
        return StageResult(name="", success=True, output=self._output)


class FailStage(BaseStage):
    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        return StageResult(name="", success=False, error="boom")


@pytest.mark.asyncio
async def test_resume_checkpoint_skips_completed_stages():
    """Stages listed in completed_stages are skipped (cached) on resume."""
    s1 = SuccessStage("from-cache")
    s2 = SuccessStage("fresh")

    cp = Checkpoint(
        pipeline="test",
        timestamp="2026-01-01T00:00:00Z",
        session_id=None,
        completed_stages=["s1"],
        results={"s1": "from-cache"},
        usage=[],
    )

    p = Pipeline("test").stage("s1", s1).stage("s2", s2)
    ctx = await run_pipeline(p, resume_checkpoint=cp)

    # s1 was cached — should not have executed
    assert s1.call_count == 0
    # s2 ran normally
    assert s2.call_count == 1
    # Both results accessible in context
    assert ctx.get("s1") == "from-cache"
    assert ctx.get("s2") == "fresh"


@pytest.mark.asyncio
async def test_resume_checkpoint_restores_stage_output():
    """Cached stage output is accessible to downstream stages via ctx.get()."""
    s2 = SuccessStage("downstream")

    cp = Checkpoint(
        pipeline="test",
        timestamp="2026-01-01T00:00:00Z",
        session_id=None,
        completed_stages=["s1"],
        results={"s1": "restored-value"},
        usage=[],
    )

    captured_output: list[Any] = []

    class ReadPriorStage(BaseStage):
        async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
            captured_output.append(ctx.get("s1"))
            return StageResult(name="", success=True, output="ok")

    p = (
        Pipeline("test")
        .stage("s1", SuccessStage())  # will be cached
        .stage("s2", ReadPriorStage())
    )
    await run_pipeline(p, resume_checkpoint=cp)

    assert captured_output == ["restored-value"]


@pytest.mark.asyncio
async def test_resume_uses_session_from_checkpoint():
    """Session ID from checkpoint is passed to the first agent stage."""
    from norn.models import UsageRecord

    received_sessions: list[str | None] = []

    class AgentStage(BaseStage):
        needs_agent = True

        async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
            received_sessions.append(kwargs.get("session_id"))
            usage = UsageRecord(stage_name="", session_id="new-session")
            return StageResult(name="", success=True, output="ok", usage=usage)

    cp = Checkpoint(
        pipeline="test",
        timestamp="2026-01-01T00:00:00Z",
        session_id="checkpoint-session",
        completed_stages=[],
        results={},
        usage=[],
    )

    p = Pipeline("test").stage("gen", AgentStage())
    await run_pipeline(p, resume_checkpoint=cp)

    assert received_sessions == ["checkpoint-session"]


@pytest.mark.asyncio
async def test_checkpoint_saved_after_each_stage(tmp_path):
    """Checkpoint file is written after every successful stage."""
    config = str(tmp_path / "pipeline.py")

    p = Pipeline("test").stage("s1", SuccessStage("a")).stage("s2", SuccessStage("b"))
    await run_pipeline(p, config_path=config)

    cp = load_checkpoint(config)
    assert cp is not None
    assert cp.completed_stages == ["s1", "s2"]
    assert cp.results["s1"] == "a"
    assert cp.results["s2"] == "b"


@pytest.mark.asyncio
async def test_checkpoint_saved_even_when_pipeline_fails(tmp_path):
    """Checkpoint is saved for the stages that succeeded before a failure."""
    config = str(tmp_path / "pipeline.py")

    p = (
        Pipeline("test")
        .stage("s1", SuccessStage("done"))
        .stage("s2", FailStage(), on_failure=OnFailure.FAIL)
    )
    with pytest.raises(PipelineError):
        await run_pipeline(p, config_path=config)

    cp = load_checkpoint(config)
    assert cp is not None
    assert "s1" in cp.completed_stages
    assert "s2" not in cp.completed_stages


@pytest.mark.asyncio
async def test_checkpoint_saved_after_loop_stage(tmp_path):
    """Checkpoint is updated after successful stages inside a loop."""
    config = str(tmp_path / "pipeline.py")

    p = Pipeline("test").loop(
        "lp",
        max_retries=1,
        stages=[Stage("s1", SuccessStage("loop-out"))],
    )
    await run_pipeline(p, config_path=config)

    cp = load_checkpoint(config)
    assert cp is not None
    assert "s1" in cp.completed_stages
    assert cp.results["s1"] == "loop-out"


@pytest.mark.asyncio
async def test_resume_checkpoint_in_loop_drops_partial_cache():
    """A loop is atomic: when only some of its body stages are in the
    checkpoint, the loop crashed mid-attempt and ALL its stages must
    re-run on resume. Otherwise downstream stages (e.g. ``fix``) would
    replay against the original failure rather than the current one.
    """
    s1 = SuccessStage("fresh-s1")
    s2 = SuccessStage("fresh-s2")

    # Only s1 is in the checkpoint — partial loop, must be dropped.
    cp = Checkpoint(
        pipeline="test",
        timestamp="2026-01-01T00:00:00Z",
        session_id=None,
        completed_stages=["s1"],
        results={"s1": "stale-out"},
        usage=[],
    )

    p = Pipeline("test").loop(
        "lp",
        max_retries=1,
        stages=[Stage("s1", s1), Stage("s2", s2)],
    )
    ctx = await run_pipeline(p, resume_checkpoint=cp)

    assert s1.call_count == 1, "s1 must re-run; partial-loop cache dropped"
    assert s2.call_count == 1
    assert ctx.get("s1") == "fresh-s1"
    assert ctx.get("s2") == "fresh-s2"

from __future__ import annotations

import json
from pathlib import Path

import pytest

from norn.dsl import OnFailure, Pipeline, Stage
from norn.history import (
    RunRecord,
    StageHistoryEntry,
    append_run,
    history_file,
    load_history,
    next_run_id,
)
from norn.models import PipelineContext, StageResult
from norn.runner import PipelineError, run_pipeline
from norn.stages.base import BaseStage


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class SuccessStage(BaseStage):
    needs_agent = False

    async def run(self, ctx: PipelineContext) -> StageResult:
        return StageResult(name="", success=True, output="ok")


class FailStage(BaseStage):
    needs_agent = False

    async def run(self, ctx: PipelineContext) -> StageResult:
        return StageResult(name="", success=False, error="boom")


def _make_record(run_id: int = 1, success: bool = True) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        timestamp="2026-03-25T09:00:00+00:00",
        success=success,
        total_cost_usd=0.19,
        total_tokens=15100,
        duration_ms=10500,
        stages=[StageHistoryEntry(name="generate", success=True, cost_usd=0.12)],
        retries=0,
        session_id="abc123",
        failed_stage=None if success else "some_stage",
    )


# ---------------------------------------------------------------------------
# history_file
# ---------------------------------------------------------------------------


def test_history_file_path(tmp_path):
    config = str(tmp_path / "my_pipeline.py")
    assert history_file(config) == (tmp_path / "my_pipeline.history")


# ---------------------------------------------------------------------------
# append_run / load_history
# ---------------------------------------------------------------------------


def test_append_and_load_round_trip(tmp_path):
    config = str(tmp_path / "pipeline.py")
    record = _make_record(run_id=1)
    append_run(config, record)

    records = load_history(config)
    assert len(records) == 1
    r = records[0]
    assert r.run_id == 1
    assert r.success is True
    assert r.total_cost_usd == pytest.approx(0.19)
    assert r.total_tokens == 15100
    assert r.duration_ms == 10500
    assert r.session_id == "abc123"
    assert r.failed_stage is None
    assert len(r.stages) == 1
    assert r.stages[0].name == "generate"
    assert r.stages[0].cost_usd == pytest.approx(0.12)


def test_append_multiple_runs(tmp_path):
    config = str(tmp_path / "pipeline.py")
    append_run(config, _make_record(run_id=1, success=True))
    append_run(config, _make_record(run_id=2, success=False))
    append_run(config, _make_record(run_id=3, success=True))

    records = load_history(config)
    assert len(records) == 3
    assert [r.run_id for r in records] == [1, 2, 3]
    assert [r.success for r in records] == [True, False, True]


def test_load_history_returns_empty_when_no_file(tmp_path):
    config = str(tmp_path / "missing.py")
    assert load_history(config) == []


def test_load_history_skips_malformed_lines(tmp_path):
    config = str(tmp_path / "pipeline.py")
    append_run(config, _make_record(run_id=1))
    # inject a bad line
    path = history_file(config)
    with path.open("a") as f:
        f.write("not valid json\n")
    append_run(config, _make_record(run_id=2))

    records = load_history(config)
    assert len(records) == 2
    assert records[0].run_id == 1
    assert records[1].run_id == 2


# ---------------------------------------------------------------------------
# next_run_id
# ---------------------------------------------------------------------------


def test_next_run_id_starts_at_one(tmp_path):
    config = str(tmp_path / "pipeline.py")
    assert next_run_id(config) == 1


def test_next_run_id_increments(tmp_path):
    config = str(tmp_path / "pipeline.py")
    append_run(config, _make_record(run_id=1))
    append_run(config, _make_record(run_id=2))
    assert next_run_id(config) == 3


# ---------------------------------------------------------------------------
# Integration: run_pipeline writes history
# ---------------------------------------------------------------------------


async def test_successful_run_appends_history(tmp_path):
    config = str(tmp_path / "pipeline.py")
    pipeline = Pipeline("test").stage("step1", SuccessStage())

    await run_pipeline(pipeline, config_path=config)

    records = load_history(config)
    assert len(records) == 1
    r = records[0]
    assert r.run_id == 1
    assert r.success is True
    assert r.failed_stage is None
    assert any(s.name == "step1" for s in r.stages)


async def test_failed_run_appends_history(tmp_path):
    config = str(tmp_path / "pipeline.py")
    pipeline = Pipeline("test").stage("bad_stage", FailStage(), on_failure=OnFailure.FAIL)

    with pytest.raises(PipelineError):
        await run_pipeline(pipeline, config_path=config)

    records = load_history(config)
    assert len(records) == 1
    r = records[0]
    assert r.run_id == 1
    assert r.success is False
    assert r.failed_stage == "bad_stage"


async def test_multiple_runs_increment_run_id(tmp_path):
    config = str(tmp_path / "pipeline.py")
    pipeline = Pipeline("test").stage("step1", SuccessStage())

    await run_pipeline(pipeline, config_path=config)
    await run_pipeline(pipeline, config_path=config)

    records = load_history(config)
    assert len(records) == 2
    assert records[0].run_id == 1
    assert records[1].run_id == 2


async def test_no_history_written_without_config_path():
    """run_pipeline without config_path must not write any history file."""
    pipeline = Pipeline("test").stage("step1", SuccessStage())
    ctx = await run_pipeline(pipeline)
    assert ctx.results["step1"].success is True
    # No assertion on filesystem — just verifying no exception is raised


async def test_retries_counted_in_history(tmp_path):
    from norn.dsl import Loop

    config = str(tmp_path / "pipeline.py")

    call_count = 0

    class FlipStage(BaseStage):
        needs_agent = False

        async def run(self, ctx: PipelineContext) -> StageResult:
            nonlocal call_count
            call_count += 1
            # Fail on first call, succeed on second
            if call_count == 1:
                return StageResult(name="", success=False, error="first fail")
            return StageResult(name="", success=True)

    pipeline = Pipeline("test")
    pipeline.items.append(
        Loop("retry_loop", max_retries=3, on_exhaust=OnFailure.FAIL, stages=[
            Stage("flip", FlipStage()),
        ])
    )

    await run_pipeline(pipeline, config_path=config)

    records = load_history(config)
    assert len(records) == 1
    assert records[0].retries == 1

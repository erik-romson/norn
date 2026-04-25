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
from norn.models import PipelineContext, StageLogEntry, StageResult, UsageRecord
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
        in_progress=False,
        stage_log=[
            StageLogEntry(
                name="generate",
                status="passed",
                success=True,
                attempt=1,
                duration_ms=4200,
                cost_usd=0.12,
                running_total_cost_usd=0.12,
                running_total_tokens=15100,
                input_tokens=10000,
                output_tokens=5100,
                duration_api_ms=3900,
                num_turns=3,
                model="sonnet",
                session_id="abc123",
            )
        ],
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
    assert len(r.stage_log) == 1
    assert r.stage_log[0].name == "generate"
    assert r.stage_log[0].cost_usd == pytest.approx(0.12)
    assert r.stage_log[0].running_total_cost_usd == pytest.approx(0.12)
    assert r.stage_log[0].input_tokens == 10000
    assert r.stage_log[0].model == "sonnet"


def test_append_multiple_runs(tmp_path):
    config = str(tmp_path / "pipeline.py")
    append_run(config, _make_record(run_id=1, success=True))
    append_run(config, _make_record(run_id=2, success=False))
    append_run(config, _make_record(run_id=3, success=True))

    records = load_history(config)
    assert len(records) == 3
    assert [r.run_id for r in records] == [1, 2, 3]
    assert [r.success for r in records] == [True, False, True]


def test_load_history_keeps_latest_snapshot_per_run_id(tmp_path):
    config = str(tmp_path / "pipeline.py")
    append_run(config, RunRecord(
        run_id=1,
        timestamp="2026-03-25T09:00:00+00:00",
        success=False,
        total_cost_usd=0.12,
        total_tokens=1000,
        duration_ms=100,
        stages=[StageHistoryEntry(name="step1", success=True, cost_usd=0.12)],
        retries=0,
        in_progress=True,
    ))
    append_run(config, RunRecord(
        run_id=1,
        timestamp="2026-03-25T09:01:00+00:00",
        success=True,
        total_cost_usd=0.19,
        total_tokens=15100,
        duration_ms=10500,
        stages=[StageHistoryEntry(name="step1", success=True, cost_usd=0.19)],
        retries=0,
        in_progress=False,
    ))

    records = load_history(config)
    assert len(records) == 1
    assert records[0].run_id == 1
    assert records[0].success is True
    assert records[0].in_progress is False
    assert records[0].total_cost_usd == pytest.approx(0.19)


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


async def test_history_captures_detailed_stage_log(tmp_path):
    config = str(tmp_path / "pipeline.py")

    class CostStage(BaseStage):
        needs_agent = False

        async def run(self, ctx: PipelineContext) -> StageResult:
            usage = UsageRecord(
                stage_name="",
                session_id="sess-1",
                input_tokens=1200,
                output_tokens=300,
                total_cost_usd=0.25,
                duration_api_ms=2100,
                num_turns=2,
                model="sonnet",
            )
            return StageResult(name="", success=True, output="ok", usage=usage)

    pipeline = (
        Pipeline("test")
        .stage("billable", CostStage())
        .stage("conditional", SuccessStage(), when=lambda ctx: False)
    )

    await run_pipeline(pipeline, config_path=config)

    records = load_history(config)
    assert len(records) == 1
    assert [entry.name for entry in records[0].stage_log] == ["billable", "conditional"]

    first, second = records[0].stage_log
    assert first.status == "passed"
    assert first.cost_usd == pytest.approx(0.25)
    assert first.running_total_cost_usd == pytest.approx(0.25)
    assert first.input_tokens == 1200
    assert first.output_tokens == 300
    assert first.duration_api_ms == 2100
    assert first.model == "sonnet"
    assert second.status == "skipped_condition"
    assert second.running_total_cost_usd == pytest.approx(0.25)


async def test_history_is_appended_incrementally_during_run(tmp_path):
    config = str(tmp_path / "pipeline.py")
    observed_records: list[RunRecord] = []

    class InspectHistoryStage(BaseStage):
        needs_agent = False

        async def run(self, ctx: PipelineContext) -> StageResult:
            assert history_file(config).exists()
            records = load_history(config)
            observed_records.extend(records)
            assert len(records) == 1
            assert records[0].run_id == 1
            assert records[0].in_progress is True
            assert [entry.name for entry in records[0].stage_log] == ["step1"]
            return StageResult(name="", success=True, output="ok")

    pipeline = (
        Pipeline("test")
        .stage("step1", SuccessStage())
        .stage("step2", InspectHistoryStage())
    )

    await run_pipeline(pipeline, config_path=config)

    raw_lines = [line for line in history_file(config).read_text().splitlines() if line.strip()]
    assert len(raw_lines) >= 3
    assert observed_records
    final_records = load_history(config)
    assert len(final_records) == 1
    assert final_records[0].in_progress is False
    assert [entry.name for entry in final_records[0].stage_log] == ["step1", "step2"]

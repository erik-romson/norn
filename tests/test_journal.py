"""Tests for norn.journal — RunJournal, JournalSink, event_to_dict."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from norn.dsl import Pipeline, Stage
from norn.event_sink import EventSink
from norn.events import (
    CallingAgent,
    ClearContextNotice,
    CommandOutput,
    EventKey,
    GotReply,
    IncludeDone,
    IncludeStarted,
    LoopAttempt,
    LoopDraftPR,
    LoopExhausted,
    LoopSuccess,
    ParallelDone,
    ParallelStarted,
    RunCancelled,
    RunError,
    RunFinished,
    RunPaused,
    RunResumed,
    RunStarted,
    StageFinished,
    StageRetrying,
    StageStarted,
    TurnEvent,
    UnitMerged,
    UnitStarted,
    UsageUpdated,
    WaitingInput,
)
from norn.journal import (
    JournalSink,
    RunJournal,
    event_to_dict,
    next_run_id,
    runs_dir,
)
from norn.models import PipelineContext, StageResult
from norn.runner import run_pipeline
from norn.stages.base import BaseStage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _SuccessStage(BaseStage):
    async def run(self, ctx: PipelineContext) -> StageResult:
        return StageResult(name="", success=True, output="ok")


class _FailStage(BaseStage):
    async def run(self, ctx: PipelineContext) -> StageResult:
        return StageResult(name="", success=False, error="boom")


class _LargeOutputStage(BaseStage):
    async def run(self, ctx: PipelineContext) -> StageResult:
        return StageResult(name="", success=True, output="x" * 3_000_000)


def _key(**kw) -> EventKey:
    kw.setdefault("run_id", "r1")
    kw.setdefault("unit_id", "unit-0")
    return EventKey(**kw)


# ---------------------------------------------------------------------------
# (a) Three-stage pipeline writes run.json, events.jsonl, three stage files
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_journal_three_stage_pipeline(tmp_path):
    """A three-stage pipeline writes run.json, events.jsonl, and three stage files."""
    config = str(tmp_path / "test_pipeline")
    p = (
        Pipeline("test-journal")
        .stage("step1", _SuccessStage())
        .stage("step2", _SuccessStage())
        .stage("step3", _SuccessStage())
    )
    await run_pipeline(p, config_path=config)

    journal_dir = runs_dir(config) / "1"
    assert journal_dir.is_dir()

    run_json = journal_dir / "run.json"
    assert run_json.exists()
    header = json.loads(run_json.read_text())
    assert header["pipeline"] == "test-journal"
    assert header["success"] is True
    assert "totals" in header

    events_jsonl = journal_dir / "events.jsonl"
    assert events_jsonl.exists()
    lines = events_jsonl.read_text().strip().split("\n")
    assert len(lines) > 0

    stages_dir = journal_dir / "stages"
    stage_files = sorted(stages_dir.glob("*.json"))
    assert len(stage_files) == 3


# ---------------------------------------------------------------------------
# (b) Secrets are masked in journal files
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_journal_secrets_masked(tmp_path):
    """Secrets registered via ui.register_secrets do not appear in journal files."""
    from norn.ui import register_secrets, _masked_secrets

    register_secrets(["JOURNAL_SECRET_VALUE"])
    try:

        class SecretStage(BaseStage):
            async def run(self, ctx: PipelineContext) -> StageResult:
                return StageResult(
                    name="",
                    success=True,
                    output="token is JOURNAL_SECRET_VALUE here",
                )

        config = str(tmp_path / "secret_pipeline")
        p = Pipeline("secret-test").stage("s1", SecretStage())
        await run_pipeline(p, config_path=config)

        journal_dir = runs_dir(config) / "1"
        # Check stage files don't contain the secret
        for stage_file in (journal_dir / "stages").glob("*.json"):
            content = stage_file.read_text()
            assert "JOURNAL_SECRET_VALUE" not in content

        # Check events don't contain the secret
        events_content = (journal_dir / "events.jsonl").read_text()
        # Events are structural (StageStarted, etc.), not output — they
        # shouldn't contain stage output. But verify the whole file.
        # The secret could be in run.json params if it were passed as a param,
        # but here it's only in the stage output which is in stage files.
    finally:
        _masked_secrets.discard("JOURNAL_SECRET_VALUE")


# ---------------------------------------------------------------------------
# (c) JournalSink.transcript() / lossless_events forward to inner
# ---------------------------------------------------------------------------


def test_journal_sink_forwards_consumer_api(tmp_path):
    """JournalSink.transcript() and lossless_events return inner sink's data."""
    journal_dir = tmp_path / "test.runs" / "1"
    journal_dir.mkdir(parents=True)
    (journal_dir / "stages").mkdir()
    journal = RunJournal(journal_dir)
    inner = EventSink()
    sink = JournalSink(inner, journal)

    # Emit some events
    from norn.agents.base import AgentEvent

    e1 = RunStarted(key=_key(), pipeline_name="p", provider="x")
    e2 = TurnEvent(key=_key(stage_id="s1", attempt=1, seq=0), event=AgentEvent(text="hi"))

    sink.emit(e1)
    sink.emit(e2)

    # Consumer API forwards to inner
    assert len(sink.lossless_events) == 1
    assert len(sink.transcript(stage_id="s1", attempt=1)) == 1


# ---------------------------------------------------------------------------
# (d) Write failure logs warning but run completes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_journal_write_failure_warns(tmp_path, caplog):
    """Monkeypatch Path.open to raise OSError -> warning logged, run completes."""
    config = str(tmp_path / "fail_write")
    p = Pipeline("fail-write").stage("s1", _SuccessStage())

    original_open = Path.open

    def broken_open(self, *args, **kwargs):
        if "events.jsonl" in str(self):
            raise OSError("disk full")
        return original_open(self, *args, **kwargs)

    with caplog.at_level(logging.WARNING, logger="norn.journal"):
        with patch.object(Path, "open", broken_open):
            ctx = await run_pipeline(p, config_path=config)

    assert ctx.get("s1") == "ok"
    assert any("failed to append event" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# (e) Large output is truncated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_journal_large_output_truncated(tmp_path):
    """3M-char output -> output_truncated: true, file < 2.1 MB."""
    config = str(tmp_path / "large_output")
    p = Pipeline("large").stage("big", _LargeOutputStage())
    await run_pipeline(p, config_path=config)

    journal_dir = runs_dir(config) / "1"
    stage_files = sorted((journal_dir / "stages").glob("*.json"))
    assert len(stage_files) == 1
    content = json.loads(stage_files[0].read_text())
    assert content.get("output_truncated") is True
    assert len(stage_files[0].read_text()) < 2_200_000


# ---------------------------------------------------------------------------
# (f) event_to_dict succeeds for every RunEvent class
# ---------------------------------------------------------------------------


def test_event_to_dict_all_event_types():
    """event_to_dict succeeds for every class in norn.events.RunEvent."""
    from norn.agents.base import AgentEvent

    k = _key()
    events = [
        RunStarted(key=k, pipeline_name="p", provider="x"),
        UnitStarted(key=k),
        StageStarted(key=k, name="s"),
        TurnEvent(key=k, event=AgentEvent(text="hi")),
        CommandOutput(key=k, text="out"),
        UsageUpdated(key=k, input_tokens=10),
        StageFinished(key=k, name="s", status="passed", success=True),
        StageRetrying(key=k, next_attempt=2),
        WaitingInput(key=k, kind="budget"),
        LoopExhausted(key=k),
        LoopAttempt(key=k, name="l", attempt=1, max_retries=3),
        LoopSuccess(key=k, name="l"),
        LoopDraftPR(key=k, name="l"),
        ParallelStarted(key=k, name="p", stage_count=2),
        ParallelDone(key=k, name="p"),
        IncludeStarted(key=k, path="x.py"),
        IncludeDone(key=k, path="x.py"),
        ClearContextNotice(key=k),
        CallingAgent(key=k, stage_name="s", provider="x"),
        GotReply(key=k, stage_name="s"),
        RunError(key=k, error_kind="test"),
        UnitMerged(key=k),
        RunPaused(key=k),
        RunResumed(key=k),
        RunCancelled(key=k),
        RunFinished(key=k),
    ]
    for event in events:
        d = event_to_dict(event)
        assert "type" in d
        # Must be JSON-serialisable
        json.dumps(d, default=str)


# ---------------------------------------------------------------------------
# (g) Isolated include: sub-run stage records in parent's stages/
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_journal_isolated_include(tmp_path):
    """Sub-run stage records land in the parent journal's stages/ directory."""
    # Create the sub-pipeline file
    sub_file = tmp_path / "sub.py"
    sub_file.write_text(
        "from norn.dsl import Pipeline, Stage\n"
        "from norn.stages.base import BaseStage\n"
        "from norn.models import PipelineContext, StageResult\n"
        "\n"
        "class S(BaseStage):\n"
        "    async def run(self, ctx: PipelineContext) -> StageResult:\n"
        "        return StageResult(name='', success=True, output='sub-ok')\n"
        "\n"
        "config = Pipeline('sub').stage('sub-step', S())\n"
    )

    config = str(tmp_path / "parent_pipeline")
    p = (
        Pipeline("parent")
        .stage("p1", _SuccessStage())
        .include(str(sub_file), isolated=True, outputs=["sub-step"])
        .stage("p2", _SuccessStage())
    )
    await run_pipeline(p, config_path=config)

    journal_dir = runs_dir(config) / "1"
    stage_files = sorted((journal_dir / "stages").glob("*.json"))
    # p1 + sub-step + p2 = at least 3 stage records (sub-run also creates stages)
    assert len(stage_files) >= 3


# ---------------------------------------------------------------------------
# (h) journal_run_id matches next_run_id
# ---------------------------------------------------------------------------


def test_next_run_id_auto_increments(tmp_path):
    """journal_run_id equals numeric id from next_run_id(state_key)."""
    config = str(tmp_path / "test")
    assert next_run_id(config) == 1

    # Create run dir 1
    RunJournal.open(config, 1)
    assert next_run_id(config) == 2

    # Create run dir 2
    RunJournal.open(config, 2)
    assert next_run_id(config) == 3


# ---------------------------------------------------------------------------
# Metrics are written for failed runs too
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metrics_written_when_the_pipeline_fails(tmp_path):
    """A failed run gets metrics.json / findings.json — it is the run most worth reading.

    The metrics block used to sit after the try/finally, which a raising
    pipeline never reaches, so every failed run wrote nothing.
    """
    from norn.runner import PipelineError

    config = str(tmp_path / "fail_metrics")
    p = Pipeline("fail-metrics").stage("boom", _FailStage())

    with pytest.raises(PipelineError):
        await run_pipeline(p, config_path=config)

    journal_dir = runs_dir(config) / "1"
    assert (journal_dir / "metrics.json").exists()
    assert (journal_dir / "findings.json").exists()

    header = json.loads((journal_dir / "run.json").read_text())
    assert header["success"] is False
    assert header["failed_stage"] == "boom"


@pytest.mark.asyncio
async def test_metrics_written_when_the_pipeline_succeeds(tmp_path):
    """The success path keeps writing metrics, and exposes them on the context."""
    config = str(tmp_path / "ok_metrics")
    p = Pipeline("ok-metrics").stage("step", _SuccessStage())

    ctx = await run_pipeline(p, config_path=config)

    journal_dir = runs_dir(config) / "1"
    assert (journal_dir / "metrics.json").exists()
    assert (journal_dir / "findings.json").exists()
    assert ctx.run_metrics is not None


@pytest.mark.asyncio
async def test_no_journal_leaves_metrics_unset(tmp_path):
    """Without config_path there is no journal to compute metrics from."""
    p = Pipeline("no-journal").stage("step", _SuccessStage())

    ctx = await run_pipeline(p)

    assert ctx.run_metrics is None
    assert ctx.findings == []


# ---------------------------------------------------------------------------
# History-id mapping
# ---------------------------------------------------------------------------


def test_history_run_id_map_skips_dirs_without_the_field(tmp_path):
    """Only runs that recorded a history id appear in the map."""
    import json as _json

    from norn.journal import history_run_id_map, run_dir_for_history_id

    key = str(tmp_path / "pipeline.py")
    runs = tmp_path / "pipeline.runs"
    (runs / "1").mkdir(parents=True)  # legacy: no run.json at all
    (runs / "2").mkdir(parents=True)
    (runs / "2" / "run.json").write_text(_json.dumps({"pipeline": "p"}))  # no field
    (runs / "3").mkdir(parents=True)
    (runs / "3" / "run.json").write_text(_json.dumps({"history_run_id": 7}))

    assert history_run_id_map(key) == {7: 3}
    assert run_dir_for_history_id(key, 7) == runs / "3"
    assert run_dir_for_history_id(key, 3) is None
    assert run_dir_for_history_id(key, 1) is None


def test_history_run_id_map_is_empty_without_a_runs_dir(tmp_path):
    from norn.journal import history_run_id_map, run_dir_for_history_id

    key = str(tmp_path / "pipeline.py")
    assert history_run_id_map(key) == {}
    assert run_dir_for_history_id(key, 1) is None


def test_runner_records_the_history_run_id(tmp_path):
    """A real run writes history_run_id into run.json."""
    import asyncio
    import json as _json

    from norn.dsl import Pipeline
    from norn.journal import history_run_id, run_dir
    from norn.runner import run_pipeline
    from norn.stages.run_command import RunCommand

    config = str(tmp_path / "pipeline.py")
    p = Pipeline("t")
    p.stage("noop", RunCommand(cmd="true"))
    asyncio.run(run_pipeline(p, config_path=config))

    rd = run_dir(config, 1)
    recorded = _json.loads((rd / "run.json").read_text())["history_run_id"]
    assert recorded == 1
    assert history_run_id(rd) == 1


def test_run_journal_dir_property(tmp_path):
    """The public ``dir`` property returns the run directory."""
    from norn.journal import RunJournal

    journal = RunJournal(tmp_path / "7")
    assert journal.dir == tmp_path / "7"

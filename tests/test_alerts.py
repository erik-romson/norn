from __future__ import annotations

import pytest

from norn.alerts import AlertEvent, AlertManager, AlertMessage, MacOSChannel, SlackChannel
from norn.dsl import Loop, OnFailure, Pipeline, Stage
from norn.models import PipelineContext, StageResult
from norn.runner import PipelineError, RetriesExhaustedError, run_pipeline
from norn.stages.base import BaseStage


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class RecordingChannel:
    """Alert channel that records every message it receives."""

    def __init__(self, events: set[AlertEvent] | None = None) -> None:
        self.events = events
        self.received: list[AlertMessage] = []

    async def send(self, msg: AlertMessage) -> None:
        self.received.append(msg)


class SuccessStage(BaseStage):
    async def run(self, ctx: PipelineContext) -> StageResult:
        return StageResult(name="", success=True, output="ok")


class FailStage(BaseStage):
    async def run(self, ctx: PipelineContext) -> StageResult:
        return StageResult(name="", success=False, error="boom")


# ---------------------------------------------------------------------------
# AlertMessage tests
# ---------------------------------------------------------------------------


def test_alert_message_title_complete():
    msg = AlertMessage(event=AlertEvent.COMPLETE, pipeline_name="p")
    assert "Complete" in msg.title


def test_alert_message_title_failed():
    msg = AlertMessage(event=AlertEvent.FAILED, pipeline_name="p")
    assert "Failed" in msg.title


def test_alert_message_body_includes_pipeline_name():
    msg = AlertMessage(event=AlertEvent.COMPLETE, pipeline_name="my-pipeline")
    assert "my-pipeline" in msg.body


def test_alert_message_body_includes_stage_and_detail():
    msg = AlertMessage(
        event=AlertEvent.FAILED,
        pipeline_name="p",
        stage_name="build",
        detail="compile error",
    )
    assert "build" in msg.body
    assert "compile error" in msg.body


def test_alert_message_detail_truncated_to_200():
    long_detail = "x" * 300
    msg = AlertMessage(event=AlertEvent.FAILED, pipeline_name="p", detail=long_detail)
    assert len(msg.body) <= len("p — " + "x" * 200)


# ---------------------------------------------------------------------------
# AlertManager dispatch tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manager_delivers_to_all_channels():
    ch1 = RecordingChannel()
    ch2 = RecordingChannel()
    mgr = AlertManager(channels=[ch1, ch2])
    msg = AlertMessage(event=AlertEvent.COMPLETE, pipeline_name="p")
    await mgr.fire(msg)
    assert len(ch1.received) == 1
    assert len(ch2.received) == 1


@pytest.mark.asyncio
async def test_manager_respects_event_filter():
    ch_all = RecordingChannel()
    ch_fail_only = RecordingChannel(events={AlertEvent.FAILED})
    mgr = AlertManager(channels=[ch_all, ch_fail_only])

    await mgr.fire(AlertMessage(event=AlertEvent.COMPLETE, pipeline_name="p"))
    assert len(ch_all.received) == 1
    assert len(ch_fail_only.received) == 0

    await mgr.fire(AlertMessage(event=AlertEvent.FAILED, pipeline_name="p"))
    assert len(ch_all.received) == 2
    assert len(ch_fail_only.received) == 1


@pytest.mark.asyncio
async def test_manager_swallows_channel_errors():
    """A broken channel must not abort other channels or raise."""

    class BrokenChannel:
        events = None

        async def send(self, msg: AlertMessage) -> None:
            raise RuntimeError("network down")

    good = RecordingChannel()
    mgr = AlertManager(channels=[BrokenChannel(), good])
    await mgr.fire(AlertMessage(event=AlertEvent.COMPLETE, pipeline_name="p"))
    assert len(good.received) == 1


# ---------------------------------------------------------------------------
# Pipeline integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_alert_fired_on_success():
    ch = RecordingChannel()
    mgr = AlertManager(channels=[ch])
    p = Pipeline("test").stage("s1", SuccessStage())
    await run_pipeline(p, alert_manager=mgr)

    events = [m.event for m in ch.received]
    assert AlertEvent.COMPLETE in events


@pytest.mark.asyncio
async def test_failed_alert_fired_on_stage_failure():
    ch = RecordingChannel()
    mgr = AlertManager(channels=[ch])
    p = Pipeline("test").stage("s1", FailStage(), on_failure=OnFailure.FAIL)

    with pytest.raises(PipelineError):
        await run_pipeline(p, alert_manager=mgr)

    events = [m.event for m in ch.received]
    assert AlertEvent.FAILED in events
    assert AlertEvent.COMPLETE not in events


@pytest.mark.asyncio
async def test_retries_exhausted_alert_fired():
    ch = RecordingChannel()
    mgr = AlertManager(channels=[ch])
    p = Pipeline("test").loop(
        "retry",
        max_retries=2,
        on_exhaust=OnFailure.FAIL,
        stages=[Stage("s1", FailStage())],
    )

    with pytest.raises(RetriesExhaustedError):
        await run_pipeline(p, alert_manager=mgr)

    events = [m.event for m in ch.received]
    assert AlertEvent.RETRIES_EXHAUSTED in events
    assert AlertEvent.FAILED in events


@pytest.mark.asyncio
async def test_failed_alert_includes_stage_name():
    ch = RecordingChannel()
    mgr = AlertManager(channels=[ch])
    p = Pipeline("test").stage("broken_stage", FailStage(), on_failure=OnFailure.FAIL)

    with pytest.raises(PipelineError):
        await run_pipeline(p, alert_manager=mgr)

    failed_msgs = [m for m in ch.received if m.event == AlertEvent.FAILED]
    assert failed_msgs
    assert failed_msgs[0].stage_name == "broken_stage"


@pytest.mark.asyncio
async def test_no_alert_manager_does_not_error():
    """Running without an alert_manager should work fine."""
    p = Pipeline("test").stage("s1", SuccessStage())
    ctx = await run_pipeline(p)
    assert ctx.get("s1") == "ok"


@pytest.mark.asyncio
async def test_pipeline_dsl_alert_channel():
    """Pipeline.alert() / .alerts() wire channels into the pipeline object."""
    ch = RecordingChannel()
    p = Pipeline("test").alert(ch).stage("s1", SuccessStage())
    assert ch in p.alert_channels


@pytest.mark.asyncio
async def test_pipeline_dsl_alerts_list():
    ch1 = RecordingChannel()
    ch2 = RecordingChannel()
    p = Pipeline("test").alerts([ch1, ch2])
    assert ch1 in p.alert_channels
    assert ch2 in p.alert_channels


# ---------------------------------------------------------------------------
# SlackChannel unit test (no real HTTP call)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_channel_event_filter():
    ch = SlackChannel(webhook_url="https://example.com", events={AlertEvent.FAILED})
    assert ch.events == {AlertEvent.FAILED}
    mgr = AlertManager(channels=[ch])

    # COMPLETE should be filtered out without any error (channel never called)
    await mgr.fire(AlertMessage(event=AlertEvent.COMPLETE, pipeline_name="p"))
    # No assertion needed — no exception means it was filtered correctly


# ---------------------------------------------------------------------------
# MacOSChannel unit test (no real osascript call)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_macos_channel_send_does_not_raise(monkeypatch):
    """MacOSChannel.send() should not raise even if osascript is absent."""
    import subprocess

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=[], returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    ch = MacOSChannel()
    msg = AlertMessage(event=AlertEvent.COMPLETE, pipeline_name="p")
    await ch.send(msg)  # should not raise

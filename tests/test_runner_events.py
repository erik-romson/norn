"""Tests that the runner emits the correct run-event sequence.

All agent calls are mocked; no real Claude invocations occur.
"""
from __future__ import annotations

import pytest

from norn.agents.base import AgentEvent, AgentRequest
from norn.agents import registry as agent_registry
from norn.dsl import Loop, OnFailure, Parallel, Pipeline, Stage
from norn.event_sink import EventSink
from norn.events import (
    CallingAgent,
    ClearContextNotice,
    GotReply,
    LoopExhausted,
    RunError,
    RunFinished,
    RunStarted,
    StageFinished,
    StageRetrying,
    StageStarted,
    TurnEvent,
    UnitStarted,
    UsageUpdated,
)
from norn.models import PipelineContext, StageResult
from norn.runner import PipelineError, RetriesExhaustedError, run_pipeline
from norn.stages.base import BaseStage
from norn.stages.generate import Generate


# ---------------------------------------------------------------------------
# Helpers & fixtures
# ---------------------------------------------------------------------------


class _SuccessStage(BaseStage):
    async def run(self, ctx: PipelineContext) -> StageResult:
        return StageResult(name="", success=True, output="ok")


class _FailStage(BaseStage):
    async def run(self, ctx: PipelineContext) -> StageResult:
        return StageResult(name="", success=False, error="boom")


class _FailThenSucceed(BaseStage):
    def __init__(self, fail_count: int = 1) -> None:
        self._fail_count = fail_count
        self._calls = 0

    async def run(self, ctx: PipelineContext) -> StageResult:
        self._calls += 1
        if self._calls <= self._fail_count:
            return StageResult(name="", success=False, error=f"fail#{self._calls}")
        return StageResult(name="", success=True, output="recovered")


class _FakeProvider:
    """Test-only provider that yields pre-configured AgentEvents."""

    name = "_fake-events-test-provider"

    def __init__(self, events: list[AgentEvent] | None = None) -> None:
        self.events: list[AgentEvent] = events or []

    async def run(self, request: AgentRequest):
        for ev in self.events:
            yield ev


@pytest.fixture()
def fake_provider():
    fp = _FakeProvider()
    agent_registry.register(fp)
    try:
        yield fp
    finally:
        agent_registry._registry.pop(fp.name, None)


def _recording_sink() -> EventSink:
    """Return an EventSink suitable for assertions."""
    return EventSink()


def _event_types(sink: EventSink) -> list[type]:
    return [type(e) for e in sink.lossless_events]


def _lossless_names(sink: EventSink) -> list[str]:
    """Names from StageStarted / StageFinished events in order."""
    names = []
    for e in sink.lossless_events:
        if isinstance(e, (StageStarted, StageFinished)):
            names.append(e.name)
    return names


# ---------------------------------------------------------------------------
# Basic lifecycle sequence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_simple_pipeline_event_sequence():
    """RunStarted → UnitStarted → StageStarted → StageFinished → RunFinished."""
    sink = _recording_sink()
    p = Pipeline("p").stage("s1", _SuccessStage())
    ctx = await run_pipeline(p, event_sink=sink)

    types = _event_types(sink)
    assert types[0] is RunStarted
    assert types[1] is UnitStarted
    assert StageStarted in types
    assert StageFinished in types
    assert types[-1] is RunFinished


@pytest.mark.asyncio
async def test_run_started_carries_pipeline_name_and_provider():
    sink = _recording_sink()
    p = Pipeline("my-pipe").stage("s1", _SuccessStage())
    await run_pipeline(p, agent_provider="claude-code", event_sink=sink)

    started = next(e for e in sink.lossless_events if isinstance(e, RunStarted))
    assert started.pipeline_name == "my-pipe"
    assert started.provider == "claude-code"


@pytest.mark.asyncio
async def test_unit_started_is_unit_zero():
    sink = _recording_sink()
    p = Pipeline("p").stage("s1", _SuccessStage())
    await run_pipeline(p, event_sink=sink)

    unit_ev = next(e for e in sink.lossless_events if isinstance(e, UnitStarted))
    assert unit_ev.key.unit_id == "unit-0"


@pytest.mark.asyncio
async def test_run_finished_success():
    sink = _recording_sink()
    p = Pipeline("p").stage("s1", _SuccessStage())
    await run_pipeline(p, event_sink=sink)

    finished = next(e for e in sink.lossless_events if isinstance(e, RunFinished))
    assert finished.success is True


@pytest.mark.asyncio
async def test_run_finished_failure():
    sink = _recording_sink()
    p = Pipeline("p").stage("s1", _FailStage(), on_failure=OnFailure.FAIL)
    with pytest.raises(PipelineError):
        await run_pipeline(p, event_sink=sink)

    finished = next(e for e in sink.lossless_events if isinstance(e, RunFinished))
    assert finished.success is False


@pytest.mark.asyncio
async def test_run_error_emitted_on_failure():
    sink = _recording_sink()
    p = Pipeline("p").stage("s1", _FailStage(), on_failure=OnFailure.FAIL)
    with pytest.raises(PipelineError):
        await run_pipeline(p, event_sink=sink)

    types = _event_types(sink)
    assert RunError in types


@pytest.mark.asyncio
async def test_stage_started_and_finished_for_each_stage():
    sink = _recording_sink()
    p = Pipeline("p").stage("alpha", _SuccessStage()).stage("beta", _SuccessStage())
    await run_pipeline(p, event_sink=sink)

    started = [e for e in sink.lossless_events if isinstance(e, StageStarted)]
    finished = [e for e in sink.lossless_events if isinstance(e, StageFinished)]

    assert {e.name for e in started} == {"alpha", "beta"}
    assert {e.name for e in finished} == {"alpha", "beta"}


@pytest.mark.asyncio
async def test_stage_finished_status_passed():
    sink = _recording_sink()
    p = Pipeline("p").stage("s1", _SuccessStage())
    await run_pipeline(p, event_sink=sink)

    fin = next(e for e in sink.lossless_events if isinstance(e, StageFinished) and e.name == "s1")
    assert fin.status == "passed"
    assert fin.success is True


@pytest.mark.asyncio
async def test_stage_finished_status_failed():
    """A failing stage emits StageFinished(status='failed') even when the run raises."""
    from unittest.mock import patch

    sink = _recording_sink()
    p = Pipeline("p").stage("s1", _FailStage(), on_failure=OnFailure.FAIL)
    with pytest.raises(PipelineError):
        await run_pipeline(p, event_sink=sink)

    fin = next(e for e in sink.lossless_events if isinstance(e, StageFinished) and e.name == "s1")
    assert fin.status == "failed"
    assert fin.success is False
    assert fin.error == "boom"


@pytest.mark.asyncio
async def test_stage_started_precedes_stage_finished():
    sink = _recording_sink()
    p = Pipeline("p").stage("s1", _SuccessStage())
    await run_pipeline(p, event_sink=sink)

    events = sink.lossless_events
    idx_started = next(i for i, e in enumerate(events) if isinstance(e, StageStarted) and e.name == "s1")
    idx_finished = next(i for i, e in enumerate(events) if isinstance(e, StageFinished) and e.name == "s1")
    assert idx_started < idx_finished


@pytest.mark.asyncio
async def test_run_id_is_consistent_within_run():
    """All events in a single run share the same run_id."""
    sink = _recording_sink()
    p = Pipeline("p").stage("s1", _SuccessStage()).stage("s2", _SuccessStage())
    await run_pipeline(p, event_sink=sink)

    run_ids = {e.key.run_id for e in sink.lossless_events}
    assert len(run_ids) == 1
    assert list(run_ids)[0] != ""


@pytest.mark.asyncio
async def test_run_ids_differ_across_runs():
    sink1 = _recording_sink()
    sink2 = _recording_sink()
    p = Pipeline("p").stage("s1", _SuccessStage())
    await run_pipeline(p, event_sink=sink1)
    await run_pipeline(p, event_sink=sink2)

    id1 = next(e for e in sink1.lossless_events if isinstance(e, RunStarted)).key.run_id
    id2 = next(e for e in sink2.lossless_events if isinstance(e, RunStarted)).key.run_id
    assert id1 != id2


# ---------------------------------------------------------------------------
# Loop events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_retry_emits_stage_retrying():
    sink = _recording_sink()
    flaky = _FailThenSucceed(fail_count=1)
    p = Pipeline("p").loop("retry", max_retries=3, stages=[Stage("s1", flaky)])
    await run_pipeline(p, event_sink=sink)

    retrying = [e for e in sink.lossless_events if isinstance(e, StageRetrying)]
    assert len(retrying) == 1
    assert retrying[0].next_attempt == 2


@pytest.mark.asyncio
async def test_loop_body_stages_emit_nested_stage_ids():
    """Loop-body stages carry the fully-qualified graph node id
    (loop:<L>/stage:<name>) so the TUI graph attributes them to the nested
    node rather than a flat stage:<name> that never matches."""
    sink = _recording_sink()
    p = Pipeline("p").loop("retry", max_retries=3, stages=[
        Stage("s1", _SuccessStage()),
        Stage("s2", _SuccessStage()),
    ])
    await run_pipeline(p, event_sink=sink)

    finished = [e for e in sink.lossless_events if isinstance(e, StageFinished)]
    stage_ids = {e.key.stage_id for e in finished}
    assert "loop:retry/stage:s1" in stage_ids
    assert "loop:retry/stage:s2" in stage_ids


@pytest.mark.asyncio
async def test_clear_context_notice_carries_clear_node_id():
    """A top-level clear-context marker emits ClearContextNotice keyed with the
    graph node id (clear:<N>) so the TUI can mark it done."""
    sink = _recording_sink()
    p = (
        Pipeline("p")
        .stage("s1", _SuccessStage())
        .clear_context()
    )
    await run_pipeline(p, event_sink=sink)

    notices = [e for e in sink.lossless_events if isinstance(e, ClearContextNotice)]
    assert len(notices) == 1
    assert notices[0].key.stage_id == "clear:0"


@pytest.mark.asyncio
async def test_loop_exhausted_emits_loop_exhausted():
    sink = _recording_sink()
    p = Pipeline("p").loop("retry", max_retries=2, on_exhaust=OnFailure.FAIL, stages=[Stage("s1", _FailStage())])
    with pytest.raises(RetriesExhaustedError):
        await run_pipeline(p, event_sink=sink)

    exhausted = [e for e in sink.lossless_events if isinstance(e, LoopExhausted)]
    assert len(exhausted) == 1
    assert exhausted[0].loop_id == "loop:retry"


@pytest.mark.asyncio
async def test_stage_retrying_carries_failure_reason():
    sink = _recording_sink()
    flaky = _FailThenSucceed(fail_count=1)
    p = Pipeline("p").loop("retry", max_retries=3, stages=[Stage("s1", flaky)])
    await run_pipeline(p, event_sink=sink)

    retrying = next(e for e in sink.lossless_events if isinstance(e, StageRetrying))
    assert "fail#1" in retrying.reason


# ---------------------------------------------------------------------------
# Generate stage → TurnEvents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_emits_turn_events(fake_provider):
    """Each AgentEvent from the provider becomes a TurnEvent in the sink."""
    fake_provider.events = [
        AgentEvent(text="Hello "),
        AgentEvent(text="world"),
    ]

    sink = _recording_sink()

    # Override provider on context by using the fake provider name
    ctx = PipelineContext(agent_provider=fake_provider.name, event_sink=sink)
    ctx.run_id = "test-run"
    ctx.unit_id = "unit-0"

    gen = Generate(prompt="say hi")
    result = await gen.run(ctx, stage_name="gen", attempt=1, fork_session=False)
    assert result.success

    turn_events = sink.transcript(stage_id="stage:gen", attempt=1)
    assert len(turn_events) == 2
    assert all(isinstance(e, TurnEvent) for e in turn_events)
    # seqs are monotonically increasing
    seqs = [e.key.seq for e in turn_events]
    assert seqs == sorted(seqs)
    assert seqs[0] >= 1


@pytest.mark.asyncio
async def test_generate_turn_events_via_run_pipeline(fake_provider):
    """TurnEvents appear in the sink when Generate is run through run_pipeline."""
    fake_provider.events = [
        AgentEvent(text="chunk1"),
        AgentEvent(text="chunk2"),
        AgentEvent(text="chunk3"),
    ]

    sink = _recording_sink()
    p = Pipeline("p").stage("gen", Generate(prompt="hi"))
    # Patch ctx after creation by using a custom sink injected via event_sink kwarg
    ctx = await run_pipeline(
        p, agent_provider=fake_provider.name, event_sink=sink
    )

    turn_events = sink.transcript(stage_id="stage:gen", attempt=1)
    assert len(turn_events) == 3


# ---------------------------------------------------------------------------
# UsageUpdated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_usage_updated_emitted_when_usage_present(fake_provider):
    """UsageUpdated appears in coalescible slot after a Generate stage with usage."""
    from norn.agents.base import AgentUsage

    fake_provider.events = [
        AgentEvent(
            text="done",
            usage=AgentUsage(
                input_tokens=10,
                output_tokens=5,
                total_cost_usd=0.001,
                provider=fake_provider.name,
                session_id="s1",
                duration_ms=100,
                duration_api_ms=90,
                num_turns=1,
                is_error=False,
            ),
        )
    ]

    sink = _recording_sink()
    p = Pipeline("p").stage("gen", Generate(prompt="hi"))
    await run_pipeline(p, agent_provider=fake_provider.name, event_sink=sink)

    updated = sink.all_coalescible()
    assert len(updated) >= 1


# ---------------------------------------------------------------------------
# Parallel — interleaved but attributable stage_ids
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parallel_stages_emit_distinct_stage_ids():
    sink = _recording_sink()
    p = Pipeline("p").parallel("par", stages=[
        Stage("pa", _SuccessStage()),
        Stage("pb", _SuccessStage()),
    ])
    await run_pipeline(p, event_sink=sink)

    finished = [e for e in sink.lossless_events if isinstance(e, StageFinished)]
    stage_ids = {e.key.stage_id for e in finished}
    # Parallel-body stages carry the fully-qualified graph node id so the TUI
    # attributes them to the nested node (matches norn.graph.build_graph).
    assert "parallel:par/stage:pa" in stage_ids
    assert "parallel:par/stage:pb" in stage_ids


@pytest.mark.asyncio
async def test_parallel_stages_both_appear_in_events():
    sink = _recording_sink()
    p = Pipeline("p").parallel("par", stages=[
        Stage("x", _SuccessStage()),
        Stage("y", _SuccessStage()),
    ])
    await run_pipeline(p, event_sink=sink)

    started_names = {e.name for e in sink.lossless_events if isinstance(e, StageStarted)}
    finished_names = {e.name for e in sink.lossless_events if isinstance(e, StageFinished)}
    assert "x" in started_names and "y" in started_names
    assert "x" in finished_names and "y" in finished_names


# ---------------------------------------------------------------------------
# Skipped / cached stages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skipped_stage_emits_stage_finished_skipped():
    sink = _recording_sink()
    p = Pipeline("p").stage("s1", _SuccessStage(), skip=True)
    await run_pipeline(p, params={"skip": {"s1"}}, event_sink=sink)

    fin = [e for e in sink.lossless_events if isinstance(e, StageFinished) and e.name == "s1"]
    assert len(fin) == 1
    assert fin[0].status == "skipped"
    assert fin[0].success is True


# ---------------------------------------------------------------------------
# Agent event node_id identity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_nested_generate_agent_events_carry_qualified_stage_id(fake_provider):
    """CallingAgent/TurnEvent/GotReply from a loop-body Generate carry the
    fully-qualified node id (loop:<L>/stage:<name>), matching StageStarted /
    StageFinished.  A mismatch here blanks the TUI transcript the moment the
    stage finishes (the view model stores blocks under the flat id, then
    StageFinished looks up the qualified id and finds nothing)."""
    fake_provider.events = [AgentEvent(text="hello")]

    sink = _recording_sink()
    p = Pipeline("p").loop("myloop", max_retries=1, stages=[
        Stage("gen", Generate(prompt="hi")),
    ])
    await run_pipeline(p, agent_provider=fake_provider.name, event_sink=sink)

    expected_id = "loop:myloop/stage:gen"

    # StageStarted / StageFinished must use the nested id (existing behaviour).
    started_ids = {e.key.stage_id for e in sink.lossless_events if isinstance(e, StageStarted)}
    finished_ids = {e.key.stage_id for e in sink.lossless_events if isinstance(e, StageFinished)}
    assert expected_id in started_ids
    assert expected_id in finished_ids

    # Agent lifecycle events must use the same id — not the flat "stage:gen".
    calling_ids = {e.key.stage_id for e in sink.lossless_events if isinstance(e, CallingAgent)}
    got_reply_ids = {e.key.stage_id for e in sink.lossless_events if isinstance(e, GotReply)}
    turn_ids = {e.key.stage_id for e in sink.transcript(stage_id=expected_id, attempt=1)}
    assert calling_ids == {expected_id}, f"CallingAgent stage_ids: {calling_ids}"
    assert got_reply_ids == {expected_id}, f"GotReply stage_ids: {got_reply_ids}"
    assert len(turn_ids) == 0 or turn_ids == {expected_id}

    # TurnEvents are stored in the sink under the qualified id.
    turn_events = sink.transcript(stage_id=expected_id, attempt=1)
    assert len(turn_events) == 1


@pytest.mark.asyncio
async def test_parallel_nested_generate_agent_events_carry_qualified_stage_id(fake_provider):
    """Same contract as the loop test but for a Parallel body."""
    fake_provider.events = [AgentEvent(text="par")]

    sink = _recording_sink()
    p = Pipeline("p").parallel("mypar", stages=[
        Stage("gen", Generate(prompt="hi")),
    ])
    await run_pipeline(p, agent_provider=fake_provider.name, event_sink=sink)

    expected_id = "parallel:mypar/stage:gen"

    started_ids = {e.key.stage_id for e in sink.lossless_events if isinstance(e, StageStarted)}
    finished_ids = {e.key.stage_id for e in sink.lossless_events if isinstance(e, StageFinished)}
    assert expected_id in started_ids
    assert expected_id in finished_ids

    calling_ids = {e.key.stage_id for e in sink.lossless_events if isinstance(e, CallingAgent)}
    got_reply_ids = {e.key.stage_id for e in sink.lossless_events if isinstance(e, GotReply)}
    assert calling_ids == {expected_id}, f"CallingAgent stage_ids: {calling_ids}"
    assert got_reply_ids == {expected_id}, f"GotReply stage_ids: {got_reply_ids}"

    turn_events = sink.transcript(stage_id=expected_id, attempt=1)
    assert len(turn_events) == 1


@pytest.mark.asyncio
async def test_user_driven_retry_increments_attempt(fake_provider):
    """A user-driven retry (ASK_USER → 'r') runs the stage with attempt=2 on
    the second call.  The two attempts must have distinct EventKeys so turn
    events from both attempts never collide in the spool."""
    from norn.responder import InputResponder  # noqa: PLC0415

    class _RetryOnceResponder(InputResponder):
        def __init__(self) -> None:
            self._calls = 0

        async def ask_failure(self, name: str, error: str | None) -> str:
            self._calls += 1
            # First failure → retry; subsequent failures → continue.
            return "r" if self._calls == 1 else "c"

        async def ask_budget(self, tracker, budget) -> str:
            return "c"

        async def ask_step(self, stage, ctx, *, session_id=None) -> str:
            return "r"

    flaky = _FailThenSucceed(fail_count=1)
    sink = _recording_sink()
    p = Pipeline("p").stage("s1", flaky, on_failure=OnFailure.ASK_USER)
    await run_pipeline(p, event_sink=sink, input_responder=_RetryOnceResponder())

    # Collect StageStarted attempts for "s1".
    started = [e for e in sink.lossless_events if isinstance(e, StageStarted) and e.name == "s1"]
    # attempt=1 on first run, attempt=2 on retry.
    attempts = [e.key.attempt for e in started]
    assert 1 in attempts, f"Expected attempt=1 in {attempts}"
    assert 2 in attempts, f"Expected attempt=2 in {attempts}"
    # The EventKeys must differ so neither attempt's events overwrite the other.
    keys = [e.key for e in started]
    assert keys[0] != keys[1], "Retry must produce a distinct EventKey"

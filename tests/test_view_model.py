"""Tests for the Textual-free RunViewModel.

All tests are offline — no SDK, no subprocess, no network.  The ViewModel
is exercised by feeding synthetic event sequences and asserting the
projected state.
"""

from __future__ import annotations

import time
import types

import pytest

from norn.agents.base import AgentEvent, TextBlock, ToolResultBlock, ToolUseBlock
from norn.events import (
    CommandOutput,
    EventKey,
    LoopExhausted,
    RunError,
    RunFinished,
    RunStarted,
    StageFinished,
    StageRetrying,
    StageStarted,
    TurnEvent,
    UsageUpdated,
    WaitingInput,
)
from norn.tui.viewmodel import HeaderSummary, RunViewModel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _key(
    *,
    run_id: str = "run-abc",
    unit_id: str = "unit-0",
    stage_id: str | None = None,
    attempt: int = 1,
    seq: int = 0,
) -> EventKey:
    return EventKey(
        run_id=run_id,
        unit_id=unit_id,
        stage_id=stage_id,
        attempt=attempt,
        seq=seq,
    )


def _run_started(*, pipeline_name: str = "my-pipe", provider: str = "claude-code") -> RunStarted:
    return RunStarted(
        key=_key(),
        pipeline_name=pipeline_name,
        provider=provider,
    )


def _stage_started(stage_id: str, name: str, attempt: int = 1) -> StageStarted:
    return StageStarted(
        key=_key(stage_id=stage_id, attempt=attempt),
        name=name,
        attempt=attempt,
    )


def _stage_finished(
    stage_id: str,
    name: str,
    *,
    status: str = "passed",
    success: bool = True,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
    error: str | None = None,
    attempt: int = 1,
) -> StageFinished:
    return StageFinished(
        key=_key(stage_id=stage_id, attempt=attempt),
        name=name,
        status=status,
        success=success,
        duration_ms=100,
        usage_input_tokens=input_tokens,
        usage_output_tokens=output_tokens,
        usage_cost_usd=cost_usd,
        error=error,
    )


def _turn_text(stage_id: str, text: str, seq: int = 0) -> TurnEvent:
    return TurnEvent(
        key=_key(stage_id=stage_id, seq=seq),
        event=AgentEvent(text=text),
    )


def _turn_block(stage_id: str, block, seq: int = 0) -> TurnEvent:
    return TurnEvent(
        key=_key(stage_id=stage_id, seq=seq),
        event=AgentEvent(block=block),
    )


def _run_finished(*, success: bool = True) -> RunFinished:
    return RunFinished(key=_key(), success=success)


# ---------------------------------------------------------------------------
# Module import guard: no textual reachable from viewmodel
# ---------------------------------------------------------------------------


def test_viewmodel_module_has_no_textual_import():
    """Importing the ViewModel must not pull in textual."""
    import norn.tui.viewmodel as vm_mod

    # Walk the module's direct imports and confirm textual is absent
    for name, obj in vars(vm_mod).items():
        if isinstance(obj, types.ModuleType):
            assert "textual" not in obj.__name__, (
                f"viewmodel.py imported textual via {name!r}"
            )


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------


def test_initial_state():
    vm = RunViewModel()
    assert vm.header.status == "pending"
    assert vm.header.pipeline_name == ""
    assert vm.header.run_id == ""
    assert vm.header.provider == ""
    assert vm.node_status == {}
    assert vm.transcript == {}
    assert vm.total_input_tokens == 0
    assert vm.total_output_tokens == 0
    assert vm.total_cost_usd == 0.0
    assert vm.waiting_input is None
    assert vm.last_error is None


# ---------------------------------------------------------------------------
# RunStarted
# ---------------------------------------------------------------------------


def test_run_started_populates_header():
    vm = RunViewModel()
    vm.apply(_run_started(pipeline_name="hello", provider="opencode"))

    assert vm.header.pipeline_name == "hello"
    assert vm.header.run_id == "run-abc"
    assert vm.header.provider == "opencode"
    assert vm.header.status == "running"


def test_run_started_sets_start_time():
    vm = RunViewModel()
    before = time.monotonic()
    vm.apply(_run_started())
    after = time.monotonic()

    assert vm._start_time is not None
    assert before <= vm._start_time <= after


# ---------------------------------------------------------------------------
# StageStarted / StageFinished — node_status transitions
# ---------------------------------------------------------------------------


def test_stage_started_sets_running():
    vm = RunViewModel()
    vm.apply(_run_started())
    vm.apply(_stage_started("stage:build", "build"))

    assert vm.node_status["stage:build"] == "running"
    assert vm.header.stages_started == 1


def test_stage_finished_passed_updates_status():
    vm = RunViewModel()
    vm.apply(_run_started())
    vm.apply(_stage_started("stage:build", "build"))
    vm.apply(_stage_finished("stage:build", "build", status="passed"))

    assert vm.node_status["stage:build"] == "passed"
    assert vm.header.stages_done == 1


def test_stage_finished_failed_updates_status():
    vm = RunViewModel()
    vm.apply(_run_started())
    vm.apply(_stage_started("stage:test", "test"))
    vm.apply(_stage_finished("stage:test", "test", status="failed", success=False))

    assert vm.node_status["stage:test"] == "failed"


def test_stage_finished_skipped_updates_status():
    vm = RunViewModel()
    vm.apply(_run_started())
    vm.apply(_stage_started("stage:lint", "lint"))
    vm.apply(_stage_finished("stage:lint", "lint", status="skipped"))

    assert vm.node_status["stage:lint"] == "skipped"


def test_multiple_stages_track_independently():
    vm = RunViewModel()
    vm.apply(_run_started())
    vm.apply(_stage_started("stage:a", "a"))
    vm.apply(_stage_finished("stage:a", "a", status="passed"))
    vm.apply(_stage_started("stage:b", "b"))
    vm.apply(_stage_finished("stage:b", "b", status="failed", success=False))

    assert vm.node_status["stage:a"] == "passed"
    assert vm.node_status["stage:b"] == "failed"
    assert vm.header.stages_done == 2
    assert vm.header.stages_started == 2


# ---------------------------------------------------------------------------
# StageRetrying
# ---------------------------------------------------------------------------


def test_stage_retrying_sets_status():
    vm = RunViewModel()
    vm.apply(_run_started())
    vm.apply(_stage_started("stage:flaky", "flaky"))
    vm.apply(StageRetrying(key=_key(stage_id="stage:flaky"), next_attempt=2, reason="test failed"))

    assert vm.node_status["stage:flaky"] == "retrying"


# ---------------------------------------------------------------------------
# TurnEvent — transcript accumulation
# ---------------------------------------------------------------------------


def test_turn_text_event_appended_as_text_block():
    vm = RunViewModel()
    vm.apply(_run_started())
    vm.apply(_stage_started("stage:gen", "gen"))
    vm.apply(_turn_text("stage:gen", "Hello world", seq=0))

    blocks = vm.transcript["stage:gen"]
    assert len(blocks) == 1
    assert isinstance(blocks[0], TextBlock)
    assert blocks[0].text == "Hello world"


def test_turn_block_event_appended_directly():
    vm = RunViewModel()
    vm.apply(_run_started())
    vm.apply(_stage_started("stage:gen", "gen"))

    tool_block = ToolUseBlock(name="write_file", input_summary='{"path": "foo.py"}')
    vm.apply(_turn_block("stage:gen", tool_block, seq=0))

    blocks = vm.transcript["stage:gen"]
    assert len(blocks) == 1
    assert isinstance(blocks[0], ToolUseBlock)
    assert blocks[0].name == "write_file"


def test_turn_result_block_appended():
    vm = RunViewModel()
    vm.apply(_run_started())
    vm.apply(_stage_started("stage:gen", "gen"))

    result_block = ToolResultBlock(ok=True, summary="wrote foo.py")
    vm.apply(_turn_block("stage:gen", result_block, seq=1))

    blocks = vm.transcript["stage:gen"]
    assert blocks[0] is result_block


def test_transcript_maintains_order():
    vm = RunViewModel()
    vm.apply(_run_started())
    vm.apply(_stage_started("stage:gen", "gen"))

    vm.apply(_turn_text("stage:gen", "chunk-1", seq=0))
    vm.apply(_turn_text("stage:gen", "chunk-2", seq=1))
    tool_block = ToolUseBlock(name="bash", input_summary="ls")
    vm.apply(_turn_block("stage:gen", tool_block, seq=2))
    vm.apply(_turn_text("stage:gen", "after tool", seq=3))

    blocks = vm.transcript["stage:gen"]
    assert len(blocks) == 4
    assert isinstance(blocks[0], TextBlock)
    assert blocks[0].text == "chunk-1"
    assert isinstance(blocks[2], ToolUseBlock)
    assert isinstance(blocks[3], TextBlock)


def test_transcript_isolated_per_stage():
    vm = RunViewModel()
    vm.apply(_run_started())
    vm.apply(_stage_started("stage:a", "a"))
    vm.apply(_turn_text("stage:a", "text for a", seq=0))
    vm.apply(_stage_finished("stage:a", "a"))
    vm.apply(_stage_started("stage:b", "b"))
    vm.apply(_turn_text("stage:b", "text for b", seq=0))

    assert len(vm.transcript["stage:a"]) == 1
    assert len(vm.transcript["stage:b"]) == 1
    assert vm.transcript["stage:a"][0].text == "text for a"
    assert vm.transcript["stage:b"][0].text == "text for b"


def test_turn_event_without_stage_id_ignored():
    vm = RunViewModel()
    vm.apply(_run_started())
    # stage_id=None — run-level event, should not crash
    vm.apply(TurnEvent(key=_key(), event=AgentEvent(text="orphan")))

    assert vm.transcript == {}


# ---------------------------------------------------------------------------
# Usage totals
# ---------------------------------------------------------------------------


def test_usage_accumulated_from_usage_updated():
    """Run totals come from UsageUpdated cumulative events, not StageFinished per-stage sums."""
    vm = RunViewModel()
    vm.apply(_run_started())
    vm.apply(_stage_started("stage:a", "a"))
    # Runner emits UsageUpdated (cumulative) then StageFinished (per-stage)
    vm.apply(UsageUpdated(key=_key(stage_id="stage:a"), input_tokens=100, output_tokens=50, total_cost_usd=0.01))
    vm.apply(_stage_finished("stage:a", "a", input_tokens=100, output_tokens=50, cost_usd=0.01))
    vm.apply(_stage_started("stage:b", "b"))
    vm.apply(UsageUpdated(key=_key(stage_id="stage:b"), input_tokens=300, output_tokens=130, total_cost_usd=0.03))
    vm.apply(_stage_finished("stage:b", "b", input_tokens=200, output_tokens=80, cost_usd=0.02))

    assert vm.total_input_tokens == 300
    assert vm.total_output_tokens == 130
    assert pytest.approx(vm.total_cost_usd, rel=1e-6) == 0.03


def test_header_usage_updated_after_usage_updated():
    """Header usage fields reflect the most recent UsageUpdated cumulative totals."""
    vm = RunViewModel()
    vm.apply(_run_started())
    vm.apply(_stage_started("stage:x", "x"))
    vm.apply(UsageUpdated(key=_key(stage_id="stage:x"), input_tokens=500, output_tokens=100, total_cost_usd=0.05))
    vm.apply(_stage_finished("stage:x", "x", input_tokens=500, output_tokens=100, cost_usd=0.05))

    assert vm.header.total_input_tokens == 500
    assert vm.header.total_output_tokens == 100
    assert pytest.approx(vm.header.total_cost_usd, rel=1e-6) == 0.05


def test_usage_updated_shows_live_running_total():
    """UsageUpdated is latest-wins (coalescible); it updates current-stage total."""
    vm = RunViewModel()
    vm.apply(_run_started())
    vm.apply(_stage_started("stage:gen", "gen"))

    # First live update
    vm.apply(UsageUpdated(key=_key(stage_id="stage:gen"), input_tokens=50, output_tokens=10, total_cost_usd=0.005))
    assert vm.total_input_tokens == 50

    # Second live update — replaces, not adds
    vm.apply(UsageUpdated(key=_key(stage_id="stage:gen"), input_tokens=120, output_tokens=30, total_cost_usd=0.012))
    assert vm.total_input_tokens == 120
    assert vm.total_output_tokens == 30


def test_usage_updated_is_authoritative_run_total():
    """UsageUpdated (cumulative) is the authoritative run total; StageFinished carries
    per-stage figures for StageDetailRecord and does NOT update run totals."""
    vm = RunViewModel()
    vm.apply(_run_started())
    vm.apply(_stage_started("stage:gen", "gen"))
    vm.apply(UsageUpdated(key=_key(stage_id="stage:gen"), input_tokens=200, output_tokens=50, total_cost_usd=0.02))

    # StageFinished arrives with slightly different per-stage figures.
    # The run total must equal the UsageUpdated value, not the StageFinished value.
    vm.apply(_stage_finished("stage:gen", "gen", input_tokens=210, output_tokens=55, cost_usd=0.021))

    # Run totals come from UsageUpdated — not accumulated from StageFinished.
    assert vm.total_input_tokens == 200
    assert vm.total_output_tokens == 50
    assert pytest.approx(vm.total_cost_usd, rel=1e-6) == 0.02
    # Per-stage figures are in the detail record.
    assert vm.stage_details["stage:gen"].usage_input_tokens == 210


def test_cached_stages_keep_counters_consistent():
    """stages_done never exceeds stages_started even when cached/skipped stages
    emit StageFinished with no preceding StageStarted.

    Scenario: two normal stages and three cached stages → header shows 5/5.
    """
    vm = RunViewModel()
    vm.apply(_run_started())

    # Two normal stages (StageStarted + StageFinished each)
    vm.apply(_stage_started("stage:a", "a"))
    vm.apply(_stage_finished("stage:a", "a", status="passed"))
    vm.apply(_stage_started("stage:b", "b"))
    vm.apply(_stage_finished("stage:b", "b", status="passed"))

    # Three cached stages — bare StageFinished, no StageStarted
    for name in ("c", "d", "e"):
        vm.apply(_stage_finished(f"stage:{name}", name, status="cached"))
        # done must never exceed started after each event
        assert vm.header.stages_done <= vm.header.stages_started

    assert vm.header.stages_done == 5
    assert vm.header.stages_started == 5


def test_usage_run_total_is_last_cumulative_not_sum():
    """UsageUpdated carries cumulative run totals; the viewmodel must NOT add
    them on top of previously-finished stage usage.

    Two $1 stages: UsageUpdated emits $1 then $2 (cumulative).
    Run total should be $2, not $3 (which would happen if $1 from stage-1
    were kept in _finished_* and $2 from stage-2 added on top).
    """
    vm = RunViewModel()
    vm.apply(_run_started())

    # Stage 1: cumulative total after = $1
    vm.apply(_stage_started("stage:x", "x"))
    vm.apply(UsageUpdated(key=_key(stage_id="stage:x"), input_tokens=100, output_tokens=20, total_cost_usd=1.0))
    vm.apply(_stage_finished("stage:x", "x", input_tokens=100, output_tokens=20, cost_usd=1.0))

    assert pytest.approx(vm.total_cost_usd, rel=1e-6) == 1.0

    # Stage 2: cumulative total after = $2 (tracker adds stage-2's $1 on top)
    vm.apply(_stage_started("stage:y", "y"))
    vm.apply(UsageUpdated(key=_key(stage_id="stage:y"), input_tokens=200, output_tokens=40, total_cost_usd=2.0))
    vm.apply(_stage_finished("stage:y", "y", input_tokens=100, output_tokens=20, cost_usd=1.0))

    # Run total must equal the last UsageUpdated value, not stage-1 + stage-2 = $3.
    assert pytest.approx(vm.total_cost_usd, rel=1e-6) == 2.0
    assert vm.total_input_tokens == 200
    assert vm.total_output_tokens == 40


def test_zero_cost_run_still_shows_tokens():
    """Zero-cost runs (subscription/opencode) must expose token counts, not zeros."""
    vm = RunViewModel()
    vm.apply(_run_started(provider="opencode"))
    vm.apply(_stage_started("stage:build", "build"))
    # Runner emits UsageUpdated with cumulative tracker totals (cost may be 0)
    vm.apply(UsageUpdated(key=_key(stage_id="stage:build"), input_tokens=5000, output_tokens=1000, total_cost_usd=0.0))
    vm.apply(_stage_finished("stage:build", "build", input_tokens=5000, output_tokens=1000, cost_usd=0.0))

    assert vm.total_input_tokens == 5000
    assert vm.total_output_tokens == 1000
    assert vm.total_cost_usd == 0.0
    assert vm.header.total_input_tokens == 5000


# ---------------------------------------------------------------------------
# WaitingInput
# ---------------------------------------------------------------------------


def test_waiting_input_captured():
    vm = RunViewModel()
    vm.apply(_run_started())
    vm.apply(_stage_started("stage:ask", "ask"))

    event = WaitingInput(key=_key(stage_id="stage:ask"), kind="budget", prompt_excerpt="Budget exceeded")
    vm.apply(event)

    assert vm.waiting_input is event
    assert vm.waiting_input.kind == "budget"


def test_waiting_input_overwritten_by_later_event():
    vm = RunViewModel()
    vm.apply(_run_started())

    first = WaitingInput(key=_key(), kind="step", prompt_excerpt="Continue?")
    second = WaitingInput(key=_key(), kind="failure_recovery", prompt_excerpt="Retry?")
    vm.apply(first)
    vm.apply(second)

    assert vm.waiting_input is second


# ---------------------------------------------------------------------------
# RunError
# ---------------------------------------------------------------------------


def test_run_error_captured():
    vm = RunViewModel()
    vm.apply(_run_started())
    err = RunError(key=_key(), error_kind="StageFailed", detail="stage blew up")
    vm.apply(err)

    assert vm.last_error is err
    assert vm.last_error.error_kind == "StageFailed"


# ---------------------------------------------------------------------------
# RunFinished
# ---------------------------------------------------------------------------


def test_run_finished_sets_passed():
    vm = RunViewModel()
    vm.apply(_run_started())
    vm.apply(_stage_started("stage:a", "a"))
    vm.apply(_stage_finished("stage:a", "a"))
    vm.apply(_run_finished(success=True))

    assert vm.header.status == "passed"


def test_run_finished_sets_failed():
    vm = RunViewModel()
    vm.apply(_run_started())
    vm.apply(_stage_finished("stage:a", "a", status="failed", success=False))
    vm.apply(_run_finished(success=False))

    assert vm.header.status == "failed"


def test_run_finished_captures_elapsed():
    vm = RunViewModel()
    vm.apply(_run_started())
    time.sleep(0.01)  # ensure measurable elapsed time
    vm.apply(_run_finished())

    assert vm.header.elapsed_s > 0.0


# ---------------------------------------------------------------------------
# Full synthetic sequence
# ---------------------------------------------------------------------------


def test_full_sequence_run_started_to_run_finished():
    """A realistic sequence: RunStarted → stage → turn events → finish → retry → finish."""
    vm = RunViewModel()

    # Run starts
    vm.apply(RunStarted(key=_key(), pipeline_name="ci", provider="claude-code"))
    assert vm.header.status == "running"

    # Stage 1 starts
    vm.apply(_stage_started("stage:lint", "lint"))
    assert vm.node_status["stage:lint"] == "running"

    # A text turn and a tool block arrive
    vm.apply(_turn_text("stage:lint", "Running linter…", seq=0))
    tool = ToolUseBlock(name="bash", input_summary="flake8 .")
    vm.apply(_turn_block("stage:lint", tool, seq=1))
    result = ToolResultBlock(ok=True, summary="No issues")
    vm.apply(_turn_block("stage:lint", result, seq=2))
    vm.apply(_turn_text("stage:lint", "Done.", seq=3))

    # Live usage update during stage (coalescible — latest-wins cumulative total)
    vm.apply(UsageUpdated(key=_key(stage_id="stage:lint"), input_tokens=80, output_tokens=20, total_cost_usd=0.008))
    assert vm.total_input_tokens == 80

    # Final UsageUpdated just before StageFinished (runner emits cumulative tracker total)
    vm.apply(UsageUpdated(key=_key(stage_id="stage:lint"), input_tokens=100, output_tokens=25, total_cost_usd=0.01))
    # Stage 1 passes
    vm.apply(_stage_finished("stage:lint", "lint", input_tokens=100, output_tokens=25, cost_usd=0.01))
    assert vm.node_status["stage:lint"] == "passed"
    assert vm.total_input_tokens == 100  # authoritative from UsageUpdated

    # Stage 2 starts, fails, then retries
    vm.apply(_stage_started("stage:test", "test", attempt=1))
    vm.apply(_stage_finished("stage:test", "test", status="failed", success=False, attempt=1))
    assert vm.node_status["stage:test"] == "failed"

    vm.apply(StageRetrying(key=_key(stage_id="stage:test"), next_attempt=2, reason="test suite failed"))
    assert vm.node_status["stage:test"] == "retrying"

    vm.apply(_stage_started("stage:test", "test", attempt=2))
    assert vm.node_status["stage:test"] == "running"

    vm.apply(_turn_text("stage:test", "Re-running tests…", seq=0))
    # Runner emits cumulative UsageUpdated before StageFinished
    vm.apply(UsageUpdated(key=_key(stage_id="stage:test"), input_tokens=250, output_tokens=65, total_cost_usd=0.025))
    vm.apply(_stage_finished("stage:test", "test", status="passed", success=True,
                             input_tokens=150, output_tokens=40, cost_usd=0.015, attempt=2))
    assert vm.node_status["stage:test"] == "passed"

    # Run finishes
    vm.apply(RunFinished(key=_key(), success=True))
    assert vm.header.status == "passed"

    # Header fields
    assert vm.header.pipeline_name == "ci"
    assert vm.header.provider == "claude-code"
    assert vm.header.stages_done == 3  # lint + test(fail) + test(retry)
    assert vm.total_input_tokens == 250   # last UsageUpdated cumulative: 100 (lint) + 150 (retry)
    assert vm.total_output_tokens == 65
    assert pytest.approx(vm.total_cost_usd, rel=1e-6) == 0.025

    # Transcript
    lint_blocks = vm.transcript["stage:lint"]
    assert len(lint_blocks) == 4
    assert isinstance(lint_blocks[0], TextBlock)
    assert isinstance(lint_blocks[1], ToolUseBlock)
    assert isinstance(lint_blocks[2], ToolResultBlock)
    assert isinstance(lint_blocks[3], TextBlock)

    test_blocks = vm.transcript["stage:test"]
    assert len(test_blocks) == 1
    assert test_blocks[0].text == "Re-running tests…"


# ---------------------------------------------------------------------------
# Unknown events are silently ignored
# ---------------------------------------------------------------------------


def test_unknown_event_type_does_not_raise():
    vm = RunViewModel()

    class _Alien:
        pass

    vm.apply(_Alien())  # must not raise
    assert vm.header.status == "pending"  # state unchanged


def test_none_op_events_do_not_mutate_state():
    """Events like CallingAgent and GotReply are in the dispatch table but have
    no projection — node_status must not change because of them."""
    from norn.events import CallingAgent, GotReply

    vm = RunViewModel()
    vm.apply(_run_started())
    # RunStarted marks the Tree root node running; that is the only entry.
    baseline = dict(vm.node_status)
    assert baseline == {"pipeline:my-pipe": "running"}

    vm.apply(CallingAgent(key=_key(), stage_name="build", provider="claude-code"))
    vm.apply(GotReply(key=_key(), stage_name="build", elapsed_s=1.2))

    # None of these change the header status or node_status
    assert vm.header.status == "running"
    assert vm.node_status == baseline


def test_container_and_clear_nodes_get_status():
    """Loop, parallel and clear-context container nodes are projected from
    their lifecycle events using the graph node id carried in the EventKey."""
    from norn.events import (
        ClearContextNotice,
        LoopAttempt,
        LoopSuccess,
        ParallelDone,
        ParallelStarted,
    )

    vm = RunViewModel()
    vm.apply(_run_started())

    vm.apply(LoopAttempt(key=_key(stage_id="loop:test"), name="test", attempt=1, max_retries=3))
    assert vm.node_status["loop:test"] == "running"
    vm.apply(LoopSuccess(key=_key(stage_id="loop:test"), name="test"))
    assert vm.node_status["loop:test"] == "passed"

    vm.apply(ParallelStarted(key=_key(stage_id="parallel:p"), name="p", stage_count=2))
    assert vm.node_status["parallel:p"] == "running"
    vm.apply(ParallelDone(key=_key(stage_id="parallel:p"), name="p"))
    assert vm.node_status["parallel:p"] == "passed"

    vm.apply(ClearContextNotice(key=_key(stage_id="clear:0")))
    assert vm.node_status["clear:0"] == "passed"


# ---------------------------------------------------------------------------
# HeaderSummary is a plain dataclass
# ---------------------------------------------------------------------------


def test_header_summary_defaults():
    h = HeaderSummary()
    assert h.pipeline_name == ""
    assert h.status == "pending"
    assert h.stages_done == 0
    assert h.total_cost_usd == 0.0


# ---------------------------------------------------------------------------
# Streamed command output
# ---------------------------------------------------------------------------


def _command_output(stage_id: str | None, text: str, *, seq: int = 0, stream: str = "stdout"):
    return CommandOutput(key=_key(stage_id=stage_id, seq=seq), text=text, stream=stream)


def test_command_output_appended_to_transcript_as_text_block():
    """Shell output shares the transcript with agent prose."""
    vm = RunViewModel()
    vm.apply(_run_started())
    vm.apply(_stage_started("stage:build", "build"))
    vm.apply(_command_output("stage:build", "[INFO] Building", seq=1))
    vm.apply(_command_output("stage:build", "[ERROR] boom", seq=2, stream="stderr"))

    blocks = vm.transcript["stage:build"]
    assert [type(b) for b in blocks] == [TextBlock, TextBlock]
    assert [b.text for b in blocks] == ["[INFO] Building", "[ERROR] boom"]


def test_command_output_without_stage_id_ignored():
    """RunCommand driven outside the runner has no node to file output under."""
    vm = RunViewModel()
    vm.apply(_run_started())
    vm.apply(_command_output(None, "orphan output"))

    assert vm.transcript == {}

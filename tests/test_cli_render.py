"""Tests for the CLI renderer (event-driven Rich output)."""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from norn.cli_render import CLIRenderer
from norn.dsl import Budget
from norn.events import (
    CallingAgent,
    ClearContextNotice,
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
    RunStarted,
    StageFinished,
    StageStarted,
    TurnEvent,
    UsageUpdated,
)


def _key(**kwargs) -> EventKey:
    return EventKey(run_id="r1", unit_id="unit-0", **kwargs)


def _make_renderer(*, budgets=None) -> tuple[CLIRenderer, io.StringIO]:
    """Create a renderer that writes to a string buffer for assertions."""
    buf = io.StringIO()
    console = Console(file=buf, highlight=False, width=120)
    renderer = CLIRenderer(budgets=budgets, console=console)
    return renderer, buf


# -- Stage lifecycle events ---------------------------------------------------


def test_stage_success_renders():
    r, buf = _make_renderer()
    r(StageFinished(
        key=_key(stage_id="stage:build"),
        name="build",
        status="passed",
        success=True,
        duration_ms=1200,
        usage_input_tokens=5000,
        usage_output_tokens=1000,
        usage_cost_usd=0.05,
    ))
    out = buf.getvalue()
    assert "build" in out
    assert "1.2s" in out
    assert "$0.05" in out


def test_stage_failure_renders_error_tail():
    r, buf = _make_renderer()
    error_lines = "\n".join(f"line {i}" for i in range(10))
    r(StageFinished(
        key=_key(stage_id="stage:test"),
        name="test",
        status="failed",
        success=False,
        duration_ms=500,
        error=error_lines,
    ))
    out = buf.getvalue()
    assert "test" in out
    assert "0.5s" in out
    # Should show last 5 lines (tail), not all 10
    assert "line 9" in out
    assert "line 5" in out


def test_stage_skipped_renders():
    r, buf = _make_renderer()
    r(StageFinished(
        key=_key(stage_id="stage:deploy"),
        name="deploy",
        status="skipped",
        success=True,
    ))
    out = buf.getvalue()
    assert "deploy" in out
    assert "skipped" in out


def test_stage_skipped_condition_renders():
    r, buf = _make_renderer()
    r(StageFinished(
        key=_key(stage_id="stage:deploy"),
        name="deploy",
        status="skipped_condition",
        success=True,
    ))
    out = buf.getvalue()
    assert "deploy" in out
    assert "condition not met" in out


def test_stage_cached_renders():
    r, buf = _make_renderer()
    r(StageFinished(
        key=_key(stage_id="stage:build"),
        name="build",
        status="cached",
        success=True,
    ))
    out = buf.getvalue()
    assert "build" in out
    assert "cached" in out


def test_stage_success_with_artifacts():
    r, buf = _make_renderer()
    r(StageFinished(
        key=_key(stage_id="stage:gen"),
        name="gen",
        status="passed",
        success=True,
        duration_ms=100,
        artifacts=["src/main.py", "src/util.py"],
    ))
    out = buf.getvalue()
    assert "src/main.py" in out
    assert "src/util.py" in out


# -- Running total (zero-cost fix) -------------------------------------------


def test_running_total_with_cost():
    """Running total should show cost when cost > 0."""
    r, buf = _make_renderer()
    r(UsageUpdated(key=_key(), input_tokens=5000, output_tokens=1000, total_cost_usd=0.12))
    r(StageFinished(
        key=_key(stage_id="stage:s1"),
        name="s1",
        status="passed",
        success=True,
        duration_ms=100,
    ))
    out = buf.getvalue()
    assert "Running total" in out
    assert "$0.12" in out


def test_running_total_zero_cost_shows_tokens():
    """Zero-cost run should show token count instead of suppressing the line."""
    r, buf = _make_renderer()
    r(UsageUpdated(key=_key(), input_tokens=10000, output_tokens=5000, total_cost_usd=0.0))
    r(StageFinished(
        key=_key(stage_id="stage:s1"),
        name="s1",
        status="passed",
        success=True,
        duration_ms=100,
    ))
    out = buf.getvalue()
    assert "Running total" in out
    assert "15,000 tokens" in out


def test_running_total_no_usage_suppressed():
    """No running total when there's no accumulated usage at all."""
    r, buf = _make_renderer()
    r(StageFinished(
        key=_key(stage_id="stage:s1"),
        name="s1",
        status="passed",
        success=True,
        duration_ms=100,
    ))
    out = buf.getvalue()
    assert "Running total" not in out


def test_running_total_with_budget_percentage():
    """Running total should show budget percentage when budgets are provided."""
    budgets = [Budget(max_cost_usd=1.00)]
    r, buf = _make_renderer(budgets=budgets)
    r(UsageUpdated(key=_key(), input_tokens=5000, output_tokens=1000, total_cost_usd=0.50))
    r(StageFinished(
        key=_key(stage_id="stage:s1"),
        name="s1",
        status="passed",
        success=True,
        duration_ms=100,
    ))
    out = buf.getvalue()
    assert "$1.00" in out
    assert "50%" in out


def test_running_total_zero_cost_with_token_budget():
    """Zero-cost run with token budget shows token percentage."""
    budgets = [Budget(max_tokens=100000)]
    r, buf = _make_renderer(budgets=budgets)
    r(UsageUpdated(key=_key(), input_tokens=10000, output_tokens=5000, total_cost_usd=0.0))
    r(StageFinished(
        key=_key(stage_id="stage:s1"),
        name="s1",
        status="passed",
        success=True,
        duration_ms=100,
    ))
    out = buf.getvalue()
    assert "15,000 tokens" in out
    assert "100,000" in out
    assert "15%" in out


# -- Loop events --------------------------------------------------------------


def test_loop_attempt_renders():
    r, buf = _make_renderer()
    r(LoopAttempt(key=_key(), name="retry", attempt=2, max_retries=3))
    out = buf.getvalue()
    assert "retry" in out
    assert "attempt 2/3" in out


def test_loop_success_renders():
    r, buf = _make_renderer()
    r(LoopSuccess(key=_key(), name="retry"))
    out = buf.getvalue()
    assert "retry" in out
    assert "all stages passed" in out


def test_loop_exhausted_renders():
    r, buf = _make_renderer()
    r(LoopExhausted(key=_key(), loop_id="loop:retry"))
    out = buf.getvalue()
    assert "retry" in out
    assert "retries exhausted" in out


def test_loop_draft_pr_renders():
    r, buf = _make_renderer()
    r(LoopDraftPR(key=_key(), name="retry"))
    out = buf.getvalue()
    assert "retry" in out
    assert "draft PR" in out


# -- Parallel events ----------------------------------------------------------


def test_parallel_start_renders():
    r, buf = _make_renderer()
    r(ParallelStarted(key=_key(), name="tests", stage_count=3))
    out = buf.getvalue()
    assert "tests" in out
    assert "3 stages" in out


def test_parallel_done_renders():
    r, buf = _make_renderer()
    r(ParallelDone(key=_key(), name="tests"))
    out = buf.getvalue()
    assert "tests" in out
    assert "all parallel stages passed" in out


# -- Include events -----------------------------------------------------------


def test_include_start_renders():
    r, buf = _make_renderer()
    r(IncludeStarted(key=_key(), path="sub/pipe.py", isolated=True))
    out = buf.getvalue()
    assert "sub/pipe.py" in out
    assert "isolated" in out


def test_include_done_renders():
    r, buf = _make_renderer()
    r(IncludeDone(key=_key(), path="sub/pipe.py"))
    out = buf.getvalue()
    assert "sub/pipe.py" in out
    assert "done" in out


# -- Other events -------------------------------------------------------------


def test_clear_context_renders():
    r, buf = _make_renderer()
    r(ClearContextNotice(key=_key()))
    out = buf.getvalue()
    assert "clear_context" in out


def test_run_started_renders():
    r, buf = _make_renderer()
    r(RunStarted(key=_key(), pipeline_name="my_pipe", provider="claude-code"))
    out = buf.getvalue()
    assert "my_pipe" in out
    assert "starting" in out


def test_run_started_with_resume_session_prints_resume_line():
    """RunStarted with resume_session set should print 'Resuming session <id>'."""
    r, buf = _make_renderer()
    r(RunStarted(key=_key(), pipeline_name="my_pipe", provider="claude-code", resume_session="ses-abc123"))
    out = buf.getvalue()
    assert "Resuming session ses-abc123" in out


def test_run_started_without_resume_session_no_resume_line():
    """RunStarted without resume_session should not print any resume line."""
    r, buf = _make_renderer()
    r(RunStarted(key=_key(), pipeline_name="my_pipe", provider="claude-code"))
    out = buf.getvalue()
    assert "Resuming" not in out


def test_stage_started_renders():
    r, buf = _make_renderer()
    r(StageStarted(key=_key(stage_id="stage:build"), name="build"))
    out = buf.getvalue()
    assert "build" in out


def test_calling_agent_renders():
    r, buf = _make_renderer()
    r(CallingAgent(
        key=_key(stage_id="stage:gen"),
        stage_name="gen",
        provider="claude-code",
        model="sonnet",
    ))
    out = buf.getvalue()
    assert "calling agent" in out
    assert "claude-code" in out
    assert "sonnet" in out


def test_got_reply_renders():
    r, buf = _make_renderer()
    r(GotReply(key=_key(stage_id="stage:gen"), stage_name="gen", elapsed_s=2.5))
    out = buf.getvalue()
    assert "got reply" in out
    assert "2.5s" in out


# -- Streamed text (TurnEvent) -----------------------------------------------


def test_turn_event_streams_text(capsys):
    """TurnEvent text should be printed directly to stdout."""
    r, _ = _make_renderer()

    from norn.agents.base import AgentEvent

    r(TurnEvent(
        key=_key(stage_id="stage:gen", seq=1),
        event=AgentEvent(text="hello "),
    ))
    r(TurnEvent(
        key=_key(stage_id="stage:gen", seq=2),
        event=AgentEvent(text="world"),
    ))

    captured = capsys.readouterr()
    assert "hello world" in captured.out


def test_got_reply_ends_streaming(capsys):
    """GotReply should print trailing newline when streaming was active."""
    r, buf = _make_renderer()

    from norn.agents.base import AgentEvent

    r(TurnEvent(
        key=_key(stage_id="stage:gen", seq=1),
        event=AgentEvent(text="output"),
    ))
    r(GotReply(key=_key(stage_id="stage:gen"), stage_name="gen", elapsed_s=1.0))

    captured = capsys.readouterr()
    # The streamed text should end with a newline
    assert captured.out.endswith("\n")


# -- Full event sequence (integration) ----------------------------------------


def test_full_event_sequence():
    """Feed a realistic event sequence and verify key lines appear."""
    r, buf = _make_renderer()

    from norn.agents.base import AgentEvent

    # Pipeline start
    r(RunStarted(key=_key(), pipeline_name="integration", provider="claude-code"))

    # Stage 1: cached
    r(StageFinished(key=_key(stage_id="stage:s1"), name="s1", status="cached", success=True))

    # Stage 2: success with usage
    r(StageStarted(key=_key(stage_id="stage:s2"), name="s2"))
    r(CallingAgent(key=_key(stage_id="stage:s2"), stage_name="s2", provider="claude-code", model="sonnet"))
    r(TurnEvent(key=_key(stage_id="stage:s2", seq=1), event=AgentEvent(text="generated")))
    r(GotReply(key=_key(stage_id="stage:s2"), stage_name="s2", elapsed_s=3.0))
    r(UsageUpdated(key=_key(stage_id="stage:s2"), input_tokens=10000, output_tokens=2000, total_cost_usd=0.08))
    r(StageFinished(
        key=_key(stage_id="stage:s2"),
        name="s2",
        status="passed",
        success=True,
        duration_ms=3100,
        usage_input_tokens=10000,
        usage_output_tokens=2000,
        usage_cost_usd=0.08,
    ))

    # Stage 3: zero-cost usage
    r(UsageUpdated(key=_key(stage_id="stage:s3"), input_tokens=20000, output_tokens=5000, total_cost_usd=0.0))
    r(StageFinished(
        key=_key(stage_id="stage:s3"),
        name="s3",
        status="passed",
        success=True,
        duration_ms=500,
        usage_input_tokens=10000,
        usage_output_tokens=3000,
        usage_cost_usd=0.0,
    ))

    out = buf.getvalue()

    # Pipeline start
    assert "integration" in out
    assert "starting" in out

    # Cached stage
    assert "s1" in out
    assert "cached" in out

    # Successful stage
    assert "s2" in out
    assert "$0.08" in out
    assert "3.1s" in out

    # Running total after s2 (has cost)
    assert "$0.08" in out

    # Zero-cost running total after s3 — this is the key zero-cost-fix assertion
    assert "25,000 tokens" in out
    assert "Running total" in out

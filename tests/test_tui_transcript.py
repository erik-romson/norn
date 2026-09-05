"""Pilot tests for the Norn TUI Transcript, StageDetail, and BudgetMeter widgets.

All tests are offline: no SDK calls, no subprocesses, no network.

Contracts pinned here:

* ``Transcript`` renders ``ToolUseBlock`` as ``"tool <name> <summary>"`` and
  ``ToolResultBlock`` as ``"tool_result ok"`` or ``"tool_result err"``.
* ``BudgetMeter.get_content()`` shows a token figure (not blank, not ``$0.00``)
  when ``total_cost_usd == 0``.
"""
from __future__ import annotations

import pytest

from norn.agents.base import (
    AgentEvent,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from norn.dsl import Budget, Pipeline, Stage
from norn.events import EventKey, RunStarted, StageFinished, StageStarted, TurnEvent, UsageUpdated
from norn.graph import build_graph
from norn.models import PipelineContext, StageResult
from norn.stages.base import BaseStage
from norn.tui.app import NornApp
from norn.tui.viewmodel import RunViewModel
from norn.tui.widgets import BudgetMeter, StageDetail, Transcript


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class _Stub(BaseStage):
    async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
        return StageResult(name="", success=True)


def _key(*, run_id: str = "r1", stage_id: str | None = None, seq: int = 0) -> EventKey:
    return EventKey(run_id=run_id, unit_id="unit-0", stage_id=stage_id, seq=seq)


def _run_started(name: str = "test-pipe") -> RunStarted:
    return RunStarted(key=_key(), pipeline_name=name, provider="claude-code")


def _stage_started(stage_id: str, attempt: int = 1) -> StageStarted:
    return StageStarted(
        key=_key(stage_id=stage_id),
        name=stage_id.split(":")[-1],
        attempt=attempt,
    )


def _stage_finished(
    stage_id: str,
    *,
    status: str = "passed",
    duration_ms: int = 100,
    usage_input_tokens: int = 0,
    usage_output_tokens: int = 0,
    usage_cost_usd: float = 0.0,
    artifacts: list[str] | None = None,
    error: str | None = None,
) -> StageFinished:
    return StageFinished(
        key=_key(stage_id=stage_id),
        name=stage_id.split(":")[-1],
        status=status,
        success=(status == "passed"),
        duration_ms=duration_ms,
        artifacts=artifacts or [],
        error=error,
        usage_input_tokens=usage_input_tokens,
        usage_output_tokens=usage_output_tokens,
        usage_cost_usd=usage_cost_usd,
    )


def _turn(stage_id: str, block, seq: int = 0) -> TurnEvent:
    return TurnEvent(
        key=_key(stage_id=stage_id, seq=seq),
        event=AgentEvent(block=block),
    )


def _usage_updated(
    stage_id: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    total_cost_usd: float = 0.0,
) -> UsageUpdated:
    return UsageUpdated(
        key=_key(stage_id=stage_id),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_cost_usd=total_cost_usd,
    )


# ---------------------------------------------------------------------------
# Transcript — unit tests (ViewModel-level, no Pilot)
# ---------------------------------------------------------------------------


def test_transcript_tool_use_block_renders_correctly():
    """ToolUseBlock renders as 'tool <name> <summary>'."""
    vm = RunViewModel()
    vm.apply(_run_started())
    vm.apply(_stage_started("stage:gen"))
    vm.apply(_turn("stage:gen", ToolUseBlock(name="Bash", input_summary="ls -la")))

    t = Transcript(vm)
    t.set_stage("stage:gen")
    lines = t.get_lines()

    assert len(lines) == 1
    assert lines[0] == "tool Bash ls -la"


def test_transcript_tool_result_ok_renders_correctly():
    """ToolResultBlock with ok=True renders as 'tool_result ok'."""
    vm = RunViewModel()
    vm.apply(_run_started())
    vm.apply(_stage_started("stage:gen"))
    vm.apply(_turn("stage:gen", ToolResultBlock(ok=True, summary="done")))

    t = Transcript(vm)
    t.set_stage("stage:gen")
    lines = t.get_lines()

    assert lines == ["tool_result ok"]


def test_transcript_tool_result_err_renders_correctly():
    """ToolResultBlock with ok=False renders as 'tool_result err'."""
    vm = RunViewModel()
    vm.apply(_run_started())
    vm.apply(_stage_started("stage:gen"))
    vm.apply(_turn("stage:gen", ToolResultBlock(ok=False, summary="oops")))

    t = Transcript(vm)
    t.set_stage("stage:gen")
    lines = t.get_lines()

    assert lines == ["tool_result err"]


def test_transcript_text_block_renders_as_prose():
    """TextBlock renders as its plain text."""
    vm = RunViewModel()
    vm.apply(_run_started())
    vm.apply(_stage_started("stage:gen"))
    vm.apply(_turn("stage:gen", TextBlock(text="Hello from agent")))

    t = Transcript(vm)
    t.set_stage("stage:gen")

    assert t.get_lines() == ["Hello from agent"]


def test_transcript_thinking_block_renders_as_text():
    """ThinkingBlock plain text is available via get_lines (dim styling is Pilot-visible only)."""
    vm = RunViewModel()
    vm.apply(_run_started())
    vm.apply(_stage_started("stage:gen"))
    vm.apply(_turn("stage:gen", ThinkingBlock(text="thinking...")))

    t = Transcript(vm)
    t.set_stage("stage:gen")

    assert t.get_lines() == ["thinking..."]


def test_transcript_mixed_blocks_ordered():
    """Multiple block types appear in insertion order."""
    vm = RunViewModel()
    vm.apply(_run_started())
    vm.apply(_stage_started("stage:gen"))
    vm.apply(_turn("stage:gen", TextBlock(text="start"), seq=0))
    vm.apply(_turn("stage:gen", ToolUseBlock(name="Read", input_summary="foo.py"), seq=1))
    vm.apply(_turn("stage:gen", ToolResultBlock(ok=True, summary="content"), seq=2))
    vm.apply(_turn("stage:gen", TextBlock(text="done"), seq=3))

    t = Transcript(vm)
    t.set_stage("stage:gen")
    lines = t.get_lines()

    assert lines == [
        "start",
        "tool Read foo.py",
        "tool_result ok",
        "done",
    ]


def test_transcript_empty_for_unknown_stage():
    """Selecting a stage with no events yields an empty list."""
    vm = RunViewModel()
    t = Transcript(vm)
    t.set_stage("stage:nonexistent")

    assert t.get_lines() == []


def test_transcript_isolates_per_stage():
    """Blocks from different stages do not mix."""
    vm = RunViewModel()
    vm.apply(_run_started())
    vm.apply(_stage_started("stage:alpha"))
    vm.apply(_turn("stage:alpha", TextBlock(text="alpha text")))
    vm.apply(_stage_started("stage:beta"))
    vm.apply(_turn("stage:beta", ToolUseBlock(name="Write", input_summary="out.txt")))

    t = Transcript(vm)

    t.set_stage("stage:alpha")
    assert t.get_lines() == ["alpha text"]

    t.set_stage("stage:beta")
    assert t.get_lines() == ["tool Write out.txt"]


# ---------------------------------------------------------------------------
# BudgetMeter — unit tests (no Pilot)
# ---------------------------------------------------------------------------


def test_budget_meter_shows_tokens_at_zero_cost():
    """When cost is 0 the meter shows token count, not '$0.0000'."""
    vm = RunViewModel()
    vm.apply(_run_started())
    vm.apply(_stage_started("stage:gen"))
    vm.apply(_usage_updated("stage:gen", input_tokens=1000, output_tokens=500, total_cost_usd=0.0))

    meter = BudgetMeter(vm)
    content = meter.get_content()

    assert "tokens" in content
    assert "$" not in content
    assert "1,500" in content  # 1000 + 500


def test_budget_meter_shows_cost_when_nonzero():
    """When cost > 0 the meter shows a dollar figure."""
    vm = RunViewModel()
    vm.apply(_run_started())
    vm.apply(_stage_started("stage:gen"))
    vm.apply(_usage_updated("stage:gen", input_tokens=500, output_tokens=200, total_cost_usd=0.0042))

    meter = BudgetMeter(vm)
    content = meter.get_content()

    assert "$" in content
    assert "0.0042" in content


def test_budget_meter_shows_usage_prefix_without_budget():
    """Without a Budget object the meter uses 'Usage:' prefix."""
    vm = RunViewModel()
    meter = BudgetMeter(vm)

    assert meter.get_content().startswith("Usage:")


def test_budget_meter_shows_budget_limit_with_tokens():
    """With a Budget(max_tokens=…) the meter shows the token limit."""
    vm = RunViewModel()
    vm.apply(_run_started())
    vm.apply(_stage_started("stage:gen"))
    vm.apply(_usage_updated("stage:gen", input_tokens=300, output_tokens=100))

    budget = Budget(max_tokens=10_000)
    meter = BudgetMeter(vm, budget=budget)
    content = meter.get_content()

    assert "Budget:" in content
    assert "10,000" in content
    assert "tokens" in content


def test_budget_meter_shows_budget_limit_with_cost():
    """With a Budget(max_cost_usd=…) and nonzero cost the meter shows the cost limit."""
    vm = RunViewModel()
    vm.apply(_run_started())
    vm.apply(_stage_started("stage:gen"))
    vm.apply(_usage_updated("stage:gen", total_cost_usd=0.50))

    budget = Budget(max_cost_usd=5.0)
    meter = BudgetMeter(vm, budget=budget)
    content = meter.get_content()

    assert "Budget:" in content
    assert "5.0000" in content


def test_budget_meter_pending_before_any_events():
    """Before any usage is reported the meter shows 'pending', not '0 tokens'."""
    vm = RunViewModel()
    meter = BudgetMeter(vm)
    content = meter.get_content()

    assert "pending" in content
    assert "0 tokens" not in content
    assert content.strip() != ""


# ---------------------------------------------------------------------------
# StageDetail — unit tests (no Pilot)
# ---------------------------------------------------------------------------


def test_stage_detail_running_after_stage_started():
    """After StageStarted (before StageFinished) the panel shows running status."""
    vm = RunViewModel()
    vm.apply(_run_started())
    vm.apply(_stage_started("stage:gen"))

    detail = StageDetail(vm)
    detail.set_stage("stage:gen")
    content = detail.get_content()

    assert "stage:gen" in content
    assert "running" in content


def test_stage_detail_pending_before_stage_started():
    """A stage never started yet shows pending status."""
    vm = RunViewModel()
    vm.apply(_run_started())

    detail = StageDetail(vm)
    detail.set_stage("stage:unseen")
    content = detail.get_content()

    assert "stage:unseen" in content
    assert "pending" in content


def test_stage_detail_shows_status_after_finished():
    """After StageFinished detail panel shows the terminal status."""
    vm = RunViewModel()
    vm.apply(_run_started())
    vm.apply(_stage_started("stage:gen"))
    vm.apply(_stage_finished("stage:gen", status="passed", duration_ms=250))

    detail = StageDetail(vm)
    detail.set_stage("stage:gen")
    content = detail.get_content()

    assert "passed" in content
    assert "250" in content


def test_stage_detail_shows_attempts():
    """Attempts field from StageStarted.attempt is reflected in detail."""
    vm = RunViewModel()
    vm.apply(_run_started())
    vm.apply(_stage_started("stage:gen", attempt=2))
    vm.apply(_stage_finished("stage:gen", status="passed"))

    detail = StageDetail(vm)
    detail.set_stage("stage:gen")
    content = detail.get_content()

    assert "2" in content


def test_stage_detail_shows_artifacts():
    """Artifacts from StageFinished appear in the detail panel."""
    vm = RunViewModel()
    vm.apply(_run_started())
    vm.apply(_stage_started("stage:gen"))
    vm.apply(_stage_finished("stage:gen", artifacts=["out/file.py"]))

    detail = StageDetail(vm)
    detail.set_stage("stage:gen")
    content = detail.get_content()

    assert "out/file.py" in content


def test_stage_detail_shows_error():
    """Error message from a failed stage appears in the detail panel."""
    vm = RunViewModel()
    vm.apply(_run_started())
    vm.apply(_stage_started("stage:gen"))
    vm.apply(_stage_finished("stage:gen", status="failed", error="Something broke"))

    detail = StageDetail(vm)
    detail.set_stage("stage:gen")
    content = detail.get_content()

    assert "Something broke" in content


def test_stage_detail_empty_before_set_stage():
    """Detail panel returns empty string before a stage is selected."""
    vm = RunViewModel()
    detail = StageDetail(vm)
    assert detail.get_content() == ""


# ---------------------------------------------------------------------------
# Pilot tests — widget rendering via Textual run_test()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pilot_transcript_tool_use_line_visible():
    """Pilot: tool-use block shows 'tool <name>' in get_lines after apply_event."""
    pipeline = Pipeline("p").stage("gen", _Stub())
    graph = build_graph(pipeline)
    vm = RunViewModel()
    app = NornApp(graph=graph, vm=vm)

    async with app.run_test() as pilot:
        app.apply_event(_run_started("p"))
        app.apply_event(_stage_started("stage:gen"))
        app.apply_event(_turn("stage:gen", ToolUseBlock(name="Bash", input_summary="echo hi")))
        await pilot.pause()

        transcript = app.query_one(Transcript)
        transcript.set_stage("stage:gen")
        lines = transcript.get_lines()
        assert any("tool Bash" in ln for ln in lines)


@pytest.mark.asyncio
async def test_pilot_transcript_tool_result_line_visible():
    """Pilot: tool-result block shows 'tool_result ok' in get_lines."""
    pipeline = Pipeline("p").stage("gen", _Stub())
    graph = build_graph(pipeline)
    vm = RunViewModel()
    app = NornApp(graph=graph, vm=vm)

    async with app.run_test() as pilot:
        app.apply_event(_run_started("p"))
        app.apply_event(_stage_started("stage:gen"))
        app.apply_event(_turn("stage:gen", ToolResultBlock(ok=True, summary="output")))
        await pilot.pause()

        transcript = app.query_one(Transcript)
        transcript.set_stage("stage:gen")
        lines = transcript.get_lines()
        assert "tool_result ok" in lines


@pytest.mark.asyncio
async def test_pilot_budget_meter_zero_cost_shows_tokens():
    """Pilot: BudgetMeter shows token figure (not blank, not '$') at zero cost."""
    pipeline = Pipeline("p").stage("gen", _Stub())
    graph = build_graph(pipeline)
    vm = RunViewModel()
    app = NornApp(graph=graph, vm=vm)

    async with app.run_test() as pilot:
        app.apply_event(_run_started("p"))
        app.apply_event(_stage_started("stage:gen"))
        app.apply_event(
            _usage_updated("stage:gen", input_tokens=800, output_tokens=200, total_cost_usd=0.0)
        )
        await pilot.pause()

        meter = app.query_one(BudgetMeter)
        content = meter.get_content()
        assert "tokens" in content
        assert "$" not in content
        assert "1,000" in content


@pytest.mark.asyncio
async def test_pilot_transcript_and_budget_meter_in_same_app():
    """Pilot: Transcript and BudgetMeter coexist in one NornApp — both show expected output."""
    pipeline = Pipeline("p").stage("gen", _Stub())
    graph = build_graph(pipeline)
    vm = RunViewModel()
    app = NornApp(graph=graph, vm=vm)

    async with app.run_test() as pilot:
        app.apply_event(_run_started("p"))
        app.apply_event(_stage_started("stage:gen"))
        app.apply_event(_turn("stage:gen", ToolUseBlock(name="Read", input_summary="README.md")))
        app.apply_event(_turn("stage:gen", ToolResultBlock(ok=False, summary="not found")))
        app.apply_event(_usage_updated("stage:gen", input_tokens=500, output_tokens=100))
        await pilot.pause()

        transcript = app.query_one(Transcript)
        transcript.set_stage("stage:gen")
        lines = transcript.get_lines()
        assert "tool Read README.md" in lines
        assert "tool_result err" in lines

        meter = app.query_one(BudgetMeter)
        assert "tokens" in meter.get_content()


@pytest.mark.asyncio
async def test_pilot_apply_event_auto_follows_failed_stage():
    """apply_event surfaces a failed stage's error without an explicit set_stage.

    A failed stage must show *why* it failed in the detail panel on its own —
    otherwise the user sees only the ✗ tree glyph with no explanation.
    """
    pipeline = Pipeline("p").stage("check", _Stub())
    graph = build_graph(pipeline)
    vm = RunViewModel()
    app = NornApp(graph=graph, vm=vm)

    async with app.run_test() as pilot:
        app.apply_event(_run_started("p"))
        app.apply_event(_stage_started("stage:check"))
        app.apply_event(
            _stage_finished("stage:check", status="failed", error="Working tree is not clean")
        )
        await pilot.pause()

        # No explicit set_stage — the panel followed the failed stage itself.
        detail = app.query_one(StageDetail)
        content = detail.get_content()
        assert "failed" in content
        assert "Working tree is not clean" in content


@pytest.mark.asyncio
async def test_pilot_failed_stage_error_with_markup_chars_does_not_crash():
    """A failed stage whose error output contains '[' / '{...}' must render.

    Raw test output (assertions, f-string fragments) routinely contains
    sequences like ``[0.1234]`` or ``upper={fpr:.4f}`` that Textual would
    parse as console markup and raise ``MarkupError`` on — crashing the whole
    TUI mid-run. The detail/header/budget panels render plain text, so markup
    parsing is disabled; this pins that behavior.
    """
    pipeline = Pipeline("p").stage("check", _Stub())
    graph = build_graph(pipeline)
    vm = RunViewModel()
    app = NornApp(graph=graph, vm=vm)

    # This exact shape raises MarkupError under Textual's markup parser: a
    # '[' opens a tag, then ':' starts a value the trailing junk fails to
    # satisfy. Mirrors a real pytest assertion message that crashed the TUI.
    nasty_error = (
        'command exited with status 1\n'
        'x [bad:.4f} > s01 upper={fpr_s01:.4f} + 0.005"\n'
    )

    async with app.run_test() as pilot:
        app.apply_event(_run_started("p"))
        app.apply_event(_stage_started("stage:check"))
        # Before the fix, applying this event raised MarkupError inside the
        # detail panel's render and propagated out of apply_event.
        app.apply_event(
            _stage_finished("stage:check", status="failed", error=nasty_error)
        )
        await pilot.pause()

        detail = app.query_one(StageDetail)
        assert "failed" in detail.get_content()
        assert "upper={fpr_s01:.4f}" in detail.get_content()


@pytest.mark.asyncio
async def test_pilot_transcript_auto_follows_running_stage():
    """apply_event makes the transcript show the running stage's output live."""
    pipeline = Pipeline("p").stage("gen", _Stub())
    graph = build_graph(pipeline)
    vm = RunViewModel()
    app = NornApp(graph=graph, vm=vm)

    async with app.run_test() as pilot:
        app.apply_event(_run_started("p"))
        app.apply_event(_stage_started("stage:gen"))
        app.apply_event(_turn("stage:gen", TextBlock(text="working on it")))
        await pilot.pause()

        # No explicit set_stage — the transcript followed the active stage.
        transcript = app.query_one(Transcript)
        assert transcript.get_lines() == ["working on it"]


@pytest.mark.asyncio
async def test_pilot_stage_detail_updates_after_stage_finished():
    """Pilot: StageDetail shows duration and status after StageFinished arrives."""
    pipeline = Pipeline("p").stage("gen", _Stub())
    graph = build_graph(pipeline)
    vm = RunViewModel()
    app = NornApp(graph=graph, vm=vm)

    async with app.run_test() as pilot:
        app.apply_event(_run_started("p"))
        app.apply_event(_stage_started("stage:gen"))
        app.apply_event(_stage_finished("stage:gen", status="passed", duration_ms=123))
        await pilot.pause()

        detail = app.query_one(StageDetail)
        detail.set_stage("stage:gen")
        content = detail.get_content()
        assert "passed" in content
        assert "123" in content


# ---------------------------------------------------------------------------
# Loop-nested stage: transcript remains accessible after StageFinished
# ---------------------------------------------------------------------------


def test_loop_nested_stage_transcript_survives_stage_finished():
    """Blocks appended under loop:<L>/stage:<name> must still be retrievable
    from the ViewModel after StageFinished arrives with the same qualified id.

    This is the user-visible symptom of the node-id mismatch bug: the Generate
    stage keys TurnEvents under the *flat* id (stage:gen) while StageStarted /
    StageFinished use the *nested* id (loop:myloop/stage:gen).  The view model
    stores transcript under the id carried by TurnEvent.key.stage_id, and
    retrieves it using the id the caller passes to vm.transcript[stage_id].
    When both events use the same qualified id, the transcript is present after
    StageFinished; when they differ, it appears empty.
    """
    qualified_id = "loop:myloop/stage:gen"

    vm = RunViewModel()
    vm.apply(_run_started())
    vm.apply(_stage_started(qualified_id))
    # TurnEvent arrives under the qualified id — as it would after the fix.
    vm.apply(_turn(qualified_id, TextBlock(text="agent output"), seq=1))
    # StageFinished under the same qualified id.
    vm.apply(_stage_finished(qualified_id, status="passed", duration_ms=50))

    # After StageFinished, the transcript for the qualified id must still hold
    # the block — not be empty because the keys differed.
    blocks = vm.transcript.get(qualified_id, [])
    assert len(blocks) == 1, (
        f"Expected 1 block under '{qualified_id}' after StageFinished, got {len(blocks)}. "
        "This means TurnEvents are keyed under a different id than StageFinished."
    )
    assert isinstance(blocks[0], TextBlock)
    assert blocks[0].text == "agent output"

    # The node status must also reflect the finished state, not be stuck on running.
    assert vm.node_status.get(qualified_id) == "passed"


# ---------------------------------------------------------------------------
# Incremental rendering
# ---------------------------------------------------------------------------


class _RecordingTranscript(Transcript):
    """Transcript that records writes and clears instead of rendering."""

    def __init__(self, vm: RunViewModel) -> None:
        super().__init__(vm)
        self.writes: list[str] = []
        self.clears = 0

    def write(self, content, *args, **kwargs):  # type: ignore[override]
        self.writes.append(str(content))
        return self

    def clear(self):  # type: ignore[override]
        self.clears += 1
        return self


def test_refresh_appends_only_new_blocks():
    """A refresh must not rewrite what is already on screen.

    The app refreshes on *every* run event, and a streaming command stage
    produces thousands of blocks — redrawing the whole log each time is
    quadratic and stalls the UI exactly when output is heaviest.
    """
    vm = RunViewModel()
    vm.transcript["stage:build"] = [TextBlock(text="line-1")]
    t = _RecordingTranscript(vm)
    t.set_stage("stage:build")
    assert t.writes == ["line-1"]

    vm.transcript["stage:build"].append(TextBlock(text="line-2"))
    t.set_stage("stage:build")

    assert t.writes == ["line-1", "line-2"]
    assert t.clears == 1, "only the initial stage selection should clear"


def test_switching_stage_redraws_from_scratch():
    vm = RunViewModel()
    vm.transcript["stage:a"] = [TextBlock(text="a1")]
    vm.transcript["stage:b"] = [TextBlock(text="b1")]
    t = _RecordingTranscript(vm)
    t.set_stage("stage:a")
    t.set_stage("stage:b")

    assert t.writes == ["a1", "b1"]
    assert t.clears == 2


def test_shrinking_spool_forces_a_full_redraw():
    """A re-run of the same stage replaces its blocks; don't append onto stale text."""
    vm = RunViewModel()
    vm.transcript["stage:build"] = [TextBlock(text="old-1"), TextBlock(text="old-2")]
    t = _RecordingTranscript(vm)
    t.set_stage("stage:build")

    vm.transcript["stage:build"] = [TextBlock(text="fresh")]
    t.refresh_vm()

    assert t.writes == ["old-1", "old-2", "fresh"]
    assert t.clears == 2

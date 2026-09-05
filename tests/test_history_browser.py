"""Pilot tests for the HistoryBrowserScreen and HistoryBrowserApp.

Uses Textual's ``run_test()`` / ``Pilot`` API to mount the screen for real
and assert that it correctly lists run history and displays stage error text
from the persisted ``stage_log``.

All tests are offline: no SDK calls, no subprocesses, no network.

Note on screen access
---------------------
``HistoryBrowserApp`` pushes ``HistoryBrowserScreen`` onto the Textual screen
stack in ``on_mount``.  Pushed screens are accessed via ``app.screen`` (the
active/top screen), **not** via ``app.query_one(HistoryBrowserScreen)`` — the
latter searches widget children, not the screen stack.
"""
from __future__ import annotations

import pytest
from textual.widgets import DataTable

from norn.checkpoint import save_checkpoint
from norn.history import RunRecord, StageHistoryEntry, append_run, load_history
from norn.models import StageLogEntry
from norn.tui.screens import HistoryBrowserApp, HistoryBrowserScreen


def _get_screen(app: HistoryBrowserApp) -> HistoryBrowserScreen:
    """Return the active HistoryBrowserScreen from the app's screen stack."""
    screen = app.screen
    assert isinstance(screen, HistoryBrowserScreen)
    return screen


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run(
    run_id: int = 1,
    *,
    success: bool = True,
    failed_stage: str | None = None,
    stage_log: list[StageLogEntry] | None = None,
    in_progress: bool = False,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        timestamp=f"2026-06-15T10:0{run_id}:00+00:00",
        success=success,
        total_cost_usd=0.25 if success else 0.10,
        total_tokens=12000,
        duration_ms=5000,
        stages=[StageHistoryEntry(name="step1", success=success, cost_usd=0.25)],
        retries=0,
        session_id="sess-abc",
        failed_stage=failed_stage,
        in_progress=in_progress,
        stage_log=stage_log or [],
    )


# ---------------------------------------------------------------------------
# Run-list display
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_browser_lists_two_runs(tmp_path):
    """Browser table shows one row for every run in the history."""
    config = str(tmp_path / "pipeline.py")
    append_run(config, _make_run(1, success=True))
    append_run(config, _make_run(2, success=False, failed_stage="bad_stage"))

    records = load_history(config)
    app = HistoryBrowserApp(records=records, config_path=config)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _get_screen(app)
        assert screen.get_run_count() == 2


@pytest.mark.asyncio
async def test_history_browser_empty_history(tmp_path):
    """Browser shows zero rows when the history file does not exist."""
    config = str(tmp_path / "missing.py")
    records = load_history(config)
    app = HistoryBrowserApp(records=records, config_path=config)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _get_screen(app)
        assert screen.get_run_count() == 0


@pytest.mark.asyncio
async def test_history_browser_status_complete_for_success(tmp_path):
    """Successful run shows the '✓ Complete' status string in the table."""
    config = str(tmp_path / "pipeline.py")
    append_run(config, _make_run(1, success=True))

    records = load_history(config)
    app = HistoryBrowserApp(records=records, config_path=config)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _get_screen(app)
        table = screen.query_one(DataTable)
        row = table.get_row_at(0)
        # Status is column index 2
        assert "Complete" in str(row[2])


@pytest.mark.asyncio
async def test_history_browser_status_failed_for_failure(tmp_path):
    """Failed run shows the '✗ Failed' status string in the table."""
    config = str(tmp_path / "pipeline.py")
    append_run(config, _make_run(1, success=False, failed_stage="crash_stage"))

    records = load_history(config)
    app = HistoryBrowserApp(records=records, config_path=config)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _get_screen(app)
        table = screen.query_one(DataTable)
        row = table.get_row_at(0)
        assert "Failed" in str(row[2])


@pytest.mark.asyncio
async def test_history_browser_in_progress_run(tmp_path):
    """In-progress run shows the '↻ Running' status and stage-count info."""
    config = str(tmp_path / "pipeline.py")
    append_run(config, _make_run(1, success=False, in_progress=True))

    records = load_history(config)
    app = HistoryBrowserApp(records=records, config_path=config)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _get_screen(app)
        table = screen.query_one(DataTable)
        row = table.get_row_at(0)
        assert "Running" in str(row[2])
        assert "stages so far" in str(row[5])


# ---------------------------------------------------------------------------
# Resumable column
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_browser_resumable_when_checkpoint_exists(tmp_path):
    """Most recent run is marked 'yes' in the Resumable column if a checkpoint exists."""
    config = str(tmp_path / "pipeline.py")
    append_run(config, _make_run(1, success=True))
    append_run(config, _make_run(2, success=False, failed_stage="bad"))

    save_checkpoint(
        config,
        "test-pipeline",
        session_id=None,
        completed_stages=["step1"],
        stage_outputs={},
    )

    records = load_history(config)
    app = HistoryBrowserApp(records=records, config_path=config)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _get_screen(app)
        table = screen.query_one(DataTable)
        # Run #1 (index 0) — not the most recent, should NOT be resumable
        row1 = table.get_row_at(0)
        assert "yes" not in str(row1[-1])
        # Run #2 (index 1) — most recent, SHOULD be resumable
        row2 = table.get_row_at(1)
        assert "yes" in str(row2[-1])


@pytest.mark.asyncio
async def test_history_browser_not_resumable_without_checkpoint(tmp_path):
    """Without a checkpoint file no run is marked resumable."""
    config = str(tmp_path / "pipeline.py")
    append_run(config, _make_run(1, success=True))

    records = load_history(config)
    app = HistoryBrowserApp(records=records, config_path=config)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _get_screen(app)
        table = screen.query_one(DataTable)
        row = table.get_row_at(0)
        assert "yes" not in str(row[-1])


# ---------------------------------------------------------------------------
# Stage detail panel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_browser_shows_stage_log_on_select(tmp_path):
    """Calling _show_stage_detail renders stage names in the detail panel."""
    config = str(tmp_path / "pipeline.py")
    stage_log = [
        StageLogEntry(name="build", status="passed", success=True, attempt=1),
        StageLogEntry(name="test", status="passed", success=True, attempt=1),
    ]
    append_run(config, _make_run(1, success=True, stage_log=stage_log))

    records = load_history(config)
    app = HistoryBrowserApp(records=records, config_path=config)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _get_screen(app)
        screen._show_stage_detail(records[0])
        await pilot.pause()

        content = screen.get_detail_content()
        assert "build" in content
        assert "test" in content


@pytest.mark.asyncio
async def test_history_browser_failed_run_shows_error_text(tmp_path):
    """The detail panel shows the persisted error text for a failed stage."""
    config = str(tmp_path / "pipeline.py")
    stage_log = [
        StageLogEntry(
            name="my_stage",
            status="failed",
            success=False,
            attempt=1,
            error="subprocess exited with code 1",
        )
    ]
    append_run(config, _make_run(1, success=False, failed_stage="my_stage", stage_log=stage_log))

    records = load_history(config)
    app = HistoryBrowserApp(records=records, config_path=config)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _get_screen(app)
        screen._show_stage_detail(records[0])
        await pilot.pause()

        content = screen.get_detail_content()
        assert "subprocess exited with code 1" in content
        assert "my_stage" in content


@pytest.mark.asyncio
async def test_history_browser_error_visible_after_enter_key(tmp_path):
    """Pressing Enter on a failed-run row shows the stage error in the detail panel."""
    config = str(tmp_path / "pipeline.py")
    stage_log = [
        StageLogEntry(
            name="failing_step",
            status="failed",
            success=False,
            attempt=1,
            error="something went boom",
        )
    ]
    append_run(config, _make_run(1, success=False, failed_stage="failing_step", stage_log=stage_log))

    records = load_history(config)
    app = HistoryBrowserApp(records=records, config_path=config)

    async with app.run_test() as pilot:
        await pilot.pause()
        # DataTable is the first focusable widget and should have focus.
        # Pressing Enter triggers RowSelected for the row under the cursor.
        await pilot.press("enter")
        await pilot.pause()

        screen = _get_screen(app)
        content = screen.get_detail_content()
        assert "something went boom" in content
        assert "failing_step" in content


@pytest.mark.asyncio
async def test_history_browser_no_stage_log_shows_placeholder(tmp_path):
    """A run with no stage_log shows a placeholder message in the detail panel."""
    config = str(tmp_path / "pipeline.py")
    append_run(config, _make_run(1, success=True, stage_log=[]))

    records = load_history(config)
    app = HistoryBrowserApp(records=records, config_path=config)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _get_screen(app)
        screen._show_stage_detail(records[0])
        await pilot.pause()

        content = screen.get_detail_content()
        assert "no stage log" in content


@pytest.mark.asyncio
async def test_history_browser_success_stage_glyph(tmp_path):
    """Passed stage shows the ✓ glyph in the detail panel."""
    config = str(tmp_path / "pipeline.py")
    stage_log = [
        StageLogEntry(name="good_step", status="passed", success=True, attempt=1),
    ]
    append_run(config, _make_run(1, success=True, stage_log=stage_log))

    records = load_history(config)
    app = HistoryBrowserApp(records=records, config_path=config)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _get_screen(app)
        screen._show_stage_detail(records[0])
        await pilot.pause()

        content = screen.get_detail_content()
        assert "✓" in content
        assert "good_step" in content


@pytest.mark.asyncio
async def test_history_browser_failed_stage_glyph(tmp_path):
    """Failed stage shows the ✗ glyph in the detail panel."""
    config = str(tmp_path / "pipeline.py")
    stage_log = [
        StageLogEntry(
            name="bad_step", status="failed", success=False, attempt=1,
            error="error here",
        ),
    ]
    append_run(config, _make_run(1, success=False, failed_stage="bad_step", stage_log=stage_log))

    records = load_history(config)
    app = HistoryBrowserApp(records=records, config_path=config)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _get_screen(app)
        screen._show_stage_detail(records[0])
        await pilot.pause()

        content = screen.get_detail_content()
        assert "✗" in content
        assert "bad_step" in content


# ---------------------------------------------------------------------------
# Error round-trip: append_run → load_history → browser
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_survives_round_trip_and_appears_in_browser(tmp_path):
    """Error written by append_run is preserved through load_history and visible in the browser.

    This is the primary contract: step-01 persisted error text in stage_log;
    the browser must surface it.
    """
    config = str(tmp_path / "pipeline.py")
    record = RunRecord(
        run_id=1,
        timestamp="2026-06-15T10:00:00+00:00",
        success=False,
        total_cost_usd=0.0,
        total_tokens=0,
        duration_ms=200,
        stages=[],
        retries=0,
        stage_log=[
            StageLogEntry(
                name="flaky_stage",
                status="failed",
                success=False,
                attempt=2,
                error="network timeout after 30s",
            )
        ],
    )
    append_run(config, record)

    # Load fresh from disk — proves the round-trip, not just in-memory state
    records = load_history(config)
    assert records[0].stage_log[0].error == "network timeout after 30s"

    app = HistoryBrowserApp(records=records, config_path=config)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _get_screen(app)
        screen._show_stage_detail(records[0])
        await pilot.pause()

        content = screen.get_detail_content()
        assert "network timeout after 30s" in content
        assert "flaky_stage" in content


@pytest.mark.asyncio
async def test_error_only_shows_last_line(tmp_path):
    """Multi-line error text shows only the last line in the detail panel (matches CLI behaviour)."""
    config = str(tmp_path / "pipeline.py")
    multiline_error = "first line\nsecond line\nfinal error message"
    stage_log = [
        StageLogEntry(
            name="step",
            status="failed",
            success=False,
            attempt=1,
            error=multiline_error,
        )
    ]
    append_run(config, _make_run(1, success=False, failed_stage="step", stage_log=stage_log))

    records = load_history(config)
    app = HistoryBrowserApp(records=records, config_path=config)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _get_screen(app)
        screen._show_stage_detail(records[0])
        await pilot.pause()

        content = screen.get_detail_content()
        assert "final error message" in content
        # First lines are NOT shown (only last line displayed)
        assert "first line" not in content
        assert "second line" not in content


# ---------------------------------------------------------------------------
# Multiple-run selection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_browser_detail_updates_on_run_change(tmp_path):
    """Calling _show_stage_detail for different runs updates the detail panel content."""
    config = str(tmp_path / "pipeline.py")
    log_run1 = [StageLogEntry(name="stage_alpha", status="passed", success=True, attempt=1)]
    log_run2 = [
        StageLogEntry(
            name="stage_beta", status="failed", success=False, attempt=1,
            error="beta error text",
        )
    ]
    append_run(config, _make_run(1, success=True, stage_log=log_run1))
    append_run(config, _make_run(2, success=False, failed_stage="stage_beta", stage_log=log_run2))

    records = load_history(config)
    app = HistoryBrowserApp(records=records, config_path=config)

    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _get_screen(app)

        # Show run 1
        screen._show_stage_detail(records[0])
        await pilot.pause()
        assert "stage_alpha" in screen.get_detail_content()
        assert "stage_beta" not in screen.get_detail_content()

        # Switch to run 2
        screen._show_stage_detail(records[1])
        await pilot.pause()
        assert "stage_beta" in screen.get_detail_content()
        assert "beta error text" in screen.get_detail_content()
        assert "stage_alpha" not in screen.get_detail_content()


# ---------------------------------------------------------------------------
# TUI-run history: regression for missing config_path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tui_run_history_visible_in_browser(tmp_path):
    """A run written via build_run_setup's config_path appears in the history browser.

    Regression: build_run_setup previously omitted config_path so _persist_history_snapshot
    returned early, leaving the browser empty after every TUI run.  After the fix,
    build_run_setup sets config_path == history_config_key(ref) so the runner writes
    history to the shared state file, and load_run_history(ref) finds it.
    """
    from norn.dsl import Pipeline
    from norn.tui.launch import build_run_setup, history_config_key, load_run_history

    # A real pipeline file provides a stable state key (same as `norn run <file>`)
    pipe_file = tmp_path / "mypipe.py"
    pipe_file.write_text("# stub")
    ref = str(pipe_file)

    # Confirm build_run_setup assigns the shared state key
    pipeline_obj = Pipeline("mypipe")
    _, _, run_kwargs = build_run_setup(pipeline_obj, {}, ref=ref)
    config_path = run_kwargs["config_path"]
    assert config_path == history_config_key(ref), (
        "build_run_setup must set config_path == history_config_key(ref)"
    )

    # Simulate what run_pipeline writes when config_path is set
    append_run(config_path, _make_run(1, success=True))

    # Load the same way the TUI history browser does
    records = load_run_history(ref)
    assert len(records) == 1, "TUI run must appear in history loaded by ref"

    # The browser must list the row
    app = HistoryBrowserApp(records=records, config_path=config_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _get_screen(app)
        assert screen.get_run_count() == 1

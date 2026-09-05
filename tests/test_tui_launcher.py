"""Pilot tests for the LauncherScreen and LauncherApp.

Uses Textual's ``run_test()`` / ``Pilot`` API to mount the screen and assert
that it correctly lists bundled and discovered pipelines.

All tests are offline: no SDK calls, no subprocesses, no network.

Screen-stack note
-----------------
``LauncherApp`` pushes ``LauncherScreen`` onto the Textual screen stack in
``on_mount``.  The active screen is accessed via ``app.screen``, NOT via
``app.query_one(LauncherScreen)``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from norn.catalog import PipelineInfo
from norn.dsl import Pipeline
from norn.models import PipelineContext, StageResult
from norn.stages.base import BaseStage
from norn.tui.screens import OPEN_FILE, HistoryRequest, LaunchRequest, LauncherApp, LauncherScreen


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_info(name: str, short: str = "", path: Path | None = None) -> PipelineInfo:
    return PipelineInfo(
        name=name,
        short=short or f"Short description for {name}",
        long=f"Long description for {name}.",
        env_vars=[],
        args={},
        path=path or Path(f"/fake/{name}.py"),
    )


def _get_screen(app: LauncherApp) -> LauncherScreen:
    screen = app.screen
    assert isinstance(screen, LauncherScreen)
    return screen


# ---------------------------------------------------------------------------
# Launcher lists pipelines
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_launcher_lists_bundled_pipelines() -> None:
    """Bundled pipeline names appear as rows in the DataTable."""
    bundled = [_make_info("hello"), _make_info("vanilla_change")]
    app = LauncherApp(bundled=bundled, discovered=[])
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _get_screen(app)
        names = screen.get_row_names()
        assert "hello" in names
        assert "vanilla_change" in names


@pytest.mark.asyncio
async def test_launcher_lists_discovered_pipelines() -> None:
    """Discovered pipeline names appear as rows in the DataTable."""
    discovered = [_make_info("my_custom_pipe")]
    app = LauncherApp(bundled=[], discovered=discovered)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _get_screen(app)
        names = screen.get_row_names()
        assert "my_custom_pipe" in names


@pytest.mark.asyncio
async def test_launcher_lists_bundled_and_discovered() -> None:
    """Both bundled and discovered pipelines appear together."""
    bundled = [_make_info("bundled_pipe")]
    discovered = [_make_info("discovered_pipe")]
    app = LauncherApp(bundled=bundled, discovered=discovered)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _get_screen(app)
        names = screen.get_row_names()
        assert "bundled_pipe" in names
        assert "discovered_pipe" in names


@pytest.mark.asyncio
async def test_launcher_includes_open_file_row() -> None:
    """An 'Open file…' row is always present at the end of the table."""
    app = LauncherApp(bundled=[], discovered=[])
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _get_screen(app)
        names = screen.get_row_names()
        assert any("Open file" in n for n in names)


@pytest.mark.asyncio
async def test_launcher_row_count_bundled_plus_open_file() -> None:
    """Row count equals len(bundled) + len(discovered) + 1 (open file)."""
    bundled = [_make_info("a"), _make_info("b")]
    discovered = [_make_info("c")]
    app = LauncherApp(bundled=bundled, discovered=discovered)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _get_screen(app)
        # 2 bundled + 1 discovered + 1 open-file = 4
        assert screen.get_pipeline_count() == 4


@pytest.mark.asyncio
async def test_launcher_empty_shows_open_file_only() -> None:
    """With no pipelines, the only row is 'Open file…'."""
    app = LauncherApp(bundled=[], discovered=[])
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _get_screen(app)
        assert screen.get_pipeline_count() == 1


# ---------------------------------------------------------------------------
# Discovered pipeline with real temp-dir metadata
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_launcher_discovered_from_temp_dir(tmp_path: Path) -> None:
    """A pipeline file discovered from a temp directory appears in the launcher."""
    from norn.catalog import list_discovered_pipelines

    (tmp_path / "mypipe.py").write_text('"""My custom pipeline."""\n')
    discovered = list_discovered_pipelines(extra_dirs=[tmp_path])

    app = LauncherApp(bundled=[], discovered=discovered)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _get_screen(app)
        names = screen.get_row_names()
        assert "mypipe" in names


@pytest.mark.asyncio
async def test_launcher_discovered_short_description_in_row(tmp_path: Path) -> None:
    """The short description is visible in the Description column of the table."""
    from norn.catalog import list_discovered_pipelines

    (tmp_path / "mypipe.py").write_text('"""My fantastic pipeline."""\n')
    discovered = list_discovered_pipelines(extra_dirs=[tmp_path])

    app = LauncherApp(bundled=[], discovered=discovered)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _get_screen(app)
        desc = screen.get_description_for_name("mypipe")
        assert desc is not None, "mypipe row not found in DataTable"
        assert "My fantastic pipeline." in desc


# ---------------------------------------------------------------------------
# Enter / launch action
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_launcher_enter_selects_highlighted_pipeline() -> None:
    """Pressing Enter on a row dismisses with that pipeline.

    Regression: a focused DataTable swallows Enter (its own ``select_cursor``
    binding) and posts ``RowSelected``, which shadowed the screen's
    ``enter`` → ``action_launch`` binding, so Enter did nothing.  The launcher
    now handles ``RowSelected`` directly.
    """
    bundled = [_make_info("hello"), _make_info("vanilla_change")]
    app = LauncherApp(bundled=bundled, discovered=[])
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")  # cursor starts on the first row
        await pilot.pause()
    assert app.selected is not None
    assert isinstance(app.selected, LaunchRequest)
    assert app.selected.info.name == "hello"
    assert app.selected.use_worktree is False


@pytest.mark.asyncio
async def test_launcher_enter_on_second_row_selects_it() -> None:
    """Moving the cursor down then pressing Enter selects the second pipeline."""
    bundled = [_make_info("hello"), _make_info("vanilla_change")]
    app = LauncherApp(bundled=bundled, discovered=[])
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
    assert app.selected is not None
    assert isinstance(app.selected, LaunchRequest)
    assert app.selected.info.name == "vanilla_change"


@pytest.mark.asyncio
async def test_launcher_enter_on_open_file_row_selects_sentinel() -> None:
    """Pressing Enter on 'Open file…' dismisses with the OPEN_FILE sentinel.

    Distinct from None (quit) so the CLI loop opens a file picker instead of
    exiting.
    """
    app = LauncherApp(bundled=[], discovered=[])  # only the open-file row
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert app.selected is OPEN_FILE


# ---------------------------------------------------------------------------
# History action
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_launcher_history_dismisses_with_request() -> None:
    """Pressing 'h' on a highlighted pipeline requests its history."""
    app = LauncherApp(bundled=[_make_info("hello")], discovered=[])
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("h")
        await pilot.pause()
    assert isinstance(app.selected, HistoryRequest)
    assert app.selected.info.name == "hello"


# ---------------------------------------------------------------------------
# Quit action
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_launcher_quit_sets_selected_none() -> None:
    """Pressing 'q' dismisses the screen; app.selected is None."""
    bundled = [_make_info("hello")]
    app = LauncherApp(bundled=bundled, discovered=[])
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
    assert app.selected is None


# ---------------------------------------------------------------------------
# Regression: bundled discovery unaffected
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Worktree toggle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_launcher_worktree_toggle_off_by_default() -> None:
    """The worktree toggle starts OFF."""
    app = LauncherApp(bundled=[_make_info("hello")], discovered=[])
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _get_screen(app)
        assert screen.get_worktree_status() is False


@pytest.mark.asyncio
async def test_launcher_worktree_toggle_on_off() -> None:
    """Pressing 'w' toggles the worktree flag and updates the status line."""
    app = LauncherApp(bundled=[_make_info("hello")], discovered=[])
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _get_screen(app)
        assert screen.get_worktree_status() is False

        await pilot.press("w")
        await pilot.pause()
        assert screen.get_worktree_status() is True

        await pilot.press("w")
        await pilot.pause()
        assert screen.get_worktree_status() is False


@pytest.mark.asyncio
async def test_launcher_worktree_on_carries_to_launch_request() -> None:
    """When worktree is ON, Enter produces a LaunchRequest with use_worktree=True."""
    bundled = [_make_info("hello")]
    app = LauncherApp(bundled=bundled, discovered=[])
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("w")  # toggle ON
        await pilot.press("enter")
        await pilot.pause()
    assert isinstance(app.selected, LaunchRequest)
    assert app.selected.use_worktree is True
    assert app.selected.info.name == "hello"


@pytest.mark.asyncio
async def test_launcher_worktree_off_carries_to_launch_request() -> None:
    """When worktree is OFF, Enter produces a LaunchRequest with use_worktree=False."""
    bundled = [_make_info("hello")]
    app = LauncherApp(bundled=bundled, discovered=[])
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert isinstance(app.selected, LaunchRequest)
    assert app.selected.use_worktree is False


# ---------------------------------------------------------------------------
# Regression: bundled discovery unaffected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_launcher_with_real_bundled_pipelines() -> None:
    """LauncherApp works with the real list_pipelines() output."""
    from norn.catalog import list_pipelines

    bundled = list_pipelines()
    app = LauncherApp(bundled=bundled, discovered=[])
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = _get_screen(app)
        # At least the bundled pipelines + open-file row
        assert screen.get_pipeline_count() >= len(bundled) + 1
        names = screen.get_row_names()
        # hello is a known bundled pipeline
        assert "hello" in names


# ---------------------------------------------------------------------------
# build_run_setup — config_path kwarg contract
# ---------------------------------------------------------------------------


class _NoOp(BaseStage):
    async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
        return StageResult(name="", success=True, output="")


def test_build_run_setup_sets_config_path_for_bundled_ref() -> None:
    """build_run_setup includes config_path == history_config_key(ref) for a bundled name.

    Regression: the old signature omitted config_path so TUI runs never wrote
    history or checkpoints.
    """
    from norn.tui.launch import build_run_setup, history_config_key

    pipeline = Pipeline("mypipe").stage("s", _NoOp())
    ref = "hello"
    _, _, run_kwargs = build_run_setup(pipeline, {}, ref=ref)
    assert run_kwargs["config_path"] == history_config_key(ref)


def test_build_run_setup_sets_config_path_for_file_ref(tmp_path: Path) -> None:
    """build_run_setup includes config_path == history_config_key(ref) for a file path."""
    from norn.tui.launch import build_run_setup, history_config_key

    pipe_file = tmp_path / "mypipe.py"
    pipe_file.write_text("# stub")
    ref = str(pipe_file)

    pipeline = Pipeline("mypipe").stage("s", _NoOp())
    _, _, run_kwargs = build_run_setup(pipeline, {}, ref=ref)
    assert run_kwargs["config_path"] == history_config_key(ref)


def test_build_run_setup_does_not_set_resume_checkpoint() -> None:
    """build_run_setup never injects resume_checkpoint — that is the resume branch's job."""
    from norn.tui.launch import build_run_setup

    pipeline = Pipeline("mypipe").stage("s", _NoOp())
    _, _, run_kwargs = build_run_setup(pipeline, {}, ref="hello")
    assert "resume_checkpoint" not in run_kwargs


def test_build_run_setup_resume_adds_only_checkpoint(tmp_path: Path) -> None:
    """Resume branch adds resume_checkpoint without overriding config_path a second time.

    Verifies the contract in _start_run: build_run_setup sets config_path for all
    runs; resume only appends resume_checkpoint on top.
    """
    from norn.checkpoint import save_checkpoint
    from norn.tui.launch import build_run_setup, history_config_key, load_run_checkpoint

    pipe_file = tmp_path / "pipe.py"
    pipe_file.write_text("# stub")
    ref = str(pipe_file)

    config_path = history_config_key(ref)
    save_checkpoint(config_path, "pipe", session_id=None, completed_stages=["s"], stage_outputs={})

    pipeline = Pipeline("pipe").stage("s", _NoOp())
    _, _, run_kwargs = build_run_setup(pipeline, {}, ref=ref)

    # Simulate what _start_run's resume branch does
    checkpoint = load_run_checkpoint(ref)
    if checkpoint is not None:
        run_kwargs["resume_checkpoint"] = checkpoint

    assert run_kwargs["config_path"] == config_path
    assert run_kwargs["resume_checkpoint"] is not None

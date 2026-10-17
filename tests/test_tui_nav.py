"""Pilot tests for the unified NornUIApp navigation (Launcher → Args → Run).

Proves the seamless single-app flow: selecting a pipeline pushes the run
screen, Back returns to the launcher, Quit exits, the args prompt appears for
pipelines that declare args, and the 'Open file…' row routes through the file
picker.

Offline: the pipeline loader is stubbed to a non-agent pipeline, so no Claude
calls are made; fzf is never spawned (the picker is monkeypatched).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Input

from norn.catalog import PipelineInfo, get_pipeline_info
from norn.dsl import Pipeline
from norn.models import PipelineContext, StageResult
from norn.stages.base import BaseStage
from norn.tui import app as app_mod
from norn.tui.app import NornUIApp, RunScreen
from norn.tui.args_prompt import ArgsPromptScreen
from norn.tui.launch import (
    BUDGET_PARAM,
    TOKEN_BUDGET_PARAM,
    launch_argv,
    load_pipeline_with_args,
    pipeline_args_meta,
    resolve_ref,
)
from norn.tui.screens import HistoryBrowserScreen, LauncherScreen


class _OK(BaseStage):
    async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
        return StageResult(name="", success=True, output="ok")


def _stub_pipeline() -> Pipeline:
    return Pipeline("stub").stage("s", _OK())


def _info(name: str, args: dict | None = None) -> PipelineInfo:
    return PipelineInfo(
        name=name, short="", long="", env_vars=[], args=args or {}, path=Path(f"/p/{name}.py")
    )


@pytest.fixture
def patch_load(monkeypatch):
    """Stub the pipeline loader so runs use an offline non-agent pipeline."""
    monkeypatch.setattr(
        app_mod,
        "load_pipeline_with_args",
        lambda ref, *, is_bundled, params: _stub_pipeline(),
    )


# ---------------------------------------------------------------------------
# Launch helpers (pure)
# ---------------------------------------------------------------------------


def test_pipeline_args_meta_reads_bundled_args() -> None:
    """A bundled pipeline's declared args are surfaced for the prompt."""
    assert "args" in pipeline_args_meta("implement_features_v1")


def test_pipeline_args_meta_empty_for_unknown() -> None:
    assert pipeline_args_meta("/nope/missing.py") == {}


def test_launch_argv_splits_positional_args() -> None:
    assert launch_argv({"args": "tmp/plan 'two words'"}) == ["tmp/plan", "two words"]


def test_launch_argv_turns_named_params_into_arg_flags() -> None:
    """Named params reach import-time argv readers the way `norn run --arg` does."""
    params = {"args": "x", "workflow": "test-pg.yml", "test_cmd": "mvn test -Dtest.db=postgres"}

    assert launch_argv(params) == [
        "x",
        "--arg", "workflow=test-pg.yml",
        "--arg", "test_cmd=mvn test -Dtest.db=postgres",
    ]


def test_launch_argv_leaves_out_blank_and_budget_params() -> None:
    params = {"workflow": "", "skip_local": "  ", BUDGET_PARAM: "5", TOKEN_BUDGET_PARAM: "100"}

    assert launch_argv(params) == []


def test_load_pipeline_with_args_exposes_named_params_at_import(tmp_path, monkeypatch) -> None:
    """A file pipeline that parses `--arg` at import sees the launch params."""
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "argv_reader.py"
    source.write_text(
        "import sys\n"
        "from norn.dsl import Pipeline\n"
        "config = Pipeline('argv_reader')\n"
        "config.seen_argv = sys.argv[1:]\n"
    )

    pipe = load_pipeline_with_args(
        str(source), is_bundled=False, params={"workflow": "test-pg.yml", "test_cmd": ""}
    )

    assert pipe.seen_argv == ["--arg", "workflow=test-pg.yml"]


def test_resolve_ref_bundled() -> None:
    info = get_pipeline_info("hello_v1")
    ref, is_bundled = resolve_ref(info)
    assert is_bundled is True
    assert ref == "hello_v1"


# ---------------------------------------------------------------------------
# Navigation flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_select_pipeline_pushes_run_screen(patch_load) -> None:
    """Selecting a no-args pipeline goes straight to the run screen."""
    app = NornUIApp(bundled=[_info("stub")], discovered=[])
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, LauncherScreen)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, RunScreen)


@pytest.mark.asyncio
async def test_back_from_run_returns_to_launcher(patch_load) -> None:
    """Pressing Back on the run screen re-opens the launcher (same app)."""
    app = NornUIApp(bundled=[_info("stub")], discovered=[])
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, RunScreen)
        await pilot.press("b")
        await pilot.pause()
        assert isinstance(app.screen, LauncherScreen)


@pytest.mark.asyncio
async def test_quit_from_run_exits_app(patch_load, monkeypatch) -> None:
    """Pressing Quit on the run screen exits the whole app (not back)."""
    app = NornUIApp(bundled=[_info("stub")], discovered=[])
    exited: list[bool] = []
    real_exit = app.exit
    monkeypatch.setattr(app, "exit", lambda *a, **k: (exited.append(True), real_exit(*a, **k))[1])
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
    assert exited  # app.exit() was invoked by Quit


@pytest.mark.asyncio
async def test_pipeline_with_args_shows_prompt_then_runs(patch_load) -> None:
    """A pipeline declaring args shows the prompt, then runs after submit."""
    app = NornUIApp(bundled=[_info("needs", args={"args": "Path to a directory"})], discovered=[])
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ArgsPromptScreen)
        app.screen.query_one("#arg-args", Input).value = "tmp/x"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, RunScreen)


@pytest.mark.asyncio
async def test_args_cancel_returns_to_launcher(patch_load) -> None:
    """Cancelling the args prompt returns to the launcher."""
    app = NornUIApp(bundled=[_info("needs", args={"args": "Path"})], discovered=[])
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ArgsPromptScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, LauncherScreen)


@pytest.mark.asyncio
async def test_open_file_routes_through_picker(patch_load, monkeypatch) -> None:
    """The 'Open file…' row uses the file picker, then runs the chosen file."""
    monkeypatch.setattr(NornUIApp, "_pick_file", lambda self: "some/pipe.py")
    app = NornUIApp(bundled=[], discovered=[])  # only the open-file row
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, RunScreen)


@pytest.mark.asyncio
async def test_open_file_cancelled_returns_to_launcher(monkeypatch) -> None:
    """Cancelling the file picker returns to the launcher."""
    monkeypatch.setattr(NornUIApp, "_pick_file", lambda self: None)
    app = NornUIApp(bundled=[_info("stub")], discovered=[])
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("down")  # move to the 'Open file…' row
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, LauncherScreen)


@pytest.mark.asyncio
async def test_direct_pipeline_skips_launcher(patch_load) -> None:
    """`norn ui <pipeline>` (initial_pipeline) runs directly, no launcher."""
    app = NornUIApp(initial_pipeline="stub")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, RunScreen)


# ---------------------------------------------------------------------------
# History browser wiring
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Worktree toggle threading
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worktree_flag_threaded_to_run_screen(patch_load) -> None:
    """Enabling worktree in the launcher threads the flag to RunScreen."""
    app = NornUIApp(bundled=[_info("stub")], discovered=[])
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, LauncherScreen)
        await pilot.press("w")  # toggle worktree ON
        await pilot.press("enter")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, RunScreen)
        assert screen._use_worktree is True
        assert screen._run_id is not None
        assert len(screen._run_id) == 8  # uuid hex[:8]


@pytest.mark.asyncio
async def test_worktree_off_no_run_id(patch_load) -> None:
    """Without worktree, RunScreen has no run_id."""
    app = NornUIApp(bundled=[_info("stub")], discovered=[])
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, RunScreen)
        assert screen._use_worktree is False
        assert screen._run_id is None


@pytest.mark.asyncio
async def test_worktree_flag_survives_args_prompt(patch_load) -> None:
    """The worktree flag threads through the args prompt to RunScreen."""
    app = NornUIApp(bundled=[_info("needs", args={"args": "Path"})], discovered=[])
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("w")  # toggle ON
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ArgsPromptScreen)
        from textual.widgets import Input
        app.screen.query_one("#arg-args", Input).value = "tmp/x"
        await pilot.press("ctrl+s")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, RunScreen)
        assert screen._use_worktree is True
        assert screen._run_id is not None


# ---------------------------------------------------------------------------
# History browser wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_from_launcher_and_back(monkeypatch) -> None:
    """Pressing 'h' opens the history browser; Esc returns to the launcher."""
    monkeypatch.setattr(app_mod, "load_run_history", lambda ref: [])
    app = NornUIApp(bundled=[_info("stub")], discovered=[])
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("h")
        await pilot.pause()
        assert isinstance(app.screen, HistoryBrowserScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, LauncherScreen)


@pytest.mark.asyncio
async def test_history_resume_starts_run(patch_load, monkeypatch, tmp_path) -> None:
    """Resuming from the history browser starts a run."""
    import norn.tui.screens as screens_mod
    from norn.history import RunRecord

    rec = RunRecord(
        run_id=1,
        timestamp="2026-01-01T00:00:00",
        success=False,
        total_cost_usd=0.0,
        total_tokens=0,
        duration_ms=0,
        stages=[],
        retries=0,
    )
    monkeypatch.setattr(app_mod, "load_run_history", lambda ref: [rec])
    monkeypatch.setattr(app_mod, "load_run_checkpoint", lambda ref: None)
    # Make a checkpoint appear to exist so the 'r' (resume) action fires.
    ckpt = tmp_path / "stub.checkpoint"
    ckpt.write_text("{}")
    monkeypatch.setattr(screens_mod, "checkpoint_file", lambda _cp: ckpt)

    app = NornUIApp(bundled=[_info("stub")], discovered=[])
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("h")
        await pilot.pause()
        assert isinstance(app.screen, HistoryBrowserScreen)
        await pilot.press("r")
        await pilot.pause()
        assert isinstance(app.screen, RunScreen)


def _resume_setup(monkeypatch, tmp_path, checkpoint) -> list[dict]:
    """Stub history + checkpoint for a resume, and record the params each load gets."""
    import norn.tui.screens as screens_mod
    from norn.history import RunRecord

    rec = RunRecord(
        run_id=1, timestamp="2026-01-01T00:00:00", success=False,
        total_cost_usd=0.0, total_tokens=0, duration_ms=0, stages=[], retries=0,
    )
    loads: list[dict] = []

    def _load(ref, *, is_bundled, params):
        loads.append(dict(params))
        return _stub_pipeline()

    monkeypatch.setattr(app_mod, "load_pipeline_with_args", _load)
    monkeypatch.setattr(app_mod, "load_run_history", lambda ref: [rec])
    monkeypatch.setattr(app_mod, "load_run_checkpoint", lambda ref: checkpoint)
    ckpt = tmp_path / "needs.checkpoint"
    ckpt.write_text("{}")
    monkeypatch.setattr(screens_mod, "checkpoint_file", lambda _cp: ckpt)
    return loads


@pytest.mark.asyncio
async def test_history_resume_prefills_args_from_checkpoint(monkeypatch, tmp_path) -> None:
    """Resume shows the args form filled with the params the run was started with."""
    from norn.checkpoint import Checkpoint

    cp = Checkpoint(
        pipeline="needs", timestamp="t", session_id=None, completed_stages=[], results={},
        usage=[], params={"args": "PLAN-final-plan"},
    )
    loads = _resume_setup(monkeypatch, tmp_path, cp)
    app = NornUIApp(bundled=[_info("needs", args={"args": "Path"})], discovered=[])
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("h")
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        assert isinstance(app.screen, ArgsPromptScreen)
        assert app.screen.get_values() == {"args": "PLAN-final-plan"}
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, RunScreen)
    assert loads == [{"args": "PLAN-final-plan"}]


@pytest.mark.asyncio
async def test_history_resume_args_cancel_returns_to_launcher(monkeypatch, tmp_path) -> None:
    _resume_setup(monkeypatch, tmp_path, None)
    app = NornUIApp(bundled=[_info("needs", args={"args": "Path"})], discovered=[])
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("h")
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        assert app.screen.get_values() == {"args": ""}
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, LauncherScreen)

"""Tests for the launcher args-prompt flow (norn/tui/args_prompt.py).

Pure helpers (path detection, fzf invocation) are tested without an app; the
prompt screen/app is driven with Textual Pilot. All offline — fzf is
monkeypatched, never actually spawned.
"""
from __future__ import annotations

import types

import pytest

import norn.tui.args_prompt as ap
from norn.tui.args_prompt import (
    ArgsPromptApp,
    ArgsPromptScreen,
    is_dir_arg,
    is_path_arg,
    run_fzf,
)
from textual.widgets import Input


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "desc, expected",
    [
        ("Path to directory containing step-*.md files", True),
        ("The input file to read", True),
        ("Target folder", True),
        ("A directory of configs", True),
        ("Number of retries", False),
        ("The issue key", False),
        ("", False),
    ],
)
def test_is_path_arg(desc: str, expected: bool) -> None:
    assert is_path_arg(desc) is expected


@pytest.mark.parametrize(
    "desc, expected",
    [
        ("Path to a directory", True),
        ("Target folder", True),
        ("The input file", False),  # a file, not a dir
        ("Number of retries", False),
    ],
)
def test_is_dir_arg(desc: str, expected: bool) -> None:
    assert is_dir_arg(desc) is expected


def test_run_fzf_raises_when_fzf_missing(monkeypatch) -> None:
    """run_fzf fails loudly (no fallback) when fzf is not on PATH."""
    monkeypatch.setattr(ap, "shutil", types.SimpleNamespace(which=lambda _name: None))
    with pytest.raises(RuntimeError, match="fzf is not installed"):
        run_fzf(".")


def test_run_fzf_returns_selection(monkeypatch) -> None:
    """run_fzf returns the stripped fzf selection when fzf succeeds."""
    monkeypatch.setattr(ap, "shutil", types.SimpleNamespace(which=lambda _name: "/usr/bin/fzf"))

    class _Result:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout

    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "find":
            return _Result("./a\n./b\n")
        return _Result("./chosen/path\n")  # fzf

    monkeypatch.setattr(ap, "subprocess", types.SimpleNamespace(run=_fake_run, PIPE=-1))
    assert run_fzf(".") == "./chosen/path"
    # find ran, then fzf ran
    assert calls[0][0] == "find"
    assert calls[1][0] == "fzf"


def test_run_fzf_prunes_noise_dirs(monkeypatch) -> None:
    """The find listing prunes dependency/build/VCS dirs like .venv and .m2."""
    monkeypatch.setattr(ap, "shutil", types.SimpleNamespace(which=lambda _name: "/usr/bin/fzf"))
    captured: dict = {}

    class _Result:
        stdout = ""

    def _fake_run(cmd, **kwargs):
        if cmd[0] == "find":
            captured["find"] = cmd
        return _Result()

    monkeypatch.setattr(ap, "subprocess", types.SimpleNamespace(run=_fake_run, PIPE=-1))
    run_fzf(".")
    find = captured["find"]
    assert "-prune" in find and "-print" in find
    for name in (".venv", ".m2", "node_modules", "target", ".git", "__pycache__"):
        assert name in find, name


def test_run_fzf_dirs_only_passes_type_d(monkeypatch) -> None:
    monkeypatch.setattr(ap, "shutil", types.SimpleNamespace(which=lambda _name: "/usr/bin/fzf"))
    seen = {}

    class _Result:
        stdout = ""

    def _fake_run(cmd, **kwargs):
        if cmd[0] == "find":
            seen["find"] = cmd
        return _Result()

    monkeypatch.setattr(ap, "subprocess", types.SimpleNamespace(run=_fake_run, PIPE=-1))
    run_fzf(".", dirs_only=True)
    assert "-type" in seen["find"] and "d" in seen["find"]


# ---------------------------------------------------------------------------
# ArgsPromptApp — collect + cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_args_prompt_collects_values() -> None:
    """Filling inputs and submitting returns the values keyed by arg name."""
    app = ArgsPromptApp("implement_features", {"args": "Path to directory of step files"})
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ArgsPromptScreen)
        screen.query_one("#arg-args", Input).value = "tmp/norn-ui"
        await pilot.press("ctrl+s")
        await pilot.pause()
    assert app.params == {"args": "tmp/norn-ui"}


@pytest.mark.asyncio
async def test_args_prompt_enter_submits() -> None:
    """Pressing Enter in an input submits the whole form."""
    app = ArgsPromptApp("p", {"args": "Path to a folder"})
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.query_one("#arg-args", Input).value = "some/dir"
        await pilot.press("enter")
        await pilot.pause()
    assert app.params == {"args": "some/dir"}


@pytest.mark.asyncio
async def test_args_prompt_escape_cancels() -> None:
    """Escape cancels the prompt; params is None (caller goes back to launcher)."""
    app = ArgsPromptApp("p", {"args": "Path to a folder"})
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert app.params is None


@pytest.mark.asyncio
async def test_args_prompt_multiple_args() -> None:
    """Multiple declared args each get an input keyed by name."""
    app = ArgsPromptApp("p", {"args": "positional text", "issue": "Jira issue key"})
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        screen.query_one("#arg-args", Input).value = "do the thing"
        screen.query_one("#arg-issue", Input).value = "CBS-123"
        await pilot.press("ctrl+s")
        await pilot.pause()
    assert app.params == {"args": "do the thing", "issue": "CBS-123"}

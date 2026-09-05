"""Args-collection screen for the Norn TUI launcher flow.

After a pipeline is chosen in the launcher, pipelines that declare ``args`` in
their ``metadata`` need values before a run can start (e.g. the bundled
``implement_features`` pipeline needs the directory holding ``step-*.md``
files).  This module renders one input per declared arg and, for args whose
description looks like a filesystem path, offers an ``fzf`` picker
(``ctrl+o``) so the user can fuzzy-find a file or folder instead of typing it.

This module imports textual and must only be imported lazily from within
``norn/tui/`` (never at core ``norn.*`` import time).  The pure helpers
(:func:`is_path_arg`, :func:`run_fzf`) are import-safe and unit-tested
without a running app.
"""
from __future__ import annotations

import re
import shutil
import subprocess

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Input, Label, Static

# Keywords in an arg's description that mark it as a filesystem path.
_PATH_HINTS = ("path", "file", "folder", "directory", "dir")
# Subset that specifically implies a directory (so fzf can list dirs only).
_DIR_HINTS = ("folder", "directory", "dir")

# Directory names pruned from the fzf listing — dependency, build, cache and
# VCS folders that are large and never the thing a user is picking. Their whole
# subtree is skipped, which also keeps `find` fast on big repos.
_FZF_PRUNE_DIRS = (
    ".git", ".hg", ".svn",
    ".venv", "venv", ".tox", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "node_modules",
    ".m2", ".gradle", "target", "build", "dist", ".eggs",
    ".idea", ".vscode",
    ".cache", ".terraform", ".cargo",
    ".next", ".nuxt", ".svelte-kit",
)


def is_path_arg(description: str) -> bool:
    """Return whether *description* suggests the arg is a filesystem path.

    Heuristic: the description mentions a path/file/folder/directory. Used to
    decide whether to offer the fzf picker for an arg input.
    """
    desc = (description or "").lower()
    return any(h in desc for h in _PATH_HINTS)


def is_dir_arg(description: str) -> bool:
    """Return whether *description* implies a directory (vs a plain file)."""
    desc = (description or "").lower()
    return any(h in desc for h in _DIR_HINTS)


def run_fzf(start_dir: str = ".", *, dirs_only: bool = False) -> str | None:
    """Run ``fzf`` over the filesystem under *start_dir* and return the choice.

    Pipes a ``find`` listing into ``fzf`` and returns the selected path, or
    ``None`` when the user cancelled (selected nothing). fzf draws its
    interactive UI on ``/dev/tty``; callers must wrap this in
    ``App.suspend()`` so the Textual app releases the terminal first.

    Raises:
        RuntimeError: if ``fzf`` is not installed (not on ``PATH``). The picker
            has no fallback — a missing ``fzf`` fails loudly rather than
            silently degrading to manual text entry.
    """
    if not shutil.which("fzf"):
        raise RuntimeError(
            "fzf is not installed (not on PATH); it is required for the "
            "file/folder picker. Install fzf, e.g. `brew install fzf`."
        )
    # Prune noisy/heavy dirs: `( -name X -o -name Y ... ) -prune -o <rest> -print`.
    # Once -prune/-o is used, find no longer prints by default, so -print is explicit.
    prune: list[str] = ["("]
    for i, name in enumerate(_FZF_PRUNE_DIRS):
        if i:
            prune.append("-o")
        prune += ["-name", name]
    prune.append(")")
    find_cmd = ["find", start_dir, "-mindepth", "1", *prune, "-prune", "-o"]
    if dirs_only:
        find_cmd += ["-type", "d"]
    find_cmd += ["-print"]
    listing = subprocess.run(find_cmd, stdout=subprocess.PIPE, text=True, check=False)
    chosen = subprocess.run(
        ["fzf", "--height=100%", "--prompt=path> "],
        input=listing.stdout,
        stdout=subprocess.PIPE,
        text=True,
        check=False,
    )
    out = chosen.stdout.strip()
    return out or None


def _input_id(name: str) -> str:
    """Map an arg name to a CSS-safe widget id."""
    return "arg-" + re.sub(r"\W", "_", name)


class ArgsPromptScreen(Screen):
    """Form collecting values for a pipeline's declared ``args``.

    One :class:`~textual.widgets.Input` per arg.  Path-like args (per
    :func:`is_path_arg`) can be filled via the ``fzf`` picker (``ctrl+o`` while
    the input is focused).  Submit with ``ctrl+s`` or Enter; cancel with
    ``Escape`` (dismisses with ``None`` so the caller returns to the launcher).
    """

    BINDINGS = [
        Binding("ctrl+s", "submit", "Run", show=True),
        Binding("ctrl+o", "browse", "Browse (fzf)", show=True),
        Binding("escape", "cancel", "Back", show=True),
    ]

    CSS = """
    ArgsPromptScreen VerticalScroll {
        height: 1fr;
        padding: 1 2;
    }
    ArgsPromptScreen Label {
        margin-top: 1;
        color: $text-muted;
    }
    ArgsPromptScreen #args-hint {
        dock: bottom;
        padding: 0 2;
        color: $text-muted;
    }
    """

    def __init__(self, pipeline_name: str, args_meta: dict[str, str]) -> None:
        super().__init__()
        self._pipeline_name = pipeline_name
        self._args_meta = dict(args_meta)
        self._id_to_name: dict[str, str] = {_input_id(n): n for n in self._args_meta}
        self._path_args = {n for n, d in self._args_meta.items() if is_path_arg(d)}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with VerticalScroll():
            yield Label(f"Arguments for [b]{self._pipeline_name}[/b]")
            for name, desc in self._args_meta.items():
                hint = " — ctrl+o to browse" if name in self._path_args else ""
                yield Label(f"{name}: {desc}{hint}")
                yield Input(id=_input_id(name), placeholder=desc)
        yield Static(
            "ctrl+s / Enter: run   ·   ctrl+o: fzf picker (path args)   ·   Esc: back",
            id="args-hint",
        )
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Norn — Arguments"
        # Focus the first input so the user can type / browse immediately.
        first = next(iter(self._args_meta), None)
        if first is not None:
            self.query_one(f"#{_input_id(first)}", Input).focus()

    # ------------------------------------------------------------------
    # Accessors (used in tests)
    # ------------------------------------------------------------------

    def get_values(self) -> dict[str, str]:
        """Return the current ``{arg_name: value}`` mapping from the inputs."""
        return {
            name: self.query_one(f"#{_input_id(name)}", Input).value
            for name in self._args_meta
        }

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in any input submits the whole form."""
        self.action_submit()

    def action_submit(self) -> None:
        """Dismiss with the collected values."""
        self.dismiss(self.get_values())

    def action_cancel(self) -> None:
        """Dismiss with ``None`` — caller treats this as 'back to launcher'."""
        self.dismiss(None)

    def action_browse(self) -> None:
        """Open the fzf picker for the focused path input.

        Browse is only meaningful on a path argument (a precondition, not a
        fallback). A missing ``fzf`` is not handled here — ``run_fzf`` raises
        loudly (see :func:`run_fzf`).
        """
        focused = self.focused
        name = self._id_to_name.get(getattr(focused, "id", "") or "")
        if name is None or name not in self._path_args:
            self.notify("Browse is only available for path arguments.", severity="warning")
            return
        with self.app.suspend():
            chosen = run_fzf(".", dirs_only=is_dir_arg(self._args_meta[name]))
        if chosen and isinstance(focused, Input):
            focused.value = chosen


class ArgsPromptApp(App):
    """Standalone app wrapping :class:`ArgsPromptScreen`.

    After the user submits or cancels, the app exits; the caller reads the
    result via :attr:`params` (``dict`` on submit, ``None`` on cancel/back).
    """

    TITLE = "Norn — Arguments"

    def __init__(self, pipeline_name: str, args_meta: dict[str, str]) -> None:
        super().__init__()
        self._pipeline_name = pipeline_name
        self._args_meta = dict(args_meta)
        self.params: dict[str, str] | None = None

    async def on_mount(self) -> None:
        def _on_dismiss(result: dict[str, str] | None) -> None:
            self.params = result
            self.exit()

        await self.push_screen(
            ArgsPromptScreen(self._pipeline_name, self._args_meta),
            _on_dismiss,
        )

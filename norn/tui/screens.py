"""Textual screens for the Norn TUI.

This module imports textual and must only be imported from within ``norn/tui/``.
It must **never** be imported at core (``norn.*``) import time.
"""
from __future__ import annotations

from dataclasses import dataclass

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from norn.catalog import PipelineInfo
from norn.checkpoint import checkpoint_file
from norn.history import RunRecord


class _OpenFileSentinel:
    """Marker the launcher dismisses with when the user picks 'Open file…'.

    Distinct from ``None`` (which means quit) so the caller can open a file
    picker instead of exiting.
    """

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "OPEN_FILE"


OPEN_FILE = _OpenFileSentinel()


@dataclass
class HistoryRequest:
    """Launcher result: the user asked to view the history of ``info``.

    Carried as the launcher's dismiss value so the hosting app can push the
    history browser for that pipeline (distinct from launching it).
    """

    info: PipelineInfo


@dataclass(frozen=True)
class LaunchRequest:
    """Launcher result: the user chose to launch a pipeline run.

    Carries both the :class:`~norn.catalog.PipelineInfo` and the worktree flag
    so the hosting app can distinguish a normal launch from a worktree-isolated
    one.  Returned as the launcher's dismiss value when the user presses Enter
    on a pipeline row.
    """

    info: PipelineInfo
    use_worktree: bool = False


class HistoryBrowserScreen(Screen):
    """Read-only history browser over ``.history`` files.

    Lists pipeline runs from ``load_history`` in a
    :class:`~textual.widgets.DataTable`.  Selecting a row (cursor movement +
    Enter, or a click) shows the ``stage_log`` summary — including error text
    for failed stages — in a detail panel below.

    Press **r** to resume: the screen dismisses with ``("resume", config_path)``
    if a checkpoint exists for the config.  Press **q** or **Escape** to dismiss
    without action.
    """

    BINDINGS = [
        Binding("r", "resume", "Resume", show=True),
        Binding("q", "quit_browser", "Quit", show=True),
        Binding("escape", "quit_browser", "Back", show=False),
    ]

    CSS = """
    HistoryBrowserScreen DataTable {
        height: 1fr;
    }
    HistoryBrowserScreen #stage-detail {
        height: 1fr;
        border-top: solid $primary;
        padding: 1;
    }
    """

    def __init__(
        self,
        records: list[RunRecord],
        config_path: str | None = None,
    ) -> None:
        super().__init__()
        self._records = records
        self._config_path = config_path
        self._selected_record: RunRecord | None = None
        self._detail_content: str = "Select a run to view stage details."

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="run-table", cursor_type="row")
        yield Static("Select a run to view stage details.", id="stage-detail")
        yield Footer()

    def on_mount(self) -> None:
        """Populate the DataTable with one row per run record."""
        table = self.query_one("#run-table", DataTable)
        table.add_columns("Run", "Timestamp", "Status", "Cost", "Time", "Info", "Resumable")

        has_checkpoint = (
            self._config_path is not None
            and checkpoint_file(self._config_path).exists()
        )

        for r in self._records:
            if r.in_progress:
                status = "↻ Running"
            else:
                status = "✓ Complete" if r.success else "✗ Failed"
            cost = f"${r.total_cost_usd:.2f}" if r.total_cost_usd else "-"
            duration = f"{r.duration_ms / 1000:.1f}s"
            if r.in_progress:
                info = f"{len(r.stages)} stages so far"
            else:
                info = f"{len(r.stages)} stages" if r.success else f"stage: {r.failed_stage or '?'}"
            ts = r.timestamp[:16].replace("T", " ")
            # Checkpoint is a singleton for the config — mark only the most
            # recent run as resumable (the one whose checkpoint would be loaded).
            is_most_recent = r is self._records[-1]
            resumable = "yes" if (has_checkpoint and is_most_recent) else "-"
            table.add_row(
                f"#{r.run_id}", ts, status, cost, duration, info, resumable,
                key=str(r.run_id),
            )

    # ------------------------------------------------------------------
    # Row selection → detail panel
    # ------------------------------------------------------------------

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Show the stage log for the selected row in the detail panel."""
        run_id = int(str(event.row_key.value))
        record = next((r for r in self._records if r.run_id == run_id), None)
        if record is None:
            return
        self._selected_record = record
        self._show_stage_detail(record)

    def _show_stage_detail(self, record: RunRecord) -> None:
        """Render the stage log for *record* into the detail panel.

        Reuses the same column/format logic as
        :func:`~norn.ui.print_history_run_details`:

        * ``✓`` / ``✗`` glyph.
        * Stage name, status.
        * Last line of ``entry.error`` if the stage failed.
        """
        lines = [f"Run #{record.run_id} — Stage Log:"]
        if not record.stage_log:
            lines.append("  (no stage log stored for this run)")
        else:
            for entry in record.stage_log:
                glyph = "✓" if entry.success else "✗"
                line = f"  {glyph} {entry.name:<20}  {entry.status}"
                if entry.error:
                    # Show last line of error text (mirrors print_history_run_details)
                    err_tail = entry.error.strip().splitlines()[-1]
                    line += f"  — {err_tail}"
                lines.append(line)
        content = "\n".join(lines)
        self._detail_content = content
        detail = self.query_one("#stage-detail", Static)
        detail.update(content)

    # ------------------------------------------------------------------
    # Pure accessors (used in tests)
    # ------------------------------------------------------------------

    def get_detail_content(self) -> str:
        """Return the current stage-detail panel text.

        Pure accessor — reads from the tracked ``_detail_content`` string,
        analogous to :meth:`~norn.tui.widgets.NornHeader.get_content`.  Used
        in Pilot tests to assert detail content without fighting Textual's
        render cycle.
        """
        return self._detail_content

    def get_run_count(self) -> int:
        """Return the number of rows currently displayed in the run table.

        Used in tests.
        """
        return self.query_one("#run-table", DataTable).row_count

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_resume(self) -> None:
        """Dismiss with a resume intent if a checkpoint exists for the config.

        The caller (app or parent screen) receives ``("resume", config_path)``
        as the dismiss value and is responsible for launching the resumed run.
        If no config_path or no checkpoint exists, the action is a no-op.
        """
        if self._config_path is None:
            return
        ckpt_path = checkpoint_file(self._config_path)
        if not ckpt_path.exists():
            return
        self.dismiss(("resume", self._config_path))

    def action_quit_browser(self) -> None:
        """Dismiss this screen without action."""
        self.dismiss(None)


class LauncherScreen(Screen):
    """Home screen listing discoverable pipelines.

    Shows bundled pipelines and any discovered pipelines from configured
    directories.  Selecting a row and pressing **Enter** dismisses the screen
    with the chosen :class:`~norn.catalog.PipelineInfo`, which the caller can
    use to launch a run.  Pressing **q** or **Escape** dismisses with
    ``None``.

    A trailing "Open file…" row lets users signal that they want to supply a
    pipeline file path manually; selecting it also dismisses with ``None``.
    """

    BINDINGS = [
        Binding("enter", "launch", "Launch", show=True),
        Binding("w", "toggle_worktree", "Worktree", show=True),
        Binding("h", "history", "History", show=True),
        Binding("q", "quit_launcher", "Quit", show=True),
        Binding("escape", "quit_launcher", "Back", show=False),
    ]

    CSS = """
    LauncherScreen DataTable {
        height: 1fr;
    }
    LauncherScreen #pipeline-detail {
        height: auto;
        max-height: 8;
        border-top: solid $primary;
        padding: 0 1;
    }
    LauncherScreen #worktree-status {
        height: 1;
        background: $surface;
        padding: 0 1;
    }
    """

    # Special sentinel key for the "open file" row
    _OPEN_FILE_KEY = "__open_file__"

    def __init__(
        self,
        bundled: list[PipelineInfo],
        discovered: list[PipelineInfo],
    ) -> None:
        super().__init__()
        self._bundled = bundled
        self._discovered = discovered
        self._selected_info: PipelineInfo | None = None
        self._use_worktree: bool = False
        # Map row key string → PipelineInfo for fast lookup
        self._key_map: dict[str, PipelineInfo] = {}
        # Column key for the "Name" column (set in on_mount)
        self._name_col_key: object = None
        self._desc_col_key: object = None

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield DataTable(id="pipeline-table", cursor_type="row")
        yield Static("", id="pipeline-detail")
        yield Static("Worktree: OFF", id="worktree-status")
        yield Footer()

    def on_mount(self) -> None:
        """Populate the table with bundled, discovered, and open-file rows."""
        table = self.query_one("#pipeline-table", DataTable)
        name_col, desc_col, _src_col = table.add_columns("Name", "Description", "Source")
        self._name_col_key = name_col
        self._desc_col_key = desc_col

        for info in self._bundled:
            row_key = f"bundled:{info.name}"
            self._key_map[row_key] = info
            table.add_row(info.name, info.short or "", "bundled", key=row_key)

        for info in self._discovered:
            row_key = f"discovered:{info.path}"
            self._key_map[row_key] = info
            table.add_row(
                info.name,
                info.short or "",
                str(info.path.parent),
                key=row_key,
            )

        # "Open file…" sentinel row — selecting it dismisses with None so the
        # caller can fall back to the CLI `norn ui <path>` workflow.
        table.add_row("Open file…", "Specify a pipeline .py file path", "", key=self._OPEN_FILE_KEY)

    # ------------------------------------------------------------------
    # Row highlighting → detail panel
    # ------------------------------------------------------------------

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Update the detail panel when the cursor moves to a row."""
        key = str(event.row_key.value)
        info = self._key_map.get(key)
        if info is None:
            self._selected_info = None
            self.query_one("#pipeline-detail", Static).update(
                "Open a pipeline .py file by path (use: norn ui <path>)"
            )
            return
        self._selected_info = info
        detail_lines = [info.long or info.short or ""]
        if info.path:
            detail_lines.append(f"Path: {info.path}")
        self.query_one("#pipeline-detail", Static).update("\n".join(detail_lines))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Launch the pipeline on the selected row.

        A focused ``DataTable`` consumes the Enter key via its own
        ``select_cursor`` binding and posts ``RowSelected``, which shadows this
        screen's ``enter`` → :meth:`action_launch` binding.  Handle the message
        directly so Enter actually launches the highlighted pipeline.  The
        "Open file…" sentinel dismisses with ``None`` (caller falls back to the
        CLI ``norn ui <path>`` workflow).
        """
        key = str(event.row_key.value)
        if key == self._OPEN_FILE_KEY:
            self.dismiss(OPEN_FILE)
            return
        info = self._key_map.get(key)
        self._selected_info = info
        if info is not None:
            self.dismiss(LaunchRequest(info=info, use_worktree=self._use_worktree))
        else:
            self.dismiss(info)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_launch(self) -> None:
        """Dismiss with a :class:`LaunchRequest` (or ``None``).

        Kept for the footer key hint and as a fallback when the ``DataTable``
        is not focused; the live Enter path runs through
        :meth:`on_data_table_row_selected`.
        """
        info = self._selected_info
        if info is not None:
            self.dismiss(LaunchRequest(info=info, use_worktree=self._use_worktree))
        else:
            self.dismiss(None)

    def action_toggle_worktree(self) -> None:
        """Toggle the 'run in worktree' mode and update the status line."""
        self._use_worktree = not self._use_worktree
        label = "Worktree: ON" if self._use_worktree else "Worktree: OFF"
        self.query_one("#worktree-status", Static).update(label)

    def action_history(self) -> None:
        """Open the run-history browser for the highlighted pipeline."""
        if self._selected_info is None:
            self.notify("Highlight a pipeline to view its history.", severity="warning")
            return
        self.dismiss(HistoryRequest(self._selected_info))

    def action_quit_launcher(self) -> None:
        """Dismiss without selection."""
        self.dismiss(None)

    # ------------------------------------------------------------------
    # Pure accessors for tests
    # ------------------------------------------------------------------

    def get_worktree_status(self) -> bool:
        """Return the current worktree toggle state (used in tests)."""
        return self._use_worktree

    def get_pipeline_count(self) -> int:
        """Return total number of rows (including the 'Open file…' row)."""
        return self.query_one("#pipeline-table", DataTable).row_count

    def get_row_names(self) -> list[str]:
        """Return the Name column values for every row (for test assertions).

        Uses the stored ``_name_col_key`` (set at :meth:`on_mount` time) to
        avoid relying on integer column indexing, which is not how Textual
        ``DataTable`` column keys work.
        """
        table = self.query_one("#pipeline-table", DataTable)
        names: list[str] = []
        for row_key in table.rows:
            cell = table.get_cell(row_key, self._name_col_key)
            names.append(str(cell))
        return names

    def get_description_for_name(self, name: str) -> str | None:
        """Return the Description column value for the row with the given *name*.

        Returns ``None`` if no row with that name exists.  Used in tests to
        avoid direct ``DataTable`` column-key arithmetic.
        """
        table = self.query_one("#pipeline-table", DataTable)
        for row_key in table.rows:
            name_cell = str(table.get_cell(row_key, self._name_col_key))
            if name_cell == name:
                return str(table.get_cell(row_key, self._desc_col_key))
        return None


class HistoryBrowserApp(App):
    """Standalone Textual app wrapping :class:`HistoryBrowserScreen`.

    Suitable for ``norn ui --history <config>`` and for Pilot-based tests.

    Example::

        records = load_history("my_pipeline.py")
        app = HistoryBrowserApp(records=records, config_path="my_pipeline.py")
        app.run()
    """

    TITLE = "Norn — History Browser"

    def __init__(
        self,
        records: list[RunRecord],
        config_path: str | None = None,
    ) -> None:
        super().__init__()
        self._records = records
        self._config_path = config_path

    async def on_mount(self) -> None:
        """Push the history browser screen onto the screen stack."""
        await self.push_screen(HistoryBrowserScreen(self._records, self._config_path))


class LauncherApp(App):
    """Standalone Textual app wrapping :class:`LauncherScreen`.

    Suitable for the ``norn ui`` subcommand (no pipeline argument) and for
    Pilot-based tests.  After the user selects a pipeline and presses Enter
    (or dismisses via Escape/q), the app exits; the caller reads the result
    via :attr:`selected`.

    Example::

        from norn.catalog import list_pipelines, list_discovered_pipelines
        from norn.tui.screens import LauncherApp

        app = LauncherApp(
            bundled=list_pipelines(),
            discovered=list_discovered_pipelines(),
        )
        app.run()
        if app.selected is not None:
            # launch a run with app.selected
            ...
    """

    TITLE = "Norn — Pipeline Launcher"

    def __init__(
        self,
        bundled: list[PipelineInfo],
        discovered: list[PipelineInfo],
    ) -> None:
        super().__init__()
        self._bundled = bundled
        self._discovered = discovered
        self.selected: object = None

    async def on_mount(self) -> None:
        """Push the launcher screen and exit when it dismisses."""

        def _on_dismiss(result: object) -> None:
            self.selected = result
            self.exit()

        await self.push_screen(
            LauncherScreen(self._bundled, self._discovered),
            _on_dismiss,
        )

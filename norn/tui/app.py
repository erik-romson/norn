"""Textual apps for Norn — the run screen and the unified launcher app.

This module imports textual and must only be imported lazily (behind a
``try/except ImportError`` guard).  It is **never** imported at core
(``norn.*``) import time.

Three public surfaces:

* :class:`RunScreen` — the live-run UI (header, graph, transcript, detail,
  budget) plus the logic that drives ``run_pipeline`` in-process.
* :class:`NornApp` — a thin app that hosts a single :class:`RunScreen` for a
  pre-built pipeline (used by ``norn ui`` direct-object paths and tests).
* :class:`NornUIApp` — the unified launcher app that pushes Launcher → Args →
  Run screens within a single ``App.run()`` so transitions are seamless.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Footer

from norn.catalog import get_pipeline_info
from norn.tui.args_prompt import ArgsPromptScreen, run_fzf
from norn.tui.launch import (
    build_run_setup,
    history_config_key,
    load_pipeline_with_args,
    load_run_checkpoint,
    load_run_history,
    pipeline_args_meta,
    resolve_ref,
)
from norn.tui.screens import (
    OPEN_FILE,
    HistoryBrowserScreen,
    HistoryRequest,
    LaunchRequest,
    LauncherScreen,
)
from norn.tui.viewmodel import RunViewModel
from norn.tui.widgets import BudgetMeter, NornGraph, NornHeader, StageDetail, Transcript


def _event_stage_id(event: object) -> str | None:
    """Return the stage_id an event refers to, or ``None`` for run-level events.

    Stage-scoped events (``StageStarted``, ``StageFinished``, ``TurnEvent`` …)
    carry a non-empty ``key.stage_id``; run-level events (``RunStarted``,
    ``RunFinished`` …) leave it empty.  Used to auto-follow the active stage in
    the transcript and detail panels.
    """
    key = getattr(event, "key", None)
    stage_id = getattr(key, "stage_id", None)
    return stage_id or None


class _RunEvent(Message):
    """Internal Textual message wrapping a run-event from the ``EventSink``."""

    def __init__(self, event: object) -> None:
        super().__init__()
        self.event = event


class _RunDone(Message):
    """Internal Textual message signalling the pipeline run has finished."""

    def __init__(self, *, success: bool, error: str | None = None, worktree_message: str | None = None) -> None:
        super().__init__()
        self.success = success
        self.error = error
        self.worktree_message = worktree_message


class RunScreen(Screen):
    """Live pipeline-run screen.

    Composes the run widgets and, when *pipeline_obj* / *run_kwargs* are
    provided, launches ``run_pipeline(...)`` as an ``asyncio.Task`` on mount,
    wiring an ``EventSink`` whose callback posts :class:`_RunEvent` messages so
    the Textual loop processes them on the UI thread.

    Dismisses with ``"back"`` (return to launcher) or ``"quit"`` (exit app);
    the hosting app decides what each means.
    """

    CSS = """
    NornHeader {
        height: 1;
        background: $primary;
        color: $text;
        padding: 0 1;
    }
    NornGraph {
        height: 1fr;
        width: 30;
    }
    Transcript {
        height: 1fr;
    }
    StageDetail {
        height: auto;
        border-top: solid $primary;
    }
    BudgetMeter {
        height: 1;
        background: $surface;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("p", "toggle_pause", "Pause/Resume", show=True),
        Binding("c", "cancel_run", "Cancel", show=True),
        Binding("a", "answer_input", "Answer", show=True),
        Binding("b", "back", "Back", show=True),
        Binding("escape", "back", "Back", show=False),
        Binding("q", "quit_app", "Quit", show=True),
    ]

    def __init__(
        self,
        pipeline: str | None = None,
        *,
        graph: Any | None = None,
        vm: RunViewModel | None = None,
        budget: Any | None = None,
        pipeline_obj: Any | None = None,
        run_kwargs: dict[str, Any] | None = None,
        capabilities: Any | None = None,
        use_worktree: bool = False,
        run_id: str | None = None,
    ) -> None:
        super().__init__()
        self._pipeline = pipeline
        self._graph = graph
        self._vm = vm if vm is not None else RunViewModel()
        self._budget = budget
        self._pipeline_obj = pipeline_obj
        self._run_kwargs: dict[str, Any] = run_kwargs or {}
        self._run_task: asyncio.Task[Any] | None = None
        self._capabilities = capabilities
        self._use_worktree = use_worktree
        self._run_id = run_id
        self.run_finished: bool = False
        self.run_success: bool | None = None
        self.worktree_message: str | None = None
        # Set when the user asks to return to the launcher (vs. fully quitting).
        self.go_back: bool = False
        self._controller: Any | None = None
        # True while the input-decision modal is on screen (avoids double-push).
        self._modal_open: bool = False

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield NornHeader(self._vm)
        if self._graph is not None:
            yield NornGraph(self._graph, self._vm)
        yield Transcript(self._vm)
        yield StageDetail(self._vm)
        yield BudgetMeter(self._vm, self._budget)
        # Footer renders the key bindings (Pause/Resume, Cancel, Answer, Back,
        # Quit). Disabled actions grey out via check_action/_is_action_enabled.
        yield Footer()

    # ------------------------------------------------------------------
    # Lifecycle — start the pipeline run as a background task
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        if self._pipeline_obj is not None:
            self._run_task = asyncio.create_task(self._drive_run())

    async def _drive_run(self) -> None:
        """Run the pipeline in-process, feeding events into the UI.

        When ``_use_worktree`` is set, the run is wrapped in a
        :class:`~norn.worktree.WorktreeSession` lifecycle: create the worktree
        before the pipeline, merge back on success, and clean up based on the
        outcome (see the cleanup matrix in the step plan).
        """
        from norn.event_sink import EventSink  # noqa: PLC0415
        from norn.responder import TUIResponder  # noqa: PLC0415
        from norn.run_control import RunController  # noqa: PLC0415
        from norn.runner import run_pipeline  # noqa: PLC0415

        controller = RunController()
        self._controller = controller
        sink = EventSink(on_event=lambda ev: self.post_message(_RunEvent(ev)))
        responder = TUIResponder(controller)

        kwargs: dict[str, Any] = {**self._run_kwargs}
        kwargs["event_sink"] = sink
        kwargs["run_controller"] = controller
        kwargs["input_responder"] = responder

        session = None
        worktree_message: str | None = None

        # --- Create worktree (if requested) ---
        if self._use_worktree and self._run_id:
            from norn.worktree import WorktreeError, WorktreeSession  # noqa: PLC0415

            try:
                session = WorktreeSession.create(self._run_id)
            except WorktreeError as exc:
                self.post_message(
                    _RunDone(success=False, error=str(exc), worktree_message=str(exc))
                )
                return
            kwargs["working_dir"] = session.worktree_dir
            kwargs["run_id"] = self._run_id

        # --- Run the pipeline ---
        success = True
        error: str | None = None
        cancelled = False
        from norn.run_control import CancelledError as _RunCancelled  # noqa: PLC0415

        try:
            await run_pipeline(self._pipeline_obj, **kwargs)
        except asyncio.CancelledError:
            success = False
            error = "Run cancelled."
            cancelled = True
            raise
        except _RunCancelled as exc:
            success = False
            error = str(exc)
            cancelled = True
        except Exception as exc:  # noqa: BLE001 — surface any run failure to the UI
            success = False
            error = str(exc)
        finally:
            # --- Worktree merge-back + cleanup ---
            if session is not None:
                worktree_message = self._worktree_finish(
                    session, success=success, cancelled=cancelled
                )
            self.post_message(
                _RunDone(success=success, error=error, worktree_message=worktree_message)
            )

    def _worktree_finish(
        self,
        session: Any,
        *,
        success: bool,
        cancelled: bool,
    ) -> str:
        """Merge back and clean up a worktree session; return a status message.

        Cleanup decision matrix (from the step plan):
        - successful merge (ff/no-ff) → removed / deleted
        - no changes → removed / deleted
        - merge conflict → kept
        - dirty-launch refusal → kept
        - commit failure (no identity) → kept
        - other git error → kept
        - pipeline stage failure → kept
        - user cancel → kept
        """
        pipeline_name = self._pipeline or "pipeline"

        # Pipeline failed or was cancelled — keep the worktree so no work is lost
        if not success or cancelled:
            reason = "cancelled" if cancelled else "failed"
            session.cleanup(keep=True)
            return (
                f"Run {reason}; work preserved at {session.worktree_dir} "
                f"on {session.work_branch}."
            )

        # Attempt merge-back
        message = f"norn run {pipeline_name} ({self._run_id})"
        result = session.merge_back(message=message)

        # No changes — nothing to preserve
        if not result.changed:
            session.cleanup(keep=False)
            return "Run made no changes."

        # Successful merge — work is now on the launch branch
        if result.merged:
            session.cleanup(keep=False)
            return f"Merged into {result.base_ref} ({len(result.files)} files)."

        # Merge conflict — keep the worktree so the user can resolve manually
        if result.conflict:
            session.cleanup(keep=True)
            msg = (
                f"Merge conflict in {len(result.files)} files. "
                f"Worktree kept at {session.worktree_dir} on branch "
                f"{session.work_branch}; resolve manually."
            )
            if getattr(result, "abort_failed", False):
                msg += (
                    f" WARNING: could not abort the merge — the launch repo at "
                    f"{result.base_ref} may be left mid-merge; run `git merge --abort`."
                )
            return msg

        # Other refusals (dirty-launch, no-identity, git-error) — keep the worktree
        refused = result.refused or "unknown error"
        session.cleanup(keep=True)
        return (
            f"Merge refused ({refused}); work preserved at "
            f"{session.worktree_dir} on {session.work_branch}."
        )

    # ------------------------------------------------------------------
    # Event driving (for testing and live runner wiring)
    # ------------------------------------------------------------------

    def apply_event(self, event: object) -> None:
        """Apply *event* to the ViewModel and refresh all bound widgets.

        The transcript and detail panels follow the stage referenced by
        *event* so the user always sees live output and — on failure — the
        error text for the stage that is currently running or just finished.
        Without this, a failed stage shows only its tree glyph (``✗``) with no
        explanation of what went wrong.
        """
        self._vm.apply(event)
        self.query_one(NornHeader).refresh_vm()
        try:
            self.query_one(NornGraph).refresh_vm()
        except NoMatches:
            pass
        stage_id = _event_stage_id(event)
        transcript = self.query_one(Transcript)
        detail = self.query_one(StageDetail)
        if stage_id is not None:
            transcript.set_stage(stage_id)
            detail.set_stage(stage_id)
        else:
            transcript.refresh_vm()
            detail.refresh_vm()
        self.query_one(BudgetMeter).refresh_vm()

    def on__run_event(self, message: _RunEvent) -> None:
        self.apply_event(message.event)
        # When the runner blocks on input, pop the decision modal so the user
        # can retry/continue/abort instead of the run silently stalling.
        if type(message.event).__name__ == "WaitingInput":
            self._open_input_modal()

    def on__run_done(self, message: _RunDone) -> None:
        self.run_finished = True
        self.run_success = message.success
        self.worktree_message = message.worktree_message
        if message.worktree_message:
            self.notify(message.worktree_message, timeout=15)

    # ------------------------------------------------------------------
    # Capability checking
    # ------------------------------------------------------------------

    def _is_action_enabled(self, action: str) -> bool:
        caps = self._capabilities
        if action in ("toggle_pause", "quit_app", "back"):
            return True
        if action == "cancel_run":
            return self._controller is not None and not self.run_finished
        if action == "answer_input":
            return self._vm.waiting_input is not None
        if action == "set_model":
            return caps is not None and getattr(caps, "live_model_switch", False)
        if action == "attach":
            return caps is not None and getattr(caps, "session_attachable", False)
        return True

    def check_action(self, action: str, parameters: tuple[Any, ...]) -> bool | None:
        if not self._is_action_enabled(action):
            return False
        return True

    # ------------------------------------------------------------------
    # Key binding actions
    # ------------------------------------------------------------------

    def action_toggle_pause(self) -> None:
        ctrl = self._controller
        if ctrl is None or self.run_finished:
            return
        if ctrl.is_paused:
            ctrl.resume()
        else:
            ctrl.pause()

    def action_cancel_run(self) -> None:
        ctrl = self._controller
        if ctrl is None or self.run_finished:
            return
        ctrl.cancel()

    def action_answer_input(self) -> None:
        """Open the decision modal for the pending WaitingInput (if any)."""
        self._open_input_modal()

    def _open_input_modal(self) -> None:
        """Push the input-decision modal for the current WaitingInput."""
        wi = self._vm.waiting_input
        if wi is None or self._modal_open or self._controller is None:
            return
        from norn.tui.modals import InputDecisionModal, node_display_name  # noqa: PLC0415

        kind = getattr(wi, "kind", "") or ""
        stage_name = node_display_name(getattr(getattr(wi, "key", None), "stage_id", None))
        excerpt = getattr(wi, "prompt_excerpt", "") or ""
        self._modal_open = True
        self.app.push_screen(
            InputDecisionModal(kind, stage_name=stage_name, excerpt=excerpt),
            self._on_input_decision,
        )

    def _on_input_decision(self, code: str | None) -> None:
        """Resolve the pending WaitingInput with the user's chosen code."""
        self._modal_open = False
        ctrl = self._controller
        if code and ctrl is not None:
            ctrl.answer_input(code)
        self._vm.waiting_input = None

    def _cancel_active_run(self) -> None:
        ctrl = self._controller
        if ctrl is not None and not self.run_finished:
            ctrl.cancel()

    def _leave(self, result: str) -> None:
        """Pop back to the previous screen, or exit if this is the base screen.

        Pushed (NornUIApp): ``dismiss`` pops and the host decides what's next.
        Base screen (NornApp): nothing to pop, so exit the app.
        """
        if len(self.app.screen_stack) > 1:
            self.dismiss(result)
        else:
            self.app.exit()

    async def action_back(self) -> None:
        """Return to the launcher, cancelling any running pipeline first.

        Awaits the run task after cancelling so the run finishes cleanly
        before the screen is dismissed — without this, events would post to
        a dismissed screen and a second run could start against the same repo.
        """
        await self._cancel_and_await()
        self.go_back = True
        self._leave("back")

    async def action_quit_app(self) -> None:
        """Quit, cancelling any running pipeline first."""
        await self._cancel_and_await()
        self._leave("quit")

    async def _cancel_and_await(self) -> None:
        """Cancel the active run and wait for its task to finish."""
        self._cancel_active_run()
        if self._run_task is not None and not self._run_task.done():
            try:
                await self._run_task
            except (Exception, asyncio.CancelledError):  # noqa: BLE001
                pass  # _drive_run handles all errors internally


class NornApp(App):
    """Thin app hosting a single :class:`RunScreen` for a pre-built pipeline.

    Preserves the original ``NornApp`` surface (``apply_event``,
    ``run_finished``, ``run_success``, ``go_back``, ``_run_task``) by
    delegating to the hosted screen, so direct-object callers and tests keep
    working while the run UI lives in a reusable screen.
    """

    def __init__(
        self,
        pipeline: str | None = None,
        *,
        graph: Any | None = None,
        vm: RunViewModel | None = None,
        budget: Any | None = None,
        pipeline_obj: Any | None = None,
        run_kwargs: dict[str, Any] | None = None,
        capabilities: Any | None = None,
    ) -> None:
        super().__init__()
        self._run_screen = RunScreen(
            pipeline=pipeline,
            graph=graph,
            vm=vm,
            budget=budget,
            pipeline_obj=pipeline_obj,
            run_kwargs=run_kwargs,
            capabilities=capabilities,
        )

    def get_default_screen(self) -> RunScreen:
        """Install the run screen as the app's base screen.

        This keeps ``app.query_one(...)`` and key bindings resolving to the run
        widgets (they live on the active screen) without an extra push.
        """
        return self._run_screen

    # Delegating accessors so the existing NornApp API keeps working.
    def apply_event(self, event: object) -> None:
        self._run_screen.apply_event(event)

    def _is_action_enabled(self, action: str) -> bool:
        return self._run_screen._is_action_enabled(action)

    def check_action(self, action: str, parameters: tuple[Any, ...]) -> bool | None:
        return self._run_screen.check_action(action, parameters)

    @property
    def _vm(self) -> RunViewModel:
        return self._run_screen._vm

    @property
    def run_finished(self) -> bool:
        return self._run_screen.run_finished

    @run_finished.setter
    def run_finished(self, value: bool) -> None:
        self._run_screen.run_finished = value

    @property
    def run_success(self) -> bool | None:
        return self._run_screen.run_success

    @property
    def go_back(self) -> bool:
        return self._run_screen.go_back

    @property
    def _run_task(self) -> asyncio.Task[Any] | None:
        return self._run_screen._run_task

    @property
    def _controller(self) -> Any | None:
        return self._run_screen._controller


class NornUIApp(App):
    """Unified launcher app: Launcher → Args → Run within one ``App.run()``.

    With *initial_pipeline* set it skips the launcher and runs that pipeline
    directly (``norn ui <pipeline>``); Back then exits. Otherwise it opens the
    launcher and Back returns to it — all without tearing down the terminal
    between screens, so transitions are seamless.
    """

    TITLE = "Norn"

    def __init__(
        self,
        *,
        bundled: list[Any] | None = None,
        discovered: list[Any] | None = None,
        initial_pipeline: str | None = None,
    ) -> None:
        super().__init__()
        self._bundled = bundled or []
        self._discovered = discovered or []
        self._initial_pipeline = initial_pipeline

    def on_mount(self) -> None:
        if self._initial_pipeline is not None:
            is_bundled = get_pipeline_info(self._initial_pipeline) is not None
            self._begin_pipeline(
                self._initial_pipeline,
                is_bundled=is_bundled,
                display_name=self._initial_pipeline,
                from_launcher=False,
            )
        else:
            self._show_launcher()

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _show_launcher(self) -> None:
        self.push_screen(
            LauncherScreen(self._bundled, self._discovered), self._on_launcher_result
        )

    def _pick_file(self) -> str | None:
        """Pick a pipeline file via fzf (suspends the app for the terminal).

        No fallback: a missing ``fzf`` (or a suspend failure) propagates and
        fails loudly. Returns ``None`` only when the user cancelled fzf.
        """
        with self.suspend():
            return run_fzf(".", dirs_only=False)

    def _on_launcher_result(self, result: Any) -> None:
        if result is None:
            self.exit()
            return
        if result is OPEN_FILE:
            path = self._pick_file()
            if not path:
                self._show_launcher()
                return
            self._begin_pipeline(
                path, is_bundled=False, display_name=path, from_launcher=True
            )
            return
        if isinstance(result, HistoryRequest):
            self._show_history(result.info)
            return
        if isinstance(result, LaunchRequest):
            ref, is_bundled = resolve_ref(result.info)
            self._begin_pipeline(
                ref,
                is_bundled=is_bundled,
                display_name=result.info.name,
                from_launcher=True,
                args_meta=dict(result.info.args),
                use_worktree=result.use_worktree,
            )
            return
        # Backward compat: bare PipelineInfo (shouldn't happen in practice)
        ref, is_bundled = resolve_ref(result)
        self._begin_pipeline(
            ref,
            is_bundled=is_bundled,
            display_name=result.name,
            from_launcher=True,
            args_meta=dict(result.args),
        )

    def _begin_pipeline(
        self,
        ref: str,
        *,
        is_bundled: bool,
        display_name: str,
        from_launcher: bool,
        args_meta: dict[str, str] | None = None,
        use_worktree: bool = False,
    ) -> None:
        if args_meta is None:
            args_meta = pipeline_args_meta(ref)
        if args_meta:
            self.push_screen(
                ArgsPromptScreen(display_name, args_meta),
                lambda params: self._on_args(
                    ref, is_bundled, display_name, from_launcher, params,
                    use_worktree=use_worktree,
                ),
            )
        else:
            self._start_run(ref, is_bundled, display_name, from_launcher, {},
                            use_worktree=use_worktree)

    def _on_args(
        self,
        ref: str,
        is_bundled: bool,
        display_name: str,
        from_launcher: bool,
        params: dict[str, str] | None,
        *,
        use_worktree: bool = False,
    ) -> None:
        if params is None:  # cancelled the args prompt
            self._return_or_exit(from_launcher)
            return
        self._start_run(ref, is_bundled, display_name, from_launcher, params,
                        use_worktree=use_worktree)

    def _start_run(
        self,
        ref: str,
        is_bundled: bool,
        display_name: str,
        from_launcher: bool,
        params: dict[str, str],
        *,
        resume: bool = False,
        use_worktree: bool = False,
    ) -> None:
        # Worktree + resume/history is disabled in v1.
        if use_worktree and resume:
            self.notify(
                "Worktree mode cannot be combined with resume/history.",
                severity="error",
            )
            self._return_or_exit(from_launcher)
            return

        try:
            pipeline_obj = load_pipeline_with_args(ref, is_bundled=is_bundled, params=params)
        except (FileNotFoundError, ValueError) as exc:
            self.notify(f"Could not load pipeline: {exc}", severity="error")
            self._return_or_exit(from_launcher)
            return
        graph, budget, run_kwargs = build_run_setup(pipeline_obj, params, ref=ref)
        if resume:
            checkpoint = load_run_checkpoint(ref)
            if checkpoint is not None:
                run_kwargs["resume_checkpoint"] = checkpoint
            # config_path already set by build_run_setup for all runs

        # Generate a single run id for both branch name and run_pipeline.
        import uuid  # noqa: PLC0415

        run_id = uuid.uuid4().hex[:8] if use_worktree else None

        screen = RunScreen(
            pipeline=display_name,
            graph=graph,
            vm=RunViewModel(),
            budget=budget,
            pipeline_obj=pipeline_obj,
            run_kwargs=run_kwargs,
            use_worktree=use_worktree,
            run_id=run_id,
        )
        self.push_screen(screen, lambda result: self._on_run_result(result, from_launcher))

    # ------------------------------------------------------------------
    # History browser
    # ------------------------------------------------------------------

    def _show_history(self, info: Any) -> None:
        """Push the history browser for *info*'s pipeline."""
        ref, is_bundled = resolve_ref(info)
        records = load_run_history(ref)
        self.push_screen(
            HistoryBrowserScreen(records, history_config_key(ref)),
            lambda result: self._on_history_result(result, info, ref, is_bundled),
        )

    def _on_history_result(
        self, result: Any, info: Any, ref: str, is_bundled: bool
    ) -> None:
        # HistoryBrowserScreen dismisses with ("resume", config_path) or None.
        if isinstance(result, tuple) and result and result[0] == "resume":
            self._start_run(ref, is_bundled, info.name, True, {}, resume=True)
            return
        self._show_launcher()

    def _on_run_result(self, result: Any, from_launcher: bool) -> None:
        if result == "back":
            self._return_or_exit(from_launcher)
        else:  # "quit"
            self.exit()

    def _return_or_exit(self, from_launcher: bool) -> None:
        if from_launcher:
            self._show_launcher()
        else:
            self.exit()

"""Run lifecycle controller — pause, cancel, answer.

The :class:`RunController` is the single coordination point between the UI
(or tests) and the runner's item loop.  It exposes:

* **Pause/resume**: an :class:`asyncio.Event` that the runner checks between
  stages.  When cleared, ``await controller.wait_if_paused()`` blocks until
  the UI calls :meth:`resume`.
* **Cancel**: sets a flag and cancels the active stage task (if any).  The
  runner checks :meth:`is_cancelled` before starting the next item.
* **Answer input**: resolves a pending ``WaitingInput`` via an
  :class:`asyncio.Future` that the TUI responder awaits.

This module is pure-asyncio, no Textual dependency, no SDK dependency.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)


class CancelledError(Exception):
    """Raised inside the runner when the run has been cancelled by the user.

    Distinct from ``PipelineError`` (which wraps stage failures) — this signals
    a deliberate user-initiated abort.

    Attributes:
        stage_name: Name of the stage that was active when cancelled, or ``None``.
    """

    def __init__(self, stage_name: str | None = None) -> None:
        self.stage_name = stage_name
        super().__init__(f"Run cancelled{f' during {stage_name!r}' if stage_name else ''}")


class RunController:
    """Cooperative run lifecycle controller.

    Create one per ``run_pipeline`` call and pass it as ``run_controller``
    on ``PipelineContext``.  The runner calls :meth:`wait_if_paused` and
    :meth:`check_cancelled` between stages; the UI calls :meth:`pause`,
    :meth:`resume`, :meth:`cancel`.

    Thread-safe: the UI may call methods from any thread; the runner awaits
    from the event loop.
    """

    def __init__(self) -> None:
        # When *set*, the run is NOT paused (stages may proceed).
        # When *cleared*, the runner blocks on ``wait_if_paused()``.
        self._resume_event = asyncio.Event()
        self._resume_event.set()  # starts unpaused

        self._cancelled = False
        self._paused = False
        self._active_task: asyncio.Task[Any] | None = None
        self._active_stage_name: str | None = None

        # Future for the next WaitingInput answer (created on demand).
        self._answer_future: asyncio.Future[str] | None = None

    # ------------------------------------------------------------------
    # State queries
    # ------------------------------------------------------------------

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    # ------------------------------------------------------------------
    # Pause / resume (UI → runner)
    # ------------------------------------------------------------------

    def pause(self) -> None:
        """Request a pause. Takes effect before the next stage starts."""
        if not self._cancelled:
            self._paused = True
            self._resume_event.clear()
            log.debug("[run_control] Pause requested")

    def resume(self) -> None:
        """Resume a paused run."""
        self._paused = False
        self._resume_event.set()
        log.debug("[run_control] Resume requested")

    async def wait_if_paused(self) -> None:
        """Called by the runner between stages. Blocks while paused.

        Raises :class:`CancelledError` if cancellation is detected while
        waiting (or was already set).
        """
        if self._cancelled:
            raise CancelledError(self._active_stage_name)
        await self._resume_event.wait()
        if self._cancelled:
            raise CancelledError(self._active_stage_name)

    # ------------------------------------------------------------------
    # Cancel (UI → runner)
    # ------------------------------------------------------------------

    def cancel(self) -> None:
        """Cancel the run. Cancels the active stage task if one exists."""
        self._cancelled = True
        # Unblock any pause wait so the CancelledError propagates.
        self._resume_event.set()
        if self._active_task is not None and not self._active_task.done():
            self._active_task.cancel()
            log.debug("[run_control] Cancelled active task for stage %s", self._active_stage_name)
        log.debug("[run_control] Cancel requested")

    def set_active_task(self, task: asyncio.Task[Any] | None, stage_name: str | None = None) -> None:
        """Called by the runner to register the currently active stage task."""
        self._active_task = task
        self._active_stage_name = stage_name

    def check_cancelled(self) -> None:
        """Synchronous check — raises :class:`CancelledError` if cancelled."""
        if self._cancelled:
            raise CancelledError(self._active_stage_name)

    # ------------------------------------------------------------------
    # Answer input (UI → responder future)
    # ------------------------------------------------------------------

    def create_answer_future(self) -> asyncio.Future[str]:
        """Create and return a future for the next ``WaitingInput`` answer.

        The TUI responder awaits this future; the UI resolves it via
        :meth:`answer_input`.
        """
        loop = asyncio.get_running_loop()
        self._answer_future = loop.create_future()
        return self._answer_future

    def answer_input(self, response: str) -> None:
        """Resolve the pending WaitingInput with *response*.

        No-op if no future is pending (e.g. the prompt was already resolved).
        """
        if self._answer_future is not None and not self._answer_future.done():
            self._answer_future.set_result(response)
            log.debug("[run_control] Answered input: %r", response)
        self._answer_future = None

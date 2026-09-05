"""Input responder abstraction for blocking prompts during pipeline execution.

The default :class:`CLIResponder` delegates to the existing :mod:`norn.ui`
blocking-stdin functions so that headless ``norn run`` behaves exactly as
before.  The TUI will plug in a non-blocking modal responder so the event
loop is never stalled waiting on stdin.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from norn.dsl import Budget, Stage
    from norn.models import PipelineContext, UsageTracker


class TUIResponder:
    """Non-blocking responder that awaits answers from the UI via ``RunController``.

    The TUI creates one of these and sets it as ``ctx.input_responder``.
    When the runner calls ``ask_budget``/``ask_failure``/``ask_step``, this
    creates an :class:`asyncio.Future` on the ``RunController`` and awaits
    it.  The UI resolves the future via ``controller.answer_input(choice)``.
    """

    def __init__(self, controller: object) -> None:
        self._controller = controller

    async def ask_budget(self, tracker: "UsageTracker", budget: "Budget") -> str:
        from norn.run_control import RunController  # noqa: PLC0415

        ctrl: RunController = self._controller  # type: ignore[assignment]
        future = ctrl.create_answer_future()
        return await future

    async def ask_failure(self, name: str, error: str | None) -> str:
        from norn.run_control import RunController  # noqa: PLC0415

        ctrl: RunController = self._controller  # type: ignore[assignment]
        future = ctrl.create_answer_future()
        return await future

    async def ask_step(
        self,
        stage: "Stage",
        ctx: "PipelineContext",
        *,
        session_id: str | None = None,
    ) -> str:
        from norn.run_control import RunController  # noqa: PLC0415

        ctrl: RunController = self._controller  # type: ignore[assignment]
        future = ctrl.create_answer_future()
        return await future


class InputResponder:
    """Abstract base for resolving blocking prompts during a pipeline run.

    Subclass this to provide custom prompt handling (e.g. a TUI modal).
    All methods are async so implementations can properly await UI events.
    """

    async def ask_budget(self, tracker: UsageTracker, budget: Budget) -> str:
        """Prompt for a budget-exceeded decision.

        Returns ``'c'`` (continue) or ``'a'`` (abort).
        """
        raise NotImplementedError

    async def ask_failure(self, name: str, error: str | None) -> str:
        """Prompt for a stage-failure or loop-exhaustion recovery decision.

        Returns ``'r'`` (retry the failed stage/loop), ``'c'`` (continue past
        the failure) or ``'a'`` (abort the run).
        """
        raise NotImplementedError

    async def ask_step(
        self,
        stage: Stage,
        ctx: PipelineContext,
        *,
        session_id: str | None = None,
    ) -> str:
        """Prompt for the next action before running a stage in step mode.

        Returns ``'r'`` (run), ``'s'`` (skip), or ``'a'`` (abort).
        """
        raise NotImplementedError


class CLIResponder(InputResponder):
    """Default responder: delegates to the existing :mod:`norn.ui` prompts.

    Importing :mod:`norn.ui` is deferred to call time so this module stays
    importable without pulling in Rich at module-load.
    """

    async def ask_budget(self, tracker: UsageTracker, budget: Budget) -> str:
        from norn import ui  # noqa: PLC0415

        return ui.ask_budget_exceeded(tracker, budget)

    async def ask_failure(self, name: str, error: str | None) -> str:
        from norn import ui  # noqa: PLC0415

        return ui.ask_user_continue(name, error)

    async def ask_step(
        self,
        stage: Stage,
        ctx: PipelineContext,
        *,
        session_id: str | None = None,
    ) -> str:
        from norn import ui  # noqa: PLC0415

        return ui.step_prompt(stage, ctx, session_id=session_id)


class NonInteractiveResponder(InputResponder):
    """Answers every prompt with abort -- for child processes that cannot prompt."""

    async def ask_budget(self, tracker: "UsageTracker", budget: "Budget") -> str:
        log.info("non-interactive: aborting on budget exceeded")
        return "a"

    async def ask_failure(self, name: str, error: str | None) -> str:
        log.info("non-interactive: aborting on failure of stage %r", name)
        return "a"

    async def ask_step(
        self,
        stage: "Stage",
        ctx: "PipelineContext",
        *,
        session_id: str | None = None,
    ) -> str:
        raise RuntimeError("step mode is incompatible with --non-interactive")

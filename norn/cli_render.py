"""Rich CLI renderer — subscribes to run-events and reproduces the classic CLI output.

This module replaces the inline ``ui.print_*`` calls that the runner and
``Generate`` stage previously made directly.  The renderer is wired as the
``on_event`` callback on an :class:`~norn.event_sink.EventSink` so every
event is handled exactly once, after redaction.

The renderer is **stateful**: it tracks whether streaming text is in
progress (to print newlines at the right time) and accumulates usage
counters from ``UsageUpdated`` events for the running-total display.
"""

from __future__ import annotations

import sys
from typing import Any

from norn.events import (
    CallingAgent,
    ClearContextNotice,
    GotReply,
    IncludeDone,
    IncludeStarted,
    LoopAttempt,
    LoopDraftPR,
    LoopExhausted,
    LoopSuccess,
    ParallelDone,
    ParallelStarted,
    RunEvent,
    RunStarted,
    StageFinished,
    StageRetrying,
    StageStarted,
    TurnEvent,
    UsageUpdated,
)


class CLIRenderer:
    """Synchronous event-driven Rich CLI output.

    Parameters
    ----------
    budgets:
        Optional list of :class:`~norn.dsl.Budget` objects for the
        running-total percentage display.
    console:
        Optional Rich ``Console`` override (defaults to ``norn.ui.console``).
    """

    def __init__(
        self,
        *,
        budgets: list[Any] | None = None,
        console: Any | None = None,
    ) -> None:
        if console is None:
            from norn.ui import console as _default

            console = _default
        self._console = console
        self._budgets = budgets

        # Accumulated usage state for running-total display.
        self._total_cost_usd: float = 0.0
        self._total_input_tokens: int = 0
        self._total_output_tokens: int = 0

        # Track whether agent text is being streamed (to emit trailing newline).
        self._streaming: bool = False

    # -- public entry point ---------------------------------------------------

    def __call__(self, event: RunEvent) -> None:
        """Dispatch a single redacted event to the appropriate handler."""
        handler = _DISPATCH.get(type(event))
        if handler is not None:
            handler(self, event)

    # -- per-event handlers ---------------------------------------------------

    def _on_run_started(self, event: RunStarted) -> None:
        self._console.print(f"\n[bold]Pipeline [cyan]{event.pipeline_name}[/cyan] starting[/bold]")
        if event.resume_session:
            self._console.print(f"  [dim]Resuming session {event.resume_session}[/dim]")

    def _on_stage_started(self, event: StageStarted) -> None:
        self._console.print(f"  [bold]\u23f3 {event.name}[/bold]", end="", highlight=False)

    def _on_calling_agent(self, event: CallingAgent) -> None:
        model_label = f" ({event.model})" if event.model else ""
        self._console.print(
            f"\n    [yellow]\u2192 calling agent ({event.provider}){model_label}\u2026[/yellow] "
            f"[dim]({event.stage_name})[/dim]",
            highlight=False,
        )

    def _on_turn_event(self, event: TurnEvent) -> None:
        if event.event.text is not None:
            self._streaming = True
            print(event.event.text, end="", flush=True)

    def _on_got_reply(self, event: GotReply) -> None:
        if self._streaming:
            print()  # end the streamed output line
            self._streaming = False
        self._console.print(
            f"    [cyan]\u2190 got reply[/cyan] [dim]({event.stage_name}, {event.elapsed_s:.1f}s)[/dim]",
            highlight=False,
        )

    def _on_usage_updated(self, event: UsageUpdated) -> None:
        self._total_cost_usd = event.total_cost_usd
        self._total_input_tokens = event.input_tokens
        self._total_output_tokens = event.output_tokens

    def _on_stage_finished(self, event: StageFinished) -> None:
        status = event.status
        elapsed = event.duration_ms / 1000.0

        if status == "passed":
            parts = [f"\r  [bold green]\u2713[/bold green] [green]{event.name:<20}[/green]"]
            if event.usage_cost_usd or event.usage_input_tokens or event.usage_output_tokens:
                cost = f"${event.usage_cost_usd:.2f}"
                tokens_in = f"{event.usage_input_tokens / 1000:.1f}k"
                tokens_out = f"{event.usage_output_tokens / 1000:.1f}k"
                parts.append(f"[dim]{cost}  ({tokens_in} in / {tokens_out} out)[/dim]")
            parts.append(f"[dim]{elapsed:.1f}s[/dim]")
            self._console.print("  ".join(parts))
            for artifact in event.artifacts:
                self._console.print(f"    [dim green]+ {artifact}[/dim green]")

        elif status == "failed":
            self._console.print(f"\r  [bold red]\u2717[/bold red] [red]{event.name:<20}[/red]  [dim]{elapsed:.1f}s[/dim]")
            if event.error:
                from norn.ui import mask

                lines = mask(event.error).strip().splitlines()
                tail = lines[-5:] if len(lines) > 5 else lines
                for line in tail:
                    self._console.print(f"    [dim red]{line}[/dim red]")

        elif status == "skipped":
            self._console.print(f"  [dim]\u2298 {event.name:<20}  skipped[/dim]")

        elif status == "skipped_condition":
            self._console.print(f"  [dim]\u2298 {event.name:<20}  skipped (condition not met)[/dim]")

        elif status == "cached":
            self._console.print(f"  [dim]\u2298 {event.name:<20}  (cached)[/dim]")

        # Running total after every finished stage that had any usage accumulation.
        self._print_running_total()

    def _on_stage_retrying(self, event: StageRetrying) -> None:
        # StageRetrying is emitted for attempts > 1; the LoopAttempt event
        # handles the display, so nothing extra needed here.
        pass

    def _on_loop_attempt(self, event: LoopAttempt) -> None:
        self._console.print(
            f"\n  [bold yellow]\u21bb[/bold yellow] [yellow]{event.name}[/yellow] "
            f"[dim](attempt {event.attempt}/{event.max_retries})[/dim]"
        )

    def _on_loop_success(self, event: LoopSuccess) -> None:
        self._console.print(f"  [bold green]\u2713[/bold green] [green]{event.name} \u2014 all stages passed[/green]")

    def _on_loop_exhausted(self, event: LoopExhausted) -> None:
        # The runner already printed this info via LoopExhausted event;
        # we render it here based on stored loop_id.
        # Note: loop_id is "loop:<name>" — extract name.
        name = event.loop_id.removeprefix("loop:")
        # We don't know max_retries from the event alone, so just display
        # the exhausted message without the count (the LoopAttempt events
        # already showed each attempt).
        self._console.print(f"\n  [bold red]\u2717[/bold red] [red]{name} \u2014 retries exhausted[/red]")

    def _on_loop_draft_pr(self, event: LoopDraftPR) -> None:
        self._console.print(
            f"  [bold yellow]\u2691[/bold yellow] [yellow]{event.name} "
            f"\u2014 retries exhausted, continuing as draft PR[/yellow]"
        )

    def _on_parallel_started(self, event: ParallelStarted) -> None:
        self._console.print(
            f"\n  [bold blue]\u21f6[/bold blue] [blue]{event.name}[/blue] "
            f"[dim](running {event.stage_count} stages in parallel)[/dim]"
        )

    def _on_parallel_done(self, event: ParallelDone) -> None:
        self._console.print(f"  [bold green]\u2713[/bold green] [green]{event.name} \u2014 all parallel stages passed[/green]")

    def _on_include_started(self, event: IncludeStarted) -> None:
        mode = "isolated" if event.isolated else "inline"
        self._console.print(f"\n  [bold magenta]\u2935[/bold magenta] [magenta]include {event.path}[/magenta] [dim]({mode})[/dim]")

    def _on_include_done(self, event: IncludeDone) -> None:
        self._console.print(f"  [bold green]\u2713[/bold green] [green]include {event.path} \u2014 done[/green]")

    def _on_clear_context(self, event: ClearContextNotice) -> None:
        self._console.print("  [dim]\u2500\u2500 clear_context \u2500\u2500[/dim]")

    # -- helpers --------------------------------------------------------------

    def _print_running_total(self) -> None:
        """Display the running cost/token total, fixing the zero-cost bug."""
        total_tokens = self._total_input_tokens + self._total_output_tokens
        if self._total_cost_usd > 0:
            cost_str = f"${self._total_cost_usd:.2f}"
            budget_str = ""
            if self._budgets:
                for b in self._budgets:
                    if b.max_cost_usd is not None:
                        pct = self._total_cost_usd / b.max_cost_usd * 100
                        budget_str = f" / ${b.max_cost_usd:.2f} ({pct:.0f}%)"
                        break
            self._console.print(f"  [dim]\u2500\u2500\u2500 Running total: {cost_str}{budget_str} \u2500\u2500\u2500[/dim]")
        elif total_tokens > 0:
            # Zero-cost run (e.g. token-only tracking) — show tokens instead
            # of suppressing the line entirely.
            token_str = f"{total_tokens:,} tokens"
            budget_str = ""
            if self._budgets:
                for b in self._budgets:
                    if b.max_tokens is not None:
                        pct = total_tokens / b.max_tokens * 100
                        budget_str = f" / {b.max_tokens:,} ({pct:.0f}%)"
                        break
            self._console.print(f"  [dim]\u2500\u2500\u2500 Running total: {token_str}{budget_str} \u2500\u2500\u2500[/dim]")


# -- dispatch table -----------------------------------------------------------

_DISPATCH: dict[type, Any] = {
    RunStarted: CLIRenderer._on_run_started,
    StageStarted: CLIRenderer._on_stage_started,
    CallingAgent: CLIRenderer._on_calling_agent,
    TurnEvent: CLIRenderer._on_turn_event,
    GotReply: CLIRenderer._on_got_reply,
    UsageUpdated: CLIRenderer._on_usage_updated,
    StageFinished: CLIRenderer._on_stage_finished,
    StageRetrying: CLIRenderer._on_stage_retrying,
    LoopAttempt: CLIRenderer._on_loop_attempt,
    LoopSuccess: CLIRenderer._on_loop_success,
    LoopExhausted: CLIRenderer._on_loop_exhausted,
    LoopDraftPR: CLIRenderer._on_loop_draft_pr,
    ParallelStarted: CLIRenderer._on_parallel_started,
    ParallelDone: CLIRenderer._on_parallel_done,
    IncludeStarted: CLIRenderer._on_include_started,
    IncludeDone: CLIRenderer._on_include_done,
    ClearContextNotice: CLIRenderer._on_clear_context,
}

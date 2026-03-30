from __future__ import annotations

import time
from typing import TYPE_CHECKING

from rich.console import Console
from rich.table import Table

from norn.dsl import Budget, ClearContext, Include, Loop, Parallel, Pipeline, Stage
from norn.models import PipelineContext, StageResult, UsageTracker

if TYPE_CHECKING:
    from collections.abc import Iterable

    from norn.history import RunRecord

console = Console()

# Secret values registered here are redacted from all UI output.
_masked_secrets: set[str] = set()


def register_secrets(values: Iterable[str]) -> None:
    """Register secret values to be redacted in all UI output."""
    _masked_secrets.update(v for v in values if v)


def mask(text: str) -> str:
    """Replace any registered secret values in *text* with ``***``."""
    for secret in _masked_secrets:
        if secret in text:
            text = text.replace(secret, "***")
    return text


def stage_type_label(stage: Stage) -> str:
    """Return a short label for the stage implementation type."""
    cls = type(stage.impl).__name__
    if hasattr(stage.impl, "cmd"):
        label = f"{cls}: {stage.impl.cmd}"
    elif hasattr(stage.impl, "path"):
        label = f"{cls}: {stage.impl.path}"
    else:
        label = cls
    if hasattr(stage.impl, "skills") and stage.impl.skills:
        skill_names = [s if isinstance(s, str) else s.name for s in stage.impl.skills]
        label = f"{label} + skills: {', '.join(skill_names)}"
    return label


def print_dry_run(pipeline: Pipeline) -> None:
    """Print the pipeline structure without executing."""
    console.print(f"\n[bold]Pipeline:[/bold] {pipeline.name}\n")
    if pipeline.pipeline_skills:
        skill_names = [s if isinstance(s, str) else s.name for s in pipeline.pipeline_skills]
        console.print(f"  [dim]Pipeline skills: {', '.join(skill_names)}[/dim]")
    for item in pipeline.items:
        if isinstance(item, ClearContext):
            console.print("  [dim]── clear_context ──[/dim]")
        elif isinstance(item, Stage):
            console.print(f"  [cyan]{item.name:<20}[/cyan] [dim]\\[{stage_type_label(item)}][/dim]")
        elif isinstance(item, Loop):
            console.print(f"  [bold yellow]Loop:[/bold yellow] {item.name} [dim](max {item.max_retries} retries)[/dim]")
            for i, stage in enumerate(item.stages, 1):
                console.print(f"    {i}. [cyan]{stage.name:<18}[/cyan] [dim]\\[{stage_type_label(stage)}][/dim]")
        elif isinstance(item, Parallel):
            console.print(f"  [bold blue]Parallel:[/bold blue] {item.name}")
            for i, stage in enumerate(item.stages, 1):
                console.print(f"    {i}. [cyan]{stage.name:<18}[/cyan] [dim]\\[{stage_type_label(stage)}][/dim]")
        elif isinstance(item, Include):
            mode = "isolated" if item.isolated else "inline"
            console.print(f"  [bold magenta]Include:[/bold magenta] {item.path} [dim]({mode})[/dim]")
    console.print()


def print_pipeline_start(name: str, resume_session: str | None = None) -> None:
    console.print(f"\n[bold]Pipeline [cyan]{name}[/cyan] starting[/bold]")
    if resume_session:
        console.print(f"  [dim]Resuming session {resume_session}[/dim]")


def print_stage_running(name: str) -> float:
    """Print stage-running indicator. Returns start time."""
    console.print(f"  [bold]⏳ {name}[/bold]", end="", highlight=False)
    return time.monotonic()


def print_stage_success(name: str, elapsed: float, result: StageResult) -> None:
    parts = [f"\r  [bold green]✓[/bold green] [green]{name:<20}[/green]"]
    if result.usage:
        cost = f"${result.usage.total_cost_usd:.2f}"
        tokens_in = f"{result.usage.input_tokens / 1000:.1f}k"
        tokens_out = f"{result.usage.output_tokens / 1000:.1f}k"
        parts.append(f"[dim]{cost}  ({tokens_in} in / {tokens_out} out)[/dim]")
    parts.append(f"[dim]{elapsed:.1f}s[/dim]")
    console.print("  ".join(parts))
    for artifact in result.artifacts:
        console.print(f"    [dim green]+ {artifact}[/dim green]")


def print_stage_failure(name: str, elapsed: float, result: StageResult) -> None:
    console.print(f"\r  [bold red]✗[/bold red] [red]{name:<20}[/red]  [dim]{elapsed:.1f}s[/dim]")
    if result.error:
        # Show last few lines of error for context (with secrets redacted)
        lines = mask(result.error).strip().splitlines()
        tail = lines[-5:] if len(lines) > 5 else lines
        for line in tail:
            console.print(f"    [dim red]{line}[/dim red]")


def print_stage_skipped(name: str) -> None:
    console.print(f"  [dim]⊘ {name:<20}  skipped[/dim]")


def print_stage_skipped_condition(name: str) -> None:
    console.print(f"  [dim]⊘ {name:<20}  skipped (condition not met)[/dim]")


def print_stage_cached(name: str) -> None:
    console.print(f"  [dim]⊘ {name:<20}  (cached)[/dim]")


def print_loop_attempt(loop_name: str, attempt: int, max_retries: int) -> None:
    console.print(f"\n  [bold yellow]↻[/bold yellow] [yellow]{loop_name}[/yellow] [dim](attempt {attempt}/{max_retries})[/dim]")


def print_loop_success(loop_name: str) -> None:
    console.print(f"  [bold green]✓[/bold green] [green]{loop_name} — all stages passed[/green]")


def print_loop_exhausted(loop_name: str, max_retries: int) -> None:
    console.print(f"\n  [bold red]✗[/bold red] [red]{loop_name} — retries exhausted ({max_retries}/{max_retries})[/red]")


def print_loop_draft_pr(loop_name: str) -> None:
    console.print(f"  [bold yellow]⚑[/bold yellow] [yellow]{loop_name} — retries exhausted, continuing as draft PR[/yellow]")


def print_parallel_start(name: str, stage_count: int) -> None:
    console.print(f"\n  [bold blue]⇶[/bold blue] [blue]{name}[/blue] [dim](running {stage_count} stages in parallel)[/dim]")


def print_parallel_done(name: str) -> None:
    console.print(f"  [bold green]✓[/bold green] [green]{name} — all parallel stages passed[/green]")


def print_include_start(path: str, *, isolated: bool) -> None:
    mode = "isolated" if isolated else "inline"
    console.print(f"\n  [bold magenta]⤵[/bold magenta] [magenta]include {path}[/magenta] [dim]({mode})[/dim]")


def print_include_done(path: str) -> None:
    console.print(f"  [bold green]✓[/bold green] [green]include {path} — done[/green]")


def print_clear_context() -> None:
    console.print("  [dim]── clear_context ──[/dim]")


def print_running_total(tracker: UsageTracker, budgets: list[Budget] | None = None) -> None:
    if tracker.total_cost_usd > 0:
        cost_str = f"${tracker.total_cost_usd:.2f}"
        budget_str = ""
        if budgets:
            for b in budgets:
                if b.max_cost_usd is not None:
                    pct = tracker.total_cost_usd / b.max_cost_usd * 100
                    budget_str = f" / ${b.max_cost_usd:.2f} ({pct:.0f}%)"
                    break
        console.print(f"  [dim]─── Running total: {cost_str}{budget_str} ───[/dim]")


def ask_budget_exceeded(tracker: UsageTracker, budget: Budget) -> str:
    """Interactive budget-exceeded prompt. Returns 'c' (continue) or 'a' (abort)."""
    if budget.max_cost_usd is not None:
        msg = f"${tracker.total_cost_usd:.4f} exceeds limit ${budget.max_cost_usd:.2f}"
    else:
        msg = f"{tracker.total_tokens:,} tokens exceeds limit {budget.max_tokens:,}"
    console.print(f"\n  [bold yellow]⚠[/bold yellow] [yellow]Budget exceeded: {msg}[/yellow]")
    console.print()
    console.print("  [bold]\\[c]ontinue  \\[a]bort[/bold]")
    while True:
        choice = console.input("  > ").strip().lower()
        if choice in ("c", "a"):
            return choice
        console.print("  [dim]Please enter 'c' or 'a'[/dim]")


def ask_user_continue(name: str, error: str | None) -> str:
    """Interactive failure recovery. Returns 'r', 's', 'a', or 'v'."""
    console.print(f"\n  [bold red]✗[/bold red] [red]{name} failed[/red]")
    if error:
        lines = mask(error).strip().splitlines()
        tail = lines[-8:] if len(lines) > 8 else lines
        for line in tail:
            console.print(f"    [dim]{line}[/dim]")
    console.print()
    console.print("  [bold]\\[c]ontinue  \\[a]bort[/bold]")
    while True:
        choice = console.input("  > ").strip().lower()
        if choice in ("c", "a"):
            return choice
        console.print("  [dim]Please enter 'c' or 'a'[/dim]")


def ask_yes_no(question: str) -> bool:
    """Prompt user for a yes/no answer. Returns True if yes."""
    response = console.input(f"[bold]{question} [y/N]: [/bold]").strip().lower()
    return response in ("y", "yes")


def _print_stage_inspect(stage: Stage, ctx: PipelineContext, session_id: str | None) -> None:
    """Print inspection details for a stage before running it."""
    impl = stage.impl
    if hasattr(impl, "_resolve_prompt"):
        resolved = impl._resolve_prompt(ctx)
        preview = resolved[:200] + ("..." if len(resolved) > 200 else "")
        console.print(f'  [dim]> Prompt (resolved): "{preview}"[/dim]')
        max_turns = getattr(impl, "max_turns", None)
        console.print(f"  [dim]> Max turns: {max_turns if max_turns is not None else 'unlimited'}[/dim]")
        console.print(f"  [dim]> Session: {session_id or 'None (new)'}[/dim]")
    elif hasattr(impl, "cmd"):
        console.print(f"  [dim]> Command: {impl.cmd}[/dim]")


def step_prompt(stage: Stage, ctx: PipelineContext, *, session_id: str | None = None) -> str:
    """Prompt the user before running a stage. Returns 'r' (run), 's' (skip), or 'a' (abort)."""
    console.print(f"\n  [bold]Next:[/bold] [cyan]{stage.name}[/cyan] [dim]\\[{stage_type_label(stage)}][/dim]")
    while True:
        console.print("  [bold]\\[r]un  \\[s]kip  \\[a]bort  \\[i]nspect?[/bold] ", end="")
        choice = console.input("").strip().lower() or "r"
        if choice == "i":
            _print_stage_inspect(stage, ctx, session_id)
            while True:
                console.print("  [bold]\\[r]un  \\[s]kip  \\[a]bort?[/bold] ", end="")
                inner = console.input("").strip().lower() or "r"
                if inner in ("r", "s", "a"):
                    return inner
                console.print("  [dim]Please enter 'r', 's', or 'a'[/dim]")
        elif choice in ("r", "s", "a"):
            return choice
        else:
            console.print("  [dim]Please enter 'r', 's', 'a', or 'i'[/dim]")


def print_usage_report(name: str, tracker: UsageTracker, config_path: str | None = None) -> None:
    if not tracker.records:
        return

    console.print()
    table = Table(title=f'Pipeline "{name}" — Usage Report', title_style="bold", show_edge=False)
    table.add_column("Stage", style="cyan")
    table.add_column("Input", justify="right")
    table.add_column("Output", justify="right")
    table.add_column("Cost", justify="right")
    table.add_column("Time", justify="right")

    for rec in tracker.records:
        model_suffix = f" ({rec.model})" if rec.model else ""
        label = f"{rec.stage_name} (att {rec.attempt}){model_suffix}" if rec.attempt > 1 else f"{rec.stage_name}{model_suffix}"
        table.add_row(
            label,
            f"{rec.input_tokens:,}",
            f"{rec.output_tokens:,}",
            f"${rec.total_cost_usd:.4f}",
            f"{rec.duration_api_ms / 1000:.1f}s",
        )

    console.print(table)

    cache_pct = (
        tracker.total_cache_read_tokens / tracker.total_input_tokens * 100
        if tracker.total_input_tokens > 0
        else 0
    )

    console.print()
    console.print(f"  [bold]Totals[/bold]")
    console.print(f"  Input tokens:     {tracker.total_input_tokens:>10,}   [dim](cache read: {tracker.total_cache_read_tokens:,} / {cache_pct:.0f}%)[/dim]")
    console.print(f"  Output tokens:    {tracker.total_output_tokens:>10,}")
    console.print(f"  Total cost:         [bold]${tracker.total_cost_usd:>9.4f}[/bold]")
    console.print(f"  API time:         {tracker.total_api_duration_ms / 1000:>10.1f}s")
    console.print(f"  Wall time:        {tracker.total_duration_ms / 1000:>10.1f}s")
    console.print(f"  Sessions:         {tracker.unique_sessions:>10}")
    console.print(f"  Turns:            {tracker.total_turns:>10}")

    # Actionable info
    session_id = tracker.last_session_id
    if session_id:
        console.print(f"\n  [dim]Session: {session_id}[/dim]")
    if config_path:
        console.print(f"  [dim]To resume: norn run {config_path} --resume[/dim]")
    console.print()


def print_history_table(records: list[RunRecord]) -> None:
    """Print a table of pipeline run history."""
    if not records:
        console.print("[dim]No history found.[/dim]")
        return

    table = Table(show_edge=False)
    table.add_column("Run", justify="right")
    table.add_column("Timestamp")
    table.add_column("Status")
    table.add_column("Cost", justify="right")
    table.add_column("Time", justify="right")
    table.add_column("Info")

    for r in records:
        status = "[green]✓ Complete[/green]" if r.success else "[red]✗ Failed[/red]"
        cost = f"${r.total_cost_usd:.2f}" if r.total_cost_usd else "-"
        duration = f"{r.duration_ms / 1000:.1f}s"
        info = f"{len(r.stages)} stages" if r.success else f"stage: {r.failed_stage or '?'}"
        ts = r.timestamp[:16].replace("T", " ")
        table.add_row(f"#{r.run_id}", ts, status, cost, duration, info)

    console.print(table)


def print_history_comparison(records: list[RunRecord], run_a: int, run_b: int) -> None:
    """Print a side-by-side comparison of two runs."""
    by_id = {r.run_id: r for r in records}
    if run_a not in by_id:
        console.print(f"[red]Run #{run_a} not found in history.[/red]")
        return
    if run_b not in by_id:
        console.print(f"[red]Run #{run_b} not found in history.[/red]")
        return

    a, b = by_id[run_a], by_id[run_b]

    def _pct(old: float, new: float) -> str:
        if old == 0:
            return ""
        diff = (new - old) / old * 100
        sign = "+" if diff > 0 else ""
        color = "red" if diff > 0 else "green"
        return f"  [{color}]({sign}{diff:.0f}%)[/{color}]"

    console.print(f"\n  [bold]Run #{run_a} vs Run #{run_b}:[/bold]")
    console.print(
        f"  Cost:    ${a.total_cost_usd:.2f} → ${b.total_cost_usd:.2f}"
        + _pct(a.total_cost_usd, b.total_cost_usd)
    )
    console.print(
        f"  Tokens:  {a.total_tokens / 1000:.1f}k → {b.total_tokens / 1000:.1f}k"
        + _pct(float(a.total_tokens), float(b.total_tokens))
    )
    console.print(f"  Retries: {a.retries} → {b.retries}")
    console.print()

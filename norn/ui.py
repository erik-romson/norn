from __future__ import annotations

from typing import TYPE_CHECKING

from rich.console import Console
from rich.table import Table

from norn.dsl import Budget, ClearContext, Include, Loop, Parallel, Pipeline, Stage
from norn.models import PipelineContext, UsageTracker

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
    """Interactive failure recovery.

    Returns 'r' (retry the failed stage/loop), 'c' (continue past the
    failure) or 'a' (abort the run).
    """
    console.print(f"\n  [bold red]✗[/bold red] [red]{name} failed[/red]")
    if error:
        lines = mask(error).strip().splitlines()
        tail = lines[-8:] if len(lines) > 8 else lines
        for line in tail:
            console.print(f"    [dim]{line}[/dim]")
    console.print()
    console.print("  [bold]\\[r]etry  \\[c]ontinue  \\[a]bort[/bold]")
    while True:
        choice = console.input("  > ").strip().lower()
        if choice in ("r", "c", "a"):
            return choice
        console.print("  [dim]Please enter 'r', 'c', or 'a'[/dim]")


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
    table.add_column("Turns", justify="right")
    table.add_column("Tools (x)", justify="right")
    table.add_column("Cost", justify="right")
    table.add_column("Time", justify="right")

    for rec in tracker.records:
        model_suffix = f" ({rec.model})" if rec.model else ""
        label = f"{rec.stage_name} (att {rec.attempt}){model_suffix}" if rec.attempt > 1 else f"{rec.stage_name}{model_suffix}"
        tools_str = f"{rec.tool_uses} ({rec.tool_errors}x)" if rec.tool_errors else str(rec.tool_uses)
        table.add_row(
            label,
            f"{rec.input_tokens:,}",
            f"{rec.output_tokens:,}",
            str(rec.num_turns),
            tools_str,
            f"${rec.total_cost_usd:.4f}",
            f"{rec.duration_api_ms / 1000:.1f}s",
        )

    console.print(table)

    cache_pct = (
        tracker.total_cache_read_tokens / tracker.total_input_tokens * 100
        if tracker.total_input_tokens > 0
        else 0
    )

    billed_pct = (
        tracker.total_billed_input_tokens / tracker.total_input_tokens * 100
        if tracker.total_input_tokens > 0
        else 0
    )

    console.print()
    console.print(f"  [bold]Totals[/bold]")
    console.print(f"  Input tokens:     {tracker.total_input_tokens:>10,}   [dim](cache read: {tracker.total_cache_read_tokens:,} / {cache_pct:.0f}%)[/dim]")
    console.print(f"  Billed input:     {tracker.total_billed_input_tokens:>10,}   [dim](cache read: {tracker.total_cache_read_tokens:,} / {billed_pct:.0f}%)[/dim]")
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


def print_history_table(records: list[RunRecord], *, state_key: str | None = None) -> None:
    """Print a table of pipeline run history."""
    if not records:
        console.print("[dim]No history found.[/dim]")
        return

    # Pre-load ratings and evaluation scores for all runs when a state key is available
    _ratings: dict[int, dict] = {}
    _eval_scores: dict[int, int] = {}
    if state_key is not None:
        from norn.journal import (
            history_run_id_map as _hist_map,
            run_dir as _run_dir,
            runs_dir as _runs_dir,
        )
        from norn.rating import load_rating as _load_rating

        _rd_base = _runs_dir(state_key)
        if _rd_base.is_dir():
            # Built once: the join is by recorded history id, never by
            # matching the two counters' values.
            _by_hist = _hist_map(state_key)
            for r in records:
                _jid = _by_hist.get(r.run_id)
                if _jid is None:
                    continue
                _rd = _run_dir(state_key, _jid)
                rating = _load_rating(_rd)
                if rating is not None:
                    _ratings[r.run_id] = rating
                _eval_path = _rd / "evaluation.json"
                if _eval_path.exists():
                    try:
                        import json as _json
                        _eval_data = _json.loads(_eval_path.read_text())
                        _eval_scores[r.run_id] = _eval_data.get("score", 0)
                    except Exception:
                        pass

    table = Table(show_edge=False)
    table.add_column("Run", justify="right")
    table.add_column("Timestamp")
    table.add_column("Status")
    table.add_column("Cost", justify="right")
    table.add_column("Time", justify="right")
    table.add_column("Rating", justify="right")
    table.add_column("Score", justify="right")
    table.add_column("Info")

    for r in records:
        if r.in_progress:
            status = "[yellow]↻ Running[/yellow]"
        else:
            status = "[green]✓ Complete[/green]" if r.success else "[red]✗ Failed[/red]"
        cost = f"${r.total_cost_usd:.2f}" if r.total_cost_usd else "-"
        duration = f"{r.duration_ms / 1000:.1f}s"
        if r.in_progress:
            info = f"{len(r.stages)} stages so far"
        else:
            info = f"{len(r.stages)} stages" if r.success else f"stage: {r.failed_stage or '?'}"
        ts = r.timestamp[:16].replace("T", " ")
        rating_data = _ratings.get(r.run_id)
        rating_str = f"{rating_data['score']}/5" if rating_data else "-"
        eval_score = _eval_scores.get(r.run_id)
        score_str = f"{eval_score}/10" if eval_score is not None else "-"
        table.add_row(f"#{r.run_id}", ts, status, cost, duration, rating_str, score_str, info)

    console.print(table)


def print_history_comparison(records: list[RunRecord], run_a: int, run_b: int, *, state_key: str | None = None) -> None:
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

    # Per-stage comparison table when both runs have metrics.json
    if state_key is not None:
        from norn.journal import run_dir_for_history_id as _rd_for_hist
        from norn.metrics import load_metrics as _load_metrics

        dir_a = _rd_for_hist(state_key, run_a)
        dir_b = _rd_for_hist(state_key, run_b)
        if dir_a is not None and dir_b is not None:
            m_a = _load_metrics(dir_a)
            m_b = _load_metrics(dir_b)
            if m_a is not None and m_b is not None:
                _print_stage_comparison(m_a, m_b, run_a, run_b)


def _print_stage_comparison(m_a: object, m_b: object, run_a: int, run_b: int) -> None:
    """Print a per-stage comparison table between two runs with metrics."""
    table = Table(title=f"Per-stage: #{run_a} vs #{run_b}", title_style="bold", show_edge=False)
    table.add_column("Stage", style="cyan")
    table.add_column(f"#{run_a} Cost", justify="right")
    table.add_column(f"#{run_b} Cost", justify="right")
    table.add_column("Turns", justify="right")
    table.add_column("Tools (x)", justify="right")
    table.add_column("Duration", justify="right")
    table.add_column("Delta", justify="right")

    # Index stages by name for run_a
    a_by_name = {s.name: s for s in m_a.stages}
    b_by_name = {s.name: s for s in m_b.stages}
    all_names = list(dict.fromkeys(
        [s.name for s in m_a.stages] + [s.name for s in m_b.stages]
    ))

    for name in all_names:
        sa = a_by_name.get(name)
        sb = b_by_name.get(name)
        a_cost = f"${sa.cost_usd:.4f}" if sa else "-"
        b_cost = f"${sb.cost_usd:.4f}" if sb else "-"
        turns_str = f"{sa.turns if sa else 0} → {sb.turns if sb else 0}"
        tools_a = f"{sa.tool_uses} ({sa.tool_errors}x)" if sa and sa.tool_errors else (str(sa.tool_uses) if sa else "-")
        tools_b = f"{sb.tool_uses} ({sb.tool_errors}x)" if sb and sb.tool_errors else (str(sb.tool_uses) if sb else "-")
        tools_str = f"{tools_a} → {tools_b}"
        dur_str = f"{(sa.duration_ms if sa else 0) / 1000:.1f}s → {(sb.duration_ms if sb else 0) / 1000:.1f}s"
        # Delta cost
        if sa and sb and sa.cost_usd > 0:
            delta = (sb.cost_usd - sa.cost_usd) / sa.cost_usd * 100
            sign = "+" if delta > 0 else ""
            color = "red" if delta > 0 else "green"
            delta_str = f"[{color}]{sign}{delta:.0f}%[/{color}]"
        else:
            delta_str = "-"

        table.add_row(name, a_cost, b_cost, turns_str, tools_str, dur_str, delta_str)

    console.print(table)
    console.print()


def print_history_run_details(records: list[RunRecord], run_id: int, *, state_key: str | None = None) -> None:
    """Print a detailed step-by-step log for a single run."""
    by_id = {r.run_id: r for r in records}
    record = by_id.get(run_id)
    if record is None:
        console.print(f"[red]Run #{run_id} not found in history.[/red]")
        return

    if record.in_progress:
        status = "[yellow]Running[/yellow]"
    else:
        status = "[green]Complete[/green]" if record.success else "[red]Failed[/red]"
    timestamp = record.timestamp[:19].replace("T", " ")

    console.print(f"\n  [bold]Run #{record.run_id}[/bold]")
    console.print(f"  Status:    {status}")
    console.print(f"  Timestamp: {timestamp}")
    console.print(f"  Cost:      ${record.total_cost_usd:.4f}")
    console.print(f"  Tokens:    {record.total_tokens:,}")
    console.print(f"  Duration:  {record.duration_ms / 1000:.1f}s")
    console.print(f"  Retries:   {record.retries}")
    if record.session_id:
        console.print(f"  Session:   {record.session_id}")
    if record.failed_stage:
        console.print(f"  Failed at: {record.failed_stage}")

    if not record.stage_log:
        console.print("\n  [dim]No detailed stage log stored for this run.[/dim]\n")
        # Still print metrics/findings if journal data exists
        if state_key is not None:
            _print_run_findings(state_key, run_id)
        return

    # Load journal metrics for optional Peak ctx column
    _peak_ctx_by_name: dict[str, int] = {}
    if state_key is not None:
        from norn.journal import run_dir_for_history_id as _rd_for_hist
        from norn.metrics import load_metrics as _load_metrics
        _rd = _rd_for_hist(state_key, run_id)
        if _rd is not None:
            _jm = _load_metrics(_rd)
            if _jm is not None:
                for _sm in _jm.stages:
                    if _sm.context_peak_tokens is not None:
                        # Store under both full node_id and bare name for matching
                        _peak_ctx_by_name[_sm.name] = _sm.context_peak_tokens
                        # Also index by bare stage name (strip "stage:" prefix)
                        _bare = _sm.name.split("/")[-1]
                        for _prefix in ("stage:", "loop:", "parallel:"):
                            if _bare.startswith(_prefix):
                                _bare = _bare[len(_prefix):]
                                break
                        _peak_ctx_by_name.setdefault(_bare, _sm.context_peak_tokens)

    _has_peak_ctx = bool(_peak_ctx_by_name)

    # Use a wide-enough console so the step log table is not truncated when
    # stdout is not a TTY (e.g. pytest capture, CI pipe) where Rich defaults
    # to 80 columns.  The table has 12 columns and needs ~160 chars for all
    # values to render unclipped.
    wide = Console(width=max(console.width, 160))
    table = Table(title=f"Run #{record.run_id} — Step Log", title_style="bold", show_edge=False)
    table.add_column("Step", style="cyan", overflow="fold")
    table.add_column("Att", justify="right")
    table.add_column("Status")
    table.add_column("Cost", justify="right")
    table.add_column("Running", justify="right")
    table.add_column("Tokens", justify="right")
    table.add_column("Turns", justify="right")
    table.add_column("Tools (x)", justify="right")
    table.add_column("API", justify="right")
    table.add_column("Wall", justify="right")
    if _has_peak_ctx:
        table.add_column("Peak ctx", justify="right")
    table.add_column("Model", overflow="fold")
    table.add_column("Info", overflow="fold")

    status_labels = {
        "passed": "[green]passed[/green]",
        "failed": "[red]failed[/red]",
        "skipped": "[dim]skipped[/dim]",
        "skipped_condition": "[dim]skipped[/dim]",
        "cached": "[dim]cached[/dim]",
    }
    info_labels = {
        "skipped_condition": "condition not met",
        "cached": "checkpoint cache",
    }

    for entry in record.stage_log:
        tokens = f"{entry.input_tokens:,}/{entry.output_tokens:,}" if entry.total_tokens else "-"
        api = f"{entry.duration_api_ms / 1000:.1f}s" if entry.duration_api_ms else "-"
        cost = f"${entry.cost_usd:.4f}" if entry.cost_usd else "-"
        running = f"${entry.running_total_cost_usd:.4f}"
        info = info_labels.get(entry.status, "")
        if entry.error:
            info = mask(entry.error).strip().splitlines()[-1]
        turns_str = str(entry.num_turns) if entry.num_turns else "-"
        tools_str = f"{entry.tool_uses} ({entry.tool_errors}x)" if entry.tool_errors else (str(entry.tool_uses) if entry.tool_uses else "-")
        peak_ctx = _peak_ctx_by_name.get(entry.name) or _peak_ctx_by_name.get(f"stage:{entry.name}")
        peak_ctx_str = f"{peak_ctx:,}" if peak_ctx is not None else "-"
        row = [
            entry.name,
            str(entry.attempt),
            status_labels.get(entry.status, entry.status),
            cost,
            running,
            tokens,
            turns_str,
            tools_str,
            api,
            f"{entry.duration_ms / 1000:.1f}s",
        ]
        if _has_peak_ctx:
            row.append(peak_ctx_str)
        row.extend([entry.model or "-", info or "-"])
        table.add_row(*row)

    wide.print()
    wide.print(table)
    wide.print()

    # Print metrics summary and findings if journal data exists
    if state_key is not None:
        _print_run_findings(state_key, run_id)


def _print_run_findings(state_key: str, run_id: int) -> None:
    """Load and print findings from the journal directory for a history run id."""
    from norn.journal import run_dir_for_history_id as _rd_for_hist
    from norn.metrics import load_findings as _load_findings, load_metrics as _load_metrics

    run_path = _rd_for_hist(state_key, run_id)
    if run_path is not None and run_path.is_dir():
        m = _load_metrics(run_path)
        f = _load_findings(run_path)
        if m is not None or f:
            print_findings(f, m)


def print_findings(findings: list, metrics: object | None = None) -> None:
    """Print findings table and one-line metrics summary."""
    if not findings and metrics is None:
        return

    if metrics is not None:
        m = metrics
        cost_str = f"${getattr(m, 'cost_usd', 0):.4f}"
        turns_str = str(getattr(m, "turns", 0))
        wall_str = f"{getattr(m, 'wall_ms', 0) / 1000:.1f}s"
        console.print(f"  [bold]Metrics:[/bold] cost={cost_str}  turns={turns_str}  wall={wall_str}")

    if not findings:
        console.print("  [dim]No findings.[/dim]\n")
        return

    severity_styles = {
        "high": "[bold red]HIGH[/bold red]",
        "warn": "[yellow]WARN[/yellow]",
        "info": "[dim]info[/dim]",
    }
    table = Table(title="Findings", title_style="bold", show_edge=False)
    table.add_column("Severity")
    table.add_column("Rule", style="cyan")
    table.add_column("Stage")
    table.add_column("Evidence", overflow="fold")
    table.add_column("Suggestion", overflow="fold")

    for f in findings:
        sev = severity_styles.get(f.severity, f.severity)
        table.add_row(
            sev,
            f.rule,
            f.stage or "-",
            f.evidence,
            f.suggestion,
        )

    console.print(table)
    console.print()


def print_evaluation_summary(result: object) -> None:
    """Print evaluation summary: score, per-stage grades, cost, feedback path."""
    score = getattr(result, "score", 0)
    stage_scores = getattr(result, "stage_scores", {})
    cost_usd = getattr(result, "cost_usd", 0.0)
    feedback_path = getattr(result, "feedback_path", None)

    console.print(f"\n  [bold]Evaluation score:[/bold] {score}/10")

    if stage_scores:
        table = Table(title="Stage Grades", title_style="bold", show_edge=False)
        table.add_column("Stage", style="cyan")
        table.add_column("Score", justify="right")

        for name, s in stage_scores.items():
            table.add_row(name, f"{s}/5")

        console.print(table)

    console.print(f"\n  [dim]Evaluation cost: ${cost_usd:.4f}[/dim]")
    if feedback_path:
        console.print(f"  [dim]Feedback: {feedback_path}[/dim]")
    console.print()

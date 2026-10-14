from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
from pathlib import Path

from norn.agents import session_totals
from norn.alerts import AlertManager
from norn.catalog import get_pipeline_info, list_pipelines, load_bundled_pipeline
from norn.envfile import apply_env_files
from norn.checkpoint import Checkpoint, load_checkpoint
from norn.dsl import Pipeline, apply_budget_overrides
from norn.history import load_history
from norn.loader import (
    find_org_for_project,
    list_orgs,
    load_org_config,
    load_pipeline as _load_pipeline_from_file,
)
from norn.responder import NonInteractiveResponder
from norn.runner import PipelineError, run_pipeline
from norn.state import (
    StateTrackedError,
    _load_checkpoint_for_config,
    _load_history_for_config,
    _primary_state_key,
    _state_key_candidates,
    publish_state_key,
)

log = logging.getLogger(__name__)

_ISSUE_KEY_RE = re.compile(r"^[A-Z]+-\d+$")


def _assert_provider_compatible(
    resolved_provider: str,
    checkpoint: "Checkpoint",
    mode: str,
) -> None:
    """Exit with an error if the resolved provider doesn't match the checkpoint provider.

    Args:
        resolved_provider: The provider selected for this run.
        checkpoint: The loaded checkpoint to compare against.
        mode: Either ``"resume"`` or ``"continue"`` for the error message.
    """
    checkpoint_provider = checkpoint.agent_provider
    if resolved_provider != checkpoint_provider:
        print(
            f"Error: cannot --{mode} with provider '{resolved_provider}': "
            f"checkpoint was created with '{checkpoint_provider}'. "
            f"Re-run with --agent-provider {checkpoint_provider} to continue this session.",
            file=sys.stderr,
        )
        sys.exit(1)



def _expand_file_refs(text: str) -> str:
    """Replace @path references with file contents, like Claude Code's @ syntax."""

    def replacer(match: re.Match[str]) -> str:
        filepath = match.group(1)
        path = Path(filepath)
        if not path.exists():
            print(f"Error: referenced file not found: {filepath}", file=sys.stderr)
            sys.exit(1)
        return path.read_text()

    return re.sub(r"@([\w./_-]+(?:\.[\w]+))", replacer, text)


def _parse_args(raw_args: list[str]) -> dict[str, str]:
    """Parse KEY=VALUE pairs from --arg flags."""
    params: dict[str, str] = {}
    for item in raw_args:
        if "=" not in item:
            print(f"Error: --arg must be KEY=VALUE, got: {item!r}", file=sys.stderr)
            sys.exit(1)
        key, _, value = item.partition("=")
        params[key.strip()] = value.strip()
    return params


def _load_pipeline(config_path: str) -> Pipeline:
    """Load a Pipeline from an external Python file (CLI wrapper with sys.exit on error)."""
    try:
        return _load_pipeline_from_file(config_path)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# `norn ui` — single unified launcher app (Launcher → Args → Run screens)
# ---------------------------------------------------------------------------


def _run_ui(pipeline_arg: str | None) -> None:
    """Entry point for the ``ui`` subcommand.

    Runs one :class:`~norn.tui.app.NornUIApp` that manages the launcher, the
    args prompt, and the live run as a stack of screens — so transitions are
    seamless and Back returns to the launcher without tearing down the
    terminal. With a pipeline argument it runs that pipeline directly.

    Textual is a required dependency; imported lazily here so that other
    subcommands don't load it, but never guarded — a missing install fails
    hard with the raw ImportError rather than a degraded path.
    """
    from norn.tui.app import NornUIApp

    from norn.catalog import list_discovered_pipelines

    app = NornUIApp(
        bundled=list_pipelines(),
        discovered=list_discovered_pipelines(),
        initial_pipeline=pipeline_arg,
    )
    app.run()


def main() -> None:
    apply_env_files()

    parser = argparse.ArgumentParser(prog="norn", description="Run a pipeline config")
    sub = parser.add_subparsers(dest="command")

    run_parser = sub.add_parser("run", help="Run a pipeline config file")
    run_parser.add_argument("config", help="Path to the pipeline config .py file or an issue key (e.g. PROJ-123)")
    run_parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    run_parser.add_argument(
        "--org",
        default=None,
        metavar="ORG_NAME",
        help="Load pipeline from this org config instead of a file path",
    )
    run_parser.add_argument(
        "--arg",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Pass a named parameter to the pipeline (e.g. --arg key=value)",
    )
    run_parser.add_argument(
        "--skip",
        action="append",
        default=[],
        metavar="STAGE_NAME",
        help="Skip a stage by name (can be repeated, e.g. --skip 'test python')",
    )
    run_parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from the checkpoint saved by the previous run (skips completed stages)",
    )
    run_parser.add_argument(
        "--continue",
        action="store_true",
        dest="continue_session",
        help="Continue the previous session (re-runs all stages but the agent remembers the conversation)",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would run without executing",
    )
    run_parser.add_argument(
        "--step",
        action="store_true",
        help="Interactive stepping mode: prompt before each stage",
    )
    run_parser.add_argument(
        "--agent-provider",
        default=None,
        metavar="PROVIDER",
        help="Agent provider to use (e.g. claude-code, opencode). Overrides NORN_AGENT_PROVIDER and pipeline setting.",
    )
    run_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="abort on every prompt; for child processes",
    )
    run_parser.add_argument(
        "--budget",
        type=float,
        default=None,
        metavar="USD",
        help="Cost ceiling for this run, overriding the pipeline's own limit",
    )
    run_parser.add_argument(
        "--token-budget",
        type=int,
        default=None,
        metavar="TOKENS",
        dest="token_budget",
        help="Token ceiling for this run, overriding the pipeline's own limit",
    )
    run_parser.add_argument(
        "--evaluate",
        action="store_true",
        help="Run LLM evaluation after the pipeline completes (or fails)",
    )
    run_parser.add_argument(
        "--quota",
        default=None,
        metavar="TOOL",
        dest="quota_probe",
        help="Sample subscription quota using the specified tool (e.g. codexbar) before and after the run",
    )
    run_parser.add_argument(
        "--require-quota",
        nargs="?",
        const=True,
        default=None,
        metavar="PCT[:WINDOW]",
        help=(
            "Require quota headroom. Bare: abort if quota sampling fails "
            "(default: warn and continue). With a value (e.g. 20 or 20:weekly): "
            "run `codexbar guard` first and abort when less than PCT%% remains "
            "in the session (default) or weekly window"
        ),
    )
    run_parser.add_argument(
        "--quota-fail-open",
        action="store_true",
        help="Start the run anyway when quota cannot be determined (overrides --require-quota)",
    )

    history_parser = sub.add_parser("history", help="Show run history for a pipeline config")
    history_parser.add_argument("config", help="Path to the pipeline config .py file")
    history_parser.add_argument(
        "--compare",
        nargs=2,
        type=int,
        metavar=("RUN_A", "RUN_B"),
        help="Compare two runs by run ID (e.g. --compare 1 3)",
    )
    history_parser.add_argument(
        "--run",
        type=int,
        metavar="RUN_ID",
        help="Show the detailed step log for one run",
    )

    compare_parser = sub.add_parser(
        "compare",
        help="Compare the last N journaled runs of two pipelines, or check pass bars",
    )
    compare_parser.add_argument(
        "pipelines",
        nargs="+",
        metavar="PIPELINE",
        help="Two pipelines (state keys) to compare, or one with --bar",
    )
    compare_parser.add_argument(
        "--last",
        type=int,
        default=5,
        metavar="N",
        help="Journaled runs per pipeline to compare (default: 5)",
    )
    compare_parser.add_argument(
        "--group",
        action="append",
        default=[],
        metavar="NAME=REGEX",
        help=(
            "Report cost share and executed count of stages whose bare name matches "
            "REGEX (repeatable)"
        ),
    )
    compare_parser.add_argument(
        "--bar",
        default=None,
        metavar="DIGEST",
        help="Check the '## Pass bar' lines of a feedback digest against one pipeline",
    )

    sub.add_parser("orgs", help="List available org configs")

    sub.add_parser("list-stages", help="List all registered stage plugins")

    sub.add_parser("list", help="List all bundled pipelines")

    sub.add_parser(
        "models",
        help="List all model labels and their pinned model IDs (see norn models for available labels)",
    )

    describe_parser = sub.add_parser("describe", help="Describe a bundled pipeline")
    describe_parser.add_argument("name", help="Name of the bundled pipeline")

    diagram_parser = sub.add_parser("diagram", help="Generate a Mermaid flowchart for a pipeline")
    diagram_parser.add_argument("config", help="Path to pipeline config .py file or bundled pipeline name")
    diagram_parser.add_argument(
        "--mermaid",
        action="store_true",
        help="Output raw Mermaid syntax instead of Markdown",
    )

    ui_parser = sub.add_parser("ui", help="Launch the Textual TUI for a pipeline run")
    ui_parser.add_argument(
        "pipeline",
        nargs="?",
        default=None,
        help="Path to pipeline config .py file or bundled pipeline name (optional)",
    )

    rate_parser = sub.add_parser("rate", help="Record a human rating (1–5) for a pipeline run")
    rate_parser.add_argument("config", help="Path to the pipeline config .py file or bundled pipeline name")
    rate_parser.add_argument("score", type=int, help="Rating score 1–5")
    rate_parser.add_argument(
        "--run",
        type=int,
        default=None,
        metavar="N",
        help="Run ID to rate, as shown by `norn history` (default: latest run)",
    )
    rate_parser.add_argument("--note", default=None, metavar="TEXT", help="Optional annotation")

    evaluate_parser = sub.add_parser("evaluate", help="Run LLM evaluation on a completed pipeline run")
    evaluate_parser.add_argument("config", help="Path to the pipeline config .py file or bundled pipeline name")
    evaluate_parser.add_argument(
        "--run",
        type=int,
        default=None,
        metavar="N",
        help="Run ID to evaluate, as shown by `norn history` (default: latest run)",
    )
    evaluate_parser.add_argument(
        "--model",
        default=None,
        metavar="MODEL",
        help="Feedback model label (default: standard). See `norn models`.",
    )
    evaluate_parser.add_argument(
        "--judge-model",
        default=None,
        metavar="MODEL",
        help="Judge model label (default: light). See `norn models`.",
    )
    evaluate_parser.add_argument(
        "--no-judge",
        action="store_true",
        help="Skip per-stage judge pass",
    )
    evaluate_parser.add_argument(
        "--stages",
        default=None,
        metavar="GLOB",
        help="Only judge stages matching this glob pattern",
    )
    evaluate_parser.add_argument(
        "--max-cost",
        type=float,
        default=5.0,
        metavar="USD",
        help="Maximum evaluation cost in USD (default: 5.0)",
    )
    evaluate_parser.add_argument(
        "--agent-provider",
        default=None,
        metavar="PROVIDER",
        help="Agent provider to use for evaluation",
    )

    args, remaining = parser.parse_known_args()

    if args.command == "history":
        from norn import ui as _ui
        from norn.journal import runs_dir as _runs_dir

        if args.compare and args.run:
            parser.error("--run cannot be used with --compare")
        records = _load_history_for_config(args.config)

        # Find the state key that has a .runs directory for metrics
        _history_state_key: str | None = None
        for candidate in _state_key_candidates(args.config):
            if _runs_dir(candidate).is_dir():
                _history_state_key = candidate
                break

        if args.run is not None:
            _ui.print_history_run_details(records, args.run, state_key=_history_state_key)
        elif args.compare:
            run_a, run_b = args.compare
            _ui.print_history_comparison(records, run_a, run_b, state_key=_history_state_key)
        else:
            _ui.print_history_table(records, state_key=_history_state_key)
        return

    if args.command == "compare":
        from norn import compare as _compare

        if remaining:
            parser.error(f"compare: unrecognized arguments: {' '.join(remaining)}")
        if args.last < 1:
            parser.error("compare: --last must be at least 1")
        if args.bar is not None:
            if len(args.pipelines) != 1:
                parser.error("compare: --bar takes exactly one pipeline")
            if args.group:
                parser.error("compare: --group cannot be used with --bar (groups come from the digest)")
            bar_path = Path(args.bar)
            if not bar_path.is_file():
                parser.error(f"compare: digest file not found: {args.bar}")
            try:
                bar_file = _compare.parse_bar_file(bar_path)
                results = _compare.check_bars(args.pipelines[0], bar_file)
            except _compare.CompareError as exc:
                print(f"compare: {exc}", file=sys.stderr)
                sys.exit(1)
            print(_compare.format_bar_results(args.pipelines[0], results))
            return
        if len(args.pipelines) != 2:
            parser.error("compare: expected two pipelines (or one pipeline with --bar)")
        groups: dict = {}
        for spec in args.group:
            try:
                gname, gregex = _compare.parse_group(spec)
            except ValueError as exc:
                parser.error(f"compare: {exc}")
            groups[gname] = gregex
        try:
            comparison = _compare.compare_pipelines(
                args.pipelines[0], args.pipelines[1], last=args.last, groups=groups
            )
        except _compare.CompareError as exc:
            print(f"compare: {exc}", file=sys.stderr)
            sys.exit(1)
        print(_compare.format_comparison(comparison))
        return

    if args.command == "orgs":
        orgs = list_orgs()
        if orgs:
            for org in orgs:
                print(org)
        else:
            print("No org configs found.")
        return

    if args.command == "list-stages":
        from norn.registry import discover_stages
        stages = discover_stages()
        if stages:
            for name in sorted(stages):
                print(f"{name}: {stages[name].__module__}.{stages[name].__name__}")
        else:
            print("No stage plugins registered.")
        return

    if args.command == "list":
        from norn.ui import console
        from rich.table import Table

        pipelines = list_pipelines()
        if not pipelines:
            console.print("No bundled pipelines found.")
            return
        table = Table(show_header=True, show_edge=False, pad_edge=False)
        table.add_column("Name")
        table.add_column("Description")
        for info in pipelines:
            table.add_row(info.name, info.short)
        console.print(table)
        return

    if args.command == "models":
        from norn.ui import console
        from norn.model_labels import labels_path, load_labels
        from rich.table import Table

        _labels = load_labels()
        _table = Table(show_header=True, show_edge=False, pad_edge=False)
        _table.add_column("Provider")
        _table.add_column("Label")
        _table.add_column("Model")
        _table.add_column("Effort")
        _table.add_column("Since")
        for _provider in sorted(_labels):
            for _label in sorted(_labels[_provider]):
                _rm = _labels[_provider][_label]
                _table.add_row(
                    _provider,
                    _label,
                    _rm.model,
                    _rm.effort or "-",
                    _rm.since or "-",
                )
        console.print(_table)
        console.print(f"\n  {labels_path()}")
        return

    if args.command == "describe":
        from norn.ui import console

        info = get_pipeline_info(args.name)
        if not info:
            print(f"Error: unknown pipeline {args.name!r}", file=sys.stderr)
            sys.exit(1)
        console.print(f"\n  [bold]{info.name}[/bold]\n")
        if info.long:
            for line in info.long.splitlines():
                console.print(f"  {line}")
            console.print()
        if info.env_vars:
            console.print("  [bold]Required environment variables:[/bold]")
            for var in info.env_vars:
                console.print(f"    {var}")
            console.print()
        if info.args:
            console.print("  [bold]Arguments:[/bold]")
            for arg_name, arg_desc in info.args.items():
                console.print(f"    {arg_name:12s}{arg_desc}")
            console.print()
        console.print(f"  [bold]Usage:[/bold]")
        usage = f"    norn run {info.name}"
        if info.args:
            usage += " " + " ".join(f"<{a}>" for a in info.args)
        console.print(usage)
        console.print()
        # Model labels: load the actual pipeline and walk Generate stages
        try:
            from norn.dsl import Loop, Parallel, Stage
            from norn.model_labels import ModelLabelError, resolve_label

            _desc_pipeline = load_bundled_pipeline(args.name)
            _provider = _desc_pipeline.agent_provider_name or "claude-code"
            _default_model = _desc_pipeline.default_model

            def _collect_labels(items, dflt):
                rows = []
                for _it in items:
                    if isinstance(_it, Stage) and hasattr(_it.impl, "model"):
                        _lbl = _it.impl.model or dflt
                        if _lbl:
                            try:
                                _rm = resolve_label(_provider, _lbl)
                                _effort = f" ({_rm.effort})" if _rm.effort else ""
                                rows.append((_it.name, _lbl, _rm.model, _effort))
                            except ModelLabelError:
                                rows.append((_it.name, _lbl, "?", ""))
                    elif isinstance(_it, Loop):
                        rows.extend(_collect_labels(_it.stages, dflt))
                    elif isinstance(_it, Parallel):
                        rows.extend(_collect_labels(_it.stages, dflt))
                return rows

            _label_rows = _collect_labels(_desc_pipeline.items, _default_model)
            if _label_rows:
                console.print(f"  [bold]Models ({_provider}):[/bold]")
                for _sname, _lbl, _mdl, _eff in _label_rows:
                    console.print(f"    {_sname}: {_lbl} -> {_mdl}{_eff}")
                console.print()
        except Exception:
            pass  # best-effort; don't crash describe if pipeline can't be loaded
        return

    if args.command == "diagram":
        from norn.diagram import to_markdown, to_mermaid

        info = get_pipeline_info(args.config)
        if info:
            pipeline = load_bundled_pipeline(args.config)
            config_path = str(info.path)
        else:
            pipeline = _load_pipeline(args.config)
            config_path = args.config
        if not pipeline.items:
            print(
                f"Warning: pipeline {pipeline.name!r} has no stages"
                " (dynamic pipelines may need runtime arguments)",
                file=sys.stderr,
            )
        if args.mermaid:
            print(to_mermaid(pipeline))
        else:
            print(to_markdown(pipeline, config_path))
        return

    if args.command == "ui":
        _run_ui(args.pipeline)
        return

    if args.command == "rate":
        from norn.journal import (
            history_run_id as _hist_id_of,
            history_run_id_map as _hist_map,
            latest_run_id as _latest_run_id,
            run_dir as _run_dir,
            runs_dir as _runs_dir,
        )
        from norn.rating import write_rating as _write_rating

        # Validate score range before touching the filesystem
        if args.score < 1 or args.score > 5:
            print(f"Error: score must be between 1 and 5 inclusive, got {args.score!r}", file=sys.stderr)
            sys.exit(1)

        # Find state key that has a .runs directory
        _rate_state_key: str | None = None
        for candidate in _state_key_candidates(args.config):
            if _runs_dir(candidate).is_dir():
                _rate_state_key = candidate
                break

        if _rate_state_key is None:
            print(
                f"no journaled runs for {args.config}; runs before this feature cannot be rated",
                file=sys.stderr,
            )
            sys.exit(1)

        # Resolve the run id. --run names the id `norn history` prints, which
        # is the history id; the journal keeps its own counter.
        if args.run is not None:
            run_id = _hist_map(_rate_state_key).get(args.run)
            if run_id is None:
                print(
                    f"run #{args.run} has no journal directory; "
                    f"runs before this feature cannot be rated",
                    file=sys.stderr,
                )
                sys.exit(1)
            label = args.run
        else:
            run_id = _latest_run_id(_rate_state_key)
            if run_id is None:
                print(
                    f"no journaled runs for {args.config}; runs before this feature cannot be rated",
                    file=sys.stderr,
                )
                sys.exit(1)
            label = _hist_id_of(_run_dir(_rate_state_key, run_id))

        rd = _run_dir(_rate_state_key, run_id)
        if not rd.is_dir():
            print(f"Error: run directory not found: {rd}", file=sys.stderr)
            sys.exit(1)

        _write_rating(rd, args.score, args.note)
        from norn.ui import console as _console
        # A run journaled before `history_run_id` existed has no history id to
        # name, and printing its directory number as one invites exactly the
        # confusion this join removes.
        _what = f"run #{label}" if label is not None else f"journal run {run_id}"
        _console.print(f"  [green]✓[/green] Rated {_what} as [bold]{args.score}/5[/bold]")
        return

    if args.command == "evaluate":
        from norn.evaluate import EvaluationError, evaluate_run
        from norn.journal import (
            history_run_id_map as _hist_map,
            latest_run_id as _latest_run_id,
            runs_dir as _runs_dir,
        )
        from norn import ui as _ui

        # Find state key
        _eval_state_key: str | None = None
        for candidate in _state_key_candidates(args.config):
            if _runs_dir(candidate).is_dir():
                _eval_state_key = candidate
                break

        if _eval_state_key is None:
            print(f"no journaled runs for {args.config}", file=sys.stderr)
            sys.exit(1)

        # --run names the id `norn history` prints; map it to the journal id
        # that `evaluate_run` addresses directories by.
        if args.run is not None:
            _eval_run_id = _hist_map(_eval_state_key).get(args.run)
            if _eval_run_id is None:
                print(
                    f"run #{args.run} has no journal directory; "
                    f"runs before this feature cannot be evaluated",
                    file=sys.stderr,
                )
                sys.exit(1)
        else:
            _eval_run_id = _latest_run_id(_eval_state_key)
            if _eval_run_id is None:
                print(f"no journaled runs for {args.config}", file=sys.stderr)
                sys.exit(1)

        try:
            result = asyncio.run(evaluate_run(
                _eval_state_key,
                _eval_run_id,
                provider_name=args.agent_provider,
                judge=not args.no_judge,
                judge_model=args.judge_model or "light",
                feedback_model=args.model or "standard",
                stage_glob=args.stages,
                max_cost_usd=args.max_cost,
            ))
            _ui.print_evaluation_summary(result)
        except EvaluationError as exc:
            print(f"Evaluation error: {exc}", file=sys.stderr)
            sys.exit(1)
        return

    if args.command != "run":
        parser.print_help()
        sys.exit(1)

    if args.step and args.non_interactive:
        parser.error("--step and --non-interactive are mutually exclusive")

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(message)s")

    params = _parse_args(args.arg)
    params["args"] = _expand_file_refs(" ".join(remaining))
    params["skip"] = set(args.skip)

    # Publish the state key before the pipeline module is imported.  A pipeline
    # that computes step ownership by diffing worktree snapshots has to exclude
    # norn's own run state — `<key>.runs/`, `<key>.checkpoint`, `<key>.history`
    # are written into the launch directory *while the run is in flight*, so a
    # pipeline launched from inside the target repo would otherwise commit them
    # and then fail its own "owned paths are clean" postcondition on the next
    # journal write.  The TUI launcher does the same in `norn/tui/launch.py`;
    # `run_pipeline` re-checks and registers a git exclude for every entry point.
    publish_state_key(args.config)

    # Resolve pipeline: --org flag, issue key auto-detection, or plain file path
    pipeline_source: str | None = None
    if args.org:
        try:
            pipeline = load_org_config(args.org)
        except FileNotFoundError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        config_path_for_checkpoint = _primary_state_key(args.config)
    elif _ISSUE_KEY_RE.match(args.config):
        project_key = args.config.split("-")[0]
        try:
            _org_name, pipeline = find_org_for_project(project_key)
        except (FileNotFoundError, ValueError) as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        params.setdefault("issue", args.config)
        config_path_for_checkpoint = _primary_state_key(args.config)
    elif get_pipeline_info(args.config):
        info = get_pipeline_info(args.config)
        pipeline = load_bundled_pipeline(args.config)
        config_path_for_checkpoint = _primary_state_key(args.config)
        pipeline_source = str(info.path)
    else:
        pipeline = _load_pipeline(args.config)
        config_path_for_checkpoint = _primary_state_key(args.config)
        pipeline_source = str(Path(args.config).resolve())

    apply_budget_overrides(
        pipeline,
        max_cost_usd=args.budget,
        max_tokens=args.token_budget,
    )

    if args.dry_run:
        from norn.ui import print_dry_run
        print_dry_run(pipeline)
        return

    from norn.agents import resolve_agent_provider

    resolved_provider = resolve_agent_provider(pipeline, cli_provider=args.agent_provider)

    # --quota-fail-open overrides --require-quota everywhere: the pre-run guard
    # starts the run anyway, and probe failures during the run stay warnings.
    require_quota = getattr(args, "require_quota", None)
    quota_fail_open = getattr(args, "quota_fail_open", False)
    abort_on_quota_failure = require_quota is not None and not quota_fail_open

    if isinstance(require_quota, str):
        from norn.quota import QuotaProbeError, check_quota_guard
        from norn.ui import console
        try:
            decision = check_quota_guard(
                resolved_provider, require_quota, fail_open=quota_fail_open
            )
        except QuotaProbeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        if not decision.allowed:
            console.print(
                f"[bold red]Quota guard blocked the run:[/bold red] {decision.message}",
                highlight=False,
            )
            sys.exit(1)
        console.print(f"  [dim]Quota guard: {decision.message}[/dim]", highlight=False)

    resume_session: str | None = None
    resume_checkpoint: Checkpoint | None = None

    if args.resume or args.continue_session:
        checkpoint = _load_checkpoint_for_config(args.config)
        if checkpoint is not None:
            mode = "resume" if args.resume else "continue"
            _assert_provider_compatible(resolved_provider, checkpoint, mode)
        if args.resume and checkpoint:
            resume_checkpoint = checkpoint
            resume_session = checkpoint.session_id
            count = len(checkpoint.completed_stages)
            label = f"{count} stage{'s' if count != 1 else ''} cached"
            from norn.ui import console
            console.print(f"  [dim]Resuming from checkpoint ({label})[/dim]")
        elif args.continue_session and checkpoint and checkpoint.session_id:
            resume_session = checkpoint.session_id
            session_totals.seed(checkpoint.session_id, checkpoint.session_totals)
            from norn.ui import console
            console.print(f"  [dim]Continuing session {resume_session}[/dim]")
        else:
            from norn.ui import console
            msg = "No saved checkpoint found" if args.resume else "No saved session found"
            console.print(f"[yellow]⚠  {msg} — starting fresh[/yellow]", highlight=False)

    alert_manager = AlertManager(channels=pipeline.alert_channels) if pipeline.alert_channels else None

    input_responder = NonInteractiveResponder() if args.non_interactive else None

    pipeline_failed = False
    try:
        asyncio.run(run_pipeline(
            pipeline,
            params=params,
            resume_session=resume_session,
            resume_checkpoint=resume_checkpoint,
            config_path=config_path_for_checkpoint,
            alert_manager=alert_manager,
            step_mode=args.step,
            agent_provider=resolved_provider,
            input_responder=input_responder,
            pipeline_source=pipeline_source,
            quota_probe=getattr(args, "quota_probe", None),
            require_quota=abort_on_quota_failure,
        ))
        # Checkpoint is saved incrementally during run_pipeline — no explicit save needed here
    except StateTrackedError as e:
        # Nothing ran: the repository has to be fixed before any run can pass a
        # commit gate, and the message says exactly how.
        from norn.ui import console
        console.print(f"\n[bold red]Cannot start the run:[/bold red] {e}", highlight=False)
        sys.exit(1)
    except PipelineError as e:
        pipeline_failed = True
        from rich.markup import escape

        from norn.ui import console
        console.print(f"\n[bold red]Pipeline failed:[/bold red] {escape(str(e))}")
        # Print findings from the latest run journal
        try:
            from norn.journal import latest_run_id as _latest_rid, runs_dir as _runs_dir
            from norn.metrics import load_findings as _load_findings

            _rid = _latest_rid(config_path_for_checkpoint)
            if _rid is not None:
                _rd = _runs_dir(config_path_for_checkpoint) / str(_rid)
                _ff = _load_findings(_rd)
                if _ff:
                    from norn import ui as _ui
                    _ui.print_findings(_ff)
        except Exception:
            pass  # best-effort; don't mask the real failure

    # Post-run evaluation (on both success and failure)
    if args.evaluate:
        try:
            from norn.evaluate import EvaluationError, evaluate_run as _evaluate_run
            from norn.journal import latest_run_id as _eval_rid
            from norn import ui as _eval_ui

            _rid = _eval_rid(config_path_for_checkpoint)
            if _rid is not None:
                result = asyncio.run(_evaluate_run(config_path_for_checkpoint, _rid))
                _eval_ui.print_evaluation_summary(result)
        except Exception as exc:
            from norn.ui import console as _ec
            _ec.print(f"[yellow]Evaluation failed: {exc}[/yellow]")

    if pipeline_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from pathlib import Path

from norn.alerts import AlertManager
from norn.catalog import get_pipeline_info, list_pipelines, load_bundled_pipeline
from norn.envfile import apply_env_files
from norn.checkpoint import Checkpoint, load_checkpoint
from norn.dsl import Pipeline
from norn.history import load_history
from norn.loader import (
    find_org_for_project,
    list_orgs,
    load_org_config,
    load_pipeline as _load_pipeline_from_file,
)
from norn.runner import PipelineError, run_pipeline

from norn.state import (
    _load_checkpoint_for_config,
    _load_history_for_config,
    _primary_state_key,
    _state_key_candidates,
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


def main() -> None:
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

    sub.add_parser("orgs", help="List available org configs")

    sub.add_parser("list-stages", help="List all registered stage plugins")

    sub.add_parser("list", help="List all bundled pipelines")

    describe_parser = sub.add_parser("describe", help="Describe a bundled pipeline")
    describe_parser.add_argument("name", help="Name of the bundled pipeline")

    diagram_parser = sub.add_parser("diagram", help="Generate a Mermaid flowchart for a pipeline")
    diagram_parser.add_argument("config", help="Path to pipeline config .py file or bundled pipeline name")
    diagram_parser.add_argument(
        "--mermaid",
        action="store_true",
        help="Output raw Mermaid syntax instead of Markdown",
    )

    args, remaining = parser.parse_known_args()

    if args.command == "history":
        from norn import ui as _ui

        if args.compare and args.run:
    ui_parser = sub.add_parser("ui", help="Launch the Textual TUI for a pipeline run")
    ui_parser.add_argument(
        "pipeline",
        nargs="?",
        default=None,
        help="Path to pipeline config .py file or bundled pipeline name (optional)",
    )

            parser.error("--run cannot be used with --compare")
        records = _load_history_for_config(args.config)
        if args.run is not None:
            _ui.print_history_run_details(records, args.run)
        elif args.compare:
            run_a, run_b = args.compare
            _ui.print_history_comparison(records, run_a, run_b)
        else:
            _ui.print_history_table(records)
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

    if args.command != "run":
        parser.print_help()
        sys.exit(1)

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(message)s")
    if args.command == "ui":
        _run_ui(args.pipeline)
        return


    params = _parse_args(args.arg)
    params["args"] = _expand_file_refs(" ".join(remaining))
    params["skip"] = set(args.skip)

    # Resolve pipeline: --org flag, issue key auto-detection, or plain file path
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
        pipeline = load_bundled_pipeline(args.config)
        config_path_for_checkpoint = _primary_state_key(args.config)
    else:
        pipeline = _load_pipeline(args.config)
        config_path_for_checkpoint = _primary_state_key(args.config)

    if args.dry_run:
        from norn.ui import print_dry_run
        print_dry_run(pipeline)
        return

    from norn.agents import resolve_agent_provider

    resolved_provider = resolve_agent_provider(pipeline, cli_provider=args.agent_provider)

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
            from norn.ui import console
            console.print(f"  [dim]Continuing session {resume_session}[/dim]")
        else:
            from norn.ui import console
            msg = "No saved checkpoint found" if args.resume else "No saved session found"
            console.print(f"[yellow]⚠  {msg} — starting fresh[/yellow]", highlight=False)

    alert_manager = AlertManager(channels=pipeline.alert_channels) if pipeline.alert_channels else None

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
        ))
        # Checkpoint is saved incrementally during run_pipeline — no explicit save needed here
    except PipelineError as e:
        from norn.ui import console
        console.print(f"\n[bold red]Pipeline failed:[/bold red] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

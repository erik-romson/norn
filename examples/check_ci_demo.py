"""Example pipeline for the CheckCI stage.

Inspect the latest GitHub Actions workflow run for a branch and see the
failure logs when something is broken. Run it to explore how CheckCI
behaves with different repos, branches, workflows, and polling settings.

Usage:
    # Default: check current repo + current branch, one-shot
    bin/norn examples/check_ci_demo.py

    # Check a specific repo and branch
    bin/norn examples/check_ci_demo.py \\
        --arg repo=owner/project --arg branch=feature-x

    # Filter by workflow file
    bin/norn examples/check_ci_demo.py --arg workflow=e2e-tests.yml

    # Poll until the run completes (useful right after a push)
    bin/norn examples/check_ci_demo.py --arg poll=true --arg timeout=45

    # Combine all of it
    bin/norn examples/check_ci_demo.py \\
        --arg repo=owner/project \\
        --arg branch=main \\
        --arg workflow=ci.yml \\
        --arg poll=true \\
        --arg interval=15 \\
        --arg timeout=30

Parameters (all optional):
    repo     : GitHub repo in owner/name format (default: auto-detect)
    branch   : Branch name                      (default: current git branch)
    workflow : Workflow file or name            (default: any)
    poll     : "true" to poll until complete    (default: false, one-shot)
    interval : Seconds between polls            (default: 30)
    timeout  : Minutes to wait when polling     (default: 30)

Requires:
    - githubkit installed (``uv pip install 'norn[github]'``)
    - A GitHub token via ``GH_TOKEN`` or ``GITHUB_TOKEN`` env var,
      or ``gh auth login``
"""
from __future__ import annotations

import sys
from typing import Any

from norn.dsl import Pipeline, fail
from norn.models import PipelineContext, StageResult
from norn.stages.base import BaseStage
from norn.stages.check_ci import CheckCI
from norn.stages.run_command import RunCommand
from norn.ui import console


class CheckAndReport(BaseStage):
    """Run CheckCI and pretty-print the full result to the terminal.

    Wraps a ``CheckCI`` instance so that:
      - The outcome (success flag, run metadata, summarized logs, error)
        is always printed via the norn rich console.
      - The stage ALWAYS reports success to the pipeline, so the demo
        runs to completion and you can eyeball the output.

    The real ``CheckCI`` return values are preserved on
    ``ctx.results["check ci"]`` so downstream stages could still read them.
    """

    needs_agent = False

    def __init__(self, *, check: CheckCI) -> None:
        self.check = check

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        result = await self.check.run(ctx, **kwargs)

        status_color = "green" if result.success else "red"
        console.rule(f"[bold {status_color}]CheckCI result[/bold {status_color}]")
        console.print(f"[bold]success:[/bold] {result.success}")

        output = result.output
        if isinstance(output, dict):
            for key, value in output.items():
                if key == "logs":
                    continue
                console.print(f"[cyan]{key}:[/cyan] {value}")
            logs = output.get("logs", "")
            if logs:
                console.rule("[dim]summarized logs[/dim]")
                console.print(logs, highlight=False, markup=False)
        elif output is not None:
            console.print(output)

        if result.error and not result.success:
            console.print(f"[red]error:[/red] {result.error}")

        console.rule()

        # Always return success so the pipeline runs to completion.
        # Preserve the raw result under the same name so downstream
        # stages can inspect it.
        return StageResult(
            name="",
            success=True,
            output=output,
        )

# --- parse --arg params out of sys.argv so we can configure CheckCI() at build time ---
_args: dict[str, str] = {}
_argv = sys.argv[1:]
i = 0
while i < len(_argv):
    if _argv[i] == "--arg" and i + 1 < len(_argv) and "=" in _argv[i + 1]:
        k, _, v = _argv[i + 1].partition("=")
        _args[k] = v
        i += 2
    else:
        i += 1

repo = _args.get("repo") or None
branch = _args.get("branch") or None
workflow = _args.get("workflow") or None
poll = _args.get("poll", "false").lower() in ("1", "true", "yes")
poll_interval = int(_args.get("interval", "30"))
timeout_minutes = int(_args.get("timeout", "30"))


config = (
    Pipeline("check_ci_demo")
    .stage(
        "show config",
        RunCommand(cmd=(
            'echo "━━━ CheckCI configuration ━━━"; '
            f'echo "  repo     = {repo or "(auto-detect from git remote)"}"; '
            f'echo "  branch   = {branch or "(current git branch)"}"; '
            f'echo "  workflow = {workflow or "(any)"}"; '
            f'echo "  poll     = {poll}"; '
            f'echo "  interval = {poll_interval}s (only used if poll=true)"; '
            f'echo "  timeout  = {timeout_minutes}m (only used if poll=true)"'
        )),
    )
    .stage(
        "verify auth",
        RunCommand(cmd=(
            'if [ -n "$GH_TOKEN" ]; then '
            '  echo "Using GH_TOKEN from env"; '
            'elif [ -n "$GITHUB_TOKEN" ]; then '
            '  echo "Using GITHUB_TOKEN from env"; '
            'elif command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then '
            '  echo "Using gh CLI auth"; gh auth status; '
            'else '
            '  echo "ERROR: No GH_TOKEN/GITHUB_TOKEN env var and gh is not authenticated."; '
            '  echo "       Run: gh auth login   (or export GH_TOKEN=...)"; '
            '  exit 1; '
            'fi'
        )),
        on_failure=fail,
    )
    .stage(
        "check ci",
        CheckAndReport(check=CheckCI(
            repo=repo,
            branch=branch,
            workflow=workflow,
            poll=poll,
            poll_interval=poll_interval,
            timeout_minutes=timeout_minutes,
        )),
    )
)

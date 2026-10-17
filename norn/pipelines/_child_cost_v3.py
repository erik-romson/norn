"""Child runs that report their cost and their failure (fix_jira_issue_v3).

A parent that runs another norn pipeline as a subprocess sees only its exit
code.  The two classes here read the child's own journal
(``<launch dir>/<state key>.runs/<n>/``) so the parent can

* attach the child's cost to its own usage tracker, which is what makes the
  parent's ceiling and budget prompt cover the work underneath it;
* name the child's failed stage in its own error;
* fire a notice when the child's cost crosses the parent's ceiling (the
  runner's budget prompt fires no alert).

The usage record carries ``session_id=None``.  A non-None session id on a
synthetic record would make the next ``Generate`` stage resume the child's
session.  Both stages rely on ``RunCommand``'s own timeout rather than the
runner's stage timeout, because the runner's timeout path returns before usage
is added and the child's cost would be lost.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from norn.dsl import Pipeline
from norn.models import PipelineContext, StageResult, UsageRecord
from norn.pipelines._child_run import failed_stage_error, latest_run_dir
from norn.pipelines._fix_jira_v3 import notify
from norn.pipelines._merge_gh import CiFixChild, ci_fix_child_cmd
from norn.pipelines._merge_state import MergePaths
from norn.stages.run_command import RunCommand

# Floor for the child's ceiling, so a parent that has already spent its whole
# budget still starts the child and lets it report its own budget stop.
MIN_CHILD_BUDGET = 1.0

# Mirrors check_and_fix_ci_v2.MAX_FIX_ATTEMPTS (a guard test pins the value;
# that module parses sys.argv at import and cannot be imported here).
CI_CHILD_ATTEMPTS = 3

ERROR_TAIL_LINES = 40


class ChildCostError(Exception):
    """The child's ``run.json`` exists but cannot be read as a run record."""


def remaining_budget(pipeline: Pipeline, ctx: PipelineContext) -> float | None:
    """Return the child's ceiling in USD, or None when this run has no cost limit."""
    # Copied from fix_jira_issue_v2.py:274-283 (cannot be imported: import-time argv parsing)
    limits = [
        b.max_cost_usd for b in (pipeline.budgets or [])
        if b.max_cost_usd is not None
    ]
    if not limits:
        return None
    spent = ctx.usage_tracker.total_cost_usd or 0.0
    return max(round(min(limits) - spent, 2), MIN_CHILD_BUDGET)


def ci_fix_timeout(ci_timeout_minutes: int) -> float:
    """Process timeout for one CI-fix child: its three CI waits plus slack."""
    return CI_CHILD_ATTEMPTS * (ci_timeout_minutes * 60 + 900) + 900


def _run_number(runs_dir: Path) -> int:
    latest = latest_run_dir(runs_dir)
    return int(latest.name) if latest is not None else 0


def _new_run_dir(runs_dir: Path, before: int) -> Path | None:
    """Return the newest run dir only when its number grew past *before*."""
    latest = latest_run_dir(runs_dir)
    if latest is None or int(latest.name) <= before:
        return None
    return latest


def child_usage(run_dir: Path | None) -> tuple[UsageRecord | None, str]:
    """Read the child's cost from ``run_dir/run.json``.

    Returns ``(None, "child cost unknown: <reason>")`` when there is no run
    dir, no ``run.json``, no ``totals`` (a child killed hard) or the child's
    cost was not tracked.  Malformed JSON raises :class:`ChildCostError`.
    """
    if run_dir is None:
        return None, "child cost unknown: the child left no new journal"
    path = run_dir / "run.json"
    if not path.is_file():
        return None, f"child cost unknown: {path} is missing"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ChildCostError(f"cannot read the child's {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ChildCostError(f"cannot read the child's {path}: not a JSON object")
    totals = data.get("totals")
    if not isinstance(totals, dict):
        return None, f"child cost unknown: {path} has no totals (the child was killed)"
    cost_mode = data.get("cost_mode")
    if cost_mode != "tracked":
        return None, f"child cost unknown: its cost_mode is {cost_mode!r}, not 'tracked'"
    try:
        cost = float(totals["cost"])
        output_tokens = int(totals["output_tokens"])
        duration_ms = int(data["duration_ms"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ChildCostError(f"cannot read the child's {path}: bad totals ({exc!r})") from exc
    record = UsageRecord(
        stage_name="",
        session_id=None,
        output_tokens=output_tokens,
        total_cost_usd=cost,
        duration_ms=duration_ms,
        num_turns=0,
    )
    return record, f"child cost ${cost:.2f}"


def child_diagnosis(run_dir: Path | None, project_dir: str, *, child: str) -> str:
    """Explain how *child*'s newest run failed, in the shape of a stage error.

    Line 1 is the headline (it is what the FAILED alert shows); the last line
    is the next action.  Built from the journal directly rather than from
    ``_child_run.report``, whose closing text is written for v1 and v2.
    """
    if run_dir is None:
        lines = [
            f"{child} failed before it started its journal",
            "The child left no new run journal, so it never started: read this stage's own output for the command that could not run.",
        ]
    else:
        run: dict[str, Any] = {}
        run_path = run_dir / "run.json"
        if run_path.is_file():
            loaded = json.loads(run_path.read_text())
            if isinstance(loaded, dict):
                run = loaded
        stage_name = run.get("failed_stage")
        if isinstance(stage_name, str) and stage_name:
            lines = [f"{child} failed at stage '{stage_name}'", f"journal: {run_dir}"]
            error = failed_stage_error(run_dir, stage_name)
            if error:
                lines.append(f"stage error (last {ERROR_TAIL_LINES} lines):")
                lines.extend(error.rstrip().splitlines()[-ERROR_TAIL_LINES:])
        else:
            lines = [f"{child} failed, and its journal names no stage", f"journal: {run_dir}"]
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=project_dir, capture_output=True, text=True, check=True,
    ).stdout.strip()
    if dirty:
        lines.append(
            "The failed step left uncommitted edits; the child refuses a dirty tree. "
            "Restore them (`git restore .`) and remove the step's new files before [r]etry."
        )
    lines.append("Fix the cause above, then [r]etry; steps the child already committed are skipped.")
    return "\n".join(lines)


def _lowest_limit(pipeline: Pipeline) -> float | None:
    limits = [b.max_cost_usd for b in (pipeline.budgets or []) if b.max_cost_usd is not None]
    return min(limits) if limits else None


async def _attach_cost(
    result: StageResult,
    *,
    run_dir: Path | None,
    pipeline: Pipeline,
    ctx: PipelineContext,
    child: str,
) -> StageResult:
    """Attach the child's usage to *result*, note it in the output, and notify
    when the child's cost takes the run past its ceiling.  Raises
    :class:`ChildCostError` when the child's ``run.json`` is malformed.
    """
    usage, text = child_usage(run_dir)
    result.usage = usage
    output = dict(result.output) if isinstance(result.output, dict) else {"output": result.output}
    output["child_cost"] = text
    result.output = output
    limit = _lowest_limit(pipeline)
    if usage is not None and limit is not None:
        total = (ctx.usage_tracker.total_cost_usd or 0.0) + usage.total_cost_usd
        if total > limit:
            await notify(
                "Budget reached",
                f"{child} brought this run to ${total:.2f} of its ${limit:.2f} ceiling",
            )
    return result


class RunChildPipelineV3(RunCommand):
    """Run the child pipeline with what is left of this run's ceiling and report its cost."""

    # Copied from fix_jira_issue_v2.py:247-291 (cannot be imported: import-time argv parsing)
    def __init__(
        self,
        *,
        cmd: str,
        pipeline: Pipeline,
        runs_dir: str,
        rewritten_marker: str,
        timeout: float | None = None,
    ) -> None:
        super().__init__(cmd=cmd, timeout=timeout)
        self._base_cmd = cmd
        self._pipeline = pipeline
        self._runs_dir = Path(runs_dir)
        self._rewritten_marker = Path(rewritten_marker)

    def remaining_budget(self, ctx: PipelineContext) -> float | None:
        return remaining_budget(self._pipeline, ctx)

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        if self._rewritten_marker.exists():
            return StageResult(
                name="",
                success=False,
                error=(
                    "implement: this branch was already rewritten into house-style commits; "
                    "a fresh run would re-implement every step.\n"
                    "Resume the earlier run with --resume, or delete tmp/jira/<KEY>/ "
                    "and start from a fresh branch."
                ),
            )
        budget = self.remaining_budget(ctx)
        self.cmd = self._base_cmd if budget is None else f"{self._base_cmd} --arg budget={budget}"
        before = _run_number(self._runs_dir)
        result = await super().run(ctx, **kwargs)
        run_dir = _new_run_dir(self._runs_dir, before)
        child = self._runs_dir.name.removesuffix(".runs")
        try:
            result = await _attach_cost(
                result, run_dir=run_dir, pipeline=self._pipeline, ctx=ctx, child=child
            )
            if not result.success:
                diagnosis = child_diagnosis(run_dir, str(self._runs_dir.parent), child=child)
                timed_out = isinstance(result.output, dict) and result.output.get("returncode") is None
                if timed_out and result.error:
                    diagnosis = f"{result.error.splitlines()[0]}\n{diagnosis}"
                result.error = diagnosis
        except ChildCostError as exc:
            return StageResult(name="", success=False, output=result.output, error=str(exc))
        return result


class CiFixChildV3(CiFixChild):
    """Run ``check_and_fix_ci_v2`` as the CI-fix child and report its cost."""

    def __init__(
        self,
        *,
        paths: MergePaths,
        stage: str,
        n: int,
        project_dir: str,
        pipeline: Pipeline,
        test_cmd_file: str,
        ci_timeout_minutes: int,
    ) -> None:
        self._test_cmd_file = test_cmd_file
        self._ci_timeout_minutes = ci_timeout_minutes
        super().__init__(
            paths=paths,
            stage=stage,
            n=n,
            project_dir=project_dir,
            pipeline=pipeline,
            test_cmd="",
            ci_pipeline="check_and_fix_ci_v2",
            timeout=ci_fix_timeout(ci_timeout_minutes),
        )

    def _build_cmd(self, ctx: PipelineContext) -> str:
        workflows = [
            w for w in Path(self._halt_paths.ci_fix_workflows(self._n)).read_text().splitlines() if w
        ]
        test_cmd = Path(self._test_cmd_file).read_text().strip()
        base = ci_fix_child_cmd(
            self._project_dir, workflows, test_cmd, self._remaining_budget(ctx), self._ci_pipeline
        )
        return f"{base} --arg ci_timeout_minutes={self._ci_timeout_minutes} --arg skip_local=true"

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        runs_dir = self._runs_dir()
        before = _run_number(runs_dir)
        result = await super().run(ctx, **kwargs)
        try:
            return await _attach_cost(
                result,
                run_dir=_new_run_dir(runs_dir, before),
                pipeline=self._pipeline,
                ctx=ctx,
                child=self._ci_pipeline,
            )
        except ChildCostError as exc:
            return StageResult(name="", success=False, output=result.output, error=str(exc))

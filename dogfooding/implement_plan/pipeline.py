"""Implement a plan from step-*.md files.

Usage:
    bin/norn dogfooding/implement_plan/pipeline.py tmp/refactor
    bin/norn dogfooding/implement_plan/pipeline.py tmp/refactor --dry-run
    bin/norn dogfooding/implement_plan/pipeline.py tmp/refactor --skip "commit step-03"
"""

from __future__ import annotations

import os
import sys
from glob import glob
from pathlib import Path

from norn.alerts import MacOSChannel
from norn.dsl import Pipeline, Stage, fail

from dogfooding.common import (
    already_committed_steps,
    clean_worktree,
    parse_front_matter,
    preflight,
    record_start,
)
from dogfooding.implement_plan.implementation_plan import ImplementationPlan, PlanStep

metadata = {
    "env_vars": ["ANTHROPIC_API_KEY"],
    "args": {"args": "Path to directory containing step-*.md files"},
}


# ---------------------------------------------------------------------------
# Input resolution helpers
# ---------------------------------------------------------------------------


def _resolve_feature_dir(project_dir: str, argv: list[str]) -> str:
    """Pick the feature directory from *argv* (first arg that is a directory)."""
    for arg in argv:
        for candidate in (Path(arg), Path(project_dir) / arg):
            if candidate.is_dir():
                return str(candidate)
    return os.path.join(project_dir, "tmp")


def _load_index(
    feature_dir: str, test_cmd: str, bats_cmd: str,
) -> tuple[str, str, str]:
    """Load ``index.md`` overrides.  Returns *(shared_context, test_cmd, bats_cmd)*."""
    index_path = Path(feature_dir) / "index.md"
    if not index_path.exists():
        return "", test_cmd, bats_cmd
    fm, body = parse_front_matter(index_path.read_text())
    if isinstance(fm.get("test_cmd"), str):
        test_cmd = fm["test_cmd"]
    if isinstance(fm.get("bats_cmd"), str):
        bats_cmd = fm["bats_cmd"]
    return body, test_cmd, bats_cmd


def _discover_step_files(feature_dir: str) -> list[str]:
    """Find step markdown files in *feature_dir*, or raise."""
    step_files = sorted(glob(os.path.join(feature_dir, "step-*.md")))
    if not step_files:
        step_files = sorted(
            f for f in glob(os.path.join(feature_dir, "*.md"))
            if Path(f).name not in ("index.md", "README.md", "refactor-plan.md")
        )
    if not step_files:
        from norn.runner import PipelineError

        raise PipelineError(
            f"No step-*.md files found in {feature_dir}\n"
            "Usage: norn run dogfooding/implement_plan/pipeline.py <directory>"
        )
    return step_files


def _apply_resume(
    step_files: list[str], project_dir: str,
) -> list[str]:
    """Drop steps whose ``refactor: <name>`` commit is already on HEAD."""
    done = already_committed_steps(project_dir)
    skipped = [f for f in step_files if Path(f).stem in done]
    remaining = [f for f in step_files if Path(f).stem not in done]
    if skipped:
        print(
            f"[implement-features] resume: skipping {len(skipped)} "
            f"already-committed steps: "
            + ", ".join(Path(f).stem for f in skipped),
            file=sys.stderr,
        )
    return remaining


# ---------------------------------------------------------------------------
# Pipeline structure
# ---------------------------------------------------------------------------


def build_pipeline(plan: ImplementationPlan) -> Pipeline:
    """Assemble the pipeline from an ImplementationPlan."""
    pipeline = (
        Pipeline("implement_plan")
        .alert(MacOSChannel())
        .stage("check clean worktree", clean_worktree(plan.project_dir))
        .stage("preflight toolchain",  preflight(plan.project_dir, "uv", "python3"))
        .stage("record start",         record_start(plan.project_dir))
    )

    for step in plan:
        pipeline.stage(f"implement {step.name}",  plan.implement(step))
        pipeline.loop(f"test {step.name}", max_retries=5, on_exhaust=fail, stages=[
            Stage(f"fix {step.name}",  plan.fix(step),  when=step.test_failed()),
            Stage(f"test {step.name}", plan.test(step)),
            Stage(f"bats {step.name}", plan.bats(step)),
        ])
        pipeline.stage(f"commit {step.name}",     plan.commit(step))
        pipeline.stage(f"summarize {step.name}",  plan.summarize(step))
        pipeline.clear_context()

    pipeline.stage("review",  plan.review())
    pipeline.stage("handoff", plan.handoff())

    return pipeline


# ---------------------------------------------------------------------------
# Entry point — read inputs, discover steps, build pipeline
# ---------------------------------------------------------------------------

project_dir = os.getcwd()
test_cmd = "uv run python -m pytest tests/ -v"
bats_cmd = "bats -r bats/ -v"

feature_dir = _resolve_feature_dir(project_dir, sys.argv[1:])
shared_context, test_cmd, bats_cmd = _load_index(feature_dir, test_cmd, bats_cmd)

step_files = _discover_step_files(feature_dir)
step_files = _apply_resume(step_files, project_dir)

steps = [
    PlanStep.from_file(f, default_test_cmd=test_cmd, default_bats_cmd=bats_cmd)
    for f in step_files
]

plan = ImplementationPlan(
    steps,
    project_dir=project_dir,
    feature_dir=feature_dir,
    shared_context=shared_context,
)

config = build_pipeline(plan)

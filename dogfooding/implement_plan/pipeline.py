"""Implement a plan from step-*.md files.

Usage:
    bin/norn dogfooding/implement_plan/pipeline.py tmp/refactor
    bin/norn dogfooding/implement_plan/pipeline.py tmp/refactor --dry-run
    bin/norn dogfooding/implement_plan/pipeline.py tmp/refactor --skip "commit step-03"
"""

from __future__ import annotations

import os
import sys

from norn.alerts import MacOSChannel
from norn.dsl import Pipeline, Stage, fail

from dogfooding.implement_plan.implementation_plan import ImplementationPlan

PROJECT_DIR = os.getcwd()

metadata = {
    "env_vars": ["ANTHROPIC_API_KEY"],
    "args": {"args": "Path to directory containing step-*.md files"},
}

plan = ImplementationPlan(
    argv=sys.argv[1:],
    project_dir=PROJECT_DIR,
    test_cmd="uv run python -m pytest tests/ -v",
    bats_cmd="bats -r bats/ -v",
)

# --- pipeline structure ----------------------------------------------------

pipeline = (
    Pipeline("implement_plan")
    .alert(MacOSChannel())
    .stage("check clean worktree", plan.clean_worktree())
    .stage("preflight toolchain",  plan.preflight("uv", "python3"))
    .stage("record start",         plan.record_start())
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

config = pipeline

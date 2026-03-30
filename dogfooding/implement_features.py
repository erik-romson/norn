"""Dogfooding pipeline: implement features from step files in a directory.

Reads each step-*.md file in the given directory (sorted by name), injects
shared context from index.md (if present), implements the step, runs tests,
and commits the result before moving to the next file.

Usage:
    bin/norn dogfooding/implement_features.py tmp/refactor
    bin/norn dogfooding/implement_features.py tmp/refactor --dry-run
    bin/norn dogfooding/implement_features.py tmp/refactor --skip "commit step-03-models"
"""

import os
import sys
from glob import glob
from pathlib import Path

from norn.alerts import MacOSChannel
from norn.dsl import Pipeline, Stage, fail, stage_failed
from norn.stages.generate import Generate
from norn.stages.run_command import RunCommand

PROJECT_DIR = os.getcwd()

# --- resolve the target directory from positional args ---
raw_args = sys.argv[1:]  # everything after the config file path
feature_dir = None
for arg in raw_args:
    candidate = Path(arg)
    if candidate.is_dir():
        feature_dir = str(candidate)
        break
    candidate = Path(PROJECT_DIR) / arg
    if candidate.is_dir():
        feature_dir = str(candidate)
        break

if feature_dir is None:
    feature_dir = os.path.join(PROJECT_DIR, "tmp")

# --- load shared context (index.md) if present ---
index_path = Path(feature_dir) / "index.md"
shared_context = ""
if index_path.exists():
    shared_context = (
        "## Shared context (from index.md — applies to every step)\n\n"
        f"{index_path.read_text()}\n\n"
        "---\n\n"
    )

# --- discover step files (step-*.md, sorted) ---
step_files = sorted(glob(os.path.join(feature_dir, "step-*.md")))

if not step_files:
    # fallback: any .md except index.md and README.md
    step_files = sorted(
        f for f in glob(os.path.join(feature_dir, "*.md"))
        if Path(f).name not in ("index.md", "README.md", "refactor-plan.md")
    )

# --- build pipeline ---
pipeline = (
    Pipeline("implement_features")
    .alert(MacOSChannel())
)

for step_file in step_files:
    name = Path(step_file).stem  # e.g. "step-01-scaffold"
    step_content = Path(step_file).read_text()

    test_name = f"test {name}"
    bats_name = f"bats {name}"

    # Step 1: implement the step
    pipeline.stage(
        f"implement {name}",
        Generate(
            prompt=(
                f"## Working directory\n{PROJECT_DIR}\n\n"
                "IMPORTANT: When creating or editing files, always use absolute paths "
                f"based on {PROJECT_DIR}.\n\n"
                f"{shared_context}"
                f"## Step to implement\n\n"
                f"### Source: {step_file}\n\n"
                f"{step_content}\n\n"
                "## Instructions\n"
                "- Read the relevant source files before making changes\n"
                "- Implement exactly what this step describes, nothing more\n"
                "- Follow the existing code style and conventions in the project\n"
                "- No fallbacks or similar — fail fast and hard\n"
                "- Do not change unrelated code\n"
                "- Tests must pass after this step\n"
                "- If this step has no tests, add a placeholder test that always succeeds\n"
            ),
            allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
            permission_mode="acceptEdits",
            cwd=PROJECT_DIR,
            setting_sources=["project"],
        ),
    )

    # Step 2: test + fix loop
    pipeline.loop(
        f"test {name}",
        max_retries=5,
        on_exhaust=fail,
        stages=[
            Stage(f"fix {name}", Generate(
                prompt=(
                    f"## Working directory\n{PROJECT_DIR}\n\n"
                    "IMPORTANT: When creating or editing files, always use absolute paths "
                    f"based on {PROJECT_DIR}.\n\n"
                    f"{shared_context}"
                    "## Fix test failures\n"
                    "The tests failed. Fix the code so the tests pass.\n\n"
                    f"### pytest output\n{{{test_name}.output}}\n\n"
                    f"### bats output\n{{{bats_name}.output}}\n"
                ),
                allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
                permission_mode="acceptEdits",
                cwd=PROJECT_DIR,
                setting_sources=["project"],
            ), when=lambda ctx, t=test_name, b=bats_name: stage_failed(t)(ctx) or stage_failed(b)(ctx)),
            Stage(test_name, RunCommand(
                cmd="uv run python -m pytest tests/ -v",
            )),
            Stage(bats_name, RunCommand(
                cmd="bats -r bats/ -v",
            )),
        ],
    )

    pipeline.stage(
        f"commit {name}",
        RunCommand(
            cmd=(
                f'cd {PROJECT_DIR} && '
                f'git add -A && '
                f'git diff --cached --quiet && echo "nothing to commit" || '
                f'git commit -m "refactor: {name}"'
            ),
        ),
    )

    pipeline.clear_context()

config = pipeline

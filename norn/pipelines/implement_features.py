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

metadata = {
    "env_vars": ["ANTHROPIC_API_KEY"],
    "args": {"args": "Path to directory containing step-*.md files"},
}

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

if not step_files:
    print(
        f"Error: no step-*.md files found in {feature_dir}\n"
        "Usage: norn run implement_features <directory>",
        file=sys.stderr,
    )
    sys.exit(1)

# --- collect all step contents for review/handoff ---
all_steps_summary = ""
for sf in step_files:
    all_steps_summary += f"### {Path(sf).name}\n\n{Path(sf).read_text()}\n\n---\n\n"

# --- build pipeline ---
pipeline = (
    Pipeline("implement_features")
    .alert(MacOSChannel())
)

# Fail early if working tree is dirty
pipeline.stage(
    "check clean worktree",
    RunCommand(cmd=(
        f'cd {PROJECT_DIR} && '
        'if [ -n "$(git status --porcelain)" ]; then '
        'echo "ERROR: Working tree is not clean. Commit or .gitignore these files before running the pipeline:" && '
        'git status --short && exit 1; fi'
    )),
)

# Record the starting commit so review/handoff can diff from it
pipeline.stage(
    "record start",
    RunCommand(cmd=f"cd {PROJECT_DIR} && git rev-parse HEAD"),
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

# --- review: verify all changes match the plan ---
pipeline.stage(
    "review",
    Generate(
        prompt=(
            f"## Working directory\n{PROJECT_DIR}\n\n"
            "IMPORTANT: When creating or editing files, always use absolute paths "
            f"based on {PROJECT_DIR}.\n\n"
            "## Task: Review all implementation changes against the plan\n\n"
            "The starting commit (before any steps were implemented) is:\n"
            "{record start.output}\n\n"
            "Run `git diff {record start.output}..HEAD` and `git log --oneline {record start.output}..HEAD` "
            "to see all changes made during this pipeline run.\n\n"
            f"{shared_context}"
            "## Plan — all steps\n\n"
            f"{all_steps_summary}\n\n"
            "## Instructions\n"
            "1. Read the full diff from the starting commit to HEAD\n"
            "2. For each step in the plan, verify that:\n"
            "   - The implementation matches what was requested\n"
            "   - No unrelated changes were introduced\n"
            "   - Code style and conventions are consistent\n"
            "   - Tests were added where required\n"
            "3. Check for cross-step issues: naming inconsistencies, "
            "duplicated code, missing integrations between steps\n"
            "4. Write the review to a file at "
            f"{feature_dir}/review.md with:\n"
            "   - A summary verdict (pass / pass with notes / needs fixes)\n"
            "   - Per-step compliance checklist\n"
            "   - Any issues found, with file paths and line numbers\n"
            "   - Suggestions for improvement (if any)\n"
        ),
        allowed_tools=["Read", "Glob", "Grep", "Bash"],
        permission_mode="acceptEdits",
        cwd=PROJECT_DIR,
        setting_sources=["project"],
    ),
)

# --- handoff document: summarize all changes ---
pipeline.stage(
    "handoff",
    Generate(
        prompt=(
            f"## Working directory\n{PROJECT_DIR}\n\n"
            "IMPORTANT: When creating or editing files, always use absolute paths "
            f"based on {PROJECT_DIR}.\n\n"
            "## Task: Create a handoff document\n\n"
            "The starting commit (before any steps were implemented) is:\n"
            "{record start.output}\n\n"
            "Run `git diff --stat {record start.output}..HEAD` and "
            "`git log --oneline {record start.output}..HEAD` to see the scope of changes.\n\n"
            f"{shared_context}"
            "## Instructions\n"
            "Create a handoff document at "
            f"{feature_dir}/handoff.md that includes:\n\n"
            "1. **Overview** — what was built and why (1-2 paragraphs)\n"
            "2. **Changes summary** — list of all files added/modified/deleted, "
            "grouped by feature area\n"
            "3. **New functionality** — what the user can now do that they couldn't before, "
            "with usage examples or commands where applicable\n"
            "4. **Architecture decisions** — key design choices made during implementation\n"
            "5. **Configuration** — any new env vars, config files, or settings introduced\n"
            "6. **Testing** — what tests were added and how to run them\n"
            "7. **Known limitations** — anything not implemented, deferred, or requiring "
            "follow-up work\n"
            "8. **Dependencies** — any new dependencies added\n\n"
            "Read the actual changed files to understand what was built — "
            "don't just summarize the plan, summarize the implementation.\n"
        ),
        allowed_tools=["Read", "Glob", "Grep", "Bash"],
        permission_mode="acceptEdits",
        cwd=PROJECT_DIR,
        setting_sources=["project"],
    ),
)

config = pipeline

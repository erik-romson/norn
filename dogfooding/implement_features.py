"""Dogfooding pipeline: implement features from step files in a directory.

Reads each step-*.md file in the given directory (sorted by name), injects
shared context from index.md (if present), implements the step, runs tests,
and commits the result before moving to the next file.

Features:
- `metadata` block for bin/norn discovery
- `check clean worktree` gate — stops untracked files from leaking into commits
- Pre-flight toolchain check (uv, python) before any expensive Generate call
- `record start` captures the starting SHA for later diffs
- `review` and `handoff` stages at the end
- Per-feature `test_cmd` override in index.md front-matter:
      ---
      test_cmd: uv run python -m pytest tests/test_foo.py -v
      ---
- Per-step `test_cmd` override in step-NN.md front-matter (falls back to feature,
  then to repo default). Also supports per-step `paths:` to scope git add:
      ---
      test_cmd: uv run python -m pytest tests/test_foo.py -v
      paths:
        - norn/stages/
        - tests/test_stages.py
      ---
- Scoped git add — defaults to `git add -u` (tracked files only), plus any
  `paths:` declared in step front-matter. Stops new untracked junk from leaking
  into commits.
- Commit message subject pulled from the first H1 in the step file.
- Resume support: any step whose `refactor: <name>` commit is already on HEAD
  is skipped at pipeline build time.

Usage:
    bin/norn dogfooding/implement_features.py tmp/refactor
    bin/norn dogfooding/implement_features.py tmp/refactor --dry-run
    bin/norn dogfooding/implement_features.py tmp/refactor --skip "commit step-03-models"
"""

import os
import re
import shlex
import subprocess
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


# --- helpers ---------------------------------------------------------------

def parse_front_matter(text: str) -> tuple[dict, str]:
    """Parse a tiny subset of YAML front-matter: `key: value` and `key:`
    + indented `- item` lists.  Returns (dict, body)."""
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    if not m:
        return {}, text
    block, body = m.group(1), m.group(2)
    data: dict = {}
    current_list_key: str | None = None
    for raw_line in block.splitlines():
        if not raw_line.strip():
            current_list_key = None
            continue
        if raw_line.lstrip().startswith("- ") and current_list_key:
            data[current_list_key].append(raw_line.lstrip()[2:].strip())
            continue
        if ":" in raw_line:
            k, v = raw_line.split(":", 1)
            k, v = k.strip(), v.strip()
            if v == "":
                data[k] = []
                current_list_key = k
            else:
                data[k] = v
                current_list_key = None
    return data, body


def first_h1(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def default_test_cmd(project_dir: str) -> str:
    """Choose a conservative repo-level validation default from common markers."""
    root = Path(project_dir)
    if (root / "pom.xml").exists():
        return "mvn -q test"
    if (root / "gradlew").exists():
        return "./gradlew test"
    if (root / "package.json").exists():
        return "npm test -- --runInBand"
    if (root / "pyproject.toml").exists() or (root / "tests").exists():
        return "uv run python -m pytest tests/ -v"
    return "true"


def default_bats_cmd(project_dir: str) -> str:
    root = Path(project_dir)
    return "bats -r bats/ -v" if (root / "bats").is_dir() else "true"


def command_executable(cmd: str) -> str | None:
    """Extract the executable name from a simple shell command."""
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return None
    if not argv:
        return None
    exe = argv[0]
    if exe in {"true", ":"}:
        return None
    return exe


def command_probe(cmd: str) -> str | None:
    exe = command_executable(cmd)
    if not exe:
        return None
    if "/" in exe:
        return (
            f'test -x {shlex.quote(exe)} || '
            f'{{ echo "ERROR: {exe} is not executable"; exit 1; }}'
        )
    return (
        f'command -v {shlex.quote(exe)} >/dev/null || '
        f'{{ echo "ERROR: {exe} not on PATH"; exit 1; }}'
    )


def already_committed_steps() -> set[str]:
    """Return step names whose `refactor: <name>` commit is already on HEAD.
    Used for resume support — those steps are skipped at build time."""
    try:
        out = subprocess.check_output(
            ["git", "-C", PROJECT_DIR, "log", "--pretty=%s", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return set()
    done = set()
    for subject in out.splitlines():
        m = re.match(r"^refactor:\s+(\S+)", subject)
        if m:
            done.add(m.group(1))
    return done


# --- resolve the target directory from positional args --------------------

raw_args = sys.argv[1:]
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


# --- load shared context (index.md) and optional front-matter -------------

index_path = Path(feature_dir) / "index.md"
shared_context = ""
feature_test_cmd = default_test_cmd(PROJECT_DIR)
feature_bats_cmd = default_bats_cmd(PROJECT_DIR)

if index_path.exists():
    fm, body = parse_front_matter(index_path.read_text())
    if isinstance(fm.get("test_cmd"), str):
        feature_test_cmd = fm["test_cmd"]
    if isinstance(fm.get("bats_cmd"), str):
        feature_bats_cmd = fm["bats_cmd"]
    shared_context = (
        "## Shared context (from index.md — applies to every step)\n\n"
        f"{body}\n\n"
        "---\n\n"
    )


# --- discover step files ---------------------------------------------------

step_files = sorted(glob(os.path.join(feature_dir, "step-*.md")))

if not step_files:
    # fallback: any .md except index.md and README.md
    step_files = sorted(
        f for f in glob(os.path.join(feature_dir, "*.md"))
        if Path(f).name not in ("index.md", "README.md", "refactor-plan.md")
    )

if not step_files:
    from norn.runner import PipelineError

    raise PipelineError(
        f"No step-*.md files found in {feature_dir}\n"
        "Usage: norn run dogfooding/implement_features.py <directory>"
    )

validation_commands = [feature_test_cmd, feature_bats_cmd]
for step_file in step_files:
    step_fm, _ = parse_front_matter(Path(step_file).read_text())
    if isinstance(step_fm.get("test_cmd"), str):
        validation_commands.append(step_fm["test_cmd"])
    if isinstance(step_fm.get("bats_cmd"), str):
        validation_commands.append(step_fm["bats_cmd"])

preflight_checks: list[str] = []
seen_checks: set[str] = set()
for cmd in validation_commands:
    probe = command_probe(cmd)
    if probe and probe not in seen_checks:
        preflight_checks.append(probe)
        seen_checks.add(probe)

# --- resume: drop steps whose commit is already on HEAD --------------------

done = already_committed_steps()
skipped_for_resume = [f for f in step_files if Path(f).stem in done]
step_files = [f for f in step_files if Path(f).stem not in done]

if skipped_for_resume:
    print(
        f"[implement-features] resume: skipping {len(skipped_for_resume)} "
        f"already-committed steps: "
        + ", ".join(Path(f).stem for f in skipped_for_resume),
        file=sys.stderr,
    )

# --- collect all step contents for review/handoff -------------------------

all_steps_summary = ""
for sf in step_files:
    all_steps_summary += f"### {Path(sf).name}\n\n{Path(sf).read_text()}\n\n---\n\n"


# --- build pipeline --------------------------------------------------------

pipeline = (
    Pipeline("implement_features", default_model="sonnet")
    .alert(MacOSChannel())
)

# Fail early if working tree is dirty.
pipeline.stage(
    "check clean worktree",
    RunCommand(cmd=(
        f'cd {shlex.quote(PROJECT_DIR)} && '
        'if [ -n "$(git status --porcelain)" ]; then '
        'echo "ERROR: Working tree is not clean. Commit or .gitignore these files:" && '
        'git status --short && exit 1; fi'
    )),
)

# Pre-flight: validate tools required by the configured test commands.
pipeline.stage(
    "preflight toolchain",
    RunCommand(cmd=(
        f'cd {shlex.quote(PROJECT_DIR)} && '
        + " && ".join(preflight_checks or ["true"])
    )),
)

# Record the starting commit so review/handoff can diff from it.
pipeline.stage(
    "record start",
    RunCommand(cmd=f"cd {shlex.quote(PROJECT_DIR)} && git rev-parse HEAD"),
)

prior_summaries = ""  # accumulates {summarize <name>.output} placeholders

for step_file in step_files:
    name = Path(step_file).stem
    raw_step_text = Path(step_file).read_text()
    step_fm, step_body = parse_front_matter(raw_step_text)

    # Per-step test_cmd override: step > feature > default.
    test_cmd = step_fm.get("test_cmd") if isinstance(step_fm.get("test_cmd"), str) else None
    test_cmd = test_cmd or feature_test_cmd

    bats_cmd = step_fm.get("bats_cmd") if isinstance(step_fm.get("bats_cmd"), str) else None
    bats_cmd = bats_cmd or feature_bats_cmd

    # Per-step paths for scoped git add. Always include `git add -u` so renames
    # and modifications to tracked files are picked up.
    extra_paths = step_fm.get("paths") if isinstance(step_fm.get("paths"), list) else []
    add_cmd_parts = ["git add -u"]
    for p in extra_paths:
        add_cmd_parts.append(f"git add {shlex.quote(p)}")
    add_cmd = " && ".join(add_cmd_parts)

    # Commit subject: first H1 from the step body, falling back to the stem.
    h1 = first_h1(step_body) or name
    commit_subject = f"refactor: {name} — {h1}" if h1 != name else f"refactor: {name}"

    test_name = f"test {name}"
    bats_name = f"bats {name}"

    # Build the prior-steps context block (empty for the first step).
    prior_context = ""
    if prior_summaries:
        prior_context = (
            "## What was done in prior steps\n\n"
            f"{prior_summaries}\n\n"
        )

    # Step 1: implement the step.
    pipeline.stage(
        f"implement {name}",
        Generate(
            prompt=(
                f"## Working directory\n{PROJECT_DIR}\n\n"
                "IMPORTANT: When creating or editing files, always use absolute paths "
                f"based on {PROJECT_DIR}.\n\n"
                f"{shared_context}"
                f"{prior_context}"
                f"## Step to implement\n\n"
                f"### Source: {step_file}\n\n"
                f"{step_body}\n\n"
                "## Instructions\n"
                "- Read the relevant source files before making changes\n"
                "- Use context7 to check that versions and similar are up-to-date\n"
                "- Implement exactly what this step describes, nothing more\n"
                "- Follow the existing code style and conventions in the project\n"
                "- No fallbacks or similar — fail fast and hard\n"
                "- Do not change unrelated code\n"
                "- Tests must pass after this step\n"
                "- Use the configured test command as the validation contract for this step\n"
                "- Add or update tests when the step changes behavior or introduces logic that should be covered\n"
                "- Do not add placeholder tests that always succeed\n"
            ),
            allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
            permission_mode="acceptEdits",
            cwd=PROJECT_DIR,
            setting_sources=["project"],
        ),
    )

    # Step 2: test + fix loop.
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
                    f"### test_cmd output\n{{{test_name}.output}}\n\n"
                    f"### bats_cmd output\n{{{bats_name}.output}}\n"
                ),
                allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
                permission_mode="acceptEdits",
                cwd=PROJECT_DIR,
                setting_sources=["project"],
            ), when=lambda ctx, t=test_name, b=bats_name: stage_failed(t)(ctx) or stage_failed(b)(ctx)),
            Stage(test_name, RunCommand(
                cmd=f"cd {shlex.quote(PROJECT_DIR)} && {test_cmd}",
            )),
            Stage(bats_name, RunCommand(
                cmd=f"cd {shlex.quote(PROJECT_DIR)} && {bats_cmd}",
            )),
        ],
    )

    # Step 3: scoped commit. Uses git commit -F - so the subject is safe no
    # matter what characters the H1 contained.
    pipeline.stage(
        f"commit {name}",
        RunCommand(
            cmd=(
                f'cd {shlex.quote(PROJECT_DIR)} && '
                f'{add_cmd} && '
                f'(git diff --cached --quiet && echo "nothing to commit") || '
                f'printf %s {shlex.quote(commit_subject)} | git commit -F -'
            ),
        ),
    )

    # Step 4: summarize what was done — feeds into the next step's context.
    summarize_name = f"summarize {name}"
    pipeline.stage(
        summarize_name,
        Generate(
            prompt=(
                f"## Working directory\n{PROJECT_DIR}\n\n"
                f"You just implemented step `{name}` from:\n"
                f"### Source: {step_file}\n\n"
                f"{step_body}\n\n"
                "## Task\n"
                "Write a concise summary (3-5 bullet points) of what was actually "
                "implemented in this step. Focus on:\n"
                "- What files were created or modified\n"
                "- Key design decisions made\n"
                "- Any deviations from the step description\n"
                "- Important details the next step's implementer should know\n\n"
                "Output ONLY the bullet points, no preamble.\n"
            ),
            model="haiku",
            allowed_tools=["Read", "Glob", "Grep", "Bash"],
            permission_mode="acceptEdits",
            cwd=PROJECT_DIR,
            setting_sources=["project"],
        ),
    )

    # Grow the running summary for subsequent steps.
    prior_summaries += f"### {name}\n{{{summarize_name}.output}}\n\n"

    pipeline.clear_context()

# --- review: verify all changes match the plan ----------------------------
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
            "Run `git diff {record start.output}..HEAD` and "
            "`git log --oneline {record start.output}..HEAD` "
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

# --- handoff document: summarize all changes ------------------------------
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
            "3. **New functionality** — what the user can now do that they couldn't before\n"
            "4. **Architecture decisions** — key design choices made during implementation\n"
            "5. **Configuration** — any new env vars, config files, or settings introduced\n"
            "6. **Testing** — what tests were added and how to run them\n"
            "7. **Known limitations** — anything deferred or requiring follow-up work\n"
            "8. **Dependencies** — any new dependencies added\n\n"
            "Read the actual changed files to understand what was built — "
            "don't just summarize the plan, summarize the implementation.\n"
        ),
        model="haiku",
        allowed_tools=["Read", "Glob", "Grep", "Bash"],
        permission_mode="acceptEdits",
        cwd=PROJECT_DIR,
        setting_sources=["project"],
    ),
)

config = pipeline

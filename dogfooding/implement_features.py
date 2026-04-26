"""Dogfooding pipeline: implement features from step files in a directory.

Reads each step-*.md file in the given directory (sorted by name), injects
shared context from index.md (if present), implements the step, runs tests,
and commits the result before moving to the next file.

Features:
- `metadata` block for bin/norn discovery
- Per-step worktree snapshot — captures `git status --porcelain` before the
  step runs and again before commit. The diff between the two is the exact
  list of paths changed during this step, which is what gets committed.
  Pre-existing dirty files (whose status didn't change) are NOT swept into
  the commit, so a dirty worktree no longer blocks the run.
- Pre-flight toolchain check (uv, python) before any expensive Generate call
- `record start` captures the starting SHA for later diffs
- `review` and `handoff` stages at the end
- Per-feature `test_cmd` override in index.md front-matter:
      ---
      test_cmd: uv run python -m pytest tests/test_foo.py -v
      ---
- Per-step `test_cmd` override in step-NN.md front-matter (falls back to feature,
  then to repo default):
      ---
      test_cmd: uv run python -m pytest tests/test_foo.py -v
      ---
- Commit message subject pulled from the first H1 in the step file.
- Resume support: any step whose `refactor: <name>` commit is already on HEAD
  is skipped at pipeline build time.

Usage:
    bin/norn dogfooding/implement_features.py tmp/refactor
    bin/norn dogfooding/implement_features.py tmp/refactor --dry-run
    bin/norn dogfooding/implement_features.py tmp/refactor --skip "commit step-03-models"
"""

import hashlib
import os
import re
import shlex
import subprocess
import sys
import tempfile
from glob import glob
from pathlib import Path

from norn.alerts import MacOSChannel
from norn.dsl import Pipeline, Stage, fail, stage_failed
from norn.stages.compress_test_log import CompressTestLog
from norn.stages.generate import Generate
from norn.stages.run_command import RunCommand

PROJECT_DIR = os.getcwd()


def _git_toplevel(start: str) -> str:
    """Resolve the git toplevel for ``start`` so all git operations can run
    from a single, stable cwd. Falls back to ``start`` when not in a repo.

    Pinning git operations to the toplevel removes whole categories of
    "git status output is rooted differently than git add expects" bugs
    that otherwise show up when PROJECT_DIR is a subdirectory of the
    repo (e.g. a `jupyter/` subfolder of a larger project)."""
    try:
        out = subprocess.check_output(
            ["git", "-C", start, "rev-parse", "--show-toplevel"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        return out or start
    except (subprocess.CalledProcessError, FileNotFoundError):
        return start


GIT_TOPLEVEL = _git_toplevel(PROJECT_DIR)

# Snapshot location: outside PROJECT_DIR so the snapshot files themselves
# never appear in `git status` output. Deterministic per feature_dir so
# resume can be made aware of prior runs if needed.
SNAPSHOT_HELPER = str(Path(__file__).parent / "_snapshot_diff.py")

metadata = {
    "env_vars": ["ANTHROPIC_API_KEY"],
    "args": {"args": "Path to directory containing step-*.md files"},
}


# --- helpers ---------------------------------------------------------------

def parse_front_matter(text: str) -> tuple[dict, str]:
    """Parse YAML front-matter delimited by ``---`` lines.

    Returns ``(data, body)``. ``data`` is the parsed mapping (empty dict
    if no front-matter), ``body`` is the remainder of the document.
    ``data`` is normalised to a dict so callers can ``.get(...)`` even
    on empty front-matter.
    """
    import yaml

    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    if not m:
        return {}, text
    parsed = yaml.safe_load(m.group(1))
    data = parsed if isinstance(parsed, dict) else {}
    return data, m.group(2)


def first_h1(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def _resolve_test_cmd(
    step_fm: dict,
    step_file: str,
    feature_test_cmd: str | None,
) -> str:
    """Resolve a step's ``test_cmd`` from its front-matter, falling back to
    the feature-level ``index.md`` value. Fails fast when neither is set
    so the agent can't silently run a guessed command.

    The split-plan skill is responsible for putting a real ``test_cmd``
    in every step file — see ``.claude/skills/split-plan/SKILL.md``.
    """
    raw = step_fm.get("test_cmd")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    if feature_test_cmd:
        return feature_test_cmd
    from norn.runner import PipelineError

    raise PipelineError(
        f"{step_file}: missing required `test_cmd:` in front-matter and no "
        f"feature-level fallback in index.md.\n"
        f"Each step must declare the command that validates it. "
        f"Re-run /split-plan to regenerate the steps with explicit test_cmd "
        f"values."
    )


def _resolve_bats_cmd(step_fm: dict, feature_bats_cmd: str | None) -> str | None:
    """Per-step ``bats_cmd`` is optional. Returns ``None`` when neither
    the step nor the feature declares one — the bats stage is then
    skipped entirely (no placeholder/no guess)."""
    raw = step_fm.get("bats_cmd")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return feature_bats_cmd


def command_executable(cmd: str) -> str | None:
    """Extract the real executable name from a shell command.

    Walks past subshell parens, ``cd <dir> &&`` wrappers, env-var
    assignments, and shell separators so we find the actual binary
    being invoked. For ``(cd client && flutter test)`` returns
    ``flutter``; for ``bash bin/run.sh`` returns ``bash``. Returns
    ``None`` when no plausible executable can be found.
    """
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return None

    separators = {"&&", "||", ";", "|", "&", "(", "{", ")", "}"}
    i = 0
    while i < len(argv):
        tok = argv[i]
        while tok and tok[0] in "({":
            tok = tok[1:]
        while tok and tok[-1] in ")}":
            tok = tok[:-1]
        if not tok or tok in separators:
            i += 1
            continue
        if tok == "cd" and i + 1 < len(argv):
            i += 2
            continue
        if tok in {"true", ":"}:
            return None
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tok):
            i += 1
            continue
        if re.fullmatch(r"[\w./-]+", tok):
            return tok
        return None
    return None


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

# Bump SNAPSHOT_VERSION whenever the snapshot file format changes (e.g. when
# we adjust git flags or change the cwd we run git from) so prior runs'
# caches are invalidated automatically.
SNAPSHOT_VERSION = "v3"
snapshot_root = os.path.join(
    tempfile.gettempdir(),
    "norn-snapshots",
    SNAPSHOT_VERSION,
    hashlib.sha1(feature_dir.encode()).hexdigest()[:12],
)
os.makedirs(snapshot_root, exist_ok=True)


# --- load shared context (index.md) and optional front-matter -------------

index_path = Path(feature_dir) / "index.md"
shared_context = ""
feature_test_cmd: str | None = None
feature_bats_cmd: str | None = None

if index_path.exists():
    fm, body = parse_front_matter(index_path.read_text())
    if isinstance(fm.get("test_cmd"), str) and fm["test_cmd"].strip():
        feature_test_cmd = fm["test_cmd"].strip()
    if isinstance(fm.get("bats_cmd"), str) and fm["bats_cmd"].strip():
        feature_bats_cmd = fm["bats_cmd"].strip()
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

validation_commands: list[str] = []
if feature_test_cmd:
    validation_commands.append(feature_test_cmd)
if feature_bats_cmd:
    validation_commands.append(feature_bats_cmd)
for step_file in step_files:
    step_fm, _ = parse_front_matter(Path(step_file).read_text())
    # Resolve eagerly so a missing test_cmd fails the whole run before any
    # expensive Generate stage starts. Bats is optional so we only collect
    # it when present.
    validation_commands.append(_resolve_test_cmd(step_fm, step_file, feature_test_cmd))
    bats = _resolve_bats_cmd(step_fm, feature_bats_cmd)
    if bats:
        validation_commands.append(bats)

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

    # Required: each step declares the validation contract in its
    # front-matter, no project-marker guessing. _resolve_test_cmd raises
    # if neither the step nor index.md provides one — see
    # .claude/skills/split-plan/SKILL.md.
    test_cmd = _resolve_test_cmd(step_fm, step_file, feature_test_cmd)
    bats_cmd = _resolve_bats_cmd(step_fm, feature_bats_cmd)

    # Per-step model override. Steps that need heavier reasoning can opt in
    # to opus via `model: opus` in their front-matter; everything else falls
    # back to the pipeline default (sonnet).
    step_model = step_fm.get("model") or None

    # Commit subject: first H1 from the step body, falling back to the stem.
    h1 = first_h1(step_body) or name
    commit_subject = f"refactor: {name} — {h1}" if h1 != name else f"refactor: {name}"

    test_name = f"test {name}"
    bats_name = f"bats {name}"

    pre_snapshot = os.path.join(snapshot_root, f"{name}.pre")
    post_snapshot = os.path.join(snapshot_root, f"{name}.post")
    changed_list = os.path.join(snapshot_root, f"{name}.changed")

    # Snapshot the worktree state before the step runs. The commit stage
    # below diffs this against the post-step snapshot to figure out exactly
    # which paths to stage.
    pipeline.stage(
        f"snapshot pre {name}",
        RunCommand(cmd=(
            f'cd {shlex.quote(GIT_TOPLEVEL)} && '
            f'git status --porcelain -uall > {shlex.quote(pre_snapshot)}'
        )),
    )

    # Build the prior-steps context block (empty for the first step).
    prior_context = ""
    if prior_summaries:
        prior_context = (
            "## What was done in prior steps\n\n"
            f"{prior_summaries}\n\n"
        )

    # Step 1: implement the step.
    # Defaults to the pipeline-level model (sonnet). Steps that need heavier
    # reasoning can opt into opus via `model: opus` in their front-matter —
    # see .claude/skills/split-plan/SKILL.md for the convention.
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
            allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash", "Task"],
            permission_mode="acceptEdits",
            cwd=PROJECT_DIR,
            setting_sources=["project"],
            model=step_model,
        ),
    )

    # Step 2: test + fix loop.
    #
    # Loop layout (do-while):
    #   1. compress {test_name}      — pulls failures from the previous
    #                                   iteration's run; "" when source
    #                                   succeeded or hasn't run yet
    #   2. compress {bats_name}      — same, only added when bats_cmd is set
    #   3. fix {name}                — only when a prior iter failed; reads
    #                                   the compressed outputs above
    #   4. {test_name}               — actual test run (declared by the step)
    #   5. {bats_name}               — optional bats integration tests
    #
    # CompressTestLog skips on success by default, so the fix prompt only
    # ever sees real failure context — no passing-pytest noise carrying
    # over to subsequent iterations.
    fix_when = (
        (lambda ctx, t=test_name, b=bats_name: stage_failed(t)(ctx) or stage_failed(b)(ctx))
        if bats_cmd
        else (lambda ctx, t=test_name: stage_failed(t)(ctx))
    )
    fix_prompt_parts = [
        f"## Working directory\n{PROJECT_DIR}\n\n",
        "IMPORTANT: When creating or editing files, always use absolute paths "
        f"based on {PROJECT_DIR}.\n\n",
        shared_context,
        "## Fix test failures\n",
        "The tests failed. Fix the code so the tests pass.\n\n",
        f"### test_cmd output (compressed)\n{{compress {test_name}.output}}\n\n",
    ]
    loop_stages: list[Stage] = [
        Stage(f"compress {test_name}", CompressTestLog(source_stage=test_name)),
    ]
    if bats_cmd:
        loop_stages.append(
            Stage(f"compress {bats_name}", CompressTestLog(source_stage=bats_name)),
        )
        fix_prompt_parts.append(
            f"### bats_cmd output (compressed)\n{{compress {bats_name}.output}}\n",
        )
    loop_stages.append(
        Stage(f"fix {name}", Generate(
            prompt="".join(fix_prompt_parts),
            allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash", "Task"],
            permission_mode="acceptEdits",
            cwd=PROJECT_DIR,
            setting_sources=["project"],
            model=step_model,
        ), when=fix_when),
    )
    # The actual test command comes verbatim from the step front-matter
    # (or feature index.md), never from a guessed project default.
    loop_stages.append(
        Stage(test_name, RunCommand(
            cmd=f"cd {shlex.quote(PROJECT_DIR)} && {test_cmd}",
        )),
    )
    if bats_cmd:
        loop_stages.append(
            Stage(bats_name, RunCommand(
                cmd=f"cd {shlex.quote(PROJECT_DIR)} && {bats_cmd}",
            )),
        )

    pipeline.loop(
        f"test {name}",
        max_retries=5,
        on_exhaust=fail,
        stages=loop_stages,
    )

    # Step 3: snapshot-scoped commit.
    #
    # Take a post-step snapshot, diff it against the pre-step snapshot to
    # get the exact list of paths whose porcelain status changed during the
    # step, then `git add -A --` only those paths. Pre-existing dirty files
    # whose status is unchanged are left untouched.
    #
    # Uses `git commit -F -` so the subject is safe regardless of what
    # characters the H1 contained.
    pipeline.stage(
        f"commit {name}",
        RunCommand(
            cmd=(
                f'cd {shlex.quote(GIT_TOPLEVEL)} && '
                f'if [ ! -f {shlex.quote(pre_snapshot)} ]; then '
                f'echo "ERROR: pre-snapshot missing at {pre_snapshot}." 1>&2; '
                f'echo "This usually means a stale checkpoint is being resumed '
                f'after the snapshot format changed." 1>&2; '
                f'echo "Fix: rm {os.path.dirname(snapshot_root)}/* and rerun '
                f'without --resume (already-committed steps are auto-skipped)." 1>&2; '
                f'exit 1; fi && '
                f'git status --porcelain -uall > {shlex.quote(post_snapshot)} && '
                f'python3 {shlex.quote(SNAPSHOT_HELPER)} '
                f'{shlex.quote(pre_snapshot)} {shlex.quote(post_snapshot)} '
                f'> {shlex.quote(changed_list)} && '
                f'if [ ! -s {shlex.quote(changed_list)} ]; then '
                f'echo "no files changed during this step"; exit 0; fi && '
                f'tr "\\n" "\\0" < {shlex.quote(changed_list)} | '
                f'xargs -0 git add -A -- && '
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

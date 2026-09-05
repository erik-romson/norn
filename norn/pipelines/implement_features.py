"""Implement features from step files in a directory.

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
    norn run implement_features tmp/refactor
    norn run implement_features tmp/refactor --dry-run
    norn run implement_features tmp/refactor --skip "commit step-03-models"
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
from norn.dsl import Pipeline, Stage, ask_user, stage_failed
from norn.stages.compress_test_log import CompressTestLog
from norn.stages.generate import Generate
from norn.stages.run_command import RunCommand

# Pinned to the launch directory at import time. This pipeline is a
# self-modifying pipeline that must target the repo it was launched from: its
# RunCommand stages `cd {PROJECT_DIR}` explicitly and its git snapshots run
# against this tree. It is therefore NOT worktree-isolated — running it with
# the TUI worktree toggle will still execute against the launch directory.
PROJECT_DIR = os.getcwd()

# Upper bound (seconds) for a single test/bats run inside the do-while loop.
# A hung command — e.g. one that backgrounds a server that inherits the
# capture pipe — fails at this mark instead of wedging the pipeline. Sits
# comfortably under norn's RunCommand backstop (1h) and is overridable per
# step via `test_timeout:` / `bats_timeout:` in front-matter (or feature-wide
# in index.md) for heavier steps such as Docker builds or full-app scans.
DEFAULT_TEST_TIMEOUT = 1800


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

    # Config error at pipeline-build time — raise ValueError so the CLI's
    # _load_pipeline handler prints it cleanly and exits 1. (PipelineError is
    # for stage failures and requires a StageResult.)
    raise ValueError(
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


def _coerce_timeout(raw: object, where: str) -> float | None:
    """Parse an optional command timeout (seconds) from front-matter.

    Returns ``None`` when unset so callers can fall back to a feature-level
    value or the pipeline default. Fails fast (ValueError, printed cleanly by
    the CLI's ``_load_pipeline`` handler) on a non-numeric or non-positive
    value rather than silently ignoring a mistyped ``test_timeout:``.
    """
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{where}: expected a number of seconds, got {raw!r}")
    if value <= 0:
        raise ValueError(f"{where}: must be a positive number of seconds, got {value!r}")
    return value


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
feature_test_timeout: float | None = None
feature_bats_timeout: float | None = None

if index_path.exists():
    fm, body = parse_front_matter(index_path.read_text())
    if isinstance(fm.get("test_cmd"), str) and fm["test_cmd"].strip():
        feature_test_cmd = fm["test_cmd"].strip()
    if isinstance(fm.get("bats_cmd"), str) and fm["bats_cmd"].strip():
        feature_bats_cmd = fm["bats_cmd"].strip()
    feature_test_timeout = _coerce_timeout(fm.get("test_timeout"), f"{index_path}: test_timeout")
    feature_bats_timeout = _coerce_timeout(fm.get("bats_timeout"), f"{index_path}: bats_timeout")
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
    # Config error — ValueError is caught and printed cleanly by the CLI's
    # _load_pipeline handler (PipelineError needs a StageResult).
    raise ValueError(
        f"No step-*.md files found in {feature_dir}\n"
        "Usage: norn run implement_features <directory>"
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

    # Per-step run timeout (seconds), falling back to the feature-level value
    # then the pipeline default. Steps with long legitimate runs raise it via
    # `test_timeout:` / `bats_timeout:` in their front-matter. Valid timeouts
    # are positive, so the `or` chain only skips the unset (None) levels.
    test_timeout = (
        _coerce_timeout(step_fm.get("test_timeout"), f"{step_file}: test_timeout")
        or feature_test_timeout
        or DEFAULT_TEST_TIMEOUT
    )
    bats_timeout = (
        _coerce_timeout(step_fm.get("bats_timeout"), f"{step_file}: bats_timeout")
        or feature_bats_timeout
        or DEFAULT_TEST_TIMEOUT
    )

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
    hookfix_status = os.path.join(snapshot_root, f"{name}.hookfix.status")
    hookfix_list = os.path.join(snapshot_root, f"{name}.hookfix")

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
            setting_sources=["project"],
            model=step_model,
        ),
        # A failed implement (e.g. a transient agent/SDK error) prompts for
        # retry/continue/abort rather than failing the whole run outright.
        on_failure=ask_user,
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
            setting_sources=["project"],
            model=step_model,
        ), when=fix_when),
    )
    # The actual test command comes verbatim from the step front-matter
    # (or feature index.md), never from a guessed project default.
    loop_stages.append(
        Stage(test_name, RunCommand(
            cmd=f"cd {shlex.quote(PROJECT_DIR)} && {test_cmd}",
            timeout=test_timeout,
        )),
    )
    if bats_cmd:
        loop_stages.append(
            Stage(bats_name, RunCommand(
                cmd=f"cd {shlex.quote(PROJECT_DIR)} && {bats_cmd}",
                timeout=bats_timeout,
            )),
        )

    pipeline.loop(
        f"test {name}",
        max_retries=5,
        # After 5 failed attempts, prompt instead of dying: the TUI shows the
        # retry/continue/abort modal (norn run prints the same prompt). Retry
        # runs the whole loop again; abort fails the run. Continue is risky
        # here — it proceeds to `commit {name}`, committing failing tests.
        on_exhaust=ask_user,
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
    #
    # Auto-fixing pre-commit hooks (ruff-format, `ruff --fix`, black, prettier)
    # rewrite a staged file and then reject the commit. That leaves the file
    # *partially staged*, which additionally makes pre-commit's stash of the
    # unstaged delta collide with its own edit ("Stashed changes conflicted with
    # hook auto-fixes ... Rolling back fixes"), so a plain retry reproduces the
    # failure forever. `restage_hook_fixes` re-stages exactly what the hooks
    # touched, read from *git state* rather than from `changed_list`: hooks run
    # against the whole staged set, so the rewritten file is often one an
    # earlier step staged and this step's own path list would never re-add it.
    restage_hook_fixes = (
        f'git status --porcelain -uall > {shlex.quote(hookfix_status)} || exit 1; '
        f'python3 {shlex.quote(SNAPSHOT_HELPER)} --hook-fixes '
        f'{shlex.quote(hookfix_status)} > {shlex.quote(hookfix_list)} || exit 1; '
        f'if [ -s {shlex.quote(hookfix_list)} ]; then '
        f'echo "re-staging files rewritten by pre-commit hooks:" 1>&2; '
        f'sed "s/^/  /" {shlex.quote(hookfix_list)} 1>&2; '
        f'tr "\\n" "\\0" < {shlex.quote(hookfix_list)} | '
        f'xargs -0 git add -- || exit 1; fi; '
    )
    commit_once = f'printf %s {shlex.quote(commit_subject)} | git commit -F -'

    pipeline.stage(
        f"commit {name}",
        RunCommand(
            cmd=(
                f'cd {shlex.quote(GIT_TOPLEVEL)} || exit 1; '
                f'if [ ! -f {shlex.quote(pre_snapshot)} ]; then '
                f'echo "ERROR: pre-snapshot missing at {pre_snapshot}." 1>&2; '
                f'echo "This usually means a stale checkpoint is being resumed '
                f'after the snapshot format changed." 1>&2; '
                f'echo "Fix: rm {os.path.dirname(snapshot_root)}/* and rerun '
                f'without --resume (already-committed steps are auto-skipped)." 1>&2; '
                f'exit 1; fi; '
                f'git status --porcelain -uall > {shlex.quote(post_snapshot)} || exit 1; '
                f'python3 {shlex.quote(SNAPSHOT_HELPER)} '
                f'{shlex.quote(pre_snapshot)} {shlex.quote(post_snapshot)} '
                f'> {shlex.quote(changed_list)} || exit 1; '
                f'if [ ! -s {shlex.quote(changed_list)} ]; then '
                f'echo "no files changed during this step"; exit 0; fi; '
                # A failing `git add` must abort, never fall through to the
                # commit: `(A && B) || C` used to commit anyway.
                f'if ! tr "\\n" "\\0" < {shlex.quote(changed_list)} | '
                f'xargs -0 git add -A --; then '
                f'echo "ERROR: git add failed for the changed paths of this step" 1>&2; '
                f'exit 1; fi; '
                # Clear partial staging left behind by an earlier rejected
                # commit, so pre-commit has nothing to stash and cannot collide
                # with its own fixes on this attempt.
                f'{restage_hook_fixes}'
                f'if git diff --cached --quiet; then '
                f'echo "nothing to commit"; exit 0; fi; '
                f'if {commit_once}; then exit 0; fi; '
                f'echo "commit rejected; re-staging pre-commit hook fixes and '
                f'retrying once" 1>&2; '
                f'{restage_hook_fixes}'
                f'if {commit_once}; then exit 0; fi; '
                f'echo "ERROR: commit failed twice." 1>&2; '
                f'echo "If hooks are still reformatting files, run the formatter '
                f'over the repo, stage the result, and retry." 1>&2; '
                f'echo "Otherwise a hook is genuinely failing (e.g. pyright / '
                f'pytest) and needs a real fix." 1>&2; '
                f'exit 1'
            ),
        ),
        # A failed commit prompts for retry/continue/abort instead of dying.
        on_failure=ask_user,
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
        setting_sources=["project"],
    ),
    # A failed review (e.g. a transient agent/SDK error like a 529 overload)
    # prompts for retry/continue/abort rather than failing the whole run after
    # all steps are already committed.
    on_failure=ask_user,
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
        setting_sources=["project"],
    ),
    # Same transient-error resilience as review: prompt instead of dying.
    on_failure=ask_user,
)

config = pipeline

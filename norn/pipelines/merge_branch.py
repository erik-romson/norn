"""Take the current feature branch from "committed locally" to "merged".

Usage:
    norn run merge_branch --non-interactive

Prerequisites:
    - ``gh auth login`` has been run with a push-capable token (the
      ``preflight`` stage checks push permission on the repo before doing
      anything).
    - macOS, for the ``osascript``-based notifications fired on pause and at
      the end of the run.
    - The current branch is committed locally and has at least one commit
      ahead of its base.

The run: resolves the base branch and rebases onto it (halting on conflict);
loops the local test suite with an LLM either fixing a failure or saying
STOP; pushes (lease-pinned, sets the upstream); opens or reuses the PR and
requests Copilot as reviewer; waits for the PR's checks (handing red Actions
checks to a ``check_and_fix_ci`` child run) and for Copilot's review,
classifies review comments and posts findings, stopping on blocking ones;
fast-forward merges the base; and watches the post-merge build. Every
automated path is bounded — no loop runs unattended forever, every wait has a
timeout — and the moment the run is stuck it halts with a written state
summary and a notification instead of guessing.

Optional ``--arg`` knobs (not available under ``norn ui``, same as
``fix_jira_issue``):
  --arg base=develop          Base branch to rebase onto and merge into.
                              Default: resolved from an open PR, then the
                              nearest fork point among common branch names,
                              then the repo's default branch.
  --arg test_cmd="..."        Local test command. Default: auto-detected.
  --arg fix_model=standard    Intent label for the local-fix Generate stage
                              (default: standard)
  --arg classify_model=standard  Intent label for the review-findings
                              classifier (default: standard)
  --arg budget=15             Cost ceiling in USD before the run pauses
                              (default: 15)
  --arg guidance="..."        Extra text passed verbatim to the local fixer
  --arg overwrite_remote=<sha>  One-shot authorisation to force-push over
                              commits on the remote branch that have no
                              patch-equivalent on HEAD. Never repeated by the
                              rerun command this pipeline prints.

Every label above is one of ``standard``, ``light`` or ``deep`` — never a raw
model name.
"""

import os
import shlex
import sys

from norn.alerts import AlertEvent, MacOSChannel
from norn.dsl import Pipeline, Stage, ask_user, fail, file_exists, file_missing
from norn.model_labels import ModelLabelError, labels_for
from norn.pipelines._comment_style import ATTRIBUTION_ERE, COMMENT_RULE
from norn.pipelines._launch_tree import AssertLaunchTree
from norn.pipelines._merge_gh import (
    BASE_RUNS_GRACE,
    CI_FIX_TIMEOUT,
    COPILOT_REVIEW_TIMEOUT,
    HEAD_SYNC_TIMEOUT,
    NO_CHECKS_GRACE,
    SETTLE_WINDOW,
    CheckMergePreconditions,
    CiFixChild,
    FetchPrReviews,
    ResolveBranch,
    WaitForChecks,
    WaitForCopilotReview,
    WatchBaseBuild,
    check_findings_cmd,
    merge_cmd,
    post_findings_comment_cmd,
    pr_not_draft_cmd,
    pr_open_or_reuse_cmd,
    push_cmd,
    rebase_cmd,
    request_copilot_cmd,
    sync_after_ci_fix_cmd,
    validate_findings_cmd,
)
from norn.pipelines._merge_state import (
    AssertFreshRun,
    AssessLocalFailure,
    CountLocalAttempt,
    Escalate,
    HaltingCommand,
    HaltingCompressTestLog,
    HaltingGenerate,
    HaltOnTimeout,
    MergePaths,
    WriteMergeState,
    all_of,
    commit_subject,
    halt_shell,
    halted,
    running,
    work_root,
)
from norn.pipelines._preplan import parse_arg_flags
from norn.pipelines._test_cmd import detect_test_cmd
from norn.stages.generate import Generate
from norn.stages.run_command import RunCommand

# ---------------------------------------------------------------------------
# Import-time resolution
# ---------------------------------------------------------------------------

# Pinned to the launch directory at import time. This pipeline operates on
# the repo it was launched from: its RunCommand stages `cd {PROJECT_DIR}`
# explicitly and its git/gh helpers run against that tree. It is therefore
# NOT worktree-isolated — running it with the TUI worktree toggle will still
# execute against the launch directory.
PROJECT_DIR = os.getcwd()

_args = parse_arg_flags(sys.argv[1:])

FIX_MODEL = _args.get("fix_model", "standard")
CLASSIFY_MODEL = _args.get("classify_model", "standard")

# --- import-time knob validation -------------------------------------------
_generate_labels = set(labels_for("claude-code")) | set(labels_for("opencode"))
for _knob_name, _knob_value in (("fix_model", FIX_MODEL), ("classify_model", CLASSIFY_MODEL)):
    if _knob_value not in _generate_labels:
        raise ModelLabelError(
            f"--arg {_knob_name}={_knob_value!r} is not a valid Generate label; "
            f"known labels: {', '.join(sorted(_generate_labels))} — see norn/model_labels.toml"
        )

_raw_budget = _args.get("budget", "15")
try:
    BUDGET = float(_raw_budget)
except (TypeError, ValueError):
    raise ValueError(f"--arg budget: expected a number of USD, got {_raw_budget!r}")

BASE_ARG = _args.get("base", "")
OVERWRITE_REMOTE = _args.get("overwrite_remote", "")
GUIDANCE = _args.get("guidance", "")

# check_and_fix_ci.py:160-164's precedence: explicit --arg, then env, then
# auto-detect. Never raise here on an undetectable command (falls back to
# "true") -- the TUI launcher could not load the pipeline at all otherwise;
# `preflight` check 7 below is what turns an undetected command into a hard
# stop for a real run.
TEST_CMD = _args.get("test_cmd") or os.environ.get("NORN_TEST_CMD") or detect_test_cmd(PROJECT_DIR)
LOCAL_FIX_ATTEMPTS = 3
TEST_TIMEOUT = 3600

# The runner backstop for `wait for checks {n}`: every phase's own worst case
# added up — head sync, the no-checks grace window, the poll timeout ceiling
# (`_ci_estimate._TIMEOUT_CEILING`, spelled out literally here since that name
# is private to that module), the settle window, three extra poll intervals
# of margin, and a fixed 120s cushion for the gh calls themselves.
_WAIT_FOR_CHECKS_TIMEOUT = HEAD_SYNC_TIMEOUT + NO_CHECKS_GRACE + 3600 + SETTLE_WINDOW + 3 * 30 + 120

# The runner backstop for `fix ci {n}`: set above `CI_FIX_TIMEOUT` (the
# CiFixChild's own timeout, which halts `ci_fix_timeout` with a clean report)
# so that named halt fires before this cruder `asyncio.wait_for` cancellation
# would. 300s of margin for the subprocess spawn/teardown itself.
_FIX_CI_STAGE_TIMEOUT = CI_FIX_TIMEOUT + 300

# The runner backstop for `wait for copilot review {n}`: set above
# `COPILOT_REVIEW_TIMEOUT` (WaitForCopilotReview's own deadline, which halts
# `copilot_timeout` with a clean report) so that named halt fires before this
# cruder `asyncio.wait_for` cancellation would. 120s of margin for the gh
# calls themselves.
_WAIT_FOR_COPILOT_TIMEOUT = COPILOT_REVIEW_TIMEOUT + 120

# The runner backstop for `watch base build`: the grace window for a run to
# appear, plus the poll timeout ceiling (`_ci_estimate._TIMEOUT_CEILING`,
# spelled out literally here — private to that module), the settle window,
# three extra poll intervals of margin, and a fixed 120s cushion for the gh
# calls themselves. Mirrors `_WAIT_FOR_CHECKS_TIMEOUT`'s shape minus the
# head-sync/no-checks phases `wait for checks {n}` has and this stage does not.
_WATCH_BASE_BUILD_TIMEOUT = BASE_RUNS_GRACE + 3600 + SETTLE_WINDOW + 3 * 30 + 120

WORK = work_root(PROJECT_DIR)
os.makedirs(WORK, exist_ok=True)
PATHS = MergePaths(WORK)

# Raising this is a deliberate edit: each round is a full CI cycle (rebase,
# push, wait for checks, wait for review) that spends real wall-clock and
# gh-API quota, so a run must not silently multiply that cost. Two is the
# bound: one round cannot absorb a base that moved between the checks and the
# fast-forward, and a third would mean three CI cycles before a human hears
# anything — the realistic cause of a second conflict is a busy base, which a
# human should decide about.
MAX_ROUNDS = 2

# The exact rerun line this run's own summary points a human at.
# `overwrite_remote` is a one-shot authorisation, never repeated here — an
# unattended rerun must not silently force away commits again.
RERUN_CMD = "norn run merge_branch --non-interactive" + "".join(
    f" --arg {shlex.quote(f'{key}={value}')}"
    for key, value in _args.items()
    if key != "overwrite_remote"
)

metadata = {"env_vars": ["ANTHROPIC_API_KEY"], "args": {}}

# ---------------------------------------------------------------------------
# Preflight command
# ---------------------------------------------------------------------------

_preflight_cmd = (
    f"cd {shlex.quote(PROJECT_DIR)} || exit 1\n"
    'command -v git >/dev/null || { echo "ERROR: git not found"; exit 1; }\n'
    'command -v gh >/dev/null || { echo "ERROR: gh not found"; exit 1; }\n'
    'command -v osascript >/dev/null || { echo "ERROR: osascript not found"; exit 1; }\n'
    'gh auth status >/dev/null 2>&1 || '
    '{ echo "ERROR: gh auth status failed — run: gh auth login"; exit 1; }\n'
    f'[ "$(git rev-parse --show-toplevel)" = {shlex.quote(PROJECT_DIR)} ] || '
    f'{{ echo "ERROR: git toplevel does not match PROJECT_DIR={PROJECT_DIR}"; exit 1; }}\n'
    '[ -z "$(git status --porcelain)" ] || '
    '{ echo "ERROR: working tree is not clean"; exit 1; }\n'
    'git symbolic-ref -q HEAD >/dev/null || '
    '{ echo "ERROR: HEAD is detached — check out a branch first"; exit 1; }\n'
    '[ -n "$(git config user.email)" ] || '
    '{ echo "ERROR: git config user.email is empty"; exit 1; }\n'
    'REMOTE_URL=$(git remote get-url origin) || '
    '{ echo "ERROR: no origin remote configured"; exit 1; }\n'
    'NWO=$(gh repo view "$REMOTE_URL" --json nameWithOwner -q .nameWithOwner) || '
    '{ echo "ERROR: gh repo view failed for $REMOTE_URL"; exit 1; }\n'
    'PUSH_OK=$(gh api "repos/$NWO" -q .permissions.push) || '
    '{ echo "ERROR: gh api repos/$NWO failed"; exit 1; }\n'
    '[ "$PUSH_OK" = "true" ] || '
    '{ echo "ERROR: no push permission on $NWO"; exit 1; }\n'
    + (
        'echo "ERROR: no test command detected; pass --arg test_cmd=... or '
        'set NORN_TEST_CMD"; exit 1\n'
        if TEST_CMD == "true"
        else ""
    )
    + f'case {shlex.quote(WORK)} in\n'
    '  */norn-merge/*) ;;\n'
    f'  *) echo "ERROR: refusing to wipe a work root outside /norn-merge/: {WORK}"; exit 1;;\n'
    'esac\n'
    f'rm -rf {shlex.quote(WORK)} && mkdir -p {shlex.quote(WORK)}\n'
)

# ---------------------------------------------------------------------------
# Local test loop — shell builders
# ---------------------------------------------------------------------------


def run_local_tests_cmd(paths: MergePaths, project_dir: str, test_cmd: str, n: int) -> str:
    """Return the POSIX sh fragment that runs the local suite for round *n*.

    Always exits 0 — a red suite is data for the loop to act on, not a stage
    failure. ``KEY`` hashes the tree (HEAD + working diff) plus the test
    command itself, and is only written to ``proof-n`` on green; ``local
    tests green n`` recomputes the same hash later to decide whether the
    suite needs rerunning (the ``_proven_validation`` rule).
    """
    red_path = shlex.quote(paths.red(n))
    proof_path = shlex.quote(paths.proof(n))
    rc_path = shlex.quote(paths.local_rc(n))
    log_path = shlex.quote(paths.local_log(n))
    key_input = shlex.quote(test_cmd)
    return (
        f"cd {shlex.quote(project_dir)} || exit 1\n"
        f"rm -f {red_path} {proof_path}\n"
        f"KEY=$({{ git rev-parse HEAD; git diff HEAD; printf '%s' {key_input}; }} "
        "| git hash-object --stdin) || exit 1\n"
        f"{{ {{ {test_cmd} ; }} ; echo $? > {rc_path} ; }} 2>&1 | tee {log_path}\n"
        f'if [ "$(cat {rc_path})" = 0 ]; then printf \'%s\\n\' "$KEY" > {proof_path}; '
        f"else touch {red_path}; fi\n"
        "exit 0\n"
    )


def check_local_verdict_cmd(paths: MergePaths, project_dir: str, n: int, last_attempt: int) -> str:
    """Return the POSIX sh fragment that validates and acts on the fixer's verdict.

    In order (see step doc): missing verdict file, malformed line 1, archive
    to ``local-verdict-<n>-<k>.md``, HEAD moved since ``assess local failure
    n`` recorded it, STOP, then for FIX: a clean diff, untracked files, an
    attribution line among the added lines, promote the pending signature,
    and — only on the loop's very last attempt — ``local_cap``. Otherwise
    ``exit 1``: the ONE deliberate retry signal that restarts the loop.
    """
    verdict_path = shlex.quote(paths.verdict(n))
    head_path = shlex.quote(paths.local_head(n))
    pending_sig = shlex.quote(paths.local_signature_pending(n))
    signature = shlex.quote(paths.local_signature(n))
    attempt_path = shlex.quote(paths.local_attempt(n))
    archive_dir = shlex.quote(paths.root)
    archive_prefix = shlex.quote(f"local-verdict-{n}-")

    missing_halt = halt_shell(paths, "local_verdict_missing")
    malformed_halt = halt_shell(paths, "local_verdict_malformed", 'printf \'%s\\n\' "$LINE1"')
    head_moved_halt = halt_shell(paths, "local_head_moved")
    stop_halt = halt_shell(paths, "local_stop", 'cat "$ARCHIVE_PATH"')
    no_diff_halt = halt_shell(paths, "local_no_diff")
    untracked_halt = halt_shell(paths, "untracked_files", 'printf \'%s\\n\' "$UNTRACKED"')
    attribution_halt = halt_shell(paths, "attribution_found", 'printf \'%s\\n\' "$ATTR_MATCH"')
    cap_halt = halt_shell(
        paths, "local_cap",
        "printf 'fix applied on the last attempt; the suite was not re-run\\n'",
    )

    return (
        f"cd {shlex.quote(project_dir)} || exit 1\n"
        f"VERDICT_FILE={verdict_path}\n"
        'if [ ! -f "$VERDICT_FILE" ]; then\n'
        f"{missing_halt}"
        "fi\n"
        'LINE1=$(head -n1 "$VERDICT_FILE") || exit 1\n'
        'case "$LINE1" in\n'
        '  "VERDICT: FIX"|"VERDICT: STOP") ;;\n'
        f"  *) {malformed_halt}    ;;\n"
        "esac\n"
        f"ARCHIVE_DIR={archive_dir}\n"
        f"ARCHIVE_PREFIX={archive_prefix}\n"
        "K=1\n"
        'while [ -f "$ARCHIVE_DIR/$ARCHIVE_PREFIX$K.md" ]; do K=$((K + 1)); done\n'
        'ARCHIVE_PATH="$ARCHIVE_DIR/$ARCHIVE_PREFIX$K.md"\n'
        'cp "$VERDICT_FILE" "$ARCHIVE_PATH" || exit 1\n'
        "CURRENT_HEAD=$(git rev-parse HEAD) || exit 1\n"
        f"RECORDED_HEAD=$(cat {head_path}) || exit 1\n"
        'if [ "$CURRENT_HEAD" != "$RECORDED_HEAD" ]; then\n'
        f"{head_moved_halt}"
        "fi\n"
        'if [ "$LINE1" = "VERDICT: STOP" ]; then\n'
        f"{stop_halt}"
        "fi\n"
        "# VERDICT: FIX from here on.\n"
        "if git diff --quiet; then\n"
        f"{no_diff_halt}"
        "fi\n"
        "UNTRACKED=$(git ls-files --others --exclude-standard)\n"
        'if [ -n "$UNTRACKED" ]; then\n'
        f"{untracked_halt}"
        "fi\n"
        f"if ATTR_MATCH=$(git diff | grep '^+' | grep -iE {shlex.quote(ATTRIBUTION_ERE)}); then\n"
        f"{attribution_halt}"
        "fi\n"
        f"mv {pending_sig} {signature} || exit 1\n"
        f"ATTEMPT=$(cat {attempt_path}) || exit 1\n"
        f'if [ "$ATTEMPT" = {shlex.quote(str(last_attempt))} ]; then\n'
        f"{cap_halt}"
        "fi\n"
        'echo "fix attempt $K applied; re-running the suite" >&2\n'
        "exit 1\n"
    )


def local_tests_green_cmd(paths: MergePaths, project_dir: str, test_cmd: str, n: int) -> str:
    """Return the POSIX sh fragment for the post-loop ``local tests green n`` gate.

    Skips rerunning the suite when ``proof-n`` matches the tree's current
    hash (the ``_proven_validation`` rule — a blind rerun here once doubled
    the suite time for nothing); otherwise it runs the suite once more and
    halts ``local_gate_red`` if it is still red.
    """
    proof_path = shlex.quote(paths.proof(n))
    key_input = shlex.quote(test_cmd)
    gate_red_halt = halt_shell(paths, "local_gate_red")
    return (
        f"cd {shlex.quote(project_dir)} || exit 1\n"
        f"KEY=$({{ git rev-parse HEAD; git diff HEAD; printf '%s' {key_input}; }} "
        "| git hash-object --stdin) || exit 1\n"
        f'if [ -f {proof_path} ] && [ "$(cat {proof_path})" = "$KEY" ]; then\n'
        '  echo "passed in the loop on this exact tree, not rerun"\n'
        "  exit 0\n"
        "fi\n"
        f"if ! {{ {test_cmd} ; }}; then\n"
        f"{gate_red_halt}"
        "fi\n"
        "exit 0\n"
    )


def commit_local_fixes_cmd(paths: MergePaths, project_dir: str, n: int) -> str:
    """Return the POSIX sh fragment for round *n*'s ``commit local fixes n`` stage.

    Untracked files halt first — ``git add -u`` would silently leave them out
    of the commit the just-proven-green run depended on. With nothing staged
    this is a no-op (the fix loop needed zero attempts); otherwise it commits
    with the one fixed subject (``commit_subject(n)``, no body, no trailer)
    and, on a hook rejection, resets the index before halting
    ``commit_rejected`` so nothing is left staged.
    """
    untracked_halt = halt_shell(paths, "untracked_files", 'printf \'%s\\n\' "$UNTRACKED"')
    commit_log = shlex.quote(paths.commit_log(n))
    commit_rejected_halt = halt_shell(paths, "commit_rejected", f"cat {commit_log}")
    subject = shlex.quote(commit_subject(n))
    return (
        f"cd {shlex.quote(project_dir)} || exit 1\n"
        "UNTRACKED=$(git ls-files --others --exclude-standard)\n"
        'if [ -n "$UNTRACKED" ]; then\n'
        f"{untracked_halt}"
        "fi\n"
        "git add -u || exit 1\n"
        "if git diff --cached --quiet; then\n"
        '  echo "no local fixes to commit"\n'
        "  exit 0\n"
        "fi\n"
        f"if git commit -m {subject} > {commit_log} 2>&1; then\n"
        "  exit 0\n"
        "fi\n"
        "git reset -q\n"
        f"{commit_rejected_halt}"
    )


def _decide_local_fix_prompt(n: int) -> str:
    """Return the ``decide local fix n`` prompt: fix the tree, or say STOP."""
    guidance_block = f"\nGuidance from the human: {GUIDANCE}\n" if GUIDANCE else ""
    return (
        f"## Working directory\n{PROJECT_DIR}\n\n"
        "## Decide: fix the local test failure, or stop\n"
        f"The local test suite (`{TEST_CMD}`) failed. Below is the compressed "
        f"failure output; the full raw log is at {PATHS.local_log(n)}.\n\n"
        "### Compressed test failure\n"
        f"{{compress local tests {n}.output}}\n"
        f"{guidance_block}\n"
        "## Instructions\n"
        "- Read the relevant source files before making changes.\n"
        "- FIX: apply a root-cause fix to tracked files only, then stop — the "
        "pipeline re-runs the suite next. Never skip or disable a test to "
        "make it pass, never run git, and never create a new file — if the "
        "real fix needs one, choose STOP instead.\n"
        "- STOP: choose this when the failure is not fixable unattended — a "
        "product decision, a missing credential, an environment fault, or a "
        "fix that would need a new file.\n"
        f"- Write your verdict to {PATHS.verdict(n)}: line 1 exactly "
        "`VERDICT: FIX` or `VERDICT: STOP`, then a line `Reason: <one line>`, "
        "then at most three more lines of rationale. Nothing else.\n\n"
        + COMMENT_RULE
    )


def _classify_findings_prompt(n: int) -> str:
    """Return the ``classify findings n`` prompt: classify new PR feedback.

    The classifier has no ``Bash`` and never touches the branch (locked
    decision 9) — it reads ``pr-reviews-<n>.md`` and ``branch-diff-<n>.patch``
    (plus any repository file it needs to judge a comment) and writes only
    ``findings-<n>.md``, which becomes the body of the PR comment
    ``post findings comment n`` posts, so it stays compact.
    """
    return (
        f"## Working directory\n{PROJECT_DIR}\n\n"
        "## Classify new pull request review feedback\n"
        f"Read {PATHS.pr_reviews(n)} for the new reviews and comments on this "
        f"pull request, and {PATHS.branch_diff(n)} for the branch's diff "
        "against its base — read any other repository file you need to judge "
        f"a comment. The PR number is in {PATHS.pr}.\n\n"
        "## Instructions\n"
        "- Blocking: correctness, security, data loss, or an explicit change "
        "request.\n"
        "- Non-blocking: style, naming, speculative refactors.\n"
        "- Answered: the diff already addresses the comment.\n"
        f"- Write only {PATHS.findings(n)}, replacing any existing content, "
        "in exactly this shape:\n\n"
        "  VERDICT: BLOCKING            (or VERDICT: CLEAR)\n"
        "  PR: <number>\n"
        "  Reviewed: <k> new comments\n"
        "\n"
        "  ## Blocking\n"
        "  - <file:line> (<author>): <one line: what and why it blocks>\n"
        "\n"
        "  ## Non-blocking\n"
        "  - <file:line> (<author>): <one line>\n"
        "\n"
        "  ## Answered\n"
        "  - <file:line> (<author>): <one line>\n\n"
        "- VERDICT is BLOCKING if and only if the Blocking section has at "
        "least one entry, otherwise CLEAR.\n"
        "- One line per finding. No preamble, no closing remark, no "
        "signature.\n"
        "- Never edit any other file, and never run a shell command.\n\n"
        + COMMENT_RULE
    )


def _summarize_run_prompt() -> str:
    """Return the ``summarize run`` prompt: narrate the finished run.

    Every fact in the summary is already rendered from artifacts by Python
    (``render_summary``); this model narrates it in prose but never touches
    the summary file itself — it writes only ``what-happened.md``, which
    ``finalize merge state`` splices into ``## What happened`` on its own
    re-render, so the narration can never alter a fact.
    """
    return (
        f"## Working directory\n{PROJECT_DIR}\n\n"
        "## Narrate this merge_branch run\n"
        f"The path to this run's summary file is written in "
        f"{PATHS.summary_path}. Read that path, then read the summary file "
        "it names.\n\n"
        "## Instructions\n"
        f"- Write only {PATHS.what_happened}, replacing any existing "
        "content: three to six plain sentences narrating what happened in "
        "this run, in prose, for a human who has not seen the summary.\n"
        "- Cover the outcome (merged, or why the run stopped) and any "
        "notable events along the way (retries, a CI fix, review "
        "findings).\n"
        "- Never edit any other file, and never run a shell command.\n\n"
        + COMMENT_RULE
    )


# ---------------------------------------------------------------------------
# After the rounds — shell builders
# ---------------------------------------------------------------------------


def merge_reached_cmd(paths: MergePaths, project_dir: str) -> str:
    """Return the POSIX sh fragment for the post-rounds ``merge reached`` guard.

    ``merge pr <n>`` writes ``merged`` the moment the fast-forward push lands,
    and touches ``RETRY-<n>`` instead when the base moved under it. So after
    the last round, a run that is still ``running`` (no ``HALT``) and has no
    ``merged`` is one where every round ended in a retry — the base kept
    moving and the bound ran out. That has no named halt of its own anywhere
    earlier, so it gets one here.

    This is also what makes ``_merge_state._status``'s "neither file" branch
    unreachable: after this stage the only way to reach ``write merge state``
    without a halt is a merge that actually landed.
    """
    halt_fragment = halt_shell(
        paths,
        "no_merge_after_rounds",
        "printf 'no fast-forward merge after %s round(s); the base kept moving\\n' "
        f"{shlex.quote(str(MAX_ROUNDS))}",
    )
    return (
        f"cd {shlex.quote(project_dir)} || exit 1\n"
        f"if [ -f {shlex.quote(paths.merged)} ]; then\n"
        f"  printf 'merged %s\\n' \"$(cat {shlex.quote(paths.merged)})\"\n"
        "  exit 0\n"
        "fi\n"
        f"{halt_fragment}"
    )


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

config = (
    Pipeline("merge_branch", default_model="standard")
    .alert(MacOSChannel(events={AlertEvent.ASK_USER, AlertEvent.COMPLETE, AlertEvent.FAILED}))
    .budget(max_cost_usd=BUDGET, on_exceed=ask_user)
    .env("GH_PROMPT_DISABLED", "1")
)

# 1. assert launch tree
config.stage(
    "assert launch tree",
    AssertLaunchTree(project_dir=PROJECT_DIR),
    on_failure=fail,
    always_run=True,
)

# 2. assert fresh run
config.stage(
    "assert fresh run",
    AssertFreshRun(),
    on_failure=fail,
    always_run=True,
)

# 3. preflight
config.stage(
    "preflight",
    RunCommand(cmd=_preflight_cmd),
    on_failure=fail,
    always_run=True,
)

# 4. resolve branch
config.stage(
    "resolve branch",
    ResolveBranch(
        paths=PATHS,
        project_dir=PROJECT_DIR,
        base_arg=BASE_ARG,
        overwrite_remote=OVERWRITE_REMOTE,
    ),
    on_failure=fail,
)


def _add_round(n: int, gate) -> None:
    """Add round *n*'s stages to the pipeline.

    ``gate`` (``running(PATHS)`` for round 1, ``all_of(running(PATHS),
    file_exists(PATHS.retry(n - 1)))`` for round 2) is ANDed into every
    stage's own ``when`` in this round — never used bare unless a stage has
    no other condition. No stage here may reference ``running(PATHS)``
    directly or a round-1 path: every file this round reads or writes is a
    round-*n* path, so round 2 re-fetches and re-rebases, re-runs the suite
    against its own ``RED-2``/``local-attempt-2``/``local-proof-2``, and
    waits for its own ``checks-2.json`` rather than trusting round 1's.

    The rounds are flat copies, never nested: ``dsl.Loop`` has no ``when``,
    so the local-test loop itself is unconditional and its six body stages
    carry ``gate`` instead. A body whose stages are all skipped passes on
    attempt one — the runner records a skipped stage ``success=True`` and
    does not checkpoint it (``runner.py:739-752``) — so a round 2 that never
    activates costs nothing.
    """
    config.stage(
        f"rebase {n}",
        HaltingCommand(cmd=rebase_cmd(PATHS, PROJECT_DIR, n), paths=PATHS, stage=f"rebase {n}"),
        on_failure=fail,
        when=gate,
    )

    # `red_n` gates every body stage that only matters on a failing suite.
    # `LAST` marks the loop's final attempt for every `HaltOnFailure` mixin
    # in the body: on any earlier attempt an unpredicted failure passes
    # through and the loop retries; on the last attempt it becomes an
    # `unexpected_failure` halt instead of exhausting the loop's own
    # `on_exhaust=fail` (see the step's "Trace" note — only a failure in
    # `count local attempt` can reach that path in normal operation).
    red_n = all_of(gate, file_exists(PATHS.red(n)))
    LAST = (n, LOCAL_FIX_ATTEMPTS + 1)

    config.loop(
        f"local tests {n}",
        max_retries=LOCAL_FIX_ATTEMPTS + 1,
        on_exhaust=fail,
        new_session=True,
        stages=[
            Stage(
                f"count local attempt {n}",
                CountLocalAttempt(paths=PATHS, n=n),
                when=gate,
            ),
            Stage(
                f"run local tests {n}",
                HaltOnTimeout(
                    paths=PATHS,
                    stage=f"run local tests {n}",
                    kind="test_timeout",
                    cmd=run_local_tests_cmd(PATHS, PROJECT_DIR, TEST_CMD, n),
                    timeout=TEST_TIMEOUT,
                    last_attempt=LAST,
                ),
                when=gate,
            ),
            Stage(
                f"compress local tests {n}",
                HaltingCompressTestLog(
                    paths=PATHS,
                    stage=f"compress local tests {n}",
                    source_stage=f"run local tests {n}",
                    last_attempt=LAST,
                ),
                when=red_n,
            ),
            Stage(
                f"assess local failure {n}",
                AssessLocalFailure(
                    paths=PATHS,
                    stage=f"assess local failure {n}",
                    n=n,
                    source_stage=f"compress local tests {n}",
                    max_fixes=LOCAL_FIX_ATTEMPTS,
                    project_dir=PROJECT_DIR,
                    last_attempt=LAST,
                ),
                when=red_n,
            ),
            Stage(
                f"decide local fix {n}",
                HaltingGenerate(
                    paths=PATHS,
                    stage=f"decide local fix {n}",
                    model=FIX_MODEL,
                    prompt=_decide_local_fix_prompt(n),
                    allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
                    permission_mode="acceptEdits",
                    setting_sources=["project"],
                    add_dirs=[WORK],
                    last_attempt=LAST,
                ),
                when=red_n,
            ),
            Stage(
                f"check local verdict {n}",
                HaltingCommand(
                    paths=PATHS,
                    stage=f"check local verdict {n}",
                    cmd=check_local_verdict_cmd(PATHS, PROJECT_DIR, n, LOCAL_FIX_ATTEMPTS + 1),
                    last_attempt=LAST,
                ),
                when=red_n,
            ),
        ],
    )

    # Top-level gate, named differently from every body stage above: the
    # `_proven_validation` rerun-skip only applies when the loop itself just
    # proved the exact tree green; any tree change since (e.g. a rebase in a
    # later round) forces a real rerun.
    config.stage(
        f"local tests green {n}",
        HaltOnTimeout(
            paths=PATHS,
            stage=f"local tests green {n}",
            kind="test_timeout",
            cmd=local_tests_green_cmd(PATHS, PROJECT_DIR, TEST_CMD, n),
            timeout=TEST_TIMEOUT,
        ),
        on_failure=fail,
        when=gate,
    )

    config.stage(
        f"commit local fixes {n}",
        HaltingCommand(
            cmd=commit_local_fixes_cmd(PATHS, PROJECT_DIR, n),
            paths=PATHS,
            stage=f"commit local fixes {n}",
        ),
        when=gate,
    )

    config.stage(
        f"push {n}",
        HaltingCommand(
            cmd=push_cmd(PATHS, PROJECT_DIR, n),
            paths=PATHS,
            stage=f"push {n}",
        ),
        when=gate,
    )

    config.stage(
        f"open or reuse pr {n}",
        HaltingCommand(
            cmd=pr_open_or_reuse_cmd(PATHS, PROJECT_DIR),
            paths=PATHS,
            stage=f"open or reuse pr {n}",
        ),
        when=gate,
    )

    config.stage(
        f"pr is not draft {n}",
        HaltingCommand(
            cmd=pr_not_draft_cmd(PATHS, PROJECT_DIR),
            paths=PATHS,
            stage=f"pr is not draft {n}",
        ),
        when=gate,
    )

    config.stage(
        f"request copilot review {n}",
        HaltingCommand(
            cmd=request_copilot_cmd(PATHS, PROJECT_DIR, rerequest=False),
            paths=PATHS,
            stage=f"request copilot review {n}",
        ),
        when=gate,
    )

    config.stage(
        f"wait for checks {n}",
        WaitForChecks(
            paths=PATHS,
            stage=f"wait for checks {n}",
            n=n,
            out_name=PATHS.checks(n),
            project_dir=PROJECT_DIR,
            on_red="fix",
        ),
        when=gate,
        timeout=_WAIT_FOR_CHECKS_TIMEOUT,
    )

    # `ci_red_n` gates the four hand-off stages below: they run only when
    # `wait for checks {n}` handed fixable red checks to a check_and_fix_ci
    # child rather than halting on its own.
    ci_red_n = all_of(gate, file_exists(PATHS.ci_red(n)))

    config.stage(
        f"fix ci {n}",
        CiFixChild(
            paths=PATHS,
            stage=f"fix ci {n}",
            n=n,
            project_dir=PROJECT_DIR,
            pipeline=config,
            test_cmd=TEST_CMD,
        ),
        when=ci_red_n,
        timeout=_FIX_CI_STAGE_TIMEOUT,
    )

    config.stage(
        f"sync after ci fix {n}",
        HaltingCommand(
            cmd=sync_after_ci_fix_cmd(PATHS, PROJECT_DIR, n),
            paths=PATHS,
            stage=f"sync after ci fix {n}",
        ),
        when=ci_red_n,
    )

    config.stage(
        f"re-request copilot review {n}",
        HaltingCommand(
            cmd=request_copilot_cmd(PATHS, PROJECT_DIR, rerequest=True),
            paths=PATHS,
            stage=f"re-request copilot review {n}",
        ),
        when=ci_red_n,
    )

    config.stage(
        f"wait for checks after ci fix {n}",
        WaitForChecks(
            paths=PATHS,
            stage=f"wait for checks after ci fix {n}",
            n=n,
            out_name=PATHS.checks_after_fix(n),
            project_dir=PROJECT_DIR,
            on_red="halt",
        ),
        when=ci_red_n,
        timeout=_WAIT_FOR_CHECKS_TIMEOUT,
    )

    config.stage(
        f"wait for copilot review {n}",
        WaitForCopilotReview(
            paths=PATHS,
            stage=f"wait for copilot review {n}",
            n=n,
            project_dir=PROJECT_DIR,
        ),
        when=gate,
        timeout=_WAIT_FOR_COPILOT_TIMEOUT,
    )

    config.stage(
        f"fetch pr reviews {n}",
        FetchPrReviews(
            paths=PATHS,
            stage=f"fetch pr reviews {n}",
            n=n,
            project_dir=PROJECT_DIR,
        ),
        on_failure=fail,
        when=gate,
    )

    # The classifier never inherits the fixer's conversation.
    config.clear_context()

    # `has_new_findings` gates the classifier and its two shell gates: they
    # run only when `fetch pr reviews n` found something new to classify —
    # on zero new entries it wrote `findings-<n>.md` (VERDICT: CLEAR) itself.
    has_new_findings = all_of(gate, file_missing(PATHS.no_new_reviews(n)))

    config.stage(
        f"classify findings {n}",
        HaltingGenerate(
            paths=PATHS,
            stage=f"classify findings {n}",
            model=CLASSIFY_MODEL,
            prompt=_classify_findings_prompt(n),
            allowed_tools=["Read", "Grep", "Glob", "Write"],
            permission_mode="acceptEdits",
            setting_sources=["project"],
            add_dirs=[WORK],
            max_turns=40,
        ),
        when=has_new_findings,
    )

    config.stage(
        f"validate findings {n}",
        HaltingCommand(
            cmd=validate_findings_cmd(PATHS, PROJECT_DIR, n),
            paths=PATHS,
            stage=f"validate findings {n}",
        ),
        when=has_new_findings,
    )

    config.stage(
        f"post findings comment {n}",
        HaltingCommand(
            cmd=post_findings_comment_cmd(PATHS, PROJECT_DIR, n),
            paths=PATHS,
            stage=f"post findings comment {n}",
        ),
        when=has_new_findings,
    )

    config.stage(
        f"check findings {n}",
        HaltingCommand(
            cmd=check_findings_cmd(PATHS, PROJECT_DIR, n),
            paths=PATHS,
            stage=f"check findings {n}",
        ),
        on_failure=fail,
        when=gate,
    )

    config.stage(
        f"check merge preconditions {n}",
        CheckMergePreconditions(
            paths=PATHS,
            stage=f"check merge preconditions {n}",
            n=n,
            project_dir=PROJECT_DIR,
        ),
        when=gate,
    )

    config.stage(
        f"merge pr {n}",
        HaltingCommand(
            cmd=merge_cmd(PATHS, PROJECT_DIR, n),
            paths=PATHS,
            stage=f"merge pr {n}",
        ),
        when=all_of(gate, file_missing(PATHS.retry(n))),
    )


_add_round(1, running(PATHS))

# Round 2 runs only when round 1 ended in a retry — `check merge
# preconditions 1` saw a conflicting/dirty/behind PR, or `merge pr 1` found
# the base had moved under the fast-forward. Every stage in it (loop body
# included) carries this gate, so with no `RETRY-1` the whole round is
# skipped at no cost.
_add_round(2, all_of(running(PATHS), file_exists(PATHS.retry(1))))

# after the rounds: the no-merge guard, the summary, then escalate if the run
# halted.
config.stage(
    "merge reached",
    HaltingCommand(
        cmd=merge_reached_cmd(PATHS, PROJECT_DIR),
        paths=PATHS,
        stage="merge reached",
    ),
    on_failure=fail,
    when=running(PATHS),
)

config.stage(
    "watch base build",
    WatchBaseBuild(
        paths=PATHS,
        stage="watch base build",
        project_dir=PROJECT_DIR,
    ),
    when=all_of(running(PATHS), file_exists(PATHS.merged)),
    timeout=_WATCH_BASE_BUILD_TIMEOUT,
)

config.stage(
    "write merge state",
    WriteMergeState(
        paths=PATHS,
        rerun_cmd=RERUN_CMD,
        max_rounds=MAX_ROUNDS,
        project_dir=PROJECT_DIR,
        test_cmd=TEST_CMD,
        local_fix_attempts=LOCAL_FIX_ATTEMPTS,
    ),
    on_failure=fail,
)

# The narrator never inherits the run's conversation.
config.clear_context()

# `summarize run` reads the factual summary `write merge state` just wrote
# and narrates it into its own file. It never edits the summary itself — no
# `HaltOnFailure` here: if it fails, the factual summary is already on disk
# and the run fails at this stage (fail-fast, not the diagnostics
# warn-and-continue exception in AGENTS.md — the narration is not a journal
# or metrics write).
config.stage(
    "summarize run",
    Generate(
        model="light",
        prompt=_summarize_run_prompt(),
        allowed_tools=["Read", "Write"],
        permission_mode="acceptEdits",
        add_dirs=[WORK],
        max_turns=10,
    ),
)

# A second `WriteMergeState` instance: re-renders the summary now that
# `what-happened.md` exists, splicing it into `## What happened`. Every
# other section is rendered from the same artifacts as the first render, so
# it is byte-identical apart from that one section.
config.stage(
    "finalize merge state",
    WriteMergeState(
        paths=PATHS,
        rerun_cmd=RERUN_CMD,
        max_rounds=MAX_ROUNDS,
        project_dir=PROJECT_DIR,
        test_cmd=TEST_CMD,
        local_fix_attempts=LOCAL_FIX_ATTEMPTS,
    ),
    on_failure=fail,
)

# Always last: the ONLY ask_user stage. Skipped entirely on a clean run.
config.stage(
    "escalate",
    Escalate(paths=PATHS),
    on_failure=ask_user,
    when=halted(PATHS),
)

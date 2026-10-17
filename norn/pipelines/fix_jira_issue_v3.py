"""Fix a Jira issue end-to-end: fetch → plan → implement → review → ship a draft PR with green checks.

Usage:  norn run fix_jira_issue_v3 CBS-1234
        norn run fix_jira_issue_v3 CBS-1234 auto
        norn run fix_jira_issue_v3 CBS-1234 auto --non-interactive   (unattended)

The run takes the issue from fetch to an open **draft** PR whose checks are
green, then stops and alerts.  A PR for the branch that is already open and
that a human has marked ready for review is reused as it is, and stays ready:
the run never changes a PR's draft state.

Gates.  Interactive runs pause at four gates; each is one file you edit, all
in tmp/jira/<KEY>/:
  1. <KEY>-plan-questions.md   answer the open questions on the plan
                               (an answer of `recommended` takes the
                               recommendation)
  2. <KEY>-plan-approval.md    set DECISION: approve (edit the plan first if
                               you like)
  3. <KEY>-code-review.md      set each Disposition (fix, skip, follow-up),
                               then STATUS: DECIDED; a bounded fix loop with
                               the tests follows
  4. <KEY>-ship.md             COMMITS (rewrite or keep), TITLE, then
                               STATUS: DECIDED; nothing is pushed before it

`auto` decides every gate it safely can and stops, with a prompt and an
alert, where it cannot.  `auto --non-interactive` turns every such stop into
an abort with a FAILED alert.

Resume.  Abort at the prompt, edit the one file it names, then rerun with
`--resume`.  `--resume` does not restore launch parameters, so retype `auto`
when the run was in auto:
        norn run fix_jira_issue_v3 CBS-1234 auto --resume

Optional ``--arg`` knobs (not available under ``norn ui``):
  --arg branch=CBS-1234        Git branch name. Default: the issue key — but
                               when the current branch already names the
                               issue, the run stays on it
  --arg model=deep             Label for the planning Generate stages (default: deep)
  --arg codex_label=review     Label for the codex reviews (default: review)
  --arg review_model=deep      Label for the Claude code review (default: deep)
  --arg fix_model=deep         Label for the gate-3 fix agent (default: deep)
  --arg budget=75              Cost ceiling in USD before the run pauses
                               (default: 75); each child run gets what is left
  --arg test_cmd=<cmd>         Gate-3 test command. Default: index.md's
                               final_test_cmd, else the union of the step
                               test_cmds
  --arg fix_retries=3          Attempts of the gate-3 fix loop (default: 3)
  --arg ci_rounds=2            CI-fix rounds on red PR checks (default: 2)
  --arg ci_timeout_minutes=60  Per-wait CI timeout of the CI-fix child (default: 60)
  --arg plan_gate=on           off approves gate 2 without a pause (default: on)
  --arg base=<branch>          PR base branch. Default: resolved like merge_branch_v2
  --arg overwrite_remote=<sha> One-shot: allow overwriting origin/<branch> at
                               exactly <sha> (default: none)
  --arg jira_browse=<url>      Jira browse URL ending in /browse/ for the PR
                               body's link. Default: env JIRA_BROWSE_BASE, else
                               no link

Notes:
  - The cost ceiling is per process: a resumed process starts its tracker at
    $0 again, and `summary` prints this process's spend.  A child killed hard
    reports no cost, so the ceiling can undercount.
  - The "PR checks finished" notices are sent under the name merge_branch.
  - One issue per launch directory at a time: the checkpoint is per pipeline.
  - A repo with no CI stops at checks_none.
  - A commit made after the ship phase is pushed only after its sha is
    appended to tmp/jira/<KEY>/v3/shipped-heads.
  - Switching a run to `auto` on resume overwrites the dispositions of a
    gate-3 file that is still PROPOSED: set STATUS: DECIDED to keep your edits.
"""

import os
import shlex
import sys
from pathlib import Path

from norn.alerts import AlertEvent, MacOSChannel
from norn.dsl import Pipeline, Stage, ask_user, fail, stage_failed, stage_succeeded
from norn.model_labels import ModelLabelError, labels_for, resolve_label
from norn.pipelines._child_cost_v3 import RunChildPipelineV3
from norn.pipelines._code_review_v3 import ResolveTestCmd, add_code_review_stages
from norn.pipelines._fix_jira_v3 import (
    CheckRunIdentity,
    RecordRunIdentity,
    RegisterChildState,
    V3Paths,
    on_off_knob,
    positive_int_knob,
    run_mode,
)
from norn.pipelines._jira_issue import (
    BRIEF_HEADINGS,
    artifact_paths,
    brief_prompt,
    resolve_issue_args,
)
from norn.pipelines._launch_tree import AssertLaunchTree
from norn.pipelines._plan_review_v2 import _SUBAGENT_LABEL, resolve_split_skill
from norn.pipelines._plan_review_v3 import add_plan_review_stages_v3
from norn.pipelines._pr_v3 import add_pr_stages
from norn.pipelines._preplan import derive_paths, parse_arg_flags, steps_dir_of
from norn.pipelines._ship_v3 import add_ship_stages, resolve_commit_skill
from norn.stages.generate import Generate
from norn.stages.run_command import RunCommand
from norn.stages.validate import Contains, FileExists, Validate

# ---------------------------------------------------------------------------
# Import-time resolution
# ---------------------------------------------------------------------------

PROJECT_DIR = os.getcwd()
_args = parse_arg_flags(sys.argv[1:])
ISSUE_KEY, _STOP = resolve_issue_args(sys.argv[1:], pipeline="fix_jira_issue_v3")
if _STOP:
    raise ValueError(
        "fix_jira_issue_v3 always pauses at the reviewed plan; `stop` is a v2 positional. "
        "Use `auto` to run hands-off or --arg plan_gate=off to skip the plan pause."
    )
AUTO = run_mode(sys.argv[1:]) == "auto"

ART = artifact_paths(PROJECT_DIR, ISSUE_KEY)
PLAN, QUESTIONS, REVIEW, RESPONSE = derive_paths(ART.preplan)
STEPS_DIR = steps_dir_of(PLAN)
V3 = V3Paths(ART.dir, ISSUE_KEY)
BASE_FILE = ART.dir + "base.sha"

COMMIT_SKILL, COMMIT_SKILL_SEARCHED = resolve_commit_skill()
SPLIT_SKILL = resolve_split_skill()

# --- knobs -----------------------------------------------------------------
BRANCH = _args.get("branch", ISSUE_KEY)
# An explicit --arg branch is an instruction, so the "already on a branch for
# this issue" shortcut in `prepare branch` applies only to the issue-key default.
BRANCH_FROM_KEY = "branch" not in _args
MODEL = _args.get("model", "deep")
CODEX_LABEL = _args.get("codex_label", "review")
REVIEW_MODEL = _args.get("review_model", "deep")
FIX_MODEL = _args.get("fix_model", "deep")
TRIAGE_MODEL = "standard"

_raw_budget = _args.get("budget", "75")
try:
    BUDGET = float(_raw_budget)
except (TypeError, ValueError):
    raise ValueError(f"--arg budget: expected a number of USD, got {_raw_budget!r}")

TEST_CMD = _args.get("test_cmd")
FIX_RETRIES = positive_int_knob(_args, "fix_retries", 3)
CI_ROUNDS = positive_int_knob(_args, "ci_rounds", 2)
CI_TIMEOUT_MINUTES = positive_int_knob(_args, "ci_timeout_minutes", 60)
PLAN_GATE = on_off_knob(_args, "plan_gate", True)
BASE = _args.get("base", "")
OVERWRITE_REMOTE = _args.get("overwrite_remote", "")

# An explicit --arg wins over the env var; an empty value means "no link".
_raw_browse = (
    _args["jira_browse"] if "jira_browse" in _args else os.environ.get("JIRA_BROWSE_BASE", "")
)
JIRA_BROWSE = _raw_browse or None
if JIRA_BROWSE is not None and not JIRA_BROWSE.endswith("/browse/"):
    raise ValueError(
        f"--arg jira_browse (or JIRA_BROWSE_BASE): must end in /browse/, got {JIRA_BROWSE!r}"
    )

_generate_labels = set(labels_for("claude-code")) | set(labels_for("opencode"))
for _knob, _label in (("model", MODEL), ("review_model", REVIEW_MODEL), ("fix_model", FIX_MODEL)):
    if _label not in _generate_labels:
        raise ModelLabelError(
            f"--arg {_knob}={_label!r} is not a valid Generate label; "
            f"known labels: {', '.join(sorted(_generate_labels))} — see norn/model_labels.toml"
        )
resolve_label("codex", CODEX_LABEL)  # fail fast if unknown

_SUBAGENT_ENV = {
    "CLAUDE_CODE_SUBAGENT_MODEL": resolve_label("claude-code", _SUBAGENT_LABEL).model,
    "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1",
}

FETCH = str(Path(__file__).parent / "_jira_fetch.sh")

# Copied from fix_jira_issue_v2.py:76-80 (cannot be imported: import-time argv parsing)
CHILD = (
    f"cd {shlex.quote(PROJECT_DIR)} && "
    f"{shlex.quote(sys.executable)} -m norn run "
    f"implement_features_v3 {shlex.quote(STEPS_DIR)} --non-interactive"
)

# The child runs that write state into the launch directory.
CHILD_STATE_KEYS = ["implement_features_v3", "check_and_fix_ci_v2"]

metadata = {
    "env_vars": ["ANTHROPIC_API_KEY", "JIRA_AUTH", "JIRA_BASE"],
    "args": {"args": "Jira issue key (e.g. CBS-2249), optionally followed by `auto`"},
}

# ---------------------------------------------------------------------------
# Preflight command
# ---------------------------------------------------------------------------

# Copied from fix_jira_issue_v2.py:93-101 (cannot be imported: import-time argv parsing)
# norn reads secrets from the process env, ~/.norn/env and ./.norn.env only.
# The `env` block of .claude/settings*.json is applied by Claude Code to the
# processes it launches, never to a norn started from a terminal, so a key set
# there looks set and is not; the message names where norn actually looks.
_ENV_SOURCES_HINT = (
    "norn reads it from the environment, ~/.norn/env or .norn.env in the launch "
    "directory (KEY=value lines). The env block of .claude/settings.local.json is "
    "applied only by Claude Code, not to norn."
)

# Copied from fix_jira_issue_v2.py:103-129 (cannot be imported: import-time argv parsing),
# without the child-checkpoint check (`register child state` replaces it), and
# with gh, gh auth, symbolic HEAD and the push-permission check of
# merge_branch_v2.py:221-243 added.
_preflight_cmd = (
    # 1. JIRA_AUTH and JIRA_BASE are non-empty; JIRA_BASE ends with /
    f'[ -n "$JIRA_AUTH" ] || {{ echo "ERROR: JIRA_AUTH is empty. {_ENV_SOURCES_HINT}"; exit 1; }} && '
    f'[ -n "$JIRA_BASE" ] || {{ echo "ERROR: JIRA_BASE is empty. {_ENV_SOURCES_HINT}"; exit 1; }} && '
    'case "$JIRA_BASE" in */) ;; *) echo "ERROR: JIRA_BASE must end with /"; exit 1;; esac && '
    # 2. Required tools on PATH
    'command -v curl >/dev/null || { echo "ERROR: curl not found"; exit 1; } && '
    'command -v jq >/dev/null || { echo "ERROR: jq not found"; exit 1; } && '
    'command -v codex >/dev/null || { echo "ERROR: codex not found"; exit 1; } && '
    'command -v git >/dev/null || { echo "ERROR: git not found"; exit 1; } && '
    'command -v gh >/dev/null || { echo "ERROR: gh not found"; exit 1; } && '
    # 3. gh is logged in
    'gh auth status >/dev/null 2>&1 || '
    '{ echo "ERROR: gh auth status failed — run: gh auth login"; exit 1; } && '
    # 4. git root equals PROJECT_DIR
    f'[ "$(git rev-parse --show-toplevel)" = {shlex.quote(PROJECT_DIR)} ] || '
    f'{{ echo "ERROR: git toplevel does not match PROJECT_DIR={PROJECT_DIR}"; exit 1; }} && '
    # 5. Clean index and clean tracked worktree
    'git diff --cached --quiet || { echo "ERROR: index has staged changes"; exit 1; } && '
    'git diff --quiet || { echo "ERROR: tracked files are modified"; exit 1; } && '
    # 6. HEAD is a branch
    'git symbolic-ref -q HEAD >/dev/null || '
    '{ echo "ERROR: HEAD is detached — check out a branch first"; exit 1; } && '
    # 7. Push permission on origin (the PR phase pushes)
    'REMOTE_URL=$(git remote get-url origin) || '
    '{ echo "ERROR: no origin remote configured"; exit 1; } && '
    'NWO=$(gh repo view "$REMOTE_URL" --json nameWithOwner -q .nameWithOwner) || '
    '{ echo "ERROR: gh repo view failed for $REMOTE_URL"; exit 1; } && '
    'PUSH_OK=$(gh api "repos/$NWO" -q .permissions.push) || '
    '{ echo "ERROR: gh api repos/$NWO failed"; exit 1; } && '
    '{ [ "$PUSH_OK" = "true" ] || { echo "ERROR: no push permission on $NWO"; exit 1; }; } && '
    # 8. git check-ignore for required paths
    f'git check-ignore -q {shlex.quote(ART.dir.rstrip("/"))} || '
    f'{{ echo "ERROR: {ART.dir.rstrip("/")} is not gitignored. '
    f"Add to .git/info/exclude: {ART.dir.rstrip('/')}\"; exit 1; }} && "
    f'git check-ignore -q fix_jira_issue_v3.checkpoint || '
    f'{{ echo "ERROR: fix_jira_issue_v3.checkpoint is not gitignored. '
    f"Add to .git/info/exclude: fix_jira_issue_v3.checkpoint\"; exit 1; }}"
)

# ---------------------------------------------------------------------------
# Prepare branch command
# ---------------------------------------------------------------------------

# Copied from fix_jira_issue_v2.py:135-160 (cannot be imported: import-time argv parsing),
# with base.sha written only when it is missing: a rerun on the same branch
# must keep the base the first run recorded, or the review and the rewrite
# would lose the commits already made.
#
# Work already under way for an issue usually sits on a branch that names it —
# `CBS-2582`, `feature/CBS-2582-sorting`, `bugfix/cbs-2582`.  Cutting a fresh
# `CBS-2582` off that branch would strand the work, so when the current branch
# already carries the key the run stays where it is.  The match is
# case-insensitive because branch names are often lowercased by hand.
_base_sha_q = shlex.quote(BASE_FILE)
_prepare_branch_cmd = (
    f"cd {shlex.quote(PROJECT_DIR)} && "
    f"BRANCH={shlex.quote(BRANCH)} && "
    f"KEY={shlex.quote(ISSUE_KEY)} && "
    f"REUSE={'1' if BRANCH_FROM_KEY else '0'} && "
    'CURRENT=$(git branch --show-current) && '
    'MATCH=0 && '
    'if [ "$REUSE" = 1 ] && [ -n "$CURRENT" ]; then '
    'CUR_UP=$(printf "%s" "$CURRENT" | tr "[:lower:]" "[:upper:]"); '
    'KEY_UP=$(printf "%s" "$KEY" | tr "[:lower:]" "[:upper:]"); '
    'case "$CUR_UP" in *"$KEY_UP"*) MATCH=1;; esac; '
    'fi && '
    'if [ "$MATCH" = 1 ]; then '
    'echo "Staying on branch $CURRENT (it already names $KEY)"; '
    'else '
    'case "$BRANCH" in main|master) echo "ERROR: refusing to work on $BRANCH"; exit 1;; esac; '
    'git switch "$BRANCH" 2>/dev/null || git switch -c "$BRANCH"; '
    'fi && '
    f"mkdir -p {shlex.quote(ART.dir)} && "
    f"{{ [ -f {_base_sha_q} ] || git rev-parse HEAD > {_base_sha_q}; }}"
)

# ---------------------------------------------------------------------------
# Fetch command and brief
# ---------------------------------------------------------------------------

# Copied from fix_jira_issue_v2.py:166-188 (cannot be imported: import-time argv parsing)
_fetch_cmd = (
    f"bash {shlex.quote(FETCH)} {shlex.quote(ISSUE_KEY)} {shlex.quote(ART.dir)}"
)

_brief_prompt = brief_prompt(
    issue_md=ART.issue_md,
    attachments=ART.attachments,
    out=ART.preplan,
    project_dir=PROJECT_DIR,
)

_brief_checks = [
    FileExists(ART.preplan),
    Contains(ART.preplan, patterns=BRIEF_HEADINGS),
]

# ---------------------------------------------------------------------------
# Check implementation and child diagnosis commands
# ---------------------------------------------------------------------------

# Copied from fix_jira_issue_v2.py:194-227 (cannot be imported: import-time argv parsing)
_review_md = str(Path(STEPS_DIR) / "review.md")
_handoff_md = str(Path(STEPS_DIR) / "handoff.md")

# Reached only when the child run reported success, so a missing artifact here
# means the child passed its own gates without writing what it promised — a
# different fault from an aborted child, and worth saying so.
_check_impl_cmd = (
    f"test -f {shlex.quote(_review_md)} || "
    f'{{ echo "ERROR: the child run passed but wrote no review.md at '
    f'{_review_md}" 1>&2; exit 1; }} && '
    f"LINE1=$(head -1 {shlex.quote(_review_md)}) && "
    f'if [ "$LINE1" != "VERDICT: PASS" ]; then '
    f'echo "ERROR: review.md line 1 is not VERDICT: PASS, got: $LINE1" 1>&2; exit 1; fi && '
    f"test -f {shlex.quote(_handoff_md)} || "
    f'{{ echo "ERROR: handoff.md not found at {_handoff_md}" 1>&2; exit 1; }}'
)

# `implement` spawns a whole norn run.  When the operator answers [c]ontinue at
# its prompt, this reads the child's own journal and ends the run with the
# stage the child actually died on.
_CHILD_REPORT = str(Path(__file__).parent / "_child_run.py")

_diagnose_child_cmd = (
    f"cd {shlex.quote(PROJECT_DIR)} && "
    f"{shlex.quote(sys.executable)} {shlex.quote(_CHILD_REPORT)} report "
    f"--launch-dir {shlex.quote(PROJECT_DIR)} --state-key implement_features_v3"
)

# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

config = (
    Pipeline("fix_jira_issue_v3")
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

# 2. preflight environment
config.stage(
    "preflight environment",
    RunCommand(cmd=_preflight_cmd),
    on_failure=fail,
)

# 3. register child state — the children's checkpoints and journals land in
#    the launch directory, so they are excluded before any child runs.
config.stage(
    "register child state",
    RegisterChildState(PROJECT_DIR, CHILD_STATE_KEYS),
    on_failure=fail,
)

# 4. prepare branch
config.stage(
    "prepare branch",
    RunCommand(cmd=_prepare_branch_cmd),
    on_failure=fail,
)

# 5-6. run identity: a checkpoint resumed for another issue, on another
#      branch or over a stale base.sha stops here.
config.stage(
    "record run identity",
    RecordRunIdentity(ISSUE_KEY, PROJECT_DIR),
    on_failure=fail,
)
config.stage(
    "check run identity",
    CheckRunIdentity(ISSUE_KEY, PROJECT_DIR, BASE_FILE),
    on_failure=fail,
    always_run=True,
)

# 7. fetch issue — copied from fix_jira_issue_v2.py:325-370 (cannot be
#    imported: import-time argv parsing), through `brief written`.
config.stage(
    "fetch issue",
    RunCommand(cmd=_fetch_cmd),
    on_failure=ask_user,
)

# 8. check issue files
config.stage(
    "check issue files",
    Validate(checks=[
        FileExists(ART.issue_json),
        FileExists(ART.issue_md),
    ]),
    on_failure=fail,
)

# 9. Loop: write brief
config.loop(
    "write brief",
    max_retries=2,
    on_exhaust=ask_user,
    stages=[
        Stage(
            "clean issue",
            Generate(
                prompt=_brief_prompt,
                model="light",
                allowed_tools=["Read", "Write", "Glob"],
                permission_mode="acceptEdits",
                max_turns=30,
                env=_SUBAGENT_ENV,
            ),
        ),
        Stage(
            "check brief",
            Validate(checks=_brief_checks),
        ),
    ],
)

# 10. brief written (hard twin of the loop's check)
config.stage(
    "brief written",
    Validate(checks=_brief_checks),
    on_failure=fail,
)

config.clear_context()

# 11-30. Plan review: gate 1 (questions) and gate 2 (approval), then the split.
add_plan_review_stages_v3(
    config,
    preplan=ART.preplan,
    plan=PLAN,
    questions=QUESTIONS,
    review=REVIEW,
    response=RESPONSE,
    steps_dir=STEPS_DIR,
    repo_dir=PROJECT_DIR,
    model=MODEL,
    codex_label=CODEX_LABEL,
    split_skill=SPLIT_SKILL,
    approval=V3.approval,
    auto=AUTO,
    plan_gate=PLAN_GATE,
)

# 31. resolve test command — before any child spend, so a test command that
#     cannot resolve stops the run while it is still cheap.
config.stage(
    "resolve test command",
    ResolveTestCmd(steps_dir=STEPS_DIR, test_cmd_arg=TEST_CMD, paths=V3),
    on_failure=fail,
)

# 32. implement
#
# `ask_user` so [r]etry re-runs the child, which resumes and skips the steps it
# already committed.  The ceiling is read when the stage runs, and the child's
# cost is added to this run's tracker.
config.stage(
    "implement",
    RunChildPipelineV3(
        cmd=CHILD,
        pipeline=config,
        runs_dir=str(Path(PROJECT_DIR) / "implement_features_v3.runs"),
        rewritten_marker=V3.rewritten,
        timeout=None,
    ),
    on_failure=ask_user,
)

# 33. diagnose the child run — only when it failed and [c]ontinue was chosen.
config.stage(
    "diagnose implementation",
    RunCommand(cmd=_diagnose_child_cmd),
    when=stage_failed("implement"),
    on_failure=fail,
)

# 34. check implementation — only when the child reported success, so this
#     gate speaks only to what the child actually produced.
config.stage(
    "check implementation",
    RunCommand(cmd=_check_impl_cmd),
    when=stage_succeeded("implement"),
    on_failure=fail,
)

# 35-51. Gate 3: two reviewers, triage, the decision, the fix loop.
add_code_review_stages(
    config,
    paths=V3,
    project_dir=PROJECT_DIR,
    steps_dir=STEPS_DIR,
    key=ISSUE_KEY,
    base_file=BASE_FILE,
    codex_label=CODEX_LABEL,
    review_model=REVIEW_MODEL,
    triage_model=TRIAGE_MODEL,
    fix_model=FIX_MODEL,
    fix_retries=FIX_RETRIES,
    auto=AUTO,
)

# 52-68. Gate 4: commit plan, PR summary and body, the ship decision, rewrite.
add_ship_stages(
    config,
    paths=V3,
    project_dir=PROJECT_DIR,
    key=ISSUE_KEY,
    plan=PLAN,
    steps_dir=STEPS_DIR,
    issue_md=ART.issue_md,
    base_file=BASE_FILE,
    skill=COMMIT_SKILL,
    skill_searched=COMMIT_SKILL_SEARCHED,
    jira_browse=JIRA_BROWSE,
    auto=AUTO,
)

# 69-82. The PR phase: every stage is always_run.
add_pr_stages(
    config,
    paths=V3,
    project_dir=PROJECT_DIR,
    key=ISSUE_KEY,
    base_file=BASE_FILE,
    base_arg=BASE,
    overwrite_remote=OVERWRITE_REMOTE,
    ci_rounds=CI_ROUNDS,
    ci_timeout_minutes=CI_TIMEOUT_MINUTES,
    budget_pipeline=config,
    auto=AUTO,
)

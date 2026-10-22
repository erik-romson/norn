"""Fix a Jira or GitHub issue end-to-end: fetch → plan → implement → review → ship a draft PR with green checks.

Usage:  norn run fix_issue_v1 123             GitHub issue 123 of the launch repo (origin)
        norn run fix_issue_v1 '#123'          the same issue; '#123' must be quoted,
                                              because an unquoted # starts a shell comment
        norn run fix_issue_v1 owner/repo#123  an issue of a named repository
        norn run fix_issue_v1 https://github.com/owner/repo/issues/123
        norn run fix_issue_v1 CBS-1234        a Jira issue, as fix_jira_issue_v3 does it
        norn run fix_issue_v1 123 auto
        norn run fix_issue_v1 123 auto --non-interactive   (unattended)

The run takes the issue from fetch to an open **draft** PR whose checks are
green, then stops and alerts.  A PR for the branch that is already open and
that a human has marked ready for review is reused as it is, and stays ready:
the run never changes a PR's draft state.

Issue directory.  Every file of a run is in tmp/jira/<KEY>/ for a Jira issue,
or in tmp/github/<slug>/ for a GitHub issue, and the file names start with
<KEY> or issue-<slug>.  The slug is the number (123) for an issue of the
launch repo, else <owner>-<repo>-<N>-<h8>, where <h8> is a short hash that
keeps look-alike repositories apart.

Gates.  Interactive runs pause at four gates; each is one file you edit, all
in the issue directory (shown here with <KEY>; issue-<slug> for GitHub):
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

`auto` (or `--arg auto=on`, the `auto` checkbox under `norn ui`) runs
hands-off: no gate pauses for you.  It answers the plan questions with their
recommendations, approves the plan, fixes every code-review finding except
nits and accepts the proposed commits and PR title.  It stops, with a prompt
and an alert, only where it cannot decide safely.  `auto --non-interactive`
turns every such stop into an abort with a FAILED alert.

Fetch notes.  A GitHub fetch also records fetch notes: attachments that did
not download, text written by people without write access, a title that
GitHub would act on in the PR body, and a closed issue or an open linked PR.
The notes never stop the run.  They appear in issue.md (## Fetch notes), in
the `fetch issue` output, in the PR body's `## For the human` list, in the
final notifications and in the `summary` stage.  A Jira fetch has none.

Resume.  Abort at the prompt, edit the one file it names, then rerun with
`--resume`.  `--resume` does not restore launch parameters, so retype `auto`
when the run was in auto:
        norn run fix_issue_v1 123 auto --resume

Optional ``--arg`` knobs (not available under ``norn ui``):
  --arg branch=<name>          Git branch name. Default: issue-<slug> for a
                               GitHub issue; the issue key for a Jira issue —
                               but when the current branch already names the
                               Jira key, the run stays on it
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
  --arg base=<branch>          PR base branch. Default: an open PR's base, else
                               the branch the run was cut from (base.branch),
                               else resolved like merge_branch_v2.  Checked
                               before any work: origin/<base> must contain
                               base.sha
  --arg overwrite_remote=<sha> One-shot: allow overwriting origin/<branch> at
                               exactly <sha> (default: none)
  --arg jira_browse=<url>      Jira issues only. Jira browse URL ending in
                               /browse/ for the PR body's link. Default: env
                               JIRA_BROWSE_BASE, else no link

Notes:
  - The cost ceiling is per process: a resumed process starts its tracker at
    $0 again, and `summary` prints this process's spend.  A child killed hard
    reports no cost, so the ceiling can undercount.
  - The "PR checks finished" notices are sent under the name merge_branch.
  - One issue per launch directory at a time: the checkpoint is per pipeline.
  - A repo with no CI stops at checks_none.
  - A commit made after the ship phase is pushed only after its sha is
    appended to <issue directory>/v3/shipped-heads.
  - Switching a run to `auto` on resume overwrites the dispositions of a
    gate-3 file that is still PROPOSED: set STATUS: DECIDED to keep your edits.
  - Put the issue before `--require-quota`: `--require-quota 123` reads 123
    as the quota.
  - A GitHub run always works on its own branch issue-<slug>, which it
    creates or switches to, unless `--arg branch=` is given.
  - A Jira key shares tmp/jira/<KEY>/ and its branch with fix_jira_issue_v3,
    so do not run both pipelines on one key.
  - GitHub goes through gh: run `gh auth login` first, and set GH_HOST for a
    GitHub Enterprise host (one host per run).
  - A Jira issue also needs JIRA_AUTH and JIRA_BASE.
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
)
from norn.pipelines._issue_source import PIPELINE, resolve_issue
from norn.pipelines._jira_issue import BRIEF_HEADINGS, brief_prompt
from norn.pipelines._launch_tree import AssertLaunchTree
from norn.pipelines._plan_review_v2 import _SUBAGENT_LABEL, resolve_split_skill
from norn.pipelines._plan_review_v3 import add_plan_review_stages_v3
from norn.pipelines._pr_v3 import CheckPrBase, add_pr_stages, pr_paths
from norn.pipelines._preplan import derive_paths, parse_arg_flags, steps_dir_of
from norn.pipelines._ship_v3 import add_ship_stages, resolve_commit_skill
from norn.stages.generate import Generate
from norn.stages.run_command import RunCommand
from norn.stages.validate import Contains, FileExists, Validate

# ---------------------------------------------------------------------------
# Import-time resolution
# ---------------------------------------------------------------------------

# Copied from fix_jira_issue_v3.py:110-127 (cannot be imported: import-time argv parsing),
# with the issue resolved by _issue_source.resolve_issue, which owns the argument
# forms, the `stop` refusal and the per-source values.
PROJECT_DIR = os.getcwd()
_args = parse_arg_flags(sys.argv[1:])
if "branch" in _args and not _args["branch"]:
    raise ValueError("--arg branch: empty value")
SOURCE, AUTO = resolve_issue(sys.argv[1:], PROJECT_DIR, _args, os.environ)

ART = SOURCE.art
PLAN, QUESTIONS, REVIEW, RESPONSE = derive_paths(ART.preplan)
STEPS_DIR = steps_dir_of(PLAN)
V3 = V3Paths(ART.dir, SOURCE.file_key)
BASE_FILE = ART.dir + "base.sha"
# The branch the run was cut from: the PR base unless --arg base or an open PR
# says otherwise.  Empty when the run did not start from a branch at base.sha.
START_BRANCH_FILE = ART.dir + "base.branch"

COMMIT_SKILL, COMMIT_SKILL_SEARCHED = resolve_commit_skill()
SPLIT_SKILL = resolve_split_skill()

# --- knobs -----------------------------------------------------------------
# Copied from fix_jira_issue_v3.py:129-152 (cannot be imported: import-time argv parsing),
# with the source's default branch.
BRANCH = _args.get("branch", SOURCE.default_branch)
# An explicit --arg branch is an instruction, so the "already on a branch for
# this issue" shortcut in `prepare branch` applies only to the default branch,
# and only for a source that reuses branches (Jira).
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

# resolve_issue owns the jira_browse rule (v3's for Jira; an error for GitHub).
JIRA_BROWSE = SOURCE.jira_browse

# Copied from fix_jira_issue_v3.py:164-176 (cannot be imported: import-time argv parsing)
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

# Copied from fix_jira_issue_v3.py:180-185 (cannot be imported: import-time argv parsing)
CHILD = (
    f"cd {shlex.quote(PROJECT_DIR)} && "
    f"{shlex.quote(sys.executable)} -m norn run "
    f"implement_features_v3 {shlex.quote(STEPS_DIR)} --non-interactive"
)

# The child runs that write state into the launch directory.
CHILD_STATE_KEYS = ["implement_features_v3", "check_and_fix_ci_v2"]

metadata = {
    "env_vars": ["ANTHROPIC_API_KEY"],
    "args": {
        "args": (
            "Issue: a Jira key (CBS-2249), a GitHub issue number (123), owner/repo#123 "
            "or an issue URL"
        ),
        "auto": {
            "type": "switch",
            "help": (
                "Hands-off run: the pipeline never pauses to ask you and approves every "
                "gate itself. It answers the plan questions with their recommendations, "
                "approves the plan, fixes every code-review finding except nits, accepts "
                "the proposed commits and PR title, and opens the draft PR. It still "
                "stops, with an alert, where it cannot decide safely."
            ),
        },
    },
}

# ---------------------------------------------------------------------------
# Preflight command
# ---------------------------------------------------------------------------

# The issue directory as a .git/info/exclude line: a pattern is relative to the
# repository root, so an absolute path there can never match.
_EXCLUDE_LINE = os.path.relpath(ART.dir, PROJECT_DIR) + "/"

# Copied from fix_jira_issue_v3.py:219-251 (cannot be imported: import-time argv parsing),
# with this pipeline's checkpoint name and a repo-relative exclude hint.  The
# check-ignore path keeps its trailing slash: git then treats it as a directory
# even before `prepare branch` creates it, so the hinted `<dir>/` line matches
# on the first run too.
_SHARED = (
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
    f"git check-ignore -q {shlex.quote(ART.dir)} || "
    f'{{ echo "ERROR: {ART.dir.rstrip("/")} is not gitignored. '
    f'Add to .git/info/exclude: {_EXCLUDE_LINE}"; exit 1; }} && '
    f"git check-ignore -q {PIPELINE}.checkpoint || "
    f'{{ echo "ERROR: {PIPELINE}.checkpoint is not gitignored. '
    f'Add to .git/info/exclude: {PIPELINE}.checkpoint"; exit 1; }}'
)

# 1. the source's env checks (Jira: JIRA_AUTH and JIRA_BASE), then the shared
#    checks, then the source's issue checks (GitHub: the issue exists and is
#    not a PR; no commit.cleanup=strip under a `#123:` prefix).
_preflight_cmd = SOURCE.env_check_sh + _SHARED + SOURCE.issue_check_sh

# ---------------------------------------------------------------------------
# Prepare branch command
# ---------------------------------------------------------------------------

# Copied from fix_jira_issue_v3.py:258-289 (cannot be imported: import-time argv parsing),
# with KEY set to the source's display reference and REUSE on only for a source
# that reuses branches.
#
# Work already under way for a Jira issue usually sits on a branch that names
# it — `CBS-2582`, `feature/CBS-2582-sorting`, `bugfix/cbs-2582`.  Cutting a
# fresh `CBS-2582` off that branch would strand the work, so when the current
# branch already carries the key the run stays where it is.  The match is
# case-insensitive because branch names are often lowercased by hand.  A GitHub
# run never adopts the branch it starts on: `123` would match far too many
# branch names, so it always switches to (or creates) its own branch.
#
# base.branch is written with base.sha, and names the branch the run started
# on only when that branch's tip is base.sha — the branch this run's PR goes
# into.  When the run stays on its issue branch, or switches to one with work
# of its own, the file is empty and the PR base is resolved without it.
_base_sha_q = shlex.quote(BASE_FILE)
_start_branch_q = shlex.quote(START_BRANCH_FILE)
_prepare_branch_cmd = (
    f"cd {shlex.quote(PROJECT_DIR)} && "
    f"BRANCH={shlex.quote(BRANCH)} && "
    f"KEY={shlex.quote(SOURCE.display)} && "
    f"REUSE={'1' if BRANCH_FROM_KEY and SOURCE.branch_reuse else '0'} && "
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
    f"{{ [ -f {_base_sha_q} ] || {{ "
    f"git rev-parse HEAD > {_base_sha_q} && "
    'START= && '
    'if [ "$CURRENT" != "$(git branch --show-current)" ] && '
    '[ "$(git rev-parse "refs/heads/$CURRENT")" = "$(git rev-parse HEAD)" ]; then START=$CURRENT; fi && '
    f'printf "%s\\n" "$START" > {_start_branch_q}; }}; }}'
)

# ---------------------------------------------------------------------------
# Fetch command and brief
# ---------------------------------------------------------------------------

# The source's fetch: Jira runs _jira_fetch.sh exactly as fix_jira_issue_v3
# does; GitHub runs _github_fetch.sh and then writes the fetch notes.
_fetch_cmd = SOURCE.fetch_cmd

# Copied from fix_jira_issue_v3.py:300-310 (cannot be imported: import-time argv parsing)
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

# Copied from fix_jira_issue_v3.py:316-343 (cannot be imported: import-time argv parsing)
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

# Copied from fix_jira_issue_v3.py:349-556 (cannot be imported: import-time argv parsing):
# the same stages in the same order, with the per-source values of SOURCE.
config = (
    Pipeline(PIPELINE)
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
#      branch or over a stale base.sha stops here.  A GitHub identity is the
#      canonical owner/repo#N, because `#123` does not tell repositories apart.
config.stage(
    "record run identity",
    RecordRunIdentity(SOURCE.identity, PROJECT_DIR, kind=SOURCE.kind),
    on_failure=fail,
)
config.stage(
    "check run identity",
    CheckRunIdentity(SOURCE.identity, PROJECT_DIR, BASE_FILE, base_hint=BASE_FILE),
    on_failure=fail,
    always_run=True,
)

# 6b. check pr base — the PR base is resolved here exactly as `resolve branch`
#     resolves it, so a base that is not on origin, is ambiguous, or does not
#     contain base.sha stops the run before any spend.  `resolve branch`
#     checks again: origin can move while the run works.
config.stage(
    "check pr base",
    CheckPrBase(
        paths=pr_paths(V3),
        project_dir=PROJECT_DIR,
        base_arg=BASE,
        overwrite_remote=OVERWRITE_REMOTE,
        start_branch_file=START_BRANCH_FILE,
        base_file=BASE_FILE,
    ),
    on_failure=fail,
    always_run=True,
)

# 7. fetch issue
config.stage(
    "fetch issue",
    RunCommand(cmd=_fetch_cmd),
    on_failure=ask_user,
)

# 8. check issue files (GitHub: the fetch notes file too)
config.stage(
    "check issue files",
    Validate(checks=[
        FileExists(ART.issue_json),
        FileExists(ART.issue_md),
        *(FileExists(p) for p in SOURCE.extra_issue_files),
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
        pipeline_name=PIPELINE,
        art_dir=ART.dir,
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
    key=SOURCE.file_key,
    base_file=BASE_FILE,
    codex_label=CODEX_LABEL,
    review_model=REVIEW_MODEL,
    triage_model=TRIAGE_MODEL,
    fix_model=FIX_MODEL,
    fix_retries=FIX_RETRIES,
    auto=AUTO,
    subject_prefix=SOURCE.subject_prefix,
)

# 52-68. Gate 4: commit plan, PR summary and body, the ship decision, rewrite.
add_ship_stages(
    config,
    paths=V3,
    project_dir=PROJECT_DIR,
    key=SOURCE.file_key,
    plan=PLAN,
    steps_dir=STEPS_DIR,
    issue_md=ART.issue_md,
    base_file=BASE_FILE,
    skill=COMMIT_SKILL,
    skill_searched=COMMIT_SKILL_SEARCHED,
    jira_browse=JIRA_BROWSE,
    auto=AUTO,
    subject_prefix=SOURCE.subject_prefix,
    issue_line=SOURCE.issue_line,
    issue_line_ok=SOURCE.issue_line_ok,
    issue_label=SOURCE.issue_label,
    extra_checklist=SOURCE.fetch_checklist,
)

# 69-82. The PR phase: every stage is always_run.
add_pr_stages(
    config,
    paths=V3,
    project_dir=PROJECT_DIR,
    key=SOURCE.file_key,
    base_file=BASE_FILE,
    base_arg=BASE,
    overwrite_remote=OVERWRITE_REMOTE,
    ci_rounds=CI_ROUNDS,
    ci_timeout_minutes=CI_TIMEOUT_MINUTES,
    budget_pipeline=config,
    auto=AUTO,
    subject_prefix=SOURCE.subject_prefix,
    pipeline_name=PIPELINE,
    resume_arg=SOURCE.resume_arg,
    fetch_notes=SOURCE.fetch_notes,
    notes_path=SOURCE.notes_path,
    start_branch_file=START_BRANCH_FILE,
)

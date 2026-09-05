"""Fix a Jira issue end-to-end: fetch → brief → plan → review → implement.

Usage:  norn run fix_jira_issue CBS-2249
        norn run fix_jira_issue CBS-2249 stop

The ``stop`` positional pauses after the plan review so you can inspect and
edit the plan before implementation begins.

Optional ``--arg`` knobs (not available under ``norn ui``):
  --arg branch=CBS-2249       Git branch name (default: the issue key)
  --arg model=sonnet          Claude model for Generate stages (default: opus)
  --arg budget=10             Cost ceiling in USD before the run pauses
"""

import os
import shlex
import sys
from pathlib import Path

from norn.alerts import AlertEvent, MacOSChannel
from norn.dsl import Pipeline, Stage, ask_user, fail
from norn.pipelines._jira_issue import (
    BRIEF_HEADINGS,
    artifact_paths,
    brief_prompt,
    resolve_issue_args,
)
from norn.pipelines._launch_tree import AssertLaunchTree
from norn.pipelines._plan_review import (
    add_plan_review_stages,
    resolve_split_skill,
)
from norn.pipelines._preplan import derive_paths, parse_arg_flags, steps_dir_of
from norn.stages.generate import Generate
from norn.stages.run_command import RunCommand
from norn.stages.validate import Contains, FileExists, Validate

# ---------------------------------------------------------------------------
# Import-time resolution
# ---------------------------------------------------------------------------

PROJECT_DIR = os.getcwd()
_args = parse_arg_flags(sys.argv[1:])
ISSUE_KEY, STOP_AFTER_PLAN = resolve_issue_args(sys.argv[1:])
ART = artifact_paths(PROJECT_DIR, ISSUE_KEY)
PLAN, QUESTIONS, REVIEW, RESPONSE = derive_paths(ART.preplan)
STEPS_DIR = steps_dir_of(PLAN)
BRANCH = _args.get("branch", ISSUE_KEY)
MODEL = _args.get("model", "opus")
FETCH = str(Path(__file__).parent / "_jira_fetch.sh")
CHILD = (
    f"cd {shlex.quote(PROJECT_DIR)} && "
    f"{shlex.quote(sys.executable)} -m norn run "
    f"implement_features_v2 {shlex.quote(STEPS_DIR)} --non-interactive"
)

SPLIT_SKILL = resolve_split_skill()

metadata = {
    "env_vars": ["ANTHROPIC_API_KEY", "JIRA_AUTH", "JIRA_BASE"],
    "args": {"args": "Jira issue key (e.g. CBS-2249), optionally followed by `stop`"},
}

# ---------------------------------------------------------------------------
# Preflight command
# ---------------------------------------------------------------------------

_preflight_cmd = (
    # 1. JIRA_AUTH and JIRA_BASE are non-empty; JIRA_BASE ends with /
    '[ -n "$JIRA_AUTH" ] || { echo "ERROR: JIRA_AUTH is empty"; exit 1; } && '
    '[ -n "$JIRA_BASE" ] || { echo "ERROR: JIRA_BASE is empty"; exit 1; } && '
    'case "$JIRA_BASE" in */) ;; *) echo "ERROR: JIRA_BASE must end with /"; exit 1;; esac && '
    # 2. Required tools on PATH
    'command -v curl >/dev/null || { echo "ERROR: curl not found"; exit 1; } && '
    'command -v jq >/dev/null || { echo "ERROR: jq not found"; exit 1; } && '
    'command -v codex >/dev/null || { echo "ERROR: codex not found"; exit 1; } && '
    'command -v git >/dev/null || { echo "ERROR: git not found"; exit 1; } && '
    # 3. git root equals PROJECT_DIR
    f'[ "$(git rev-parse --show-toplevel)" = {shlex.quote(PROJECT_DIR)} ] || '
    f'{{ echo "ERROR: git toplevel does not match PROJECT_DIR={PROJECT_DIR}"; exit 1; }} && '
    # 4. Clean index and clean tracked worktree
    'git diff --cached --quiet || { echo "ERROR: index has staged changes"; exit 1; } && '
    'git diff --quiet || { echo "ERROR: tracked files are modified"; exit 1; } && '
    # 5. git check-ignore for required paths
    f'git check-ignore -q {shlex.quote(ART.dir.rstrip("/"))} || '
    f'{{ echo "ERROR: {ART.dir.rstrip("/")} is not gitignored. '
    f"Add to .git/info/exclude: {ART.dir.rstrip('/')}\"; exit 1; }} && "
    f'git check-ignore -q fix_jira_issue.checkpoint || '
    f'{{ echo "ERROR: fix_jira_issue.checkpoint is not gitignored. '
    f"Add to .git/info/exclude: fix_jira_issue.checkpoint\"; exit 1; }} && "
    f'git check-ignore -q implement_features_v2.checkpoint || '
    f'{{ echo "ERROR: implement_features_v2.checkpoint is not gitignored. '
    f"Add to .git/info/exclude: implement_features_v2.checkpoint\"; exit 1; }}"
)

# ---------------------------------------------------------------------------
# Prepare branch command
# ---------------------------------------------------------------------------

_prepare_branch_cmd = (
    f"cd {shlex.quote(PROJECT_DIR)} && "
    f'BRANCH={shlex.quote(BRANCH)} && '
    'case "$BRANCH" in main|master) echo "ERROR: refusing to work on $BRANCH"; exit 1;; esac && '
    'git switch "$BRANCH" 2>/dev/null || git switch -c "$BRANCH" && '
    f"mkdir -p {shlex.quote(ART.dir)} && "
    f"git rev-parse HEAD > {shlex.quote(ART.dir + 'base.sha')}"
)

# ---------------------------------------------------------------------------
# Fetch command
# ---------------------------------------------------------------------------

_fetch_cmd = (
    f"bash {shlex.quote(FETCH)} {shlex.quote(ISSUE_KEY)} {shlex.quote(ART.dir)}"
)

# ---------------------------------------------------------------------------
# Brief prompt
# ---------------------------------------------------------------------------

_brief_prompt = brief_prompt(
    issue_md=ART.issue_md,
    attachments=ART.attachments,
    out=ART.preplan,
    project_dir=PROJECT_DIR,
)

# ---------------------------------------------------------------------------
# Brief headings gate
# ---------------------------------------------------------------------------

_brief_checks = [
    FileExists(ART.preplan),
    Contains(ART.preplan, patterns=BRIEF_HEADINGS),
]

# ---------------------------------------------------------------------------
# Check implementation command
# ---------------------------------------------------------------------------

_review_md = str(Path(STEPS_DIR) / "review.md")
_handoff_md = str(Path(STEPS_DIR) / "handoff.md")

_check_impl_cmd = (
    f"LINE1=$(head -1 {shlex.quote(_review_md)}) && "
    f'if [ "$LINE1" != "VERDICT: PASS" ]; then '
    f'echo "ERROR: review.md line 1 is not VERDICT: PASS, got: $LINE1" 1>&2; exit 1; fi && '
    f"test -f {shlex.quote(_handoff_md)} || "
    f'{{ echo "ERROR: handoff.md not found at {_handoff_md}" 1>&2; exit 1; }}'
)

# ---------------------------------------------------------------------------
# Summary command
# ---------------------------------------------------------------------------

_summary_cmd = (
    f"cd {shlex.quote(PROJECT_DIR)} && "
    f'echo "Branch: $(git branch --show-current)" && '
    f'echo "Commits:" && git log --oneline "@{{u}}.." 2>/dev/null || git log --oneline -20 && '
    f'echo "Handoff: {_handoff_md}"'
)

# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

_pipeline = (
    Pipeline("fix_jira_issue")
    .alert(MacOSChannel(events={AlertEvent.ASK_USER, AlertEvent.COMPLETE, AlertEvent.FAILED}))
    .budget(max_cost_usd=float(_args.get("budget", "10")), on_exceed=ask_user)
)

# 1. assert launch tree
_pipeline.stage(
    "assert launch tree",
    AssertLaunchTree(project_dir=PROJECT_DIR),
    on_failure=fail,
)

# 2. preflight environment
_pipeline.stage(
    "preflight environment",
    RunCommand(cmd=_preflight_cmd),
    on_failure=fail,
)

# 3. prepare branch
_pipeline.stage(
    "prepare branch",
    RunCommand(cmd=_prepare_branch_cmd),
    on_failure=fail,
)

# 4. fetch issue
_pipeline.stage(
    "fetch issue",
    RunCommand(cmd=_fetch_cmd),
    on_failure=ask_user,
)

# 5. check issue files
_pipeline.stage(
    "check issue files",
    Validate(checks=[
        FileExists(ART.issue_json),
        FileExists(ART.issue_md),
    ]),
    on_failure=fail,
)

# 6. Loop: write brief
_pipeline.loop(
    "write brief",
    max_retries=2,
    on_exhaust=ask_user,
    stages=[
        Stage(
            "clean issue",
            Generate(
                prompt=_brief_prompt,
                model="haiku",
                allowed_tools=["Read", "Write", "Glob"],
                permission_mode="acceptEdits",
                max_turns=30,
            ),
        ),
        Stage(
            "check brief",
            Validate(checks=_brief_checks),
        ),
    ],
)

# 7. brief written (hard twin of 6b)
_pipeline.stage(
    "brief written",
    Validate(checks=_brief_checks),
    on_failure=fail,
)

# 8. clear context
_pipeline.clear_context()

# 9-20. Builder stages (plan review flow)
config = add_plan_review_stages(
    _pipeline,
    preplan=ART.preplan,
    plan=PLAN,
    questions=QUESTIONS,
    review=REVIEW,
    response=RESPONSE,
    steps_dir=STEPS_DIR,
    repo_dir=PROJECT_DIR,
    model=MODEL,
    codex_model=_args.get("codex_model"),
    split_skill=SPLIT_SKILL,
    pause_for_approval=STOP_AFTER_PLAN,
)

# 21. implement
config.stage(
    "implement",
    RunCommand(cmd=CHILD, timeout=None),
    on_failure=ask_user,
)

# 22. check implementation
config.stage(
    "check implementation",
    RunCommand(cmd=_check_impl_cmd),
    on_failure=fail,
)

# 23. summary
config.stage(
    "summary",
    RunCommand(cmd=_summary_cmd),
)

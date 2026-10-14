"""Fix a Jira issue end-to-end: fetch → brief → plan → review → implement.

Usage:  norn run fix_jira_issue_v1 CBS-2249
        norn run fix_jira_issue_v1 CBS-2249 stop

The ``stop`` positional pauses after the plan review so you can inspect and
edit the plan before implementation begins.

Optional ``--arg`` knobs (not available under ``norn ui``):
  --arg branch=CBS-2249       Git branch name. Default: the issue key — but when
                              the current branch already names the issue, the run
                              stays on it instead of cutting a new one
  --arg model=deep            Intent label for Generate stages (default: deep)
  --arg codex_label=review    Intent label for codex review stage (default: review)
  --arg budget=50             Cost ceiling in USD before the run pauses; the
                              child implementation run gets the same ceiling
"""

import os
import shlex
import sys
from pathlib import Path

from norn.alerts import AlertEvent, MacOSChannel
from norn.dsl import Pipeline, Stage, ask_user, fail, stage_failed, stage_succeeded
from norn.model_labels import ModelLabelError, labels_for, resolve_label
from norn.pipelines._jira_issue import (
    BRIEF_HEADINGS,
    artifact_paths,
    brief_prompt,
    resolve_issue_args,
)
from norn.pipelines._launch_tree import AssertLaunchTree
from norn.pipelines._plan_review_v1 import (
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
# An explicit --arg branch is an instruction, so the "already on a branch for
# this issue" shortcut below applies only to the issue-key default.
BRANCH_FROM_KEY = "branch" not in _args
MODEL = _args.get("model", "deep")
CODEX_LABEL = _args.get("codex_label", "review")

_raw_budget = _args.get("budget", "50")
try:
    BUDGET = float(_raw_budget)
except (TypeError, ValueError):
    raise ValueError(f"--arg budget: expected a number of USD, got {_raw_budget!r}")

# --- import-time knob validation -------------------------------------------
_generate_labels = set(labels_for("claude-code")) | set(labels_for("opencode"))
if MODEL not in _generate_labels:
    raise ModelLabelError(
        f"--arg model={MODEL!r} is not a valid Generate label; "
        f"known labels: {', '.join(sorted(_generate_labels))} — see norn/model_labels.toml"
    )
resolve_label("codex", CODEX_LABEL)  # fail fast if unknown
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

# norn reads secrets from the process env, ~/.norn/env and ./.norn.env only.
# The `env` block of .claude/settings*.json is applied by Claude Code to the
# processes it launches, never to a norn started from a terminal, so a key set
# there looks set and is not; the message names where norn actually looks.
_ENV_SOURCES_HINT = (
    "norn reads it from the environment, ~/.norn/env or .norn.env in the launch "
    "directory (KEY=value lines). The env block of .claude/settings.local.json is "
    "applied only by Claude Code, not to norn."
)

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
    f'git check-ignore -q fix_jira_issue_v1.checkpoint || '
    f'{{ echo "ERROR: fix_jira_issue_v1.checkpoint is not gitignored. '
    f"Add to .git/info/exclude: fix_jira_issue_v1.checkpoint\"; exit 1; }} && "
    f'git check-ignore -q implement_features_v2.checkpoint || '
    f'{{ echo "ERROR: implement_features_v2.checkpoint is not gitignored. '
    f"Add to .git/info/exclude: implement_features_v2.checkpoint\"; exit 1; }}"
)

# ---------------------------------------------------------------------------
# Prepare branch command
# ---------------------------------------------------------------------------

# Work already under way for an issue usually sits on a branch that names it —
# `CBS-2582`, `feature/CBS-2582-sorting`, `bugfix/cbs-2582`.  Cutting a fresh
# `CBS-2582` off that branch would strand the work, so when the current branch
# already carries the key the run stays where it is.  The match is
# case-insensitive because branch names are often lowercased by hand.
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

# ---------------------------------------------------------------------------
# Child run diagnosis command
# ---------------------------------------------------------------------------

# `implement` spawns a whole norn run, and a RunCommand sees only its exit
# code.  When that run aborts -- any ask_user gate answered by
# NonInteractiveResponder, an exhausted loop, a budget stop -- every later gate
# here checks for artifacts the child never got far enough to write, and
# reports a missing file several stages removed from the cause.  This reads the
# child's own journal and reports the stage it actually died on.
_CHILD_REPORT = str(Path(__file__).parent / "_child_run.py")

_diagnose_child_cmd = (
    f"cd {shlex.quote(PROJECT_DIR)} && "
    f"{shlex.quote(sys.executable)} {shlex.quote(_CHILD_REPORT)} report "
    f"--launch-dir {shlex.quote(PROJECT_DIR)} --state-key implement_features_v2"
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
# Child run stage
# ---------------------------------------------------------------------------

# Floor for the child's ceiling, so a parent that has already spent its whole
# budget still starts the child and lets it report its own budget stop rather
# than handing it a zero or negative limit it cannot act on.
MIN_CHILD_BUDGET = 1.0


class RunChildPipeline(RunCommand):
    """Run the child pipeline with what is left of this run's cost ceiling.

    The child is a separate norn run with a budget of its own, and its default
    is lower than this pipeline's.  Left alone it stops at that default however
    high the ceiling here is set — and a budget stop is an ``ask_user`` gate,
    which ``--non-interactive`` answers by aborting.

    The ceiling has to be read when the stage runs, not when this module is
    imported: ``norn run --budget`` and the TUI's budget field both reach the
    pipeline through ``dsl.apply_budget_overrides`` *after* import, and an
    extension granted at a budget prompt raises the limit later still.  So the
    stage holds the pipeline and reads ``budgets`` each time.

    What travels down is the remainder, not the whole ceiling: planning and
    review already spent part of it here, and the point of a ceiling is that
    the work underneath it adds up to no more than the number asked for.
    """

    def __init__(self, *, cmd: str, pipeline: Pipeline, timeout: float | None = None) -> None:
        super().__init__(cmd=cmd, timeout=timeout)
        self._base_cmd = cmd
        self._pipeline = pipeline

    def remaining_budget(self, ctx) -> float | None:
        """Return the child's ceiling in USD, or None when this run has none."""
        limits = [
            b.max_cost_usd for b in (self._pipeline.budgets or [])
            if b.max_cost_usd is not None
        ]
        if not limits:
            return None
        spent = ctx.usage_tracker.total_cost_usd or 0.0
        return max(round(min(limits) - spent, 2), MIN_CHILD_BUDGET)

    async def run(self, ctx, **kwargs):
        budget = self.remaining_budget(ctx)
        self.cmd = (
            self._base_cmd if budget is None
            else f"{self._base_cmd} --arg budget={budget}"
        )
        return await super().run(ctx, **kwargs)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

_pipeline = (
    Pipeline("fix_jira_issue_v1")
    .alert(MacOSChannel(events={AlertEvent.ASK_USER, AlertEvent.COMPLETE, AlertEvent.FAILED}))
    .budget(max_cost_usd=BUDGET, on_exceed=ask_user)
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
                model="light",
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
    codex_label=CODEX_LABEL,
    split_skill=SPLIT_SKILL,
    pause_for_approval=STOP_AFTER_PLAN,
)

# 21. implement
#
# `ask_user` so [r]etry re-runs the child, which resumes and skips the steps it
# already committed.  [c]ontinue lands on the diagnosis stage below rather than
# on a gate that can only report a missing file.
config.stage(
    "implement",
    RunChildPipeline(cmd=CHILD, pipeline=_pipeline, timeout=None),
    on_failure=ask_user,
)

# 22. diagnose the child run — only when it failed.
#
# Ends the run with the child's own failed stage and error text.  Without it,
# [c]ontinue at the prompt above walks into `check implementation`, which fails
# on artifacts the child never reached the stage of writing.
config.stage(
    "diagnose implementation",
    RunCommand(cmd=_diagnose_child_cmd),
    when=stage_failed("implement"),
    on_failure=fail,
)

# 23. check implementation — only when the child reported success, so this gate
#     speaks only to what the child actually produced.
config.stage(
    "check implementation",
    RunCommand(cmd=_check_impl_cmd),
    when=stage_succeeded("implement"),
    on_failure=fail,
)

# 24. summary
config.stage(
    "summary",
    RunCommand(cmd=_summary_cmd),
)

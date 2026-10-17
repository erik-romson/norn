"""Private helper: the gate-4 ship builder for fix_jira_issue_v3 — not a pipeline.

The ONE home of ``add_ship_stages`` (stages 52-68 of fix_jira_issue_v3's
stage table):

* ``record rewrite range`` opens gate 4: it clears the phase's outputs and
  records ``base``/``start``/``head``/``tree`` of the range to regroup;
* the commit plan, written by an agent following the ``split-commit-plan``
  skill and validated by ``_commit_rewrite_v3.py check``;
* the PR title and summary (the only agent-written PR text) and the
  deterministic PR body around them;
* gate 4 itself (``<KEY>-ship.md``), the rewrite, and ``record shipped head``,
  which writes the first line of ``v3/shipped-heads`` (A18).

``project_dir`` is the git toplevel (the pipeline's preflight checks it):
``_commit_rewrite_v3.py`` refuses any other ``--repo``.
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

from norn.dsl import Pipeline, Stage, ask_user, fail
from norn.model_labels import resolve_label
from norn.models import PipelineContext, StageResult
from norn.pipelines._comment_style import COMMENT_RULE
from norn.pipelines._decisions_v3 import (
    AutoDecide,
    DecisionGate,
    ShipDecisionGate,
    WriteDecision,
    read_field,
)
from norn.pipelines._fix_jira_v3 import (
    V3Paths,
    _headings,
    attribution_lines,
    followup_items,
    human_checklist,
    human_followup_items,
    human_sections,
    shorten_home,
    subject_problems,
    test_steps,
)
from norn.pipelines._plan_gates_v2 import format_gate_error
from norn.pipelines._plan_review_v2 import _SUBAGENT_LABEL, _TOOLS
from norn.runner import resolve_run_path
from norn.skills import Skill
from norn.stages.base import BaseStage
from norn.stages.generate import Generate
from norn.stages.run_command import RunCommand

REWRITE_SCRIPT = str(Path(__file__).resolve().parent / "_commit_rewrite_v3.py")

COMMIT_SKILL = "split-commit-plan"
COMMIT_STYLE_FILE = "commit-message-style.md"

COMMIT_PLAN_MODEL = "standard"
PR_SUMMARY_MODEL = "light"
# The summary agent reads three files and writes two; it runs no command.
PR_SUMMARY_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep"]
PR_SUMMARY_MAX_LINES = 8

BODY_START = "<!-- norn:fix_jira_issue_v3:start -->"
BODY_END = "<!-- norn:fix_jira_issue_v3:end -->"
# GitHub refuses a body over 65,536 characters; keep a margin for later edits.
BODY_LIMIT = 60_000
DETAILS_OPEN = "<details><summary>Full instructions</summary>"
DETAILS_CLOSE = "</details>"

# What `write commit plan`'s prompt shows until the stage runs; the real range
# is read from rewrite-range.txt then (it does not exist at import time).
START_PREVIEW = "<start from rewrite-range.txt>"
HEAD_PREVIEW = "<head from rewrite-range.txt>"

RANGE_KEYS = ("base", "start", "head", "tree")


def _ok(output: Any = "") -> StageResult:
    return StageResult(name="", success=True, output=output)


def _fail(error: str, output: Any = "") -> StageResult:
    return StageResult(name="", success=False, error=error, output=output)


def _subagent_env() -> dict[str, str]:
    return {
        "CLAUDE_CODE_SUBAGENT_MODEL": resolve_label("claude-code", _SUBAGENT_LABEL).model,
        "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1",
    }


def _run(cwd: str | None, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        cwd=cwd,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _script(*args: str) -> subprocess.CompletedProcess[str]:
    """Run ``_commit_rewrite_v3.py`` (it pins every git call to ``--repo`` itself)."""
    return _run(None, sys.executable, REWRITE_SCRIPT, *args)


def _lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def _repo_fail(proc: subprocess.CompletedProcess[str]) -> StageResult:
    return _fail(
        format_gate_error(
            "cannot read the repository state",
            _lines(proc.stderr),
            "Fix the repository state, then --resume.",
        )
    )


class ShipInputError(Exception):
    """A file an earlier stage writes is missing or malformed; ``str()`` is the stage error."""


def checklist_file(paths: V3Paths) -> str:
    """The checklist ``assemble pr body`` wrote, one item per line (empty: no items).

    ``pr body still complete`` reads it to know whether ``## For the human``
    must be present.
    """
    return str(Path(paths.work) / "pr-body-checklist.md")


def read_rewrite_range(path: str | Path) -> dict[str, str]:
    """Parse ``rewrite-range.txt``: one ``<key> <sha>`` line per key of ``RANGE_KEYS``."""
    p = Path(path)
    if not p.is_file():
        raise ShipInputError(
            format_gate_error(
                f"{p} is missing", [], "Run without --resume so 'record rewrite range' runs."
            )
        )
    out: dict[str, str] = {}
    for line in p.read_text().splitlines():
        parts = line.split()
        if len(parts) == 2:
            out[parts[0]] = parts[1]
    missing = [k for k in RANGE_KEYS if not out.get(k)]
    if missing:
        raise ShipInputError(
            format_gate_error(
                f"{p} has no {', '.join(missing)} line",
                [],
                "Run without --resume so 'record rewrite range' runs.",
            )
        )
    return out


def commits_mode(ship: str | Path) -> str | None:
    """``COMMITS`` of the ship file, lower-cased (the gate accepts any case); None when unset."""
    value = read_field(ship, "COMMITS")
    return value.lower() if value else None


def _is_rewrite(paths: V3Paths) -> Callable[[PipelineContext], bool]:
    """The ``when`` of every stage that only a ``COMMITS: rewrite`` decision runs."""

    def when(ctx: PipelineContext) -> bool:
        return commits_mode(paths.ship) == "rewrite"

    return when


def _read_required(ctx: PipelineContext, path: str, what: str) -> str:
    resolved = resolve_run_path(ctx, path)
    if not resolved.is_file():
        raise ShipInputError(
            format_gate_error(
                f"{what} is missing: {resolved}", [], "Run without --resume so it is written."
            )
        )
    return resolved.read_text()


def _read_optional(ctx: PipelineContext, path: str) -> str:
    """A source the child run writes only sometimes (README, handoff, followups)."""
    resolved = resolve_run_path(ctx, path)
    return resolved.read_text() if resolved.is_file() else ""


# ---------------------------------------------------------------------------
# The commit skill (finding 18)
# ---------------------------------------------------------------------------


def resolve_commit_skill() -> tuple[Skill | None, list[str]]:
    """Find ``split-commit-plan`` (``SKILL.md`` plus its style file); never raises when missing.

    The bases are ``norn.skills``'s, in its order (pipeline-local, project,
    user), resolved against the process cwd: a documented multi-location
    lookup.  The first base holding both files wins.  Returns the skill (or
    None) and the skill directories searched.
    """
    cwd = Path.cwd()
    bases = [cwd / "skills", cwd / ".claude" / "skills", Path.home() / ".claude" / "skills"]
    searched: list[str] = []
    for base in bases:
        folder = base / COMMIT_SKILL
        searched.append(str(folder))
        skill_md = folder / "SKILL.md"
        style = folder / COMMIT_STYLE_FILE
        if skill_md.is_file() and style.is_file():
            content = skill_md.read_text() + "\n\n" + style.read_text()
            return Skill(COMMIT_SKILL, content), searched
    return None, searched


class CheckSkill(BaseStage):
    """``check commit skill``: fail before ``Generate.run`` would crash on a missing skill."""

    needs_agent = False

    def __init__(self, *, skill: Skill | None, searched: list[str]) -> None:
        self.skill = skill
        self.searched = searched

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        if self.skill is None:
            return _fail(
                format_gate_error(
                    f"the {COMMIT_SKILL} skill was not found",
                    [f"searched: {p}" for p in self.searched],
                    f"Put SKILL.md and {COMMIT_STYLE_FILE} in one of those directories, "
                    "then --resume.",
                )
            )
        return _ok(self.skill.name)


# ---------------------------------------------------------------------------
# Gate-4 opener
# ---------------------------------------------------------------------------


class RecordRewriteRange(BaseStage):
    """Open gate 4: clear its outputs, then record the range ``apply`` will regroup."""

    needs_agent = False

    def __init__(self, *, paths: V3Paths, project_dir: str, base_file: str, key: str) -> None:
        self.paths = paths
        self.project_dir = project_dir
        self.base_file = base_file
        self.key = key

    def _outputs(self) -> list[str]:
        p = self.paths
        return [
            p.commit_plan,
            p.ship,
            p.pr_body,
            checklist_file(p),
            p.pr_title,
            p.pr_summary,
            p.commit_plan_problems,
            p.pr_summary_problems,
            p.rewritten,
            p.shipped_heads,
            p.rewrite_range,
        ]

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        base_path = resolve_run_path(ctx, self.base_file)
        if not base_path.is_file():
            return _fail(
                format_gate_error(
                    f"{base_path} is missing", [], "Run without --resume to start fresh."
                )
            )
        base = base_path.read_text().strip()
        for name in self._outputs():
            resolve_run_path(ctx, name).unlink(missing_ok=True)

        proc = _script("range", "--repo", self.project_dir, "--base", base, "--key", self.key)
        if proc.returncode == 1 and _lines(proc.stdout):
            problems = _lines(proc.stdout)
            return _fail(
                format_gate_error(
                    f"cannot regroup {base[:7]}..HEAD: {len(problems)} problems",
                    problems,
                    "Fix them as each line says, then --resume.",
                ),
                output="\n".join(problems),
            )
        if proc.returncode != 0:
            return _fail(
                format_gate_error(
                    f"_commit_rewrite_v3.py range exited with status {proc.returncode}",
                    _lines(proc.stderr + proc.stdout),
                    "Fix the repository state, then --resume.",
                )
            )
        data = json.loads(proc.stdout)
        work = resolve_run_path(ctx, self.paths.work)
        work.mkdir(parents=True, exist_ok=True)
        resolve_run_path(ctx, self.paths.rewrite_range).write_text(
            f"base {base}\nstart {data['start']}\nhead {data['head']}\ntree {data['tree']}\n"
        )
        return _ok(f"{data['start'][:7]}..{data['head'][:7]}: {data['count']} commits")


# ---------------------------------------------------------------------------
# Script checks
# ---------------------------------------------------------------------------


def _describe(argv: Sequence[str]) -> str:
    """``<script>.py <subcommand>`` for a stage error headline."""
    for i, token in enumerate(argv):
        if token.endswith(".py"):
            sub = argv[i + 1] if i + 1 < len(argv) else ""
            return f"{Path(token).name} {sub}".strip()
    return argv[0] if argv else "script"


class ScriptCheck(BaseStage):
    """Run a script: exit 0 passes, 1 lists problems on stdout, 2 is a usage error.

    With *problems_file*, exit 1 writes the script's stdout there (the repair
    agent reads it) and exit 0 deletes it.  *argv* is a list, or a callable
    that builds the list when the stage runs (it may raise ``ShipInputError``).
    """

    needs_agent = False

    def __init__(
        self,
        *,
        argv: Sequence[str] | Callable[[PipelineContext], Sequence[str]],
        problems_file: str | None = None,
    ) -> None:
        self.argv = argv
        self.problems_file = problems_file

    def _action(self) -> str:
        if self.problems_file is not None:
            return (
                "The next attempt gives them to the agent; once the loop is exhausted, "
                "fix them by hand, then [r]etry."
            )
        return "Fix them as each line says, then --resume."

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        try:
            argv = list(self.argv(ctx) if callable(self.argv) else self.argv)
        except ShipInputError as exc:
            return _fail(str(exc))
        what = _describe(argv)
        proc = _run(None, *argv)
        problems_path = (
            resolve_run_path(ctx, self.problems_file) if self.problems_file is not None else None
        )
        if proc.returncode == 0:
            if problems_path is not None:
                problems_path.unlink(missing_ok=True)
            return _ok(proc.stdout.strip())
        problems = _lines(proc.stdout)
        if proc.returncode == 1 and problems:
            if problems_path is not None:
                problems_path.parent.mkdir(parents=True, exist_ok=True)
                problems_path.write_text(proc.stdout)
            return _fail(
                format_gate_error(f"{what}: {len(problems)} problems", problems, self._action()),
                output="\n".join(problems),
            )
        headline = (
            f"{what}: usage error"
            if proc.returncode == 2
            else f"{what} exited with status {proc.returncode}"
        )
        return _fail(
            format_gate_error(
                headline, _lines(proc.stderr + proc.stdout), "Fix its inputs, then --resume."
            )
        )


def check_plan_argv(
    *, paths: V3Paths, project_dir: str, key: str
) -> Callable[[PipelineContext], list[str]]:
    """``_commit_rewrite_v3.py check`` against the range ``record rewrite range`` pinned."""

    def argv(ctx: PipelineContext) -> list[str]:
        rng = read_rewrite_range(resolve_run_path(ctx, paths.rewrite_range))
        return [
            sys.executable,
            REWRITE_SCRIPT,
            "check",
            "--plan",
            str(resolve_run_path(ctx, paths.commit_plan)),
            "--repo",
            project_dir,
            "--base",
            rng["base"],
            "--key",
            key,
            "--expect-head",
            rng["head"],
        ]

    return argv


class CheckCommitMessages(ScriptCheck):
    """``messages`` over ``base.sha..HEAD``: the house style for ``rewrite``, attribution for ``keep``.

    ``COMMITS`` is read when the stage runs.
    """

    def __init__(self, *, paths: V3Paths, project_dir: str, base_file: str, key: str) -> None:
        self.paths = paths
        self.project_dir = project_dir
        self.base_file = base_file
        self.key = key
        self._checked: str | None = None  # the COMMITS value the last run checked
        super().__init__(argv=self._messages_argv)

    def _mode(self, ctx: PipelineContext) -> str:
        mode = commits_mode(resolve_run_path(ctx, self.paths.ship))
        if mode not in ("rewrite", "keep"):
            raise ShipInputError(
                format_gate_error(
                    f"COMMITS is {mode or 'missing'} in {self.paths.ship}",
                    [],
                    "Set COMMITS to rewrite or keep, then --resume.",
                )
            )
        return mode

    def _messages_argv(self, ctx: PipelineContext) -> list[str]:
        mode = self._mode(ctx)
        self._checked = mode
        base = _read_required(ctx, self.base_file, "base.sha").strip()
        argv = [
            sys.executable,
            REWRITE_SCRIPT,
            "messages",
            "--repo",
            self.project_dir,
            "--key",
            self.key,
            "--range",
            f"{base}..HEAD",
        ]
        if mode == "keep":
            argv.append("--attribution-only")
        return argv

    def _action(self) -> str:
        return "Reword those commits (git rebase -i <base.sha>), then --resume."

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        result = await super().run(ctx, **kwargs)
        if not result.success:
            return result
        if self._checked == "keep":
            return _ok("COMMITS: keep — no attribution in the commit messages")
        return _ok("COMMITS: rewrite — every commit message passes the house style")


# ---------------------------------------------------------------------------
# The commit plan
# ---------------------------------------------------------------------------


def commit_plan_prompt(*, paths: V3Paths, key: str, start: str, head: str, issue_md: str) -> str:
    """The ``write commit plan`` prompt: the skill plus every override of it (external-docs 1d)."""
    return f"""\
Plan how to regroup the commits of {key} into new commits, following the
{COMMIT_SKILL} skill in your system prompt. Its {COMMIT_STYLE_FILE} follows
the skill there; do not look for either file on disk. The skill's start
commit is {start}, so the range is `{start}^..{head}`.

These rules override the skill wherever they disagree:

- This is an unattended stage: skip the phase-1 chat and write the file now.
  Nobody will answer a question or confirm a proposal.
- Write the plan to {paths.commit_plan}, not to `tmp/plans/…`.
- `Start commit (inclusive)` is `{start}` and `HEAD at plan time` is `{head}`.
  Keep the skill's header bullets `**Start commit (inclusive):**` and
  `**HEAD at plan time:**`, each followed by the full sha in backticks, and the
  group headings `## Group <n> — <title>`.
- Whole-file groups only: assign every changed file whole, under
  `**Files (whole-file):**`. Write no `**Hunks (partial-file):**` bullets.
- Every subject is `{key}: <lowercase summary>`: at most 72 characters, a
  lower-case word right after `{key}: `, and no trailing period. Do not copy
  the `refactor:` style of the commits in scope. Do not use `KEY type:` forms,
  comma-joined keys, exempt prefixes (chore, style, docs, ci, build) or
  `#KEY` forms.
- The body is optional and has at most 4 lines, not 6, after one blank line.
- No footers or trailers.
- Pass no Jira hints and do not run `jira-fetch.sh`: the issue text is at
  {issue_md}.
- Read-only git only (`git log`, `git diff`, `git show`, `git rev-list`,
  `git merge-base`). Run no other command and change no file but the plan.
- If {paths.commit_plan_problems} exists, the plan failed its check: fix
  exactly the problems listed there and change nothing else.

{COMMENT_RULE}
"""


def _needs_commit_plan(paths: V3Paths) -> Callable[[PipelineContext], bool]:
    """The ``when`` of ``write commit plan``: no plan yet, or its check failed."""

    def when(ctx: PipelineContext) -> bool:
        return not Path(paths.commit_plan).exists() or Path(paths.commit_plan_problems).exists()

    return when


class CommitPlanGenerate(Generate):
    """``write commit plan``: the prompt is built from ``rewrite-range.txt`` when the stage runs.

    ``prompt`` holds a preview until then, with the range shown as ``<…>``.
    """

    def __init__(self, *, paths: V3Paths, key: str, issue_md: str, skill: Skill | None) -> None:
        self.paths = paths
        self.key = key
        self.issue_md = issue_md
        super().__init__(
            prompt=commit_plan_prompt(
                paths=paths, key=key, start=START_PREVIEW, head=HEAD_PREVIEW, issue_md=issue_md
            ),
            model=COMMIT_PLAN_MODEL,
            skills=[skill] if skill else [],
            allowed_tools=list(_TOOLS),
            permission_mode="acceptEdits",
            setting_sources=["project"],
            env=_subagent_env(),
        )

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        try:
            rng = read_rewrite_range(resolve_run_path(ctx, self.paths.rewrite_range))
        except ShipInputError as exc:
            return _fail(str(exc))
        self.prompt = commit_plan_prompt(
            paths=self.paths,
            key=self.key,
            start=rng["start"],
            head=rng["head"],
            issue_md=self.issue_md,
        )
        return await super().run(ctx, **kwargs)


def plan_summary(plan: str) -> list[str]:
    """``_commit_rewrite_v3.py summary``: ``<n>. <subject> (<k> files)`` per group."""
    proc = _script("summary", "--plan", plan)
    if proc.returncode != 0:
        raise ShipInputError(
            format_gate_error(
                f"cannot summarize the commit plan {plan} (exit {proc.returncode})",
                _lines(proc.stdout + proc.stderr),
                "Fix the commit plan, then --resume.",
            )
        )
    return _lines(proc.stdout)


_SUMMARY_LINE_RE = re.compile(r"^\d+\.\s+(.*)$")


# ---------------------------------------------------------------------------
# PR title and summary
# ---------------------------------------------------------------------------


def pr_summary_prompt(*, paths: V3Paths, key: str, plan: str, issue_md: str) -> str:
    return f"""\
Write the title and the summary of the pull request for {key}.

Read:
- {issue_md}: the Jira issue.
- {plan}: the plan the change implements, with the reasons for it.
- {paths.commit_plan}: the commits and the files each one changes.

Write exactly these two files:

1. {paths.pr_title}: one line, the PR title, as `{key}: <lowercase summary>`:
   at most 72 characters, a lower-case word right after `{key}: `, and no
   trailing period.
2. {paths.pr_summary}: 1 to {PR_SUMMARY_MAX_LINES} lines for a reviewer,
   saying what changed and why. Plain sentences or bullets; no headings.

- Change no other file and run no command.
- If {paths.pr_summary_problems} exists, the files failed their check: fix
  exactly the problems listed there and change nothing else.

{COMMENT_RULE}
"""


def _needs_pr_summary(paths: V3Paths) -> Callable[[PipelineContext], bool]:
    """The ``when`` of ``write pr summary``: a file is missing, or the check failed."""

    def when(ctx: PipelineContext) -> bool:
        return (
            not Path(paths.pr_title).exists()
            or not Path(paths.pr_summary).exists()
            or Path(paths.pr_summary_problems).exists()
        )

    return when


def pr_summary_problems(title: str | None, summary: str | None, key: str) -> list[str]:
    """One line per problem with ``pr-title.txt`` and ``pr-summary.md`` (None: missing)."""
    problems: list[str] = []
    if title is None:
        problems.append("pr-title.txt was not written")
    else:
        title_lines = _lines(title)
        if len(title_lines) != 1:
            problems.append(f"pr-title.txt has {len(title_lines)} lines; it must hold exactly one")
        else:
            problems += [f"title: {p}" for p in subject_problems(title_lines[0].strip(), key)]
    if summary is None:
        problems.append("pr-summary.md was not written")
    else:
        count = len(_lines(summary))
        if not 1 <= count <= PR_SUMMARY_MAX_LINES:
            problems.append(
                f"pr-summary.md has {count} non-blank lines; it must have 1 to {PR_SUMMARY_MAX_LINES}"
            )
        problems += [f"summary: attribution line: {ln.strip()!r}" for ln in attribution_lines(summary)]
    return problems


class PrSummaryGate(BaseStage):
    """The title passes the house style; the summary has 1-8 lines and no attribution."""

    needs_agent = False

    def __init__(self, *, paths: V3Paths, key: str, problems_file: str | None = None) -> None:
        self.paths = paths
        self.key = key
        self.problems_file = problems_file

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        title_path = resolve_run_path(ctx, self.paths.pr_title)
        summary_path = resolve_run_path(ctx, self.paths.pr_summary)
        problems = pr_summary_problems(
            title_path.read_text() if title_path.is_file() else None,
            summary_path.read_text() if summary_path.is_file() else None,
            self.key,
        )
        if self.problems_file is not None:
            pf = resolve_run_path(ctx, self.problems_file)
            if problems:
                pf.parent.mkdir(parents=True, exist_ok=True)
                pf.write_text("\n".join(problems) + "\n")
            else:
                pf.unlink(missing_ok=True)
        if problems:
            action = (
                "The next attempt gives them to the agent; once the loop is exhausted, "
                "fix them by hand, then [r]etry."
                if self.problems_file is not None
                else f"Fix {title_path} and {summary_path}, then --resume."
            )
            return _fail(
                format_gate_error(f"{len(problems)} problems in the PR title or summary", problems, action),
                output="\n".join(problems),
            )
        return _ok()


# ---------------------------------------------------------------------------
# PR body (D8)
# ---------------------------------------------------------------------------


def _jira_line(*, key: str, summary: str, jira_browse: str | None) -> str:
    if jira_browse:
        return f"Jira: [{key}]({jira_browse}{key}) — {summary}"
    return f"Jira: {key} — {summary}"


def _section(heading: str, lines: list[str]) -> list[str]:
    return [heading, *lines, ""] if lines else []


def render_pr_body(
    *,
    jira_line: str,
    summary: str,
    commits: list[str],
    checklist: list[str],
    instructions: str,
    plan_path: str,
    followups: list[str],
    nits: list[str],
    reword: list[str],
    testing_line: str,
    test_labels: list[str],
    details: bool,
) -> str:
    """The marker block of the PR body.

    With *details* the human sections go verbatim into a ``<details>`` block;
    without it, a ``Full instructions: <plan path>`` line replaces the block.
    """
    lines = [BODY_START, jira_line, *_lines_kept(summary), ""]
    lines += ["## Commits", *commits, ""]
    if checklist:
        lines += ["## For the human", *checklist, ""]
        if instructions and details:
            lines += [DETAILS_OPEN, "", instructions, "", DETAILS_CLOSE, ""]
        elif instructions:
            lines += [f"Full instructions: {shorten_home(plan_path)}", ""]
    lines += _section("## Follow-ups", followups)
    lines += _section("## Nits (not fixed)", nits)
    lines += _section("## Reword at squash/merge", reword)
    lines += ["## Testing", testing_line, *[f"- {label}" for label in test_labels], BODY_END]
    return "\n".join(lines) + "\n"


def _lines_kept(text: str) -> list[str]:
    """*text*'s lines, with leading and trailing blank lines dropped."""
    return text.strip("\n").splitlines() if text.strip() else []


class AssemblePrBody(BaseStage):
    """Write ``<KEY>-pr-body.md`` deterministically; the agent wrote only the summary.

    ``project_dir`` is needed for ``git log`` (the Reword section).
    """

    needs_agent = False

    def __init__(
        self,
        *,
        paths: V3Paths,
        plan: str,
        steps_dir: str,
        issue_md: str,
        key: str,
        jira_browse: str | None,
        project_dir: str,
    ) -> None:
        self.paths = paths
        self.plan = plan
        self.steps_dir = steps_dir
        self.issue_md = issue_md
        self.key = key
        self.jira_browse = jira_browse
        self.project_dir = project_dir

    def _step_file(self, name: str) -> str:
        return str(Path(self.steps_dir) / name)

    def _human(self, ctx: PipelineContext) -> tuple[list[str], list[str], str]:
        """``(sections, checklist, followups.md text)``."""
        sources = [_read_required(ctx, self.plan, "the final plan")]
        sources += [_read_optional(ctx, self._step_file(n)) for n in ("README.md", "handoff.md")]
        sections: list[str] = []
        seen: set[str] = set()
        for text in sources:
            for section in human_sections(text):
                norm = " ".join(section.split())
                if norm not in seen:
                    seen.add(norm)
                    sections.append(section)
        followups_md = _read_optional(ctx, self._step_file("followups.md"))
        checklist = human_checklist(sections) + human_followup_items(followups_md)
        return sections, checklist, followups_md

    def _reword(self, base: str) -> list[str]:
        log = _run(
            self.project_dir, "git", "log", "--reverse", "--format=%H %s", f"{base}..HEAD"
        )
        if log.returncode != 0:
            raise ShipInputError(
                format_gate_error(
                    f"cannot list the commits of {base[:7]}..HEAD",
                    _lines(log.stderr),
                    "Fix the repository state, then --resume.",
                )
            )
        out: list[str] = []
        for line in _lines(log.stdout):
            sha, _, subject = line.partition(" ")
            if subject_problems(subject, self.key):
                out.append(f"- {sha[:7]} {subject}")
        return out

    def _testing(self, ctx: PipelineContext, tree: str) -> tuple[str, list[str]]:
        proof = _read_required(ctx, self.paths.test_proof, "the gate-3 test proof").splitlines()
        if len(proof) < 2 or not proof[1].strip():
            raise ShipInputError(
                format_gate_error(
                    f"{self.paths.test_proof} has no tree line",
                    [],
                    "Run without --resume so the gate-3 tests run.",
                )
            )
        tested = proof[1].strip()
        # The proof's tree is HEAD's tree when the tests passed. The review
        # fixes were not committed yet, so that tree lacks them: never show it
        # as the tree the PR ships.
        if tested == tree:
            line = f"Local, passed on tree {tested[:7]} (gate 3):"
        else:
            line = f"Local, passed on tree {tested[:7]} plus the changes uncommitted at the time (gate 3):"
        script = _read_required(ctx, self.paths.test_cmd_sh, "the gate-3 test script").splitlines()
        # `set -e` is render_test_script's first line, not a test step.
        if script and script[0].strip() == "set -e":
            script = script[1:]
        return line, test_steps("\n".join(script))

    def _build(self, ctx: PipelineContext) -> tuple[str, str, list[str]]:
        """``(body with details, body without details, checklist)``."""
        rng = read_rewrite_range(resolve_run_path(ctx, self.paths.rewrite_range))
        first = _lines_kept(_read_required(ctx, self.issue_md, "issue.md"))
        summary_line = first[0].lstrip("#").strip() if first else ""
        commits = []
        for line in plan_summary(str(resolve_run_path(ctx, self.paths.commit_plan))):
            m = _SUMMARY_LINE_RE.match(line)
            commits.append(f"- {m.group(1) if m else line}")
        sections, checklist, followups_md = self._human(ctx)
        followups = _lines(_read_required(ctx, self.paths.review_followups, "review-followups.md"))
        followups += followup_items(followups_md)
        nits = _lines(_read_required(ctx, self.paths.review_nits, "review-nits.md"))
        testing_line, labels = self._testing(ctx, rng["tree"])
        kwargs = dict(
            jira_line=_jira_line(key=self.key, summary=summary_line, jira_browse=self.jira_browse),
            summary=_read_required(ctx, self.paths.pr_summary, "pr-summary.md"),
            commits=commits,
            checklist=checklist,
            instructions=shorten_home("\n\n".join(sections)),
            plan_path=self.plan,
            followups=followups,
            nits=nits,
            reword=self._reword(rng["base"]),
            testing_line=testing_line,
            test_labels=labels,
        )
        return (
            render_pr_body(**kwargs, details=True),
            render_pr_body(**kwargs, details=False),
            checklist,
        )

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        try:
            full, short, checklist = self._build(ctx)
        except ShipInputError as exc:
            return _fail(str(exc))
        body = full if len(full) <= BODY_LIMIT else short
        if len(body) > BODY_LIMIT:
            return _fail(
                format_gate_error(
                    f"the PR body is {len(body)} characters; the limit is {BODY_LIMIT}",
                    [f"without the full instructions it is still {len(short)}"],
                    "Shorten the summary, the follow-ups or the human checklist, then --resume.",
                )
            )
        out = resolve_run_path(ctx, self.paths.pr_body)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(body)
        side = resolve_run_path(ctx, checklist_file(self.paths))
        side.parent.mkdir(parents=True, exist_ok=True)
        side.write_text("".join(f"{item}\n" for item in checklist))
        shown = "" if body is full else " (full instructions replaced by the plan path)"
        return _ok(f"{out}: {len(body)} characters{shown}")


_CHECKBOX_RE = re.compile(r"^\s*[-*] \[[ xX]\]")


def pr_body_problems(text: str, key: str, *, expect_human: bool) -> list[str]:
    """One line per way *text* breaks the PR-body contract (step 09 edits it by its markers)."""
    problems: list[str] = []
    has_start, has_end = BODY_START in text, BODY_END in text
    if not has_start:
        problems.append(f"the start marker {BODY_START} is missing")
    if not has_end:
        problems.append(f"the end marker {BODY_END} is missing")
    if has_start and has_end and text.index(BODY_START) > text.index(BODY_END):
        problems.append("the end marker comes before the start marker")

    lines = text.splitlines()
    if has_start:
        after = text.split(BODY_START, 1)[1].splitlines()
        first = next((ln.strip() for ln in after if ln.strip()), "")
        if not first.startswith("Jira:") or key not in first:
            problems.append(f"the first line after the start marker must be 'Jira: {key} …', got {first!r}")

    heads = _headings(lines)
    if not any(level == 2 and title == "Commits" for _i, level, title in heads):
        problems.append("the '## Commits' section is missing")

    human = [
        (pos, i) for pos, (i, level, title) in enumerate(heads)
        if level == 2 and title.lower().startswith("for the human")
    ]
    if expect_human and not human:
        problems.append("the '## For the human' section is missing, but its sources have items")
    if not expect_human and human:
        problems.append("there is a '## For the human' section, but its sources have no items")
    if "<details>" in text:
        checklist = False
        for pos, start in human:
            end = next((i for i, level, _t in heads[pos + 1:] if level <= 2), len(lines))
            checklist = checklist or any(_CHECKBOX_RE.match(ln) for ln in lines[start + 1:end])
        if not checklist:
            problems.append("the <details> block has no '- [ ]' checklist under '## For the human'")

    problems += [f"attribution line: {ln.strip()!r}" for ln in attribution_lines(text)]
    if len(text) > BODY_LIMIT:
        problems.append(f"the body is {len(text)} characters; the limit is {BODY_LIMIT}")
    return problems


class PrBodyGate(BaseStage):
    """``pr body still complete``: the human's gate-4 edits kept the body's contract."""

    needs_agent = False

    def __init__(self, *, paths: V3Paths, key: str) -> None:
        self.paths = paths
        self.key = key

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        body = resolve_run_path(ctx, self.paths.pr_body)
        side = resolve_run_path(ctx, checklist_file(self.paths))
        for needed in (body, side):
            if not needed.is_file():
                return _fail(
                    format_gate_error(
                        f"{needed} is missing",
                        [],
                        "Run without --resume so 'assemble pr body' runs.",
                    )
                )
        problems = pr_body_problems(
            body.read_text(), self.key, expect_human=bool(_lines(side.read_text()))
        )
        if problems:
            return _fail(
                format_gate_error(
                    f"{len(problems)} problems in the PR body {body}",
                    problems,
                    "Fix them in that file, then --resume.",
                ),
                output="\n".join(problems),
            )
        return _ok()


# ---------------------------------------------------------------------------
# Gate 4
# ---------------------------------------------------------------------------


def render_ship(*, key: str, paths: V3Paths, summary: list[str], title: str) -> str:
    """The gate-4 file (format: the shared context's "Gate-file contracts")."""
    return (
        f"# Ship — {key}\n"
        "\n"
        "STATUS: PROPOSED\n"
        "COMMITS: rewrite\n"
        f"TITLE: {title}\n"
        "\n"
        f"Proposed commits (from {paths.commit_plan}; edit groups and messages there):\n"
        + "".join(f"{line}\n" for line in summary)
        + f"PR body: {paths.pr_body} (edit freely)\n"
        "Set COMMITS to rewrite or keep, edit TITLE if you like, then set the STATUS line to DECIDED.\n"
    )


def _render_ship_now(*, key: str, paths: V3Paths) -> Callable[[PipelineContext], str]:
    def render(ctx: PipelineContext) -> str:
        title = _lines(_read_required(ctx, paths.pr_title, "pr-title.txt"))
        return render_ship(
            key=key,
            paths=paths,
            summary=plan_summary(str(resolve_run_path(ctx, paths.commit_plan))),
            title=title[0].strip() if title else "",
        )

    return render


class WriteShipDecision(WriteDecision):
    """``write ship decision``: a missing input is a stage failure, not an exception."""

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        try:
            return await super().run(ctx, **kwargs)
        except ShipInputError as exc:
            return _fail(str(exc))


def rewrite_cmd(*, paths: V3Paths, project_dir: str, key: str) -> str:
    """``apply`` with the range read from ``rewrite-range.txt`` when it runs, then mark it done."""
    q = shlex.quote
    rng = q(paths.rewrite_range)
    return (
        f"BASE=$(sed -n 's/^base //p' {rng}) && HEAD_SHA=$(sed -n 's/^head //p' {rng}) && "
        f"python3 {q(REWRITE_SCRIPT)} apply --plan {q(paths.commit_plan)} --repo {q(project_dir)} "
        f'--base "$BASE" --key {q(key)} --expect-head "$HEAD_SHA" && '
        f"touch {q(paths.rewritten)}"
    )


def _git_line(project_dir: str, *args: str) -> str | StageResult:
    proc = _run(project_dir, "git", *args)
    if proc.returncode != 0:
        return _repo_fail(proc)
    return proc.stdout.strip()


class RewriteTreeGate(BaseStage):
    """``rewrite matches tree``: ``HEAD^{tree}`` is the tree ``record rewrite range`` recorded."""

    needs_agent = False

    def __init__(self, *, paths: V3Paths, project_dir: str) -> None:
        self.paths = paths
        self.project_dir = project_dir

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        try:
            rng = read_rewrite_range(resolve_run_path(ctx, self.paths.rewrite_range))
        except ShipInputError as exc:
            return _fail(str(exc))
        tree = _git_line(self.project_dir, "rev-parse", "HEAD^{tree}")
        if isinstance(tree, StageResult):
            return tree
        if tree != rng["tree"]:
            return _fail(
                format_gate_error(
                    f"the rewritten tree {tree[:7]} is not the recorded tree {rng['tree'][:7]}",
                    [],
                    "Restore with the backup ref 'rewrite commits' printed, then --resume.",
                )
            )
        return _ok(tree)


def _dirty_paths(project_dir: str) -> list[str] | StageResult:
    status = _run(project_dir, "git", "status", "--porcelain", "--untracked-files=normal")
    if status.returncode != 0:
        return _repo_fail(status)
    return [line[3:] for line in status.stdout.splitlines() if line.strip()]


class RecordShippedHead(BaseStage):
    """Close gate 4: check HEAD against the decision, then write ``shipped-heads`` (A18).

    ``keep`` needs HEAD to be the recorded head (A10's gap for ``keep``);
    ``rewrite`` needs ``HEAD^{tree}`` to be the recorded tree.  Both need a
    clean tree, untracked files included.  The file is replaced, not appended.
    """

    needs_agent = False

    def __init__(self, *, paths: V3Paths, project_dir: str) -> None:
        self.paths = paths
        self.project_dir = project_dir

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        try:
            rng = read_rewrite_range(resolve_run_path(ctx, self.paths.rewrite_range))
        except ShipInputError as exc:
            return _fail(str(exc))
        mode = commits_mode(resolve_run_path(ctx, self.paths.ship))
        head = _git_line(self.project_dir, "rev-parse", "HEAD")
        if isinstance(head, StageResult):
            return head

        if mode == "keep":
            if head != rng["head"]:
                return _fail(
                    format_gate_error(
                        f"HEAD moved at gate 4: HEAD is {head[:7]}, the ship phase "
                        f"recorded {rng['head'][:7]}",
                        [f"HEAD: {head}", f"recorded: {rng['head']}"],
                        "Undo the new commit, or start the ship phase over from a fresh run.",
                    )
                )
        elif mode == "rewrite":
            tree = _git_line(self.project_dir, "rev-parse", "HEAD^{tree}")
            if isinstance(tree, StageResult):
                return tree
            if tree != rng["tree"]:
                return _fail(
                    format_gate_error(
                        f"HEAD's tree {tree[:7]} is not the recorded tree {rng['tree'][:7]}",
                        [f"HEAD: {head}"],
                        "Restore with the backup ref 'rewrite commits' printed, then --resume.",
                    )
                )
        else:
            return _fail(
                format_gate_error(
                    f"COMMITS is {mode or 'missing'} in {self.paths.ship}",
                    [],
                    "Set COMMITS to rewrite or keep, then --resume.",
                )
            )

        dirty = _dirty_paths(self.project_dir)
        if isinstance(dirty, StageResult):
            return dirty
        if dirty:
            return _fail(
                format_gate_error(
                    f"{len(dirty)} paths are changed or untracked at the end of gate 4",
                    dirty,
                    "Undo those changes, or add untracked files to .git/info/exclude; then --resume.",
                ),
                output="\n".join(dirty),
            )

        shipped = resolve_run_path(ctx, self.paths.shipped_heads)
        shipped.parent.mkdir(parents=True, exist_ok=True)
        shipped.write_text(f"{head}\n")
        return _ok(head)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def add_ship_stages(
    pipeline: Pipeline,
    *,
    paths: V3Paths,
    project_dir: str,
    key: str,
    plan: str,
    steps_dir: str,
    issue_md: str,
    base_file: str,
    skill: Skill | None,
    skill_searched: list[str],
    jira_browse: str | None,
    auto: bool,
) -> Pipeline:
    """Add stages 52-68 (``record rewrite range`` … ``record shipped head``)."""
    is_rewrite = _is_rewrite(paths)
    check_argv = check_plan_argv(paths=paths, project_dir=project_dir, key=key)

    # 52-53. Opens gate 4; cached on resume, so it clears outputs on a fresh pass only.
    pipeline.stage(
        "record rewrite range",
        RecordRewriteRange(paths=paths, project_dir=project_dir, base_file=base_file, key=key),
        on_failure=fail,
    )
    pipeline.stage(
        "check commit skill",
        CheckSkill(skill=skill, searched=skill_searched),
        on_failure=fail,
    )

    # 54-55. The commit plan.
    pipeline.clear_context()
    pipeline.loop(
        "plan commits",
        max_retries=2,
        on_exhaust=ask_user,
        stages=[
            Stage(
                "write commit plan",
                CommitPlanGenerate(paths=paths, key=key, issue_md=issue_md, skill=skill),
                when=_needs_commit_plan(paths),
            ),
            Stage(
                "check commit plan",
                ScriptCheck(argv=check_argv, problems_file=paths.commit_plan_problems),
            ),
        ],
    )
    pipeline.stage("commit plan valid", ScriptCheck(argv=check_argv), on_failure=fail)

    # 56-57. The PR title and summary.
    pipeline.clear_context()
    pipeline.loop(
        "summarize pr",
        max_retries=2,
        on_exhaust=ask_user,
        stages=[
            Stage(
                "write pr summary",
                Generate(
                    prompt=pr_summary_prompt(paths=paths, key=key, plan=plan, issue_md=issue_md),
                    model=PR_SUMMARY_MODEL,
                    allowed_tools=list(PR_SUMMARY_TOOLS),
                    permission_mode="acceptEdits",
                    setting_sources=["project"],
                    env=_subagent_env(),
                ),
                when=_needs_pr_summary(paths),
            ),
            Stage(
                "check pr summary",
                PrSummaryGate(paths=paths, key=key, problems_file=paths.pr_summary_problems),
            ),
        ],
    )
    pipeline.stage("pr summary valid", PrSummaryGate(paths=paths, key=key), on_failure=fail)

    # 58. The body around the summary.
    pipeline.stage(
        "assemble pr body",
        AssemblePrBody(
            paths=paths,
            plan=plan,
            steps_dir=steps_dir,
            issue_md=issue_md,
            key=key,
            jira_browse=jira_browse,
            project_dir=project_dir,
        ),
        on_failure=fail,
    )

    # 59-62. Gate 4.
    pipeline.stage(
        "write ship decision",
        WriteShipDecision(path=paths.ship, render=_render_ship_now(key=key, paths=paths)),
        on_failure=fail,
    )
    if auto:
        pipeline.stage(
            "auto decide ship",
            AutoDecide(path=paths.ship, field="STATUS", value="DECIDED"),
            on_failure=fail,
        )
    pipeline.stage(
        "ship decided",
        DecisionGate(
            path=paths.ship,
            field="STATUS",
            accept=frozenset({"DECIDED"}),
            action=f"Set the STATUS line to DECIDED in {paths.ship}, then [r]etry.",
        ),
        on_failure=ask_user,
    )
    pipeline.stage(
        "ship decision recorded",
        ShipDecisionGate(path=paths.ship, key=key),
        on_failure=fail,
        always_run=True,
    )

    # 63-66. The rewrite, only for COMMITS: rewrite.
    pipeline.stage(
        "commit plan still valid",
        ScriptCheck(argv=check_argv),
        on_failure=fail,
        when=is_rewrite,
    )
    pipeline.stage("pr body still complete", PrBodyGate(paths=paths, key=key), on_failure=fail)
    pipeline.stage(
        "rewrite commits",
        RunCommand(cmd=rewrite_cmd(paths=paths, project_dir=project_dir, key=key)),
        on_failure=ask_user,
        when=is_rewrite,
    )
    pipeline.stage(
        "rewrite matches tree",
        RewriteTreeGate(paths=paths, project_dir=project_dir),
        on_failure=fail,
        when=is_rewrite,
    )

    # 67-68.
    pipeline.stage(
        "check commit messages",
        CheckCommitMessages(paths=paths, project_dir=project_dir, base_file=base_file, key=key),
        on_failure=fail,
    )
    pipeline.stage(
        "record shipped head",
        RecordShippedHead(paths=paths, project_dir=project_dir),
        on_failure=fail,
    )
    return pipeline

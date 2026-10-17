"""Private helper: the gate-3 code-review builder for fix_jira_issue_v3 — not a pipeline.

The ONE home of ``add_code_review_stages``.  Part 1 (this file's stages 35-45
of fix_jira_issue_v3's stage table):

* ``record review base`` opens gate 3: it clears the phase's outputs, records
  the reviewed range and takes a content-aware snapshot of the worktree;
* two reviewers (codex, then Claude), each a loop with a shape check and a
  hard twin;
* ``review left tree unchanged``: the reviewers changed no file and no commit;
* triage into the gate-3 file, auto decisions, and the gate itself.

Part 2 (the fix loop onwards) is added by ``add_review_fix_stages``.

``ResolveTestCmd`` lives here too: the pipeline wires it before ``implement``
so a test command that cannot resolve stops the run before any child spend.
"""
from __future__ import annotations

import glob
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from norn.dsl import Pipeline, Stage, ask_user, fail
from norn.model_labels import resolve_label
from norn.models import PipelineContext, StageResult
from norn.pipelines._comment_style import COMMENT_RULE
from norn.pipelines._decisions_v3 import AutoDecideCodeReview, CodeReviewGate
from norn.pipelines._fix_jira_v3 import V3Paths
from norn.pipelines._plan_gates_v2 import format_gate_error, front_matter
from norn.pipelines._plan_review_v2 import _SUBAGENT_LABEL
from norn.runner import resolve_run_path
from norn.stages.base import BaseStage
from norn.stages.generate import Generate
from norn.stages.run_command import RunCommand

SNAPSHOT_HELPER = str(Path(__file__).resolve().parent / "_step_snapshot.py")

DEFAULT_TEST_TIMEOUT = 1800
CODEX_REVIEW_TIMEOUT = 1800
REVIEW_MAX_TURNS = 80

REVIEW_TOOLS = ["Read", "Grep", "Glob", "Bash", "Write"]
TRIAGE_TOOLS = ["Read", "Grep", "Glob", "Write", "Edit"]

# What the codex stage's ``cmd`` shows before it runs; the real base is read
# from ``review-range.txt`` at run time.
BASE_PREVIEW = "<base from review-range.txt>"

# Resolved by Generate from `record review base`'s output (the base sha).
BASE_PLACEHOLDER = "{record review base.output}"


def _ok(output: Any = "") -> StageResult:
    return StageResult(name="", success=True, output=output)


def _fail(error: str, output: Any = "") -> StageResult:
    return StageResult(name="", success=False, error=error, output=output)


def _run(cwd: str, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), cwd=cwd, capture_output=True, text=True, check=False)


def _subagent_env() -> dict[str, str]:
    return {
        "CLAUDE_CODE_SUBAGENT_MODEL": resolve_label("claude-code", _SUBAGENT_LABEL).model,
        "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1",
    }


# ---------------------------------------------------------------------------
# Test command (finding 3)
# ---------------------------------------------------------------------------


class UnresolvedTestCmd(ValueError):
    """The gate-3 test command or its timeout cannot be resolved."""


# Copied from implement_features_v3.py:339-353 (cannot be imported: import-time
# argv parsing), with the bool rejection its callers do first.
def _coerce_timeout(raw: object, where: str) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise UnresolvedTestCmd(f"{where}: expected a number of seconds, got {raw!r}")
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise UnresolvedTestCmd(f"{where}: expected a number of seconds, got {raw!r}")
    if value <= 0:
        raise UnresolvedTestCmd(f"{where}: must be a positive number of seconds, got {value!r}")
    return value


def _front_matter_of(path: Path) -> dict:
    text = path.read_text()
    data = front_matter(text)
    if data is None:
        if text.startswith("---\n"):
            raise UnresolvedTestCmd(f"cannot parse the front matter of {path}")
        return {}
    return data


def _str_key(data: dict, key: str) -> str | None:
    value = data.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def resolve_test_cmd(steps_dir: str | Path, test_cmd_arg: str | None) -> tuple[str, float]:
    """Return the gate-3 test command and its timeout.

    The command is *test_cmd_arg* when given; otherwise the child's final test
    rule.  The timeout is always the child's.  Raises ``UnresolvedTestCmd``.
    """
    # Copied from implement_features_v3.py:1929-1994 `_compute_aggregate_cmd_and_timeout`
    # (cannot be imported: import-time argv parsing).
    steps = Path(steps_dir)
    index_path = steps / "index.md"
    index_fm = _front_matter_of(index_path) if index_path.is_file() else {}
    feature_test_cmd = _str_key(index_fm, "test_cmd")
    feature_bats_cmd = _str_key(index_fm, "bats_cmd")
    feature_test_timeout = _coerce_timeout(index_fm.get("test_timeout"), f"{index_path}: test_timeout")
    feature_bats_timeout = _coerce_timeout(index_fm.get("bats_timeout"), f"{index_path}: bats_timeout")
    final_test_cmd = _str_key(index_fm, "final_test_cmd")

    step_files = sorted(glob.glob(os.path.join(str(steps), "step-*.md")))

    parts: list[str] = []
    seen_cmds: set[str] = set()
    missing: list[str] = []
    max_timeout: float = DEFAULT_TEST_TIMEOUT
    for sf in step_files:
        sfm = _front_matter_of(Path(sf))
        stem = Path(sf).stem
        tc = _str_key(sfm, "test_cmd") or feature_test_cmd
        bc = _str_key(sfm, "bats_cmd") or feature_bats_cmd
        if tc is None:
            missing.append(stem)
        else:
            if bc:
                vcmd = f'echo "=== test_cmd ===" && {tc} && echo "=== bats_cmd ===" && {bc}'
            else:
                vcmd = tc
            if vcmd not in seen_cmds:
                seen_cmds.add(vcmd)
                parts.append(f'echo "=== {stem} ===" && {vcmd}')
        test_t = (
            _coerce_timeout(sfm.get("test_timeout"), f"{sf}: test_timeout")
            or feature_test_timeout
            or DEFAULT_TEST_TIMEOUT
        )
        bats_t = (
            _coerce_timeout(sfm.get("bats_timeout"), f"{sf}: bats_timeout")
            or feature_bats_timeout
            or DEFAULT_TEST_TIMEOUT
        )
        max_timeout = max(max_timeout, max(test_t, bats_t) if bc else test_t)

    if final_test_cmd is not None:
        timeout = feature_test_timeout or DEFAULT_TEST_TIMEOUT
    else:
        timeout = max_timeout

    if test_cmd_arg is not None:
        return test_cmd_arg, timeout
    if final_test_cmd is not None:
        return final_test_cmd, timeout
    if not parts:
        raise UnresolvedTestCmd(
            format_gate_error(
                "no test command resolves for the code-review tests",
                [f"index: {index_path}", f"steps dir: {steps}"],
                "Set final_test_cmd or test_cmd in index.md, or pass --arg test_cmd=<cmd>.",
            )
        )
    if missing:
        raise UnresolvedTestCmd(
            format_gate_error(
                f"{len(missing)} step files have no test_cmd and {index_path} sets none",
                missing,
                f"Set test_cmd in those steps or in index.md ({steps}), "
                "or pass --arg test_cmd=<cmd>.",
            )
        )
    return " && ".join(parts), timeout


def render_test_script(cmd: str) -> str:
    """The body of ``test-cmd.sh``: ``set -e`` first, then *cmd* verbatim.

    ``set -e`` makes a plain multi-line command fail on any failing line, not
    just on its last one.
    """
    return f"set -e\n{cmd}\n"


class ResolveTestCmd(BaseStage):
    """Write ``test-cmd.sh`` and ``test-timeout`` for the gate-3 test runs."""

    needs_agent = False

    def __init__(self, *, steps_dir: str, test_cmd_arg: str | None, paths: V3Paths) -> None:
        if test_cmd_arg is not None and not test_cmd_arg.strip():
            raise ValueError("--arg test_cmd: must not be empty")
        self.steps_dir = steps_dir
        self.test_cmd_arg = test_cmd_arg.strip() if test_cmd_arg is not None else None
        self.paths = paths

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        steps = resolve_run_path(ctx, self.steps_dir)
        try:
            cmd, timeout = resolve_test_cmd(steps, self.test_cmd_arg)
        except UnresolvedTestCmd as exc:
            return _fail(str(exc))
        script = resolve_run_path(ctx, self.paths.test_cmd_sh)
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(render_test_script(cmd))
        resolve_run_path(ctx, self.paths.test_timeout).write_text(f"{timeout:g}\n")
        return _ok(cmd)


# ---------------------------------------------------------------------------
# Gate-3 opener
# ---------------------------------------------------------------------------


def read_review_range(path: str | Path) -> dict[str, str]:
    """Parse ``review-range.txt`` (``base <sha>`` / ``head <sha>`` lines)."""
    out: dict[str, str] = {}
    for line in Path(path).read_text().splitlines():
        parts = line.split()
        if len(parts) == 2:
            out[parts[0]] = parts[1]
    return out


def _toplevel(project_dir: str) -> subprocess.CompletedProcess[str]:
    return _run(project_dir, "git", "rev-parse", "--show-toplevel")


def _snapshot(top: str, out: str) -> subprocess.CompletedProcess[str]:
    return _run(top, sys.executable, SNAPSHOT_HELPER, "snapshot", "--root", top, out)


class RecordReviewBase(BaseStage):
    """Open gate 3: clear its outputs, record ``base``/``head``, snapshot the tree."""

    needs_agent = False

    def __init__(self, *, paths: V3Paths, project_dir: str, base_file: str) -> None:
        self.paths = paths
        self.project_dir = project_dir
        self.base_file = base_file

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        base_path = resolve_run_path(ctx, self.base_file)
        if not base_path.is_file():
            return _fail(
                format_gate_error(
                    f"{base_path} is missing", [], "Run without --resume to start fresh."
                )
            )
        base = base_path.read_text().strip()
        head = _run(self.project_dir, "git", "rev-parse", "HEAD")
        top = _toplevel(self.project_dir)
        for proc in (head, top):
            if proc.returncode != 0:
                return _fail(
                    format_gate_error(
                        "cannot read the repository state",
                        [proc.stderr.strip()],
                        "Fix the repository state, then --resume.",
                    )
                )

        work = resolve_run_path(ctx, self.paths.work)
        for name in (
            self.paths.code_review,
            self.paths.code_review_problems,
            self.paths.review_fixes,
            self.paths.owned,
        ):
            resolve_run_path(ctx, name).unlink(missing_ok=True)
        for stale in work.glob("review-*.md"):
            stale.unlink()
        fix_dir = resolve_run_path(ctx, self.paths.fix_dir)
        if fix_dir.exists():
            shutil.rmtree(fix_dir)

        work.mkdir(parents=True, exist_ok=True)
        resolve_run_path(ctx, self.paths.review_range).write_text(
            f"base {base}\nhead {head.stdout.strip()}\n"
        )
        snap = _snapshot(top.stdout.strip(), str(resolve_run_path(ctx, self.paths.review_pre)))
        if snap.returncode != 0:
            return _fail(
                format_gate_error(
                    "cannot snapshot the worktree before the review",
                    [snap.stderr.strip()],
                    "Fix the repository state, then --resume.",
                )
            )
        return _ok(base)


# ---------------------------------------------------------------------------
# Reviewers
# ---------------------------------------------------------------------------

REVIEW_ANGLES = """\
Check the change from these five angles:

1. AGENTS.md / CLAUDE.md compliance: the changed code breaks a rule in the
   repository's agent guidance (the root files and any in a touched directory).
2. Bugs in the changed lines: wrong conditions, missed cases, null or empty
   handling, resource leaks, errors swallowed or reported wrongly.
3. Git history and blame of the touched code: use `git log` and `git blame` on
   the touched lines; the change reverts an earlier fix, or breaks something a
   past commit message explains.
4. Comments in the touched files: the change contradicts a nearby comment,
   docstring or TODO, or leaves one stale.
5. Test runnability and isolation: tests the build never runs (such as JUnit 5
   tests under a provider pinned to an older engine); shared static state and
   order dependence; concurrency in test-only helpers; time and precision
   assumptions.
"""

FINDING_FORMAT = """\
Write each finding exactly like this, numbered R1, R2, …:

```
## R1. <short title> [critical|major|minor|nit]
Where: <path>:<line>
Problem: <what is wrong, in one or two sentences>
Fix: <the change you would make>
Confidence: <0-100>
```

- The heading is exactly `## R<n>. <title> [critical|major|minor|nit]`, with
  one of those four severities in the brackets.
- Each field starts its own line, in plain text (not bold, not a list item).
- Report only findings with Confidence 80 or higher.
- When nothing reaches 80, the whole review is the single line `No findings.`
"""


def codex_review_prompt(*, repo: str, base: str, out: str) -> str:
    """The codex reviewer's prompt.

    Codex runs read-only and ``codex exec -o`` captures its final message to
    *out* (see ``_plan_review_v2.review_prompt``), so it must not try to write.
    """
    return (
        f"Review the change in the repository at {repo}: review `git diff {base}..HEAD`. "
        "Read any file you need for context.\n\n"
        "Do not run builds or tests. "
        f"Write only {out}, and only through your final message: norn captures that "
        f"message verbatim to {out}, so emit the review document and nothing else. "
        "Do not create or edit any file yourself. This session is sandboxed read-only, "
        "so a write is rejected and the rejection notice becomes the review.\n\n"
        + REVIEW_ANGLES
        + "\n"
        + FINDING_FORMAT
    )


def codex_review_cmd(*, repo: str, out: str, base: str, codex_label: str) -> str:
    """``rm -f <out> && codex exec …``: a stale file can never pass the shape check."""
    resolved = resolve_label("codex", codex_label)
    effort = shlex.quote(f'model_reasoning_effort="{resolved.effort}"')
    return (
        f"rm -f {shlex.quote(out)} && "
        "codex exec --sandbox read-only --skip-git-repo-check "
        f"-m {shlex.quote(resolved.model)} "
        f"-c {effort} "
        f"-C {shlex.quote(repo)} -o {shlex.quote(out)} "
        + shlex.quote(codex_review_prompt(repo=repo, base=base, out=out))
    )


class CodexCodeReview(RunCommand):
    """``codex_review_cmd`` with the base read from ``review-range.txt`` when it runs.

    ``cmd`` holds a preview until then: ``base.sha`` does not exist at import
    time on a fresh run.
    """

    def __init__(self, *, paths: V3Paths, repo: str, codex_label: str) -> None:
        self.paths = paths
        self.repo = repo
        self.codex_label = codex_label
        super().__init__(
            cmd=codex_review_cmd(
                repo=repo, out=paths.review_codex, base=BASE_PREVIEW, codex_label=codex_label
            ),
            timeout=CODEX_REVIEW_TIMEOUT,
        )

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        range_path = resolve_run_path(ctx, self.paths.review_range)
        base = read_review_range(range_path).get("base") if range_path.is_file() else None
        if not base:
            return _fail(
                format_gate_error(
                    f"no base recorded in {range_path}",
                    [],
                    "Run without --resume so 'record review base' runs.",
                )
            )
        self.cmd = codex_review_cmd(
            repo=self.repo, out=self.paths.review_codex, base=base, codex_label=self.codex_label
        )
        return await super().run(ctx, **kwargs)


def claude_review_prompt(*, repo: str, base: str, out: str) -> str:
    return (
        f"Review the change in the repository at {repo}: review `git diff {base}..HEAD`. "
        "Read any file you need for context.\n\n"
        f"Do not run builds or tests; write only {out}. Use Bash only for read-only "
        "git (`git diff`, `git log`, `git blame`, `git show`), and do not start "
        "background tasks. Change no other file and make no commit.\n\n"
        "Use parallel subagents, one per angle: start five subagents at once, give "
        f"each one angle and the range `{base}..HEAD`, and have each report its "
        "findings back to you without writing any file. Then merge duplicates, keep "
        f"only findings with Confidence 80 or higher, and write {out}.\n\n"
        + REVIEW_ANGLES
        + "\n"
        + FINDING_FORMAT
    )


_R_HEADING_RE = re.compile(r"^## R(\d+)\. (\S.*?) \[(?i:critical|major|minor|nit)\]\s*$")
_R_LOOSE_RE = re.compile(r"^#{1,6}[ \t]*R\d+\b")
# A finding ends at the next level-1 or level-2 heading.
_TOP_HEADING_RE = re.compile(r"^#{1,2}[ \t]")
REVIEW_FIELDS = ("Where", "Problem", "Fix", "Confidence")


def review_file_problems(text: str) -> list[str]:
    """Return one line per shape problem of a reviewer's file."""
    if not text.strip():
        return ["the review file is empty"]
    lines = text.splitlines()
    problems: list[str] = []
    starts: list[int] = []
    for i, line in enumerate(lines):
        if _R_HEADING_RE.match(line):
            starts.append(i)
        elif _R_LOOSE_RE.match(line):
            problems.append(
                f"line {i + 1}: malformed finding heading {line.strip()!r}; "
                "use '## R<n>. <title> [critical|major|minor|nit]'"
            )
    for start in starts:
        end = next(
            (j for j in range(start + 1, len(lines)) if _TOP_HEADING_RE.match(lines[j])),
            len(lines),
        )
        body = lines[start + 1:end]
        rid = f"R{_R_HEADING_RE.match(lines[start]).group(1)}"  # type: ignore[union-attr]
        for field in REVIEW_FIELDS:
            pat = re.compile(rf"^{field}:[ \t]*\S")
            if not any(pat.match(line) for line in body):
                problems.append(f"{rid}: missing {field}:")
    no_findings = any(line.strip() == "No findings." for line in lines)
    if not starts and not no_findings:
        problems.append("no '## R<n>.' finding and no 'No findings.' line")
    return problems


class ReviewFileGate(BaseStage):
    """A reviewer's file exists, is non-empty, and holds findings or ``No findings.``."""

    needs_agent = False

    def __init__(self, *, path: str) -> None:
        self.path = path

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        resolved = resolve_run_path(ctx, self.path)
        if not resolved.is_file():
            problems = [f"review file not written: {resolved}"]
        else:
            problems = review_file_problems(resolved.read_text())
        if problems:
            return _fail(
                format_gate_error(
                    f"{len(problems)} problems in the review file {resolved}",
                    problems,
                    "[r]etry reruns the reviewer; after a stop, --resume does.",
                ),
                output="\n".join(problems),
            )
        return _ok()


class ReviewTreeGuard(BaseStage):
    """The reviewers changed no file content and made no commit (finding 8)."""

    needs_agent = False

    def __init__(self, *, paths: V3Paths, project_dir: str) -> None:
        self.paths = paths
        self.project_dir = project_dir

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        range_path = resolve_run_path(ctx, self.paths.review_range)
        expected = read_review_range(range_path).get("head") if range_path.is_file() else None
        if not expected:
            return _fail(
                format_gate_error(
                    f"no head recorded in {range_path}",
                    [],
                    "Run without --resume so 'record review base' runs.",
                )
            )
        top = _toplevel(self.project_dir)
        head = _run(self.project_dir, "git", "rev-parse", "HEAD")
        for proc in (top, head):
            if proc.returncode != 0:
                return _fail(
                    format_gate_error(
                        "cannot read the repository state",
                        [proc.stderr.strip()],
                        "Fix the repository state, then --resume.",
                    )
                )
        root = top.stdout.strip()
        pre = str(resolve_run_path(ctx, self.paths.review_pre))
        post = str(resolve_run_path(ctx, self.paths.review_post))
        snap = _snapshot(root, post)
        if snap.returncode != 0:
            return _fail(
                format_gate_error(
                    "cannot snapshot the worktree after the review",
                    [snap.stderr.strip()],
                    "Fix the repository state, then --resume.",
                )
            )
        diff = _run(root, sys.executable, SNAPSHOT_HELPER, "diff", pre, post, "--root", root)
        if diff.returncode != 0:
            return _fail(
                format_gate_error(
                    "cannot compare the worktree snapshots",
                    [diff.stderr.strip()],
                    "Fix the repository state, then --resume.",
                )
            )
        items: list[str] = []
        actual = head.stdout.strip()
        if actual != expected:
            items.append(f"HEAD moved from {expected[:7]} to {actual[:7]}")
        items += [p for p in diff.stdout.split("\0") if p]
        if items:
            return _fail(
                format_gate_error(
                    "the code reviewers changed the repository",
                    items,
                    f"Restore them (HEAD to {expected[:7]}), then --resume.",
                ),
                output="\n".join(items),
            )
        return _ok()


# ---------------------------------------------------------------------------
# Triage
# ---------------------------------------------------------------------------


def triage_prompt(*, paths: V3Paths, steps_dir: str, key: str) -> str:
    steps = steps_dir.rstrip("/")
    return f"""\
Triage the code review of {key} into the gate-3 file {paths.code_review}.

Inputs:
- {paths.review_codex}: the codex review (`## R<n>.` findings, or `No findings.`).
- {paths.review_claude}: the Claude review, in the same format.
- {steps}/followups.md, when it exists: the items under `## Unapplied review findings`.
- {steps}/review.md, when it exists: the `## Non-blocking` section.
- {paths.review_range}: its `base` and `head` lines name the reviewed range.

Check every finding against the code. Merge duplicates across sources into one
finding. A finding that is wrong is a false positive: list it under
`## Dropped as false positives` as `- <source> <id> <title> — <why it is wrong>`,
never as a finding.

Write {paths.code_review} in exactly this shape:

```
# Code review — {key} (<base7>..<head7>)

STATUS: PROPOSED
<!-- Set each Disposition to fix, skip or follow-up, then set the STATUS line to DECIDED. -->

## C1. <title> [critical|major|minor|nit]
Where: <path>:<line>
Problem: <what is wrong>
Fix: <the change to make>
Source: <claude, codex, followups, review>
Disposition: fix

## Dropped as false positives
- <source> <id> <title> — <why it is wrong>

## Omitted findings
- <title> [nit]
```

Rules:
- `<base7>` and `<head7>` are the first 7 characters of the `base` and `head`
  shas in {paths.review_range}.
- Order the findings by severity (critical, major, minor, nit) and number them
  C1, C2, … in that order.
- Every critical, major and minor finding, and every finding without a
  severity, gets its own `## C<n>.` section. There is no cap on them, however
  many there are. A non-nit must never be omitted.
- Nits fill sections only while there are fewer than 15 sections in total. The
  remaining nits go under `## Omitted findings` as one-line `- <title> [nit]`
  bullets. Only nits may go under `## Omitted findings`.
- A section is at most 8 lines, the heading included. Each field starts its own
  line, in plain text; `Where:`, `Problem:` and `Disposition:` are required.
- Propose a Disposition: `fix` for critical and major findings and for small
  minor ones; `follow-up` for larger minor ones; `skip (<reason>)` for nits.
- Leave out an empty `## Dropped as false positives` or `## Omitted findings`
  section.
- When no finding is left, write the heading, the line `STATUS: DECIDED`, and
  the single line `No findings.` instead of any `## C<n>.` section.
- If {paths.code_review_problems} exists, {paths.code_review} failed its shape
  check: fix exactly the problems listed there and change nothing else.
- Write only {paths.code_review}. Do not edit code; do not run builds or tests.

{COMMENT_RULE}
"""


def _needs_triage(paths: V3Paths):
    """The ``when`` of ``triage findings``: no gate-3 file yet, or its shape check failed."""

    def when(ctx: PipelineContext) -> bool:
        return not Path(paths.code_review).exists() or Path(paths.code_review_problems).exists()

    return when


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def add_review_fix_stages(
    pipeline: Pipeline,
    *,
    paths: V3Paths,
    project_dir: str,
    key: str,
    fix_model: str,
    fix_retries: int,
    auto: bool,
) -> Pipeline:
    """Part 2 of gate 3 (stages 46-51): added in step 07; adds nothing yet."""
    return pipeline


def add_code_review_stages(
    pipeline: Pipeline,
    *,
    paths: V3Paths,
    project_dir: str,
    steps_dir: str,
    key: str,
    base_file: str,
    codex_label: str,
    review_model: str,
    triage_model: str,
    fix_model: str,
    fix_retries: int,
    auto: bool,
) -> Pipeline:
    """Add stages 35-45 (``record review base`` … ``code review dispositions recorded``)."""
    env = _subagent_env()

    # 35. Opens gate 3; cached on resume, so it clears outputs on a fresh pass only.
    pipeline.stage(
        "record review base",
        RecordReviewBase(paths=paths, project_dir=project_dir, base_file=base_file),
        on_failure=fail,
    )

    # 36-37. Codex reviews read-only; re-running is the only repair.
    pipeline.loop(
        "codex code review",
        max_retries=2,
        on_exhaust=ask_user,
        stages=[
            Stage(
                "run codex code review",
                CodexCodeReview(paths=paths, repo=project_dir, codex_label=codex_label),
            ),
            Stage("check codex code review", ReviewFileGate(path=paths.review_codex)),
        ],
    )
    pipeline.stage(
        "codex code review written",
        ReviewFileGate(path=paths.review_codex),
        on_failure=fail,
    )

    # 38-39. Claude reviews with one subagent per angle.
    pipeline.clear_context()
    pipeline.loop(
        "claude code review",
        max_retries=2,
        on_exhaust=ask_user,
        stages=[
            Stage(
                "run claude code review",
                Generate(
                    prompt=claude_review_prompt(
                        repo=project_dir, base=BASE_PLACEHOLDER, out=paths.review_claude
                    ),
                    model=review_model,
                    allowed_tools=list(REVIEW_TOOLS),
                    permission_mode="acceptEdits",
                    setting_sources=["project"],
                    max_turns=REVIEW_MAX_TURNS,
                    env=env,
                ),
            ),
            Stage("check claude code review", ReviewFileGate(path=paths.review_claude)),
        ],
    )
    pipeline.stage(
        "claude code review written",
        ReviewFileGate(path=paths.review_claude),
        on_failure=fail,
    )

    # 40. Content-aware: a `git status` digest misses edits to dirty files.
    pipeline.stage(
        "review left tree unchanged",
        ReviewTreeGuard(paths=paths, project_dir=project_dir),
        on_failure=fail,
    )

    # 41-42. Triage into the gate-3 file.
    pipeline.clear_context()
    pipeline.loop(
        "triage code review",
        max_retries=2,
        on_exhaust=ask_user,
        stages=[
            Stage(
                "triage findings",
                Generate(
                    prompt=triage_prompt(paths=paths, steps_dir=steps_dir, key=key),
                    model=triage_model,
                    allowed_tools=list(TRIAGE_TOOLS),
                    permission_mode="acceptEdits",
                    setting_sources=["project"],
                    env=env,
                ),
                when=_needs_triage(paths),
            ),
            Stage(
                "check code review",
                CodeReviewGate(path=paths.code_review, problems_file=paths.code_review_problems),
            ),
        ],
    )
    pipeline.stage(
        "code review shape ok",
        CodeReviewGate(path=paths.code_review),
        on_failure=fail,
    )

    # 43-45. Gate 3.
    if auto:
        pipeline.stage(
            "auto decide code review",
            AutoDecideCodeReview(path=paths.code_review),
            on_failure=fail,
        )
    pipeline.stage(
        "code review decided",
        CodeReviewGate(path=paths.code_review, require_decided=True),
        on_failure=ask_user,
    )
    pipeline.stage(
        "code review dispositions recorded",
        CodeReviewGate(path=paths.code_review, require_decided=True),
        on_failure=fail,
        always_run=True,
    )

    return add_review_fix_stages(
        pipeline,
        paths=paths,
        project_dir=project_dir,
        key=key,
        fix_model=fix_model,
        fix_retries=fix_retries,
        auto=auto,
    )

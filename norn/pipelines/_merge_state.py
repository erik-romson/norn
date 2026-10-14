"""Private helper for ``merge_branch``: work root, halt machinery, summary skeleton.

Not a pipeline module — ``norn/catalog.py`` skips anything under
``norn/pipelines/`` whose name starts with ``_``.

This module is the foundation every later ``merge_branch`` step builds on. It
knows nothing about git or ``gh`` (that lives in ``_merge_gh.py``) and nothing
about the pipeline's own stage graph (that lives in ``merge_branch.py``). It
only knows: where the run's private state lives on disk, how a stage stops the
run cleanly when something goes wrong ("halting"), and how to render the
human-facing summary of what happened.

Deliberately narrow imports (see the step file): ``norn.stages.generate`` is
safe to import at module level because it — like this module — defers the
``claude-agent-sdk`` import to inside ``Generate.run()``.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shlex
import subprocess
import sys
import tempfile
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from norn.alerts import AlertEvent, AlertManager, AlertMessage, MacOSChannel
from norn.models import PipelineContext, StageResult
from norn.stages.base import BaseStage
from norn.stages.compress_test_log import CompressTestLog
from norn.stages.generate import Generate
from norn.stages.run_command import RunCommand

# ---------------------------------------------------------------------------
# Work root
# ---------------------------------------------------------------------------

VERSION = "v1"


def work_root(project_dir: str) -> str:
    """Return the deterministic work-root directory for *project_dir*.

    Never inside the repository — nothing this pipeline writes may enter the
    target repo. Deterministic so a rerun against the same project reuses (and
    the pipeline's own ``preflight`` stage empties) the same directory. This
    function never creates the directory; the pipeline does that at import.
    """
    digest = hashlib.sha1(project_dir.encode()).hexdigest()[:12]
    return str(Path(tempfile.gettempdir()) / "norn-merge" / VERSION / digest)


@dataclass(frozen=True)
class MergePaths:
    """Filesystem layout for one ``merge_branch`` work root.

    One attribute per fixed file, one method per per-round file family. Every
    path returned is an absolute string under ``root``. Every stage and helper
    in the ``merge_branch`` pipeline takes a ``MergePaths`` rather than a bare
    root string, so tests can point one at ``tmp_path``.
    """

    root: str

    def _p(self, name: str) -> str:
        return str(Path(self.root) / name)

    # -- fixed files --------------------------------------------------

    @property
    def halt(self) -> str:
        return self._p("HALT")

    @property
    def halt_reason(self) -> str:
        return self._p("halt-reason.md")

    @property
    def branch(self) -> str:
        return self._p("branch")

    @property
    def slug(self) -> str:
        return self._p("slug")

    @property
    def repo(self) -> str:
        return self._p("repo")

    @property
    def base(self) -> str:
        return self._p("base")

    @property
    def base_source(self) -> str:
        return self._p("base-source")

    @property
    def default_branch(self) -> str:
        return self._p("default-branch")

    @property
    def start_head(self) -> str:
        return self._p("start-head")

    @property
    def remote_head(self) -> str:
        return self._p("remote-head")

    @property
    def pushed_head(self) -> str:
        return self._p("pushed-head")

    @property
    def pr(self) -> str:
        return self._p("pr")

    @property
    def pr_json(self) -> str:
        return self._p("pr.json")

    @property
    def pr_origin(self) -> str:
        return self._p("pr-origin")

    @property
    def copilot_reviewer(self) -> str:
        return self._p("copilot-reviewer")

    @property
    def copilot_reviewer_log(self) -> str:
        return self._p("copilot-reviewer.log")

    @property
    def copilot_requested_at(self) -> str:
        return self._p("copilot-requested-at")

    @property
    def copilot_requested_head(self) -> str:
        return self._p("copilot-requested-head")

    @property
    def green_head(self) -> str:
        return self._p("green-head")

    @property
    def reviews_seen(self) -> str:
        return self._p("reviews-seen")

    @property
    def merged(self) -> str:
        return self._p("merged")

    @property
    def base_build(self) -> str:
        return self._p("base-build.json")

    @property
    def warnings(self) -> str:
        return self._p("warnings.txt")

    @property
    def summary_path(self) -> str:
        return self._p("summary-path.txt")

    @property
    def what_happened(self) -> str:
        return self._p("what-happened.md")

    # -- per-round files ------------------------------------------------

    def retry(self, n: int) -> str:
        return self._p(f"RETRY-{n}")

    def red(self, n: int) -> str:
        return self._p(f"RED-{n}")

    def proof(self, n: int) -> str:
        return self._p(f"local-proof-{n}")

    def local_log(self, n: int) -> str:
        return self._p(f"local-tests-{n}.log")

    def local_rc(self, n: int) -> str:
        return self._p(f"local-rc-{n}")

    def local_attempt(self, n: int) -> str:
        return self._p(f"local-attempt-{n}")

    def local_signature(self, n: int) -> str:
        return self._p(f"local-signature-{n}")

    def local_signature_pending(self, n: int) -> str:
        return self._p(f"local-signature-{n}.pending")

    def local_head(self, n: int) -> str:
        return self._p(f"local-head-{n}")

    def verdict(self, n: int) -> str:
        return self._p(f"local-verdict-{n}.md")

    def verdict_archive(self, n: int, k: int) -> str:
        return self._p(f"local-verdict-{n}-{k}.md")

    def rebase_conflicts(self, n: int) -> str:
        return self._p(f"rebase-conflicts-{n}.txt")

    def commit_log(self, n: int) -> str:
        return self._p(f"commit-{n}.log")

    def push_log(self, n: int) -> str:
        return self._p(f"push-{n}.log")

    def checks(self, n: int) -> str:
        return self._p(f"checks-{n}.json")

    def checks_after_fix(self, n: int) -> str:
        return self._p(f"checks-{n}-after-fix.json")

    def ci_red(self, n: int) -> str:
        return self._p(f"CI-RED-{n}")

    def ci_fix_workflows(self, n: int) -> str:
        return self._p(f"ci-fix-workflows-{n}")

    def ci_fix(self, n: int) -> str:
        return self._p(f"ci-fix-{n}.json")

    def ci_fix_commits(self, n: int) -> str:
        return self._p(f"ci-fix-commits-{n}.txt")

    def copilot_review(self, n: int) -> str:
        return self._p(f"copilot-review-{n}.json")

    def pr_reviews(self, n: int) -> str:
        return self._p(f"pr-reviews-{n}.md")

    def branch_diff(self, n: int) -> str:
        return self._p(f"branch-diff-{n}.patch")

    def review_decision(self, n: int) -> str:
        return self._p(f"review-decision-{n}")

    def no_new_reviews(self, n: int) -> str:
        return self._p(f"no-new-reviews-{n}")

    def findings(self, n: int) -> str:
        return self._p(f"findings-{n}.md")

    def findings_comment(self, n: int) -> str:
        return self._p(f"findings-comment-{n}.md")

    def findings_comment_url(self, n: int) -> str:
        return self._p(f"findings-comment-{n}.url")

    def merge_state(self, n: int) -> str:
        return self._p(f"merge-state-{n}.json")

    def merge_log(self, n: int) -> str:
        return self._p(f"merge-{n}.log")

    def summary(self, slug: str) -> str:
        return self._p(f"{slug}-merge-state.md")


# ---------------------------------------------------------------------------
# Small file helpers
# ---------------------------------------------------------------------------


def _read_text(path: str) -> str | None:
    """Return the stripped content of *path*, or ``None`` when it is not a file."""
    p = Path(path)
    return p.read_text().strip() if p.is_file() else None


def _read_or_none(path: str) -> str:
    """Return the stripped content of *path*, or the literal ``"(none)"``."""
    text = _read_text(path)
    return text if text else "(none)"


# ---------------------------------------------------------------------------
# Halt catalogue
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HaltKind:
    """One entry in the halt catalogue: what happened, and what to do about it."""

    heading: str
    next_step: str


# The ONE home of every halt kind (AGENTS.md / index.md "Halt catalogue").
# Later steps add their own kinds here as they introduce the stage that raises
# them. `halt()`/`halt_shell()` raise KeyError for a kind not listed here.
HALTS: dict[str, HaltKind] = {
    "rebase_conflict": HaltKind(
        heading="Rebase onto the base branch produced conflicts",
        next_step="Rebase by hand onto `origin/<base>`, resolve, then rerun.",
    ),
    "unexpected_failure": HaltKind(
        heading="A stage failed in a way this pipeline has no specific handler for",
        next_step=(
            "Read `## Why it stopped`. Transient (gh 5xx, network)? Rerun. "
            "Else fix the cause. `Status` says whether the merge landed."
        ),
    ),
    "local_stop": HaltKind(
        heading="The local fixer decided to stop rather than attempt another fix",
        next_step=(
            'Fix it yourself, commit, and rerun. Or rerun with `--arg guidance="..."`.'
        ),
    ),
    "local_cap": HaltKind(
        heading="The local-fix loop used all its allowed fix attempts",
        next_step="As `local_stop`.",
    ),
    "local_stuck": HaltKind(
        heading="The same test failure recurred after a fix attempt",
        next_step="As `local_stop`.",
    ),
    "local_verdict_missing": HaltKind(
        heading="The local fixer's verdict file is missing",
        next_step="Rerun. If it repeats, fix by hand.",
    ),
    "local_verdict_malformed": HaltKind(
        heading="The local fixer's verdict file could not be parsed",
        next_step="Rerun. If it repeats, fix by hand.",
    ),
    "local_head_moved": HaltKind(
        heading="HEAD moved unexpectedly during the local-fix loop",
        next_step="Inspect `git status`/`git log`, clean up, rerun.",
    ),
    "local_no_diff": HaltKind(
        heading="The local fixer reported a fix but left no diff",
        next_step="Inspect `git status`/`git log`, clean up, rerun.",
    ),
    "untracked_files": HaltKind(
        heading="Untracked files are present that the commit step won't pick up",
        next_step="Inspect `git status`/`git log`, clean up, rerun.",
    ),
    "local_gate_red": HaltKind(
        heading="The local test suite is still red after the fix loop",
        next_step="Fix, commit, rerun.",
    ),
    "test_timeout": HaltKind(
        heading="The local test suite did not finish before its timeout",
        next_step="Fix, commit, rerun.",
    ),
    "commit_rejected": HaltKind(
        heading="The commit of local fixes was rejected",
        next_step="Read `commit-<n>.log` (usually a pre-commit hook), fix, rerun.",
    ),
    "push_rejected": HaltKind(
        heading="The push was rejected",
        next_step="The remote branch moved or refused the push. Reconcile, rerun.",
    ),
    "attribution_found": HaltKind(
        heading="An assistant-attribution line was found where it must not be",
        next_step="Remove the assistant attribution line, rerun.",
    ),
    "pr_base_mismatch": HaltKind(
        heading="The pull request's base branch does not match the rebase base",
        next_step="Retarget or undraft the PR yourself, rerun.",
    ),
    "pr_draft": HaltKind(
        heading="The pull request is a draft",
        next_step="Retarget or undraft the PR yourself, rerun.",
    ),
    "checks_red": HaltKind(
        heading="The pull request's checks settled red",
        next_step="Fix CI by hand, push, rerun.",
    ),
    "checks_timeout": HaltKind(
        heading="The pull request's checks did not settle before the timeout",
        next_step="Check the Actions tab, push, rerun.",
    ),
    "checks_none": HaltKind(
        heading="No checks were reported on the pull request",
        next_step=(
            "`checks_none` means the workflows did not trigger. "
            "Check the Actions tab, push, rerun."
        ),
    ),
    "pr_head_moved": HaltKind(
        heading="The pull request's head moved during the check wait",
        next_step="Check the Actions tab, push, rerun.",
    ),
    "ci_fix_failed": HaltKind(
        heading="The check_and_fix_ci child run did not get the PR's checks green",
        next_step="Read the child's journal (path in the summary). Fix CI by hand, push, rerun.",
    ),
    "ci_fix_timeout": HaltKind(
        heading="The check_and_fix_ci child run did not finish before its timeout",
        next_step="Read the child's journal (path in the summary). Fix CI by hand, push, rerun.",
    ),
    "ci_fix_unpushed": HaltKind(
        heading="The check_and_fix_ci child left the branch out of sync with the remote",
        next_step="The child left commits unpushed or edits uncommitted. Inspect, clean up, rerun.",
    ),
    "copilot_rerequest_failed": HaltKind(
        heading="Re-requesting Copilot's review after the CI fix was refused",
        next_step="Re-request Copilot on the PR by hand (see `copilot-reviewer.log`), rerun.",
    ),
    "copilot_timeout": HaltKind(
        heading="Copilot's review of the pushed head did not arrive before the deadline",
        next_step="Wait for or re-request the Copilot review of the current head, rerun.",
    ),
    "findings_malformed": HaltKind(
        heading="The review-findings classifier's verdict file could not be parsed",
        next_step="Rerun. Discard any change the classifier made.",
    ),
    "classifier_touched_repo": HaltKind(
        heading="The review-findings classifier left the working tree dirty",
        next_step="Rerun. Discard any change the classifier made.",
    ),
    "findings_blocking": HaltKind(
        heading="The review-findings classifier found a blocking issue",
        next_step="Address the findings (by hand or with `implement_features_v2`), push, rerun.",
    ),
    "changes_requested": HaltKind(
        heading="A human reviewer requested changes on the pull request",
        next_step="Address the findings (by hand or with `implement_features_v2`), push, rerun.",
    ),
    "review_required": HaltKind(
        heading="The pull request needs a human approval this pipeline cannot give",
        next_step="Get the approval or fix the named merge state, rerun.",
    ),
    "merge_state": HaltKind(
        heading="The pull request's merge state is not clean",
        next_step="Get the approval or fix the named merge state, rerun.",
    ),
    "checks_changed": HaltKind(
        heading="A check appeared or changed after the checks settled",
        next_step=(
            "Look at it, then rerun. A check that must gate the merge belongs "
            "in branch protection."
        ),
    ),
    "head_moved_since_checks": HaltKind(
        heading="A commit landed on the branch after the checks ran",
        next_step="Rerun.",
    ),
    "merge_rejected": HaltKind(
        heading="Branch protection refused the fast-forward push",
        next_step="See README. Merge by hand.",
    ),
    "merged_pr_not_closed": HaltKind(
        heading="The fast-forward push landed but the pull request did not close",
        next_step="The base is already fast-forwarded. Check the PR and the branch by hand.",
    ),
    "branch_delete_failed": HaltKind(
        heading="The fast-forward push and merge succeeded but deleting the remote branch failed",
        next_step="The base is already fast-forwarded. Check the PR and the branch by hand.",
    ),
    "no_merge_after_rounds": HaltKind(
        heading="Both rounds finished without the branch merging",
        next_step="The base moves too fast. Merge by hand or rerun later.",
    ),
    "base_build_red": HaltKind(
        heading="The post-merge build on the base branch settled red",
        next_step="The merge landed and the base build is red. Fix the base.",
    ),
    "base_build_timeout": HaltKind(
        heading="The post-merge build on the base branch did not settle before the timeout",
        next_step="The merge landed and the base build is red. Fix the base.",
    ),
}


def halt(paths: MergePaths, kind: str, detail: str) -> None:
    """Write ``halt-reason.md`` and touch ``HALT``. Unknown *kind* raises ``KeyError``."""
    heading = HALTS[kind].heading
    reason_path = Path(paths.halt_reason)
    reason_path.parent.mkdir(parents=True, exist_ok=True)
    reason_path.write_text(f"KIND: {kind}\n{heading}\n{detail}\n")
    Path(paths.halt).touch()


def halt_shell(paths: MergePaths, kind: str, detail_cmd: str | None = None) -> str:
    """Return a POSIX ``sh`` fragment that halts the run the same way ``halt()`` does.

    Writes line 1 ``KIND: <kind>``, line 2 the heading, then (when *detail_cmd*
    is given) that command's own stdout, into ``halt-reason.md``; touches
    ``HALT``; and ``exit 0``. Unknown *kind* raises ``KeyError`` when the
    fragment is *built*, not when the shell runs it, so a typo fails fast in
    the pipeline that assembles the string.
    """
    heading = HALTS[kind].heading  # raises KeyError for an unknown kind
    body_lines = [
        f"printf 'KIND: %s\\n' {shlex.quote(kind)}",
        f"printf '%s\\n' {shlex.quote(heading)}",
    ]
    if detail_cmd:
        body_lines.append(detail_cmd)
    body = "\n  ".join(body_lines)
    return (
        f"{{\n  {body}\n}} > {shlex.quote(paths.halt_reason)}\n"
        f"touch {shlex.quote(paths.halt)}\n"
        f"exit 0\n"
    )


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------


def running(paths: MergePaths) -> Callable[[PipelineContext], bool]:
    """Predicate: true while the run has not halted (``HALT`` absent)."""
    return lambda ctx: not Path(paths.halt).exists()


def halted(paths: MergePaths) -> Callable[[PipelineContext], bool]:
    """Predicate: true once the run has halted (``HALT`` present)."""
    return lambda ctx: Path(paths.halt).exists()


def all_of(*preds: Callable[[PipelineContext], bool]) -> Callable[[PipelineContext], bool]:
    """Predicate: true when every predicate in *preds* is true."""
    return lambda ctx: all(p(ctx) for p in preds)


# ---------------------------------------------------------------------------
# HaltOnFailure and its concrete stage classes
# ---------------------------------------------------------------------------


def _format_result_detail(stage: str, result: StageResult) -> str:
    """Build halt detail text from a failed (non-raising) StageResult."""
    lines = [f"stage: {stage}", f"error: {result.error or '(none)'}"]
    stderr = ""
    if isinstance(result.output, dict):
        stderr = str(result.output.get("stderr") or "")
    if stderr.strip():
        lines.append("stderr (last 40 lines):")
        lines.extend(stderr.splitlines()[-40:])
    return "\n".join(lines)


def _format_exception_detail(stage: str, exc: Exception) -> str:
    """Build halt detail text from an exception the stage raised."""
    tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    lines = [
        f"stage: {stage}",
        f"exception: {type(exc).__name__}: {exc}",
        "traceback (last 40 lines):",
        *tb_text.splitlines()[-40:],
    ]
    return "\n".join(lines)


class HaltOnFailure:
    """Mixin: turn a stage's failure into an ``unexpected_failure`` halt.

    Placed before the concrete stage class in the MRO
    (``class HaltingCommand(HaltOnFailure, RunCommand)``) so its ``run()``
    intercepts the result before the runner sees it. Deliberately plain (does
    not inherit ``BaseStage``): the concrete subclass already does, through
    its other base.

    ``asyncio.CancelledError`` and any exception raised by ``halt()`` itself
    are never intercepted — they propagate unchanged, per the stop contract
    (AGENTS.md): a halt is a controlled stop, not a place to swallow a real
    problem with the halt machinery or an external cancellation.

    With ``last_attempt=(n, last)`` the mixin only acts once
    ``paths.local_attempt(n)`` holds ``last`` — i.e. only on the loop's last
    attempt. On any earlier attempt the failure (or exception) is passed
    through unchanged so the loop retries as usual.
    """

    def __init__(
        self,
        *,
        paths: MergePaths,
        stage: str,
        last_attempt: tuple[int, int] | None = None,
        **kwargs: Any,
    ) -> None:
        self._halt_paths = paths
        self._halt_stage = stage
        self._halt_last_attempt = last_attempt
        super().__init__(**kwargs)

    def _pass_through(self) -> bool:
        """True when a ``last_attempt`` gate says not to halt yet."""
        if self._halt_last_attempt is None:
            return False
        n, last = self._halt_last_attempt
        current = _read_text(self._halt_paths.local_attempt(n))
        return current != str(last)

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        try:
            result = await super().run(ctx, **kwargs)  # type: ignore[misc]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._pass_through():
                raise
            halt(self._halt_paths, "unexpected_failure", _format_exception_detail(self._halt_stage, exc))
            return StageResult(name="", success=True)

        if result.success or self._pass_through():
            return result

        halt(self._halt_paths, "unexpected_failure", _format_result_detail(self._halt_stage, result))
        return StageResult(name="", success=True)


class HaltingCommand(HaltOnFailure, RunCommand):
    """``RunCommand`` whose unexpected failures halt the run instead of failing it."""


class HaltingGenerate(HaltOnFailure, Generate):
    """``Generate`` whose unexpected failures halt the run instead of failing it."""


class HaltingCompressTestLog(HaltOnFailure, CompressTestLog):
    """``CompressTestLog`` whose unexpected failures halt the run instead of failing it.

    ``CompressTestLog(source_stage=...)`` reads the source stage's ``output``
    whether it passed or failed, so a suite stage that always exits 0 (even on
    red, per the local-loop contract) still feeds it correctly.
    """


class HaltOnTimeout(HaltingCommand):
    """``HaltingCommand`` that gives its own ``RunCommand`` timeout a named halt.

    ``RunCommand`` itself reports a timeout as a failed ``StageResult`` with
    ``output["returncode"] is None``. Left to the inherited
    ``HaltOnFailure.run()``, that would halt as the generic
    ``unexpected_failure`` — indistinguishable from any other command failure.
    This class checks for that specific shape *before* falling through to the
    ``HaltOnFailure`` rule, so a hanging command (e.g. a wedged test suite)
    halts under its own named kind (e.g. ``test_timeout``) instead of being
    retried by the loop up to ``max_retries`` times.
    """

    def __init__(self, *, kind: str, **kwargs: Any) -> None:
        self._timeout_kind = kind
        super().__init__(**kwargs)

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        try:
            result = await RunCommand.run(self, ctx, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._pass_through():
                raise
            halt(self._halt_paths, "unexpected_failure", _format_exception_detail(self._halt_stage, exc))
            return StageResult(name="", success=True)

        if result.success:
            return result

        if isinstance(result.output, dict) and result.output.get("returncode") is None:
            halt(self._halt_paths, self._timeout_kind, result.error or "command timed out")
            return StageResult(name="", success=True)

        if self._pass_through():
            return result

        halt(self._halt_paths, "unexpected_failure", _format_result_detail(self._halt_stage, result))
        return StageResult(name="", success=True)


# ---------------------------------------------------------------------------
# Local-loop helpers
# ---------------------------------------------------------------------------


class CountLocalAttempt(BaseStage):
    """Increment ``local-attempt-<n>``, starting from 1, and succeed.

    Deliberately does NOT carry the ``HaltOnFailure`` mixin — it is the
    mixin's *input* (via ``last_attempt=(n, LOCAL_FIX_ATTEMPTS + 1)``), so a
    halt here would be circular.
    """

    needs_agent = False

    def __init__(self, *, paths: MergePaths, n: int) -> None:
        self._paths = paths
        self._n = n

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        path = Path(self._paths.local_attempt(self._n))
        current = _read_text(str(path))
        next_value = int(current) + 1 if current else 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(next_value))
        return StageResult(name="", success=True, output=next_value)


# ANSI CSI sequences (colour codes, cursor movement, etc.) that a test runner
# writing to a real terminal emits but that are pure noise for "is this the
# same failure as last time".
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
# Durations vary run to run even when the failure is identical (a slow CI
# runner, a warmed-up cache); normalise them out rather than let them defeat
# "the same failure" detection.
_DURATION_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:ms|s|sec|seconds)\b")
# Heap/pointer addresses in a traceback vary run to run for the same failure.
_ADDR_RE = re.compile(r"0x[0-9a-fA-F]+")
_WHITESPACE_RE = re.compile(r"\s+")


def failure_signature(text: str) -> str:
    """Return a sha1 hex digest of *text*, normalised for "same failure" comparison.

    Strips ANSI escapes, collapses durations and addresses to placeholders,
    and collapses whitespace runs, then hashes the *whole* normalised text —
    not a prefix, since a raw log's first lines are usually an identical
    runner header shared by every failure.
    """
    normalised = _ANSI_RE.sub("", text)
    normalised = _DURATION_RE.sub("<t>", normalised)
    normalised = _ADDR_RE.sub("<addr>", normalised)
    normalised = _WHITESPACE_RE.sub(" ", normalised).strip()
    return hashlib.sha1(normalised.encode()).hexdigest()


def _git_rev_parse_head(project_dir: str) -> str:
    """Return ``git rev-parse HEAD`` for *project_dir*.

    No fallback: a failure here (e.g. not a git repo, git missing) propagates
    — this is not a diagnostics-only path (AGENTS.md Principles).
    """
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return result.stdout.strip()


def _head_lines(text: str, n: int = 40) -> str:
    return "\n".join(text.splitlines()[:n])


class AssessLocalFailure(HaltOnFailure, BaseStage):
    """Decide whether local-test failure ``n`` is progress, a repeat, or over budget.

    Unlike ``HaltingCommand``/``HaltingGenerate``/``HaltingCompressTestLog``,
    there is no wrapped stage class supplying the actual work — this stage's
    own logic *is* the work. It performs its own try/except around that logic
    (mirroring what ``HaltOnFailure.run()`` does for a wrapped stage) so that
    a genuinely unpredicted exception (e.g. a git invocation failing) still
    becomes ``unexpected_failure``, while the two domain-specific outcomes
    (``local_cap``, ``local_stuck``) are raised directly as the stage's normal
    logic, not treated as failures of the mixin's wrapping.
    """

    needs_agent = False

    def __init__(
        self,
        *,
        n: int,
        source_stage: str,
        max_fixes: int,
        project_dir: str,
        **kwargs: Any,
    ) -> None:
        self._n = n
        self._source_stage = source_stage
        self._max_fixes = max_fixes
        self._project_dir = project_dir
        super().__init__(**kwargs)

    def _archived_verdict_count(self) -> int:
        return len(list(Path(self._halt_paths.root).glob(f"local-verdict-{self._n}-*.md")))

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        try:
            return await self._assess(ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._pass_through():
                raise
            halt(self._halt_paths, "unexpected_failure", _format_exception_detail(self._halt_stage, exc))
            return StageResult(name="", success=True)

    async def _assess(self, ctx: PipelineContext) -> StageResult:
        paths = self._halt_paths
        n = self._n

        if self._archived_verdict_count() >= self._max_fixes:
            failure_text = str(ctx.get(self._source_stage))
            halt(paths, "local_cap", _head_lines(failure_text))
            return StageResult(name="", success=True)

        signature = failure_signature(str(ctx.get(self._source_stage)))
        promoted = _read_text(paths.local_signature(n))
        if promoted is not None and promoted == signature:
            halt(paths, "local_stuck", "the failure signature repeated after a fix attempt")
            return StageResult(name="", success=True)

        Path(paths.local_signature_pending(n)).write_text(signature)
        Path(paths.local_head(n)).write_text(_git_rev_parse_head(self._project_dir))
        verdict_path = Path(paths.verdict(n))
        if verdict_path.is_file():
            verdict_path.unlink()
        return StageResult(name="", success=True)


# ---------------------------------------------------------------------------
# Pre-rebase guards
# ---------------------------------------------------------------------------


class AssertFreshRun(BaseStage):
    """Fail when this run is a ``--resume``.

    A halted guard exits 0 and is checkpointed as a success, so ``--resume``
    would silently skip it and every stage after it. Recovery is always "fix,
    then rerun" — every stage in this pipeline is idempotent.
    """

    needs_agent = False

    def __init__(self, pipeline: str = "merge_branch_v1") -> None:
        self._pipeline = pipeline

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        if ctx.checkpoint_stages:
            return StageResult(
                name="",
                success=False,
                error=(
                    f"{self._pipeline} does not resume: every stage is idempotent — "
                    f"rerun `norn run {self._pipeline}` without --resume"
                ),
            )
        args = ctx.params.get("args")
        if args:
            return StageResult(
                name="",
                success=False,
                error=(
                    f"{self._pipeline} takes no positional arguments (got: {args}); "
                    "pass the base as `--arg base=X`"
                ),
            )
        return StageResult(name="", success=True)


# ---------------------------------------------------------------------------
# Escalate
# ---------------------------------------------------------------------------


def _halt_heading(paths: MergePaths) -> str:
    """Return line 2 (the heading) of ``halt-reason.md``, or a placeholder."""
    text = _read_text(paths.halt_reason)
    if not text:
        return "(no halt reason recorded)"
    lines = text.splitlines()
    return lines[1] if len(lines) > 1 else "(no halt reason recorded)"


def _halt_kind(paths: MergePaths) -> str | None:
    """Return the ``KIND:`` value on line 1 of ``halt-reason.md``, or ``None``."""
    text = _read_text(paths.halt_reason)
    if not text:
        return None
    first = text.splitlines()[0]
    return first[len("KIND: "):].strip() if first.startswith("KIND: ") else None


class Escalate(BaseStage):
    """The last stage: pauses the run for a human, when the run halted.

    Shaped like ``_plan_review.WaitForApproval`` — a deliberate
    ``StageResult(success=False)`` so the runner fires ``ASK_USER`` and
    prompts. This is the ONLY ``ask_user`` stage in the pipeline; it is
    ``when=halted(PATHS)`` so a clean merge skips it entirely.
    """

    needs_agent = False

    def __init__(self, *, paths: MergePaths) -> None:
        self._paths = paths

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        heading = _halt_heading(self._paths)
        summary_path = _read_text(self._paths.summary_path) or self._paths.summary_path
        return StageResult(
            name="",
            success=False,
            error=(
                f"needs human intervention — {heading}\n"
                f"Summary: {summary_path}\n"
                "[a]bort ends the run (recommended; --non-interactive does this).\n"
                "[c]ontinue ends it recorded as a success although nothing merged.\n"
                "[r]etry asks again."
            ),
        )


# ---------------------------------------------------------------------------
# Summary rendering
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_status_short(project_dir: str) -> str:
    """Return ``git status --short`` output for *project_dir*, never raising."""
    try:
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except OSError as exc:
        return f"(git status failed: {exc})"
    output = (result.stdout + result.stderr).strip()
    return output or "(clean)"


def _current_round(paths: MergePaths, max_rounds: int) -> int:
    """Return the highest round reached, inferred from which ``RETRY-<n>`` files exist."""
    round_n = 1
    for n in range(2, max_rounds + 1):
        if Path(paths.retry(n - 1)).is_file():
            round_n = n
    return round_n


def _status(paths: MergePaths) -> str:
    """Return the run's ``Status``, computed from the work root's own files.

    Three values, and only three (``NOT_MERGED`` from phase 1 is gone):

    * ``merged`` and no ``HALT`` — ``MERGED``: the fast-forward landed and
      nothing stopped the run afterwards.
    * both — ``MERGED_NEEDS_ATTENTION``: ``merge pr <n>`` wrote ``merged``
      right after the push, and a later step (a PR that never closed, a
      failed branch delete, a red post-merge build, or the ``HaltOnFailure``
      mixin's ``unexpected_failure``) halted. The base moved; a human still
      has to look.
    * ``HALT`` alone — ``HALTED``.

    With neither file this raises. ``merge reached`` halts
    ``no_merge_after_rounds`` whenever the rounds end without ``merged``, and
    every stage after a halt is skipped by ``running(PATHS)``, so reaching
    ``write merge state`` with neither file is unreachable — and reaching it
    would be a bug in the stage graph, not a missing optional artifact to
    degrade to ``(none)``.
    """
    merged = Path(paths.merged).is_file()
    stopped = Path(paths.halt).is_file()
    if merged:
        return "MERGED_NEEDS_ATTENTION" if stopped else "MERGED"
    if stopped:
        return "HALTED"
    raise RuntimeError(
        f"merge_branch: neither `merged` nor `HALT` exists in {paths.root} — "
        "`merge reached` halts `no_merge_after_rounds` when the rounds end "
        "without a merge, so this state is unreachable; reaching it means the "
        "stage graph is wrong"
    )


def _next_step_section(paths: MergePaths, rerun_cmd: str) -> str:
    if not Path(paths.halt).is_file():
        return "Nothing to do — see Status."
    kind = _halt_kind(paths)
    next_step = HALTS[kind].next_step if kind in HALTS else "(unknown halt kind — see Why it stopped)"
    return f"{next_step}\n\nRerun: `{rerun_cmd}`"


def _archived_local_verdicts(paths: MergePaths, n: int) -> list[tuple[int, str, str]]:
    """Return ``(k, verdict, reason)`` for every archived verdict of round *n*.

    Sorted by ``k`` (the fix-attempt index baked into the filename by
    ``check local verdict n``'s shell fragment), never by mtime — the index
    is the human-meaningful order even if the filesystem's mtimes are
    coarser than the gap between two fix attempts.
    """
    entries: list[tuple[int, str, str]] = []
    for path in Path(paths.root).glob(f"local-verdict-{n}-*.md"):
        suffix = path.stem.rsplit("-", 1)[-1]
        if not suffix.isdigit():
            continue
        lines = path.read_text().splitlines()
        verdict = lines[0][len("VERDICT: "):] if lines and lines[0].startswith("VERDICT: ") else "?"
        reason = ""
        for line in lines[1:]:
            if line.startswith("Reason:"):
                reason = line[len("Reason:"):].strip()
                break
        entries.append((int(suffix), verdict, reason))
    entries.sort(key=lambda entry: entry[0])
    return entries


def commit_subject(n: int) -> str:
    """Return round *n*'s fixed one-line commit subject.

    The ONE home of this text: ``merge_branch.commit_local_fixes_cmd`` uses it
    to build the actual ``git commit -m`` command, and
    ``_commit_sha_line`` below greps for the exact same string rather than
    parsing ``git commit``'s own (format-fragile) confirmation line.
    """
    return f"fix: local test failures (merge_branch round {n})"


def _commit_sha_line(paths: MergePaths, round_n: int, project_dir: str) -> str:
    """Return the ``Commit: <sha>`` (or ``Commit: (none)``) line for round *n*.

    Not every round makes a commit — the fix loop may have needed zero
    attempts. Rather than parse ``commit-<n>.log`` (``git commit``'s own
    confirmation text, whose format is not a contract), this looks for a
    commit reachable from HEAD but not from ``start-head`` whose subject is
    exactly ``commit_subject(round_n)`` — the one and only commit this round
    could have made. Never raises: a git failure here degrades to ``(none)``,
    matching ``_git_status_short``'s precedent for this human-facing summary.
    """
    start_head = _read_text(paths.start_head)
    if not start_head:
        return "Commit: (none)"
    try:
        result = subprocess.run(
            ["git", "log", "--format=%H%x1f%s", f"{start_head}..HEAD"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except OSError:
        return "Commit: (none)"
    if result.returncode != 0:
        return "Commit: (none)"
    subject = commit_subject(round_n)
    for line in result.stdout.splitlines():
        sha, _, message = line.partition("\x1f")
        if message == subject:
            return f"Commit: {sha}"
    return "Commit: (none)"


def _local_tests_section(
    paths: MergePaths,
    round_n: int,
    *,
    test_cmd: str,
    local_fix_attempts: int | None,
    project_dir: str,
) -> list[str]:
    """Render the ``## Local tests`` block for ``render_summary``."""
    attempts_used = _read_text(paths.local_attempt(round_n)) or "0"
    cap = str(local_fix_attempts + 1) if local_fix_attempts is not None else "?"
    if Path(paths.proof(round_n)).is_file():
        final_state = "GREEN"
    elif Path(paths.red(round_n)).is_file():
        final_state = "RED"
    else:
        final_state = "not run"

    verdict_lines = [
        f"attempt {k} -> VERDICT: {verdict}" + (f" ({reason})" if reason else "")
        for k, verdict, reason in _archived_local_verdicts(paths, round_n)
    ]

    return [
        "## Local tests",
        "",
        f"Command: `{test_cmd}`" if test_cmd else "Command: (none)",
        f"Attempts: {attempts_used} / {cap}",
        f"Final state: {final_state}",
        _commit_sha_line(paths, round_n, project_dir),
        *(verdict_lines or ["(no fix attempts)"]),
    ]


def _pull_request_section(paths: MergePaths) -> list[str]:
    """Render the ``## Pull request`` block for ``render_summary``.

    ``pr.json`` is only ever written by ``open or reuse pr n`` on success, so
    a missing or unparseable file (the PR stage never reached, or the run
    halted before it) degrades to ``(none)`` for the URL — matching every
    other optional-file precedent in this renderer — rather than raising.
    """
    number = _read_text(paths.pr)
    url = "(none)"
    pr_json_text = _read_text(paths.pr_json)
    if pr_json_text:
        try:
            url = json.loads(pr_json_text).get("url") or "(none)"
        except json.JSONDecodeError:
            url = "(none)"
    origin = _read_or_none(paths.pr_origin)
    copilot = _read_or_none(paths.copilot_reviewer)
    pr_line = f"PR: #{number} {url} ({origin})" if number else "PR: (none)"
    return [
        "## Pull request",
        "",
        pr_line,
        f"Copilot: {copilot}",
    ]


def _format_check_duration(started_at: str | None, completed_at: str | None) -> str:
    """Return ``<m>m<ss>s`` for a check's wall-clock duration, or ``"-"``."""
    if not started_at or not completed_at:
        return "-"
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        completed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    except ValueError:
        return "-"
    total_seconds = max(0, int((completed - started).total_seconds()))
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}m{seconds:02d}s"


def _checks_table_lines(path: str) -> list[str]:
    """Render the check/build table and poll-plan line for one ``checks-*.json`` file.

    Shared by ``_builds_section`` (``checks-<n>.json``, the first wait) and
    ``_ci_fix_section`` (``checks-<n>-after-fix.json``, the second wait). A
    missing or unparseable file degrades to ``(none)`` — the same precedent
    every other optional-file section in this renderer follows.
    """
    text = _read_text(path)
    if not text:
        return ["(none)"]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return ["(none)"]

    lines: list[str] = []
    checks = data.get("checks") or []
    per_workflow = data.get("per_workflow_estimate") or {}
    if checks:
        lines.append("| Workflow | Check | Bucket | Duration | Estimate | Link |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for check in checks:
            workflow = check.get("workflow") or "-"
            estimate = per_workflow.get(workflow)
            estimate_text = f"{int(estimate)}s" if estimate is not None else "-"
            lines.append(
                f"| {workflow} | {check.get('name') or '-'} | {check.get('bucket') or '-'} | "
                f"{_format_check_duration(check.get('startedAt'), check.get('completedAt'))} | "
                f"{estimate_text} | {check.get('link') or '-'} |"
            )
    else:
        lines.append("(no checks reported)")

    plan = data.get("plan")
    if plan:
        lines.append("")
        lines.append(
            f"Poll plan: pre_sleep={plan.get('pre_sleep', 0):.0f}s, "
            f"interval={plan.get('interval', 0):.0f}s, timeout={plan.get('timeout', 0):.0f}s"
        )
    return lines


def _builds_section(paths: MergePaths, round_n: int) -> list[str]:
    """Render the ``## Builds`` block for ``render_summary``.

    Reads ``checks-<n>.json`` (written by ``wait for checks n``).
    """
    return ["## Builds", "", *_checks_table_lines(paths.checks(round_n))]


def _ci_fix_section(paths: MergePaths, round_n: int) -> list[str]:
    """Render the ``## CI fix`` block for ``render_summary``.

    Included only when ``CI-RED-<n>`` exists (red PR checks were handed to a
    ``check_and_fix_ci`` child this round): the workflows handed over, the
    child's outcome and journal path (``ci-fix-<n>.json``, written by
    ``fix ci n``), the commits it pushed (``ci-fix-commits-<n>.txt``, written
    by ``sync after ci fix n``), and the second wait's table
    (``checks-<n>-after-fix.json``). Every one of those files is optional —
    the run may have halted before writing it — and degrades to ``(none)``.
    """
    if not Path(paths.ci_red(round_n)).is_file():
        return []

    workflows_text = _read_text(paths.ci_fix_workflows(round_n))
    workflows = ", ".join(workflows_text.splitlines()) if workflows_text else "(none)"

    outcome = "(none)"
    journal = "(none)"
    ci_fix_text = _read_text(paths.ci_fix(round_n))
    if ci_fix_text:
        try:
            data = json.loads(ci_fix_text)
        except json.JSONDecodeError:
            data = {}
        outcome = data.get("outcome") or "(none)"
        journal = data.get("journal") or "(none)"

    return [
        "## CI fix",
        "",
        f"Workflows: {workflows} | Child: {outcome} | Journal: {journal}",
        f"Commits: {_read_or_none(paths.ci_fix_commits(round_n))}",
        "",
        *_checks_table_lines(paths.checks_after_fix(round_n)),
        "",
    ]


def _learning_pointer_lines(paths: MergePaths, max_rounds: int) -> list[str]:
    """Return one ``Learning proposal: <journal>/learning.md`` line per CI-fix child run that wrote one.

    Reads ``ci-fix-<n>.json`` for every round; the ``journal`` field names the
    child's run directory, and a proposal exists only when ``learning.md`` is
    in it. A missing or unreadable file, or a journal without a proposal,
    contributes nothing.
    """
    lines: list[str] = []
    for round_n in range(1, max_rounds + 1):
        text = _read_text(paths.ci_fix(round_n))
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        journal = data.get("journal") if isinstance(data, dict) else None
        if not journal:
            continue
        proposal = Path(journal) / "learning.md"
        if proposal.is_file():
            lines.append(f"Learning proposal: {proposal}")
    return lines


def _copilot_review_section(paths: MergePaths, round_n: int) -> list[str]:
    """Render the ``## Copilot review`` block for ``render_summary``.

    ``State`` reads ``copilot-reviewer`` (``requested``/``already-reviewed``/
    ``unavailable``, or ``(none)`` when the request stage never ran).
    ``Reviewed head``/``Arrived`` read ``copilot-review-<n>.json`` (written by
    ``wait for copilot review n`` once a matching review arrives) — absent
    when the run halted before one arrived. ``Re-requested after CI fix`` is
    inferred from ``CI-RED-<n>``: ``re-request copilot review n`` only runs
    when this round's checks went to a ``check_and_fix_ci`` child. Every field
    degrades to ``(none)``/``no`` rather than raising, matching every other
    optional-file precedent in this renderer.
    """
    state = _read_or_none(paths.copilot_reviewer)
    reviewed_head = "(none)"
    arrived_at = "(none)"
    review_text = _read_text(paths.copilot_review(round_n))
    if review_text:
        try:
            data = json.loads(review_text)
        except json.JSONDecodeError:
            data = {}
        reviewed_head = data.get("commit_id") or "(none)"
        arrived_at = data.get("submitted_at") or "(none)"
    re_requested = "yes" if Path(paths.ci_red(round_n)).is_file() else "no"
    return [
        "## Copilot review",
        "",
        f"State: {state}",
        f"Reviewed head: {reviewed_head}",
        f"Re-requested after CI fix: {re_requested}",
        f"Arrived: {arrived_at}",
    ]


def _findings_bullet_section(text: str, heading: str) -> list[str]:
    """Return the bullet lines under ``## <heading>`` in a findings-<n>.md body.

    A plain line-scan keyed on the exact ``## `` heading text the classifier
    prompt and ``_clear_findings_body`` both use — stops at the next ``## ``
    heading (or the end of the file).
    """
    lines: list[str] = []
    in_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            in_section = stripped[3:].strip() == heading
            continue
        if in_section and stripped.startswith("-"):
            lines.append(stripped)
    return lines


def _review_findings_section(paths: MergePaths, round_n: int) -> list[str]:
    """Render the ``## Review findings`` block for ``render_summary``.

    ``pr-reviews-<n>.md`` (written by ``fetch pr reviews n``, absent when
    nothing new arrived) gives the per-author new-comment counts, counted from
    its ``### <login> (<kind>, <state>)`` headers. ``findings-<n>.md``
    (written by the classifier, or by ``fetch pr reviews n`` itself on zero
    new entries — so it is always present by the time this renders) gives the
    verdict, the ``## Blocking`` section verbatim, and the non-blocking/
    answered counts. ``findings-comment-<n>.url`` (written by ``post findings
    comment n``) is the posted comment link, absent when the run halted
    before posting. Every field degrades to ``(none)``, matching every other
    optional-file precedent in this renderer.
    """
    reviews_text = _read_text(paths.pr_reviews(round_n)) or ""
    authors: dict[str, int] = {}
    for line in reviews_text.splitlines():
        if line.startswith("### "):
            login = line[len("### "):].split(" (", 1)[0].strip()
            authors[login] = authors.get(login, 0) + 1
    author_lines = (
        [f"- {login}: {count}" for login, count in sorted(authors.items())]
        if authors
        else ["(no new reviews)"]
    )

    findings_text = _read_text(paths.findings(round_n)) or ""
    verdict = "(none)"
    for line in findings_text.splitlines():
        if line.startswith("VERDICT:"):
            verdict = line[len("VERDICT:"):].strip()
            break
    blocking_lines = _findings_bullet_section(findings_text, "Blocking") or ["(none)"]
    non_blocking_count = len(_findings_bullet_section(findings_text, "Non-blocking"))
    answered_count = len(_findings_bullet_section(findings_text, "Answered"))

    return [
        "## Review findings",
        "",
        "New comments by author:",
        *author_lines,
        f"Verdict: {verdict}",
        "",
        "Blocking:",
        *blocking_lines,
        f"Non-blocking: {non_blocking_count}",
        f"Answered: {answered_count}",
        f"Comment: {_read_or_none(paths.findings_comment_url(round_n))}",
    ]


def _branch_deleted(branch: str, project_dir: str) -> str:
    """Return ``"yes"``/``"no"``/``"(unknown)"`` for whether *branch* still
    exists on ``origin``, via a live ``git ls-remote`` — deletion is never
    itself recorded to a work-root file. Never raises: any git failure (no
    remote, not a git repo, a network hiccup) degrades to ``"(unknown)"``,
    matching every other optional-file precedent in this renderer.
    """
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--exit-code", "--heads", "origin", branch],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except OSError:
        return "(unknown)"
    if result.returncode == 2:
        return "yes"
    if result.returncode == 0:
        return "no"
    return "(unknown)"


def _merge_section(paths: MergePaths, round_n: int, project_dir: str) -> list[str]:
    """Render the ``## Merge`` block for ``render_summary``.

    ``Preconditions`` reads ``merge-state-<n>.json`` (written by ``check
    merge preconditions n``) — the snapshot of ``mergeable``,
    ``mergeStateStatus``, ``reviewDecision``, ``isDraft`` and ``headRefOid``
    at the time preconditions were checked. ``Merged`` reads ``merged``
    (written by ``merge pr n`` right after the fast-forward push lands, even
    if a later step in that stage halts). ``Branch deleted`` is only
    meaningful once a merge landed; otherwise it reads ``(n/a)``. Every field
    degrades to ``(none)``/``(n/a)`` rather than raising, matching every
    other optional-file precedent in this renderer.
    """
    state_text = _read_text(paths.merge_state(round_n))
    preconditions = "(none)"
    if state_text:
        try:
            state = json.loads(state_text)
        except json.JSONDecodeError:
            state = {}
        preconditions = ", ".join(
            f"{key}={state.get(key)}"
            for key in ("mergeable", "mergeStateStatus", "reviewDecision", "isDraft", "headRefOid")
        )

    merged_sha = _read_text(paths.merged)
    branch = _read_text(paths.branch)
    branch_deleted = _branch_deleted(branch, project_dir) if (merged_sha and branch) else "(n/a)"

    return [
        "## Merge",
        "",
        f"Preconditions: {preconditions}",
        f"Merged: {merged_sha or '(none)'}",
        f"Branch deleted: {branch_deleted}",
    ]


def _post_merge_build_result(outcome: str | None) -> str:
    """Map ``base-build.json``'s ``outcome`` to the summary's ``Result:`` text.

    Exactly the three values the plan's summary block names: the warning
    outcome (no workflow ran), or a plain passed/failed — a timed-out build
    (``base_build_timeout``) renders as ``failed`` here too; the timeout
    itself is visible in ``## Why it stopped``, and the plan's summary block
    lists only these three ``Result`` strings.
    """
    if outcome == "no_runs":
        return "no workflow ran within 120 s (warning)"
    if outcome == "passed":
        return "passed"
    return "failed"


def _post_merge_build_section(paths: MergePaths) -> list[str]:
    """Render the ``## Post-merge build`` block for ``render_summary``.

    Reads ``base-build.json`` (written by ``watch base build``, gated on
    ``merged`` — absent when the run halted before the merge landed, which
    degrades to ``(none)`` matching every other optional-file precedent in
    this renderer).
    """
    text = _read_text(paths.base_build)
    if not text:
        return ["## Post-merge build", "", "Result: (none)"]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return ["## Post-merge build", "", "Result: (none)"]

    lines = ["## Post-merge build", "", f"Result: {_post_merge_build_result(data.get('outcome'))}"]
    runs = data.get("runs") or []
    if runs:
        lines.append("")
        lines.append("| Workflow | Status | Conclusion | Link |")
        lines.append("| --- | --- | --- | --- |")
        for run in runs:
            lines.append(
                f"| {run.get('workflowName') or '-'} | {run.get('status') or '-'} | "
                f"{run.get('conclusion') or '-'} | {run.get('url') or '-'} |"
            )
    return lines


def _warnings_lines(paths: MergePaths) -> list[str]:
    """Return every line of ``warnings.txt`` (``[]`` when absent or empty)."""
    text = _read_text(paths.warnings)
    return text.splitlines() if text else []


def _warnings_section(paths: MergePaths) -> list[str]:
    """Render the ``## Warnings`` block for ``render_summary``, or ``[]``
    when ``warnings.txt`` has no entries — this section only appears when
    there is at least one warning to show.
    """
    lines = _warnings_lines(paths)
    if not lines:
        return []
    return ["## Warnings", "", *[f"- {line}" for line in lines]]


def _what_happened_section(paths: MergePaths) -> list[str]:
    """Render the ``## What happened`` block for ``render_summary``.

    Reads ``what-happened.md`` (written by ``summarize run``, the ``light``
    model's narration of the run). Absent on the first render (``write merge
    state`` runs before ``summarize run``) — renders ``(not written)``, same
    as every other optional-file precedent in this renderer. ``finalize merge
    state`` re-renders after ``summarize run`` has written the file, so this
    is the only section that differs between the two renders.
    """
    text = _read_text(paths.what_happened)
    return ["## What happened", "", text if text else "(not written)"]


def render_summary(
    paths: MergePaths,
    *,
    rerun_cmd: str,
    max_rounds: int,
    project_dir: str,
    test_cmd: str = "",
    local_fix_attempts: int | None = None,
    learning_pointers: bool = False,
) -> str:
    """Render the ``<slug>-merge-state.md`` body.

    ``learning_pointers`` (off by default, so ``merge_branch_v1``'s output is
    unchanged) adds a ``Learning proposal: <journal>/learning.md`` line for
    every CI-fix child run that wrote a proposal.

    Section order matches the plan's "Summary output" block; later steps only
    ever insert new sections here, never reorder these. ``Status`` is
    ``MERGED``/``MERGED_NEEDS_ATTENTION``/``HALTED`` (see ``_status``, which
    raises on the unreachable fourth combination). Every *round*-dependent
    section renders the round that ran last — round 2 once ``RETRY-1``
    exists, per ``_current_round``. Missing optional files render as
    ``(none)``.
    """
    repo = _read_or_none(paths.repo)
    branch = _read_or_none(paths.branch)
    base_name = _read_or_none(paths.base)
    base_source = _read_or_none(paths.base_source)
    start_head = _read_or_none(paths.start_head)
    round_n = _current_round(paths, max_rounds)
    status = _status(paths)
    warning_count = len(_warnings_lines(paths))
    pointer_lines = _learning_pointer_lines(paths, max_rounds) if learning_pointers else []

    lines = [
        f"# merge_branch: {repo}, branch {branch}",
        "",
        f"Status: {status}",
        f"Base: {base_name} (source: {base_source})",
        f"Head: {start_head}",
        f"Round: {round_n} of {max_rounds}",
        *([f"Warnings: {warning_count}"] if warning_count else []),
        f"Finished: {_now_iso()}",
        f"Rerun: `{rerun_cmd}`",
        "",
        "## Rebase",
        "",
        _read_or_none(paths.rebase_conflicts(round_n)),
        "",
        *_local_tests_section(
            paths,
            round_n,
            test_cmd=test_cmd,
            local_fix_attempts=local_fix_attempts,
            project_dir=project_dir,
        ),
        "",
        *_pull_request_section(paths),
        "",
        *_builds_section(paths, round_n),
        "",
        *_ci_fix_section(paths, round_n),
        *(
            [*pointer_lines, ""]
            if pointer_lines
            else []
        ),
        *_copilot_review_section(paths, round_n),
        "",
        *_review_findings_section(paths, round_n),
        "",
        *_merge_section(paths, round_n, project_dir),
        "",
        *_post_merge_build_section(paths),
        "",
        *([*_warnings_section(paths), ""] if warning_count else []),
        *_what_happened_section(paths),
        "",
        "## Working tree at stop",
        "",
        "```",
        _git_status_short(project_dir),
        "```",
        "",
        "## Why it stopped",
        "",
        _read_or_none(paths.halt_reason),
        "",
        "## What to do next",
        "",
        _next_step_section(paths, rerun_cmd),
        "",
    ]
    return "\n".join(lines)


class WriteMergeState(BaseStage):
    """Write ``<slug>-merge-state.md`` and ``summary-path.txt`` into the work root.

    Output is the summary path, so ``Escalate`` and the human can find it via
    ``ctx.get("write merge state")`` or by reading ``summary-path.txt``
    directly (the latter is what ``Escalate`` does, since it may run in a
    fresh checkpoint restore that never re-executed this stage this attempt).
    """

    needs_agent = False

    def __init__(
        self,
        *,
        paths: MergePaths,
        rerun_cmd: str,
        max_rounds: int,
        project_dir: str,
        test_cmd: str = "",
        local_fix_attempts: int | None = None,
        learning_pointers: bool = False,
    ) -> None:
        self._paths = paths
        self._learning_pointers = learning_pointers
        self._rerun_cmd = rerun_cmd
        self._max_rounds = max_rounds
        self._project_dir = project_dir
        self._test_cmd = test_cmd
        self._local_fix_attempts = local_fix_attempts

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        slug = _read_text(self._paths.slug) or "run"
        body = render_summary(
            self._paths,
            rerun_cmd=self._rerun_cmd,
            max_rounds=self._max_rounds,
            project_dir=self._project_dir,
            test_cmd=self._test_cmd,
            local_fix_attempts=self._local_fix_attempts,
            learning_pointers=self._learning_pointers,
        )
        summary_path = self._paths.summary(slug)
        Path(summary_path).parent.mkdir(parents=True, exist_ok=True)
        Path(summary_path).write_text(body)
        Path(self._paths.summary_path).write_text(summary_path)
        return StageResult(name="", success=True, output=summary_path)


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


class _Notice(AlertMessage):
    """An ``AlertMessage`` whose ``title`` is an explicit heading, not the event's."""

    def __init__(self, heading: str, detail: str) -> None:
        super().__init__(event=AlertEvent.COMPLETE, pipeline_name="merge_branch", detail=detail)
        self._heading = heading

    @property
    def title(self) -> str:
        return self._heading


async def notify(heading: str, detail: str) -> None:
    """Fire an in-run notification.

    The only in-run notification path (besides the runner's own lifecycle
    alerts) — no second ``osascript`` call anywhere else in this pipeline.
    """
    manager = AlertManager(channels=[MacOSChannel()])
    await manager.fire(_Notice(heading, detail))


# ---------------------------------------------------------------------------
# warn — the one permitted warn-and-continue path (AGENTS.md Principles)
# ---------------------------------------------------------------------------


def warn(paths: MergePaths, text: str) -> None:
    """Append *text* to ``warnings.txt`` and print it to stderr.

    Never halts, never changes an exit code — used only for a merge with no
    post-merge workflow, the one report the plan calls a warning rather than
    a halt.
    """
    path = Path(paths.warnings)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(text.rstrip("\n") + "\n")
    print(f"WARNING: {text}", file=sys.stderr)

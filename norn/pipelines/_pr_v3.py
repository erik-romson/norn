"""Private helper: the PR phase of fix_jira_issue_v3 — not a pipeline.

The ONE home of ``add_pr_stages`` (stages 69-82 of fix_jira_issue_v3's stage
table): the provenance check, the PR-state wipe, the fork-safe branch
resolver, the range check, the push, the draft PR, the CI rounds with
``check_and_fix_ci_v2`` children, and the stop-or-announce ending.

Every stage is ``always_run``: the whole PR phase reruns on every
``--resume``, so a halted guard can never be restored from the checkpoint as a
success. That is why v3 needs no ``AssertFreshRun``. ``check pr provenance``
runs first and reads nothing from the PR root; ``reset pr state`` then deletes
and recreates the root (A12), so nothing in it has to survive a rerun. The
record of which heads may be pushed, ``v3/shipped-heads``, lives outside the
root (A18).

The merge helpers are reused by import and composition only
(``_merge_gh.py`` and ``_merge_state.py`` are frozen for this change).
``WaitForChecks`` sends its own "PR checks finished" notices under the name
``merge_branch`` (A14): cosmetic, documented, not fixed.

``fix_issue_v1`` reuses the builder through keyword parameters whose defaults
keep fix_jira_issue_v3 unchanged: the commit-subject prefix, the pipeline name
of notices and resume hints, the resume argument, the fetch notes, and the
start branch that sets the PR base. ``CheckPrBase`` is the same resolution run
before any work; only fix_issue_v1 adds it.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from norn.dsl import Pipeline, fail, file_exists
from norn.models import PipelineContext, StageResult
from norn.pipelines._child_cost_v3 import CiFixChildV3
from norn.pipelines._decisions_v3 import read_field
from norn.pipelines._fix_jira_v3 import (
    PIPELINE,
    V3Paths,
    _headings,
    attribution_lines,
    notify,
    subject_problems,
)
from norn.pipelines._merge_gh import (
    HEAD_SYNC_TIMEOUT,
    NO_CHECKS_GRACE,
    SETTLE_WINDOW,
    ResolveBranch,
    WaitForChecks,
    _Fail,
    _write,
    gh,
    git,
    push_cmd,
    sync_after_ci_fix_cmd,
)
from norn.pipelines._merge_state import (
    HaltingCommand,
    MergePaths,
    all_of,
    halt_shell,
    halted,
    running,
)
from norn.pipelines._plan_gates_v2 import format_gate_error
from norn.pipelines._ship_v3 import BODY_END, BODY_START, ShipInputError, read_rewrite_range
from norn.runner import resolve_run_path
from norn.stages.base import BaseStage

# Stage backstop of every `wait for checks` stage (finding 12). WaitForChecks
# has no overall deadline of its own: the head sync (HEAD_SYNC_TIMEOUT), the
# no-checks grace (NO_CHECKS_GRACE), then `pre_sleep` (up to 3,540 s: 0.8 x the
# CI estimate, capped at the poll timeout minus 60) BEFORE the poll deadline
# starts, the poll timeout (at most 3,600 s), the settle window, three 30 s
# poll intervals of slack and 120 s for the gh calls. Lower, a slow CI would
# end as a plain stage timeout instead of the named `checks_timeout` halt.
WAIT_FOR_CHECKS_TIMEOUT = (
    HEAD_SYNC_TIMEOUT + NO_CHECKS_GRACE + 3540 + 3600 + SETTLE_WINDOW + 3 * 30 + 120
)

# gh's own `-q` filter for `gh pr list --json number,baseRefName,isCrossRepository`:
# one "<number> <base>" line per PR whose head is in this repository. `--head`
# matches by branch name only, so a fork's PR can share the name (finding 19).
NATIVE_PR_Q = '.[] | select(.isCrossRepository == false) | "\\(.number) \\(.baseRefName)"'

COMMITS_TITLE = "Commits"
REWORD_TITLE = "Reword at squash/merge"
TESTING_TITLE = "Testing"

ANNOUNCE_STAGE = "announce pr ready"

# Prints the marker block of a file, both marker lines included.
_BLOCK_AWK = "index($0, s) { p = 1 } p { print } index($0, e) { p = 0 }"
# The awk pattern of _merge_gh.pr_followups_body_cmd: drops the marker block.
# It also drops trailing blank lines, so a rerun does not grow the body.
_DROP_BLOCK_AWK = (
    "index($0, s) { skip = 1 } "
    "!skip { if ($0 ~ /^[ \\t\\r]*$/) { blank++ } "
    'else { while (blank > 0) { print ""; blank-- } print } } '
    "index($0, e) { skip = 0 }"
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _ok(output: Any = "") -> StageResult:
    return StageResult(name="", success=True, output=output)


def _fail(error: str, output: Any = "") -> StageResult:
    return StageResult(name="", success=False, error=error, output=output)


def _git(project_dir: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=project_dir,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def _repo_fail(proc: subprocess.CompletedProcess[str]) -> StageResult:
    return _fail(
        format_gate_error(
            "cannot read the repository state",
            [f"$ {' '.join(proc.args)}", *_lines(proc.stderr)],
            "Fix the repository state, then --resume.",
        )
    )


def pr_paths(paths: V3Paths) -> MergePaths:
    """The ``MergePaths`` of the PR phase (its root is ``v3/pr``)."""
    return MergePaths(paths.pr_state)


def title_file(pr: MergePaths) -> str:
    """The PR title ``update pr body`` writes and ``open draft pr`` reads."""
    return str(Path(pr.root) / "title")


def questions_file(paths: V3Paths) -> str:
    """Gate 1's file; the plan-review builder gets the same path from the pipeline."""
    return str(Path(paths.art_dir) / f"{paths.key}-plan-questions.md")


def resume_hint(key: str, auto: bool, *, pipeline: str = PIPELINE) -> str:
    """The resume command; ``auto`` is not restored by ``--resume`` (A16)."""
    return f"norn run {pipeline} {key}{' auto' if auto else ''} --resume"


async def _notify(title: str, detail: str, pipeline: str) -> None:
    """Send *title*/*detail*; the default pipeline name keeps the two-argument call."""
    if pipeline == PIPELINE:
        await notify(title, detail)
    else:
        await notify(title, detail, pipeline=pipeline)


FetchNotes = Callable[[], list[str]]

FETCH_NOTES_ACTION = "Run without --resume so 'fetch issue' writes it again."


def _check_notes_pair(owner: str, fetch_notes: FetchNotes | None, notes_path: str | None) -> None:
    if (fetch_notes is None) != (notes_path is None):
        raise ValueError(f"{owner}: pass fetch_notes and notes_path together, or neither")


def _read_notes(fetch_notes: FetchNotes | None) -> list[str] | None:
    """The fetch notes, or None without a source; raises ``ShipInputError`` naming the error."""
    if fetch_notes is None:
        return None
    try:
        return list(fetch_notes())
    except Exception as exc:
        raise ShipInputError(
            format_gate_error(
                f"cannot read the fetch notes: {type(exc).__name__}",
                _lines(str(exc)),
                FETCH_NOTES_ACTION,
            )
        ) from exc


def _notes_suffix(notes: list[str] | None) -> str:
    """`` — <n> fetch notes, see issue.md`` when there is at least one note."""
    return f" — {len(notes)} fetch notes, see issue.md" if notes else ""


def _read_required(ctx: PipelineContext, path: str, what: str) -> str:
    resolved = resolve_run_path(ctx, path)
    if not resolved.is_file():
        raise ShipInputError(
            format_gate_error(
                f"{what} is missing: {resolved}", [], "Run the PR phase again with --resume."
            )
        )
    return resolved.read_text().strip()


# ---------------------------------------------------------------------------
# 69. check pr provenance (A18)
# ---------------------------------------------------------------------------


class CheckPrProvenance(BaseStage):
    """HEAD is a head this run shipped (or a CI fix it accepted), and the tree is clean.

    Runs before anything in the PR root is touched and reads nothing from it.
    """

    needs_agent = False

    def __init__(
        self,
        *,
        paths: V3Paths,
        project_dir: str,
        key: str,
        auto: bool,
        pipeline_name: str = PIPELINE,
    ) -> None:
        self.paths = paths
        self.project_dir = project_dir
        self.key = key
        self.auto = auto
        self.pipeline_name = pipeline_name

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        resume = resume_hint(self.key, self.auto, pipeline=self.pipeline_name)
        shipped = resolve_run_path(ctx, self.paths.shipped_heads)
        heads = _lines(shipped.read_text()) if shipped.is_file() else []
        heads = [h.strip() for h in heads]
        if not heads:
            return _fail(
                format_gate_error(
                    "no shipped head recorded: the ship phase did not finish",
                    [f"{shipped} is missing or empty"],
                    f"Finish the ship phase: {resume}",
                )
            )

        proc = _git(self.project_dir, "rev-parse", "HEAD")
        if proc.returncode != 0:
            return _repo_fail(proc)
        head = proc.stdout.strip()

        if head not in heads:
            last = heads[-1]
            anc = _git(self.project_dir, "merge-base", "--is-ancestor", last, "HEAD")
            if anc.returncode not in (0, 1):
                return _repo_fail(anc)
            lines = [f"HEAD {head[:7]} was not shipped by this run"]
            if anc.returncode == 0:
                log = _git(self.project_dir, "log", "--oneline", "-5", f"{last}..HEAD")
                if log.returncode != 0:
                    return _repo_fail(log)
                lines.append(f"commits after {last[:7]}:")
                lines += [f"  {line}" for line in _lines(log.stdout)]
            else:
                lines.append(
                    f"HEAD does not descend from {last[:7]}: history was rewritten after gate 4"
                )
            lines.append(
                f"`git reset --hard {last}` to drop them, or append `{head}` to `{shipped}` "
                f"to push them without this run's review; then `{resume}`"
            )
            return _fail("\n".join(lines))

        status = _git(self.project_dir, "status", "--porcelain", "--untracked-files=normal")
        if status.returncode != 0:
            return _repo_fail(status)
        dirty = [line[3:] for line in status.stdout.splitlines() if line.strip()]
        if dirty:
            return _fail(
                format_gate_error(
                    f"{len(dirty)} paths are changed or untracked before the push",
                    dirty,
                    "the push and the CI-fix child need a clean tree: commit (then append the "
                    f"new HEAD to {shipped}), stash or delete them; then {resume}",
                ),
                output="\n".join(dirty),
            )
        return _ok(head)


# ---------------------------------------------------------------------------
# 70. reset pr state (A12)
# ---------------------------------------------------------------------------


class ResetPrState(BaseStage):
    """Delete and recreate the PR root. ``v3/shipped-heads`` lives outside it."""

    needs_agent = False

    def __init__(self, *, paths: V3Paths) -> None:
        self.paths = paths

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        root = resolve_run_path(ctx, self.paths.pr_state)
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        return _ok(str(root))


# ---------------------------------------------------------------------------
# 71. resolve branch (finding 19)
# ---------------------------------------------------------------------------


class ResolveBranchV3(ResolveBranch):
    """``ResolveBranch`` whose open-PR lookup ignores PRs from forks.

    Only ``run`` is overridden; ``_resolve_base`` and ``_check_remote_branch``
    are inherited unchanged.

    *start_branch_file* names the file ``prepare branch`` writes with the
    branch the run was cut from. A name in it is the base after ``--arg base``
    and an open PR, ahead of the fork-point guess, which only knows the
    default branch and ``_FORK_POINT_CANDIDATES``. An empty or missing file
    leaves the resolution to ``_resolve_base``.

    *base_file* (``base.sha``) adds the check that ``origin/<base>`` contains
    the commit the run started from; without it the PR would also carry every
    commit between the two, none of which this run reviewed.

    Both default to None, which keeps fix_jira_issue_v3 unchanged.
    """

    def __init__(
        self,
        *,
        paths: MergePaths,
        project_dir: str,
        base_arg: str,
        overwrite_remote: str,
        start_branch_file: str | None = None,
        base_file: str | None = None,
    ) -> None:
        super().__init__(
            paths=paths, project_dir=project_dir, base_arg=base_arg, overwrite_remote=overwrite_remote
        )
        self._start_branch_file = start_branch_file
        self._base_file = base_file

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        Path(self._paths.root).mkdir(parents=True, exist_ok=True)
        try:
            resolved = await self._resolve(ctx, early=False)
        except _Fail as exc:
            return StageResult(name="", success=False, error=str(exc))

        paths = self._paths
        _write(paths.branch, resolved["branch"])
        _write(paths.slug, resolved["slug"])
        _write(paths.repo, resolved["repo"])
        _write(paths.base, resolved["base"])
        _write(paths.base_source, resolved["source"])
        _write(paths.default_branch, resolved["default_branch"])
        _write(paths.start_head, resolved["start_head"])
        _write(paths.remote_head, resolved["remote_head"])

        message = f"base: {resolved['base']} ({resolved['source']})"
        if resolved["note"]:
            message = f"{message}\n{resolved['note']}"
        print(message)

        return StageResult(
            name="",
            success=True,
            output={k: resolved[k] for k in ("base", "source", "branch", "repo", "note")},
        )

    def _start_branch(self, ctx: PipelineContext) -> str:
        if self._start_branch_file is None:
            return ""
        path = resolve_run_path(ctx, self._start_branch_file)
        return path.read_text().strip() if path.is_file() else ""

    # Copied from _merge_gh.py:269-356 (cannot be edited: _merge_gh.py is frozen for this change),
    # with the start branch, the base.sha check and *early*: before any work,
    # HEAD is still base.sha, so there is nothing to merge yet and no remote
    # branch of this run to guard.
    async def _resolve(self, ctx: PipelineContext, *, early: bool) -> dict[str, str]:
        project_dir = self._project_dir
        env = ctx.env

        await git(["fetch", "origin", "--prune"], cwd=project_dir, env=env)

        _, out, _ = await git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=project_dir, env=env)
        branch = out.strip()
        slug = re.sub(r"[^A-Za-z0-9._-]", "-", branch)

        _, out, _ = await git(["remote", "get-url", "origin"], cwd=project_dir, env=env)
        remote_url = out.strip()

        _, out, _ = await gh(
            ["repo", "view", remote_url, "--json", "nameWithOwner,defaultBranchRef"],
            cwd=project_dir, env=env,
        )
        repo_data = json.loads(out)
        repo = repo_data["nameWithOwner"]
        default_branch = repo_data["defaultBranchRef"]["name"]

        _, out, _ = await gh(
            [
                "pr", "list", "-R", repo, "--head", branch, "--state", "open",
                "--json", "number,baseRefName,isCrossRepository",
            ],
            cwd=project_dir, env=env,
        )
        # The one change: a fork's PR that shares the branch name is not
        # this branch's PR, so it can neither set the base nor count below.
        open_prs = [pr for pr in json.loads(out) if not pr["isCrossRepository"]]
        if len(open_prs) > 1:
            listing = "; ".join(f"#{pr['number']} (base {pr['baseRefName']})" for pr in open_prs)
            raise _Fail(f"more than one open PR for {branch}: {listing}")
        open_pr = open_prs[0] if open_prs else None

        start = self._start_branch(ctx)
        if start and not self._base_arg and not open_pr and start != branch:
            base, source = start, "start branch"
        else:
            base, source = await self._resolve_base(
                branch=branch,
                project_dir=project_dir,
                env=env,
                default_branch=default_branch,
                open_pr=open_pr,
            )

        if branch == base:
            raise _Fail(f"branch and base are both {branch!r}; pass --arg base=<name> or switch branches")

        rc, _, _ = await git(
            ["rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{base}"],
            cwd=project_dir, env=env, ok=(0, 1),
        )
        if not base or rc != 0:
            raise _Fail(f"base {base!r} ({source}) does not exist on origin; push it first or pass --arg base=<name>")

        if self._base_file is not None:
            base_path = resolve_run_path(ctx, self._base_file)
            if not base_path.is_file():
                raise _Fail(f"base.sha is missing: {base_path}")
            problem = base_sha_problem(project_dir, base, source, base_path.read_text().strip())
            if problem:
                raise _Fail(problem)

        if early:
            return {"base": base, "source": source, "branch": branch, "repo": repo}

        _, out, _ = await git(["rev-list", "--count", f"origin/{base}..HEAD"], cwd=project_dir, env=env)
        if out.strip() == "0":
            raise _Fail(f"nothing to merge: HEAD has no commits ahead of origin/{base}")

        note = await self._check_remote_branch(branch=branch, project_dir=project_dir, env=env)

        _, out, _ = await git(["rev-parse", "HEAD"], cwd=project_dir, env=env)
        start_head = out.strip()

        rc, out, _ = await git(
            ["rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"],
            cwd=project_dir, env=env, ok=(0, 1),
        )
        remote_head = out.strip() if rc == 0 else ""

        return {
            "base": base, "source": source, "branch": branch, "repo": repo, "note": note,
            "slug": slug, "default_branch": default_branch, "start_head": start_head,
            "remote_head": remote_head,
        }


class CheckPrBase(ResolveBranchV3):
    """Resolve the PR base before any work, and stop when it cannot take this run's PR.

    The same resolution as ``resolve branch``, so a base that is missing on
    origin, ambiguous, or behind ``base.sha`` stops the run in its first
    minute rather than after the review and the ship gate. Writes nothing:
    the PR phase resolves the base again, because origin can move while the
    run works.
    """

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        try:
            resolved = await self._resolve(ctx, early=True)
        except _Fail as exc:
            return StageResult(name="", success=False, error=str(exc))
        print(f"PR base: {resolved['base']} ({resolved['source']})")
        return StageResult(name="", success=True, output=resolved)


def base_sha_problem(project_dir: str, base: str, source: str, base_sha: str) -> str | None:
    """Say why ``origin/<base>`` cannot be this run's PR base, or None when it can.

    It can when it contains *base_sha*, the commit the run started from: then
    ``origin/<base>..HEAD`` holds only the run's own commits.
    """
    anc = _git(project_dir, "merge-base", "--is-ancestor", base_sha, f"origin/{base}")
    if anc.returncode == 0:
        return None
    if anc.returncode != 1:
        raise _Fail(f"git merge-base --is-ancestor {base_sha[:7]} origin/{base}: {anc.stderr.strip()}")
    log = _git(project_dir, "log", "--format=%h %s", f"origin/{base}..{base_sha}")
    if log.returncode != 0:
        raise _Fail(f"git log origin/{base}..{base_sha[:7]}: {log.stderr.strip()}")
    missing = _lines(log.stdout)
    local = _git(project_dir, "rev-parse", "--verify", "--quiet", f"refs/heads/{base}")
    if local.returncode == 0 and local.stdout.strip() == base_sha:
        action = f"push {base} (`git push origin {base}`), or pass `--arg base=<branch>`; then --resume"
    else:
        action = "push those commits to origin/" + base + ", rebase, or pass `--arg base=<branch>`; then --resume"
    return format_gate_error(
        f"origin/{base} ({source}) does not contain base.sha {base_sha[:7]}: "
        f"the PR would carry {len(missing)} commits this run did not make",
        missing,
        action,
    )


# ---------------------------------------------------------------------------
# 72. check pr range (finding 20)
# ---------------------------------------------------------------------------


def _describe_commits(project_dir: str, shas: list[str]) -> list[str] | StageResult:
    if not shas:
        return []
    proc = _git(project_dir, "show", "-s", "--format=%h %s", *shas)
    if proc.returncode != 0:
        return _repo_fail(proc)
    return _lines(proc.stdout)


class CheckPrRange(BaseStage):
    """The PR's commits (``origin/<base>..HEAD``) are the commits this run reviewed."""

    needs_agent = False

    def __init__(self, *, paths: V3Paths, project_dir: str, base_file: str) -> None:
        self.paths = paths
        self.project_dir = project_dir
        self.base_file = base_file

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        pr = pr_paths(self.paths)
        try:
            base = _read_required(ctx, pr.base, "the PR base")
            base_sha = _read_required(ctx, self.base_file, "base.sha")
        except ShipInputError as exc:
            return _fail(str(exc))

        pr_list = _git(self.project_dir, "rev-list", f"origin/{base}..HEAD")
        if pr_list.returncode != 0:
            return _repo_fail(pr_list)
        run_list = _git(self.project_dir, "rev-list", f"{base_sha}..HEAD")
        if run_list.returncode != 0:
            return _repo_fail(run_list)
        in_pr, in_run = pr_list.stdout.split(), run_list.stdout.split()
        if len(in_pr) == len(in_run) and set(in_pr) == set(in_run):
            return _ok(f"{len(in_pr)} commits on origin/{base}..HEAD")

        action = "pass `--arg base=<branch>` or rebase, then --resume"
        run_set, pr_set = set(in_run), set(in_pr)
        extra = [sha for sha in in_pr if sha not in run_set]
        if extra:
            described = _describe_commits(self.project_dir, extra)
            if isinstance(described, StageResult):
                return described
            return _fail(
                format_gate_error(
                    f"origin/{base}..HEAD has {len(in_pr)} commits; this run reviewed "
                    f"{len(in_run)} ({base_sha[:7]}..HEAD)",
                    described,
                    f"the PR would contain commits this run did not review; {action}",
                )
            )
        missing = [sha for sha in in_run if sha not in pr_set]
        described = _describe_commits(self.project_dir, missing)
        if isinstance(described, StageResult):
            return described
        return _fail(
            format_gate_error(
                f"origin/{base} already contains {len(missing)} of the commits this run reviewed",
                described,
                f"the PR would not contain every commit this run reviewed; {action}",
            )
        )


# ---------------------------------------------------------------------------
# PR body: the marker block (A11)
# ---------------------------------------------------------------------------


class PrBodyError(ValueError):
    """The PR body file breaks the marker contract; ``str()`` is the stage error."""


def _marker_lines(lines: list[str]) -> tuple[int, int]:
    start = next((i for i, line in enumerate(lines) if BODY_START in line), None)
    end = next((i for i, line in enumerate(lines) if BODY_END in line), None)
    if start is None or end is None or end < start:
        raise PrBodyError(
            format_gate_error(
                "the PR body has no complete marker block",
                [f"expected {BODY_START} … {BODY_END}"],
                "Restore the markers in the PR body file, then --resume.",
            )
        )
    return start, end


def _span(lines: list[str], title: str) -> tuple[int, int] | None:
    """``(heading line, end)`` of the level-2 section *title* inside the marker block."""
    start_marker, end_marker = _marker_lines(lines)
    heads = _headings(lines)
    for pos, (i, level, text) in enumerate(heads):
        if level != 2 or text != title or not start_marker < i < end_marker:
            continue
        end = next((j for j, lvl, _t in heads[pos + 1:] if lvl <= 2), len(lines))
        return i, min(end, end_marker)
    return None


def refresh_sections(text: str, *, commits: list[str], reword: list[str]) -> str:
    """Replace the bodies of ``## Commits`` and ``## Reword at squash/merge``.

    The Reword section is added (before ``## Testing``, else before the end
    marker) or removed as *reword* requires. Everything else is kept.
    """
    lines = text.splitlines()
    # Reword comes after Commits: edit it first so the Commits span stays valid.
    reword_span = _span(lines, REWORD_TITLE)
    reword_block = [f"## {REWORD_TITLE}", *reword, ""] if reword else []
    if reword_span is not None:
        lines[reword_span[0]:reword_span[1]] = reword_block
    elif reword_block:
        testing = _span(lines, TESTING_TITLE)
        at = testing[0] if testing is not None else _marker_lines(lines)[1]
        lines[at:at] = reword_block

    commits_span = _span(lines, COMMITS_TITLE)
    if commits_span is None:
        raise PrBodyError(
            format_gate_error(
                f"the PR body has no '## {COMMITS_TITLE}' section inside the marker block",
                [],
                f"Restore the '## {COMMITS_TITLE}' heading in the PR body file, then --resume.",
            )
        )
    lines[commits_span[0]:commits_span[1]] = [f"## {COMMITS_TITLE}", *commits, ""]
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def commit_lines(project_dir: str, base: str, key: str) -> tuple[list[str], list[str]]:
    """``(commits, reword)`` for ``base..HEAD``, oldest first.

    A commit line is ``- <subject> (<n> files)``; a reword line is
    ``- <sha7> <subject>`` for a subject that breaks the house style.
    """
    log = _git(project_dir, "log", "--reverse", "--format=%H %s", f"{base}..HEAD")
    if log.returncode != 0:
        raise PrBodyError(
            format_gate_error(
                f"cannot list the commits of {base[:7]}..HEAD",
                _lines(log.stderr),
                "Fix the repository state, then --resume.",
            )
        )
    commits: list[str] = []
    reword: list[str] = []
    for line in _lines(log.stdout):
        sha, _, subject = line.partition(" ")
        files = _git(project_dir, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", sha)
        if files.returncode != 0:
            raise PrBodyError(
                format_gate_error(
                    f"cannot list the files of {sha[:7]}",
                    _lines(files.stderr),
                    "Fix the repository state, then --resume.",
                )
            )
        count = len([name for name in files.stdout.split("\0") if name])
        commits.append(f"- {subject} ({count} files)")
        if subject_problems(subject, key):
            reword.append(f"- {sha[:7]} {subject}")
    return commits, reword


def replace_block_sh(pr: MergePaths, body_file: str) -> str:
    """sh lines that put *body_file*'s marker block into the live body of PR ``$NUM``.

    Needs ``$REPO`` and ``$NUM``. The old block is dropped wherever it is and
    the new one appended, so a rerun replaces the block and never duplicates
    it. Text outside the markers is kept. Ends in one ``gh pr edit``.
    """
    q = shlex.quote
    block = q(str(Path(pr.root) / "pr-block.md"))
    live = q(str(Path(pr.root) / "pr-live-body.md"))
    live_new = q(str(Path(pr.root) / "pr-live-body.md.new"))
    start, end = q(BODY_START), q(BODY_END)
    return (
        f"awk -v s={start} -v e={end} {q(_BLOCK_AWK)} {q(body_file)} > {block} || exit 1\n"
        f"grep -qF {end} {block} || "
        f"{{ echo {q('no complete marker block in ' + body_file)} >&2; exit 1; }}\n"
        f'gh pr view "$NUM" -R "$REPO" --json body -q .body > {live} || exit 1\n'
        f"awk -v s={start} -v e={end} {q(_DROP_BLOCK_AWK)} {live} > {live_new} || exit 1\n"
        f"{{ if [ -s {live_new} ]; then cat {live_new}; printf '\\n'; fi; cat {block}; }} > {live} || exit 1\n"
        f"rm -f {live_new}\n"
        f'gh pr edit "$NUM" -R "$REPO" --body-file {live} || exit 1\n'
    )


class UpdatePrBody(BaseStage):
    """Refresh ``## Commits`` and ``## Reword at squash/merge`` and write the title.

    ``update pr body`` (``publish=False``) runs before ``open draft pr``;
    ``refresh pr body`` (``publish=True``) runs after the CI rounds and, when
    the body changed and the PR exists, replaces the marker block of the live
    PR body. An unchanged body makes no ``gh`` call. *subject_prefix*
    (default: the file key) is the prefix the Reword check expects.
    """

    needs_agent = False

    def __init__(
        self,
        *,
        paths: V3Paths,
        project_dir: str,
        publish: bool,
        subject_prefix: str | None = None,
    ) -> None:
        self.paths = paths
        self.project_dir = project_dir
        self.publish = publish
        self.subject_prefix = subject_prefix

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        pr = pr_paths(self.paths)
        body_path = resolve_run_path(ctx, self.paths.pr_body)
        ship = resolve_run_path(ctx, self.paths.ship)
        try:
            base = read_rewrite_range(resolve_run_path(ctx, self.paths.rewrite_range))["base"]
            if not body_path.is_file():
                raise ShipInputError(
                    format_gate_error(
                        f"the PR body is missing: {body_path}",
                        [],
                        "Run without --resume so 'assemble pr body' runs.",
                    )
                )
            title = (read_field(ship, "TITLE", first_token=False) or "").strip()
            if not title:
                raise ShipInputError(
                    format_gate_error(
                        f"no TITLE in {ship}", [], "Set the TITLE line there, then --resume."
                    )
                )
            text = body_path.read_text()
            commits, reword = commit_lines(
                self.project_dir,
                base,
                self.paths.key if self.subject_prefix is None else self.subject_prefix,
            )
            new = refresh_sections(text, commits=commits, reword=reword)
        except (ShipInputError, PrBodyError) as exc:
            return _fail(str(exc))

        title_path = resolve_run_path(ctx, title_file(pr))
        title_path.parent.mkdir(parents=True, exist_ok=True)
        title_path.write_text(f"{title}\n")

        changed = new != text
        if changed:
            body_path.write_text(new)
        published = False
        if self.publish and changed and resolve_run_path(ctx, pr.pr).is_file():
            script = (
                f"cd {shlex.quote(self.project_dir)} || exit 1\n"
                f"REPO=$(cat {shlex.quote(pr.repo)}) || exit 1\n"
                f"NUM=$(cat {shlex.quote(pr.pr)}) || exit 1\n"
                + replace_block_sh(pr, str(body_path))
            )
            proc = subprocess.run(
                ["sh", "-c", script],
                env={**os.environ, **ctx.env},
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if proc.returncode != 0:
                return _fail(
                    format_gate_error(
                        "could not update the live PR body",
                        _lines(proc.stderr + proc.stdout)[-5:],
                        "Fix the cause above (often gh auth or the network), then --resume.",
                    )
                )
            published = True
        return _ok({"changed": changed, "published": published, "commits": len(commits)})


# ---------------------------------------------------------------------------
# 75. open draft pr (findings 13, 19)
# ---------------------------------------------------------------------------


def draft_pr_cmd(paths: MergePaths, project_dir: str, title_file: str, body_file: str) -> str:
    """Return the sh fragment of ``open draft pr``.

    No open PR from this repository: create a draft. One: halt
    ``pr_base_mismatch`` on another base, else replace its marker block. Fork
    PRs that share the branch name are ignored. Both paths write ``pr`` and
    ``pr.json`` (``WaitForChecks`` reads ``pr``). Never marks a PR ready,
    never converts a ready PR back to draft and never requests reviewers.
    """
    q = shlex.quote
    list_cmd = (
        'gh pr list -R "$REPO" --head "$BRANCH" --state open '
        f"--json number,baseRefName,isCrossRepository -q {q(NATIVE_PR_Q)}"
    )
    mismatch_halt = halt_shell(
        paths,
        "pr_base_mismatch",
        'printf \'PR #%s base is %s, expected %s\\n\' "$NUM" "$BASE_REF" "$BASE"',
    )
    return (
        f"cd {q(project_dir)} || exit 1\n"
        f"REPO=$(cat {q(paths.repo)}) || exit 1\n"
        f"BRANCH=$(cat {q(paths.branch)}) || exit 1\n"
        f"BASE=$(cat {q(paths.base)}) || exit 1\n"
        f"OPEN=$({list_cmd}) || exit 1\n"
        'if [ "$(printf \'%s\\n\' "$OPEN" | grep -c .)" -gt 1 ]; then\n'
        '  printf \'more than one open PR for %s:\\n%s\\n\' "$BRANCH" "$OPEN" >&2\n'
        "  exit 1\n"
        "fi\n"
        'if [ -z "$OPEN" ]; then\n'
        '  gh pr create -R "$REPO" --draft --base "$BASE" --head "$BRANCH" '
        f'--title "$(cat {q(title_file)})" --body-file {q(body_file)} || exit 1\n'
        f"  OPEN=$({list_cmd}) || exit 1\n"
        '  NUM=${OPEN%% *}\n'
        '  [ -n "$NUM" ] || { echo "the new PR is not listed for $BRANCH" >&2; exit 1; }\n'
        f"  printf 'created\\n' > {q(paths.pr_origin)}\n"
        "else\n"
        '  NUM=${OPEN%% *}\n'
        '  BASE_REF=${OPEN#* }\n'
        '  if [ "$BASE_REF" != "$BASE" ]; then\n'
        f"{mismatch_halt}"
        "  fi\n"
        f"{replace_block_sh(paths, body_file)}"
        f"  printf 'reused\\n' > {q(paths.pr_origin)}\n"
        "fi\n"
        f'printf \'%s\\n\' "$NUM" > {q(paths.pr)}\n'
        f'gh pr view "$NUM" -R "$REPO" --json number,url,isDraft,headRefOid > {q(paths.pr_json)} || exit 1\n'
    )


# ---------------------------------------------------------------------------
# 78. stop for human
# ---------------------------------------------------------------------------


def _pr_url(pr: MergePaths) -> str | None:
    path = Path(pr.pr_json)
    if not path.is_file():
        return None
    return json.loads(path.read_text())["url"]


class StopForHuman(BaseStage):
    """End a halted PR phase with the halt's kind, detail and the resume command.

    Never prints ``HALTS[kind].next_step``: that text is written for
    merge_branch. With *fetch_notes* and *notes_path* (both or neither), a
    run with fetch notes counts them in the notice and names *notes_path*.
    """

    needs_agent = False

    def __init__(
        self,
        *,
        paths: V3Paths,
        key: str,
        auto: bool,
        pipeline_name: str = PIPELINE,
        fetch_notes: FetchNotes | None = None,
        notes_path: str | None = None,
    ) -> None:
        _check_notes_pair("StopForHuman", fetch_notes, notes_path)
        self.paths = paths
        self.key = key
        self.auto = auto
        self.pipeline_name = pipeline_name
        self.fetch_notes = fetch_notes
        self.notes_path = notes_path

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        try:
            notes = _read_notes(self.fetch_notes)
        except ShipInputError as exc:
            return _fail(str(exc))
        pr = pr_paths(self.paths)
        reason = resolve_run_path(ctx, pr.halt_reason)
        resume = resume_hint(self.key, self.auto, pipeline=self.pipeline_name)
        if not reason.is_file():
            return _fail(
                format_gate_error(
                    "the PR phase halted without a reason",
                    [f"{reason} is missing"],
                    resume,
                )
            )
        lines = reason.read_text().splitlines()
        kind = lines[0].removeprefix("KIND:").strip() if lines else "unknown"
        heading = lines[1].strip() if len(lines) > 1 else "The PR phase halted"
        detail = _lines("\n".join(lines[2:]))[-5:]
        await _notify("Stopped", f"{heading}{_notes_suffix(notes)}", self.pipeline_name)

        error = [f"{heading} ({kind})", *detail]
        url = _pr_url(pr)
        if url:
            error.append(f"PR: {url}")
        if kind == "checks_none":
            error.append("workflows may skip draft PRs, or the PR has a merge conflict")
        if notes:
            error.append(f"{len(notes)} fetch notes: {self.notes_path}")
        error.append(resume)
        return _fail("\n".join(error), output=kind)


# ---------------------------------------------------------------------------
# 79. checks green (findings 11, 14)
# ---------------------------------------------------------------------------


def green_checks(pr: MergePaths) -> list[tuple[Path, dict]]:
    """Every checks file of the PR root whose outcome is ``green``.

    The root is wiped at the start of each PR phase, and a green wait ends the
    rounds, so a passing phase has exactly one.
    """
    found: list[tuple[Path, dict]] = []
    for path in sorted(Path(pr.root).glob("checks-*.json")):
        data = json.loads(path.read_text())
        if data.get("outcome") == "green":
            found.append((path, data))
    return found


def _passed(data: dict) -> list[dict]:
    return [c for c in data.get("checks", []) if c.get("bucket") == "pass"]


class ChecksGreen(BaseStage):
    """The checks passed on the head that is pushed, and no commit carries attribution."""

    needs_agent = False

    def __init__(self, *, paths: V3Paths, project_dir: str, base_file: str) -> None:
        self.paths = paths
        self.project_dir = project_dir
        self.base_file = base_file

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        pr = pr_paths(self.paths)
        if resolve_run_path(ctx, pr.halt).exists():
            return _fail(
                format_gate_error(
                    "the PR phase halted", [str(pr.halt_reason)], "Read the halt reason, then --resume."
                )
            )
        try:
            green = _read_required(ctx, pr.green_head, "the green head")
            branch = _read_required(ctx, pr.branch, "the branch")
            base_sha = _read_required(ctx, self.base_file, "base.sha")
        except ShipInputError as exc:
            return _fail(str(exc))

        head = _git(self.project_dir, "rev-parse", "HEAD")
        if head.returncode != 0:
            return _repo_fail(head)
        fetch = _git(self.project_dir, "fetch", "origin", "--prune")
        if fetch.returncode != 0:
            return _repo_fail(fetch)
        remote = _git(self.project_dir, "rev-parse", f"refs/remotes/origin/{branch}")
        if remote.returncode != 0:
            return _repo_fail(remote)
        local, origin = head.stdout.strip(), remote.stdout.strip()
        if green != local or green != origin:
            return _fail(
                format_gate_error(
                    f"the checks went green on {green[:7]}, but that is not the pushed HEAD",
                    [f"green head: {green}", f"HEAD: {local}", f"origin/{branch}: {origin}"],
                    "Push the head you want checked, then --resume.",
                )
            )

        found = green_checks(pr)
        if len(found) != 1:
            return _fail(
                format_gate_error(
                    f"expected one green checks file in {pr.root}, found {len(found)}",
                    [str(path) for path, _data in found],
                    "Run the PR phase again with --resume.",
                )
            )
        path, data = found[0]
        if not _passed(data):
            names = [f"{c.get('name')}: {c.get('bucket')}" for c in data.get("checks", [])]
            return _fail(
                format_gate_error(
                    "no check passed: every reported check was skipped",
                    names,
                    "Workflows may skip draft PRs; run the checks on this PR, then --resume.",
                ),
                output=str(path),
            )

        log = _git(self.project_dir, "log", "--format=%B", f"{base_sha}..HEAD")
        if log.returncode != 0:
            return _repo_fail(log)
        bad = attribution_lines(log.stdout)
        if bad:
            return _fail(
                format_gate_error(
                    f"{len(bad)} attribution lines in the commit messages of {base_sha[:7]}..HEAD",
                    [line.strip() for line in bad],
                    "Reword those commits (then append the new HEAD to "
                    f"{self.paths.shipped_heads}), push, then --resume.",
                )
            )
        return _ok(f"{len(_passed(data))} checks passed on {green[:7]}")


# ---------------------------------------------------------------------------
# 81. announce pr ready
# ---------------------------------------------------------------------------


class AnnouncePrReady(BaseStage):
    """Notify that the PR is green, saying whether it is a draft.

    The PR's state is read again when the stage runs: a human can mark it
    ready during the check wait. The output is ``draft`` or ``ready``. With
    *fetch_notes* and *notes_path* (both or neither), the notice counts the
    fetch notes.
    """

    needs_agent = False

    def __init__(
        self,
        *,
        paths: V3Paths,
        project_dir: str,
        pipeline_name: str = PIPELINE,
        fetch_notes: FetchNotes | None = None,
        notes_path: str | None = None,
    ) -> None:
        _check_notes_pair("AnnouncePrReady", fetch_notes, notes_path)
        self.paths = paths
        self.project_dir = project_dir
        self.pipeline_name = pipeline_name
        self.fetch_notes = fetch_notes
        self.notes_path = notes_path

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        pr = pr_paths(self.paths)
        try:
            notes = _read_notes(self.fetch_notes)
            repo = _read_required(ctx, pr.repo, "the repository name")
            num = _read_required(ctx, pr.pr, "the PR number")
        except ShipInputError as exc:
            return _fail(str(exc))
        _, out, _ = await gh(
            ["pr", "view", "-R", repo, num, "--json", "isDraft,url"],
            cwd=self.project_dir,
            env=ctx.env,
        )
        data = json.loads(out)
        found = green_checks(pr)
        if len(found) != 1:
            return _fail(
                format_gate_error(
                    f"expected one green checks file in {pr.root}, found {len(found)}",
                    [str(path) for path, _data in found],
                    "Run the PR phase again with --resume.",
                )
            )
        k = len(_passed(found[0][1]))
        url = data["url"]
        if data["isDraft"]:
            state = "draft"
            detail = f"PR #{num} draft, {k} checks green — {url}"
        else:
            state = "ready"
            detail = f"PR #{num} ready for review (not marked by this run), {k} checks green — {url}"
        await _notify("PR ready", f"{detail}{_notes_suffix(notes)}", self.pipeline_name)
        return _ok(state)


# ---------------------------------------------------------------------------
# 82. summary (Q1)
# ---------------------------------------------------------------------------


class RunSummary(BaseStage):
    """Print the branch, the commits, the PR, the gate files and this process's spend.

    With *fetch_notes* and *notes_path* (both or neither), the fetch notes are
    listed before the spend line.
    """

    needs_agent = False

    def __init__(
        self,
        *,
        paths: V3Paths,
        project_dir: str,
        base_file: str,
        fetch_notes: FetchNotes | None = None,
        notes_path: str | None = None,
    ) -> None:
        _check_notes_pair("RunSummary", fetch_notes, notes_path)
        self.paths = paths
        self.project_dir = project_dir
        self.base_file = base_file
        self.fetch_notes = fetch_notes
        self.notes_path = notes_path

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        pr = pr_paths(self.paths)
        try:
            notes = _read_notes(self.fetch_notes)
            branch = _read_required(ctx, pr.branch, "the branch")
            base_sha = _read_required(ctx, self.base_file, "base.sha")
            _read_required(ctx, pr.pr_json, "pr.json")
        except ShipInputError as exc:
            return _fail(str(exc))
        announced = ctx.results.get(ANNOUNCE_STAGE)
        if announced is None or announced.output not in ("draft", "ready"):
            return _fail(
                format_gate_error(
                    f"'{ANNOUNCE_STAGE}' recorded no PR state",
                    [],
                    "Run the PR phase again with --resume.",
                )
            )
        log = _git(self.project_dir, "log", "--oneline", f"{base_sha}..HEAD")
        if log.returncode != 0:
            return _repo_fail(log)
        state = "draft" if announced.output == "draft" else "ready for review"
        spent = ctx.usage_tracker.total_cost_usd or 0.0
        lines = [
            f"branch: {branch}",
            f"commits ({base_sha[:7]}..HEAD):",
            *[f"  {line}" for line in _lines(log.stdout)],
            f"PR: {_pr_url(pr)} ({state})",
            "gate files:",
            f"  {questions_file(self.paths)}",
            f"  {self.paths.approval}",
            f"  {self.paths.code_review}",
            f"  {self.paths.ship}",
        ]
        if notes is not None:
            if notes:
                lines.append(f"fetch notes ({self.notes_path}):")
                lines += [f"  - {note}" for note in notes]
            else:
                lines.append("fetch notes: none")
        lines.append(f"spend this process: ${spent:.2f} (the ceiling restarts on --resume)")
        text = "\n".join(lines)
        print(text)
        return _ok(text)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def add_pr_stages(
    pipeline: Pipeline,
    *,
    paths: V3Paths,
    project_dir: str,
    key: str,
    base_file: str,
    base_arg: str | None,
    overwrite_remote: str | None,
    ci_rounds: int,
    ci_timeout_minutes: int,
    budget_pipeline: Pipeline,
    auto: bool,
    subject_prefix: str | None = None,
    pipeline_name: str = PIPELINE,
    resume_arg: str | None = None,
    fetch_notes: FetchNotes | None = None,
    notes_path: str | None = None,
    start_branch_file: str | None = None,
) -> Pipeline:
    """Add stages 69-82 (``check pr provenance`` … ``summary``); every one is ``always_run``.

    *key* feeds only the resume hints, and *resume_arg* (default: *key*)
    replaces it there. *subject_prefix* (default: the file key) is what the PR
    body refresh's Reword check expects. *pipeline_name* brands the notices,
    the resume hints and the CI-fix children's notices. *fetch_notes* and
    *notes_path* (both or neither) add the fetch notes to the ending stages.
    *start_branch_file* makes ``resolve branch`` take the branch the run was
    cut from as the base and check that ``origin/<base>`` contains base.sha
    (see ``ResolveBranchV3``).
    """
    pr = pr_paths(paths)
    gate = running(pr)
    resume = key if resume_arg is None else resume_arg

    # 69-72. Provenance, the wipe, the branch and the range.
    pipeline.stage(
        "check pr provenance",
        CheckPrProvenance(
            paths=paths,
            project_dir=project_dir,
            key=resume,
            auto=auto,
            pipeline_name=pipeline_name,
        ),
        on_failure=fail,
        always_run=True,
    )
    pipeline.stage("reset pr state", ResetPrState(paths=paths), on_failure=fail, always_run=True)
    pipeline.stage(
        "resolve branch",
        ResolveBranchV3(
            paths=pr,
            project_dir=project_dir,
            base_arg=base_arg or "",
            overwrite_remote=overwrite_remote or "",
            start_branch_file=start_branch_file,
            base_file=base_file if start_branch_file is not None else None,
        ),
        on_failure=fail,
        always_run=True,
    )
    pipeline.stage(
        "check pr range",
        CheckPrRange(paths=paths, project_dir=project_dir, base_file=base_file),
        on_failure=fail,
        always_run=True,
    )

    # 73-75. Push, then the draft PR. A rejected push halts and exits 0, so
    # everything after it is gated on `running` (finding 13).
    pipeline.stage(
        "push",
        HaltingCommand(cmd=push_cmd(pr, project_dir, 1), paths=pr, stage="push"),
        always_run=True,
    )
    pipeline.stage(
        "update pr body",
        UpdatePrBody(
            paths=paths, project_dir=project_dir, publish=False, subject_prefix=subject_prefix
        ),
        on_failure=fail,
        when=gate,
        always_run=True,
    )
    pipeline.stage(
        "open draft pr",
        HaltingCommand(
            cmd=draft_pr_cmd(pr, project_dir, title_file(pr), paths.pr_body),
            paths=pr,
            stage="open draft pr",
        ),
        when=gate,
        always_run=True,
    )

    # 76-77. CI rounds. `fix ci <n>` has no stage timeout: the child runs under
    # RunCommand's own timeout, so its cost is not lost (finding 15).
    for n in range(1, ci_rounds + 1):
        wait_when = gate if n == 1 else all_of(gate, file_exists(pr.ci_red(n - 1)))
        pipeline.stage(
            f"wait for checks {n}",
            WaitForChecks(
                paths=pr,
                stage=f"wait for checks {n}",
                n=n,
                out_name=pr.checks(n),
                project_dir=project_dir,
                on_red="fix",
            ),
            when=wait_when,
            timeout=WAIT_FOR_CHECKS_TIMEOUT,
            always_run=True,
        )
        red = all_of(gate, file_exists(pr.ci_red(n)))
        pipeline.stage(
            f"fix ci {n}",
            CiFixChildV3(
                paths=pr,
                stage=f"fix ci {n}",
                n=n,
                project_dir=project_dir,
                pipeline=budget_pipeline,
                test_cmd_file=paths.test_cmd_sh,
                ci_timeout_minutes=ci_timeout_minutes,
                pipeline_name=pipeline_name,
            ),
            when=red,
            always_run=True,
        )
        # Every halt in the sync fragment ends in `exit 0`, so the appended
        # line records the CI-fix head as accepted only when the sync took it (A18).
        pipeline.stage(
            f"sync after ci fix {n}",
            HaltingCommand(
                cmd=sync_after_ci_fix_cmd(pr, project_dir, n)
                + f"git rev-parse HEAD >> {shlex.quote(paths.shipped_heads)} || exit 1\n",
                paths=pr,
                stage=f"sync after ci fix {n}",
            ),
            when=red,
            always_run=True,
        )
    pipeline.stage(
        "wait for checks final",
        WaitForChecks(
            paths=pr,
            stage="wait for checks final",
            n=ci_rounds,
            out_name=pr.checks_after_fix(ci_rounds),
            project_dir=project_dir,
            on_red="halt",
        ),
        when=all_of(gate, file_exists(pr.ci_red(ci_rounds))),
        timeout=WAIT_FOR_CHECKS_TIMEOUT,
        always_run=True,
    )

    # 78-82. Stop, or announce.
    pipeline.stage(
        "stop for human",
        StopForHuman(
            paths=paths,
            key=resume,
            auto=auto,
            pipeline_name=pipeline_name,
            fetch_notes=fetch_notes,
            notes_path=notes_path,
        ),
        on_failure=fail,
        when=halted(pr),
        always_run=True,
    )
    pipeline.stage(
        "checks green",
        ChecksGreen(paths=paths, project_dir=project_dir, base_file=base_file),
        on_failure=fail,
        when=gate,
        always_run=True,
    )
    pipeline.stage(
        "refresh pr body",
        UpdatePrBody(
            paths=paths, project_dir=project_dir, publish=True, subject_prefix=subject_prefix
        ),
        on_failure=fail,
        when=gate,
        always_run=True,
    )
    pipeline.stage(
        ANNOUNCE_STAGE,
        AnnouncePrReady(
            paths=paths,
            project_dir=project_dir,
            pipeline_name=pipeline_name,
            fetch_notes=fetch_notes,
            notes_path=notes_path,
        ),
        on_failure=fail,
        when=gate,
        always_run=True,
    )
    pipeline.stage(
        "summary",
        RunSummary(
            paths=paths,
            project_dir=project_dir,
            base_file=base_file,
            fetch_notes=fetch_notes,
            notes_path=notes_path,
        ),
        on_failure=fail,
        always_run=True,
    )
    return pipeline

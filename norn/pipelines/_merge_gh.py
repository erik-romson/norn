"""Private helper for ``merge_branch``: every ``gh`` and remote-git invocation.

Not a pipeline module — ``norn/catalog.py`` skips anything under
``norn/pipelines/`` whose name starts with ``_``.

So far this module holds the base-resolution and rebase pieces (``gh``/``git``
runners, ``GhError``, ``ResolveBranch``, ``rebase_cmd``), the lease-pinned
push (``push_cmd``), PR open/reuse plus the Copilot review request
(``pr_open_or_reuse_cmd``, ``pr_not_draft_cmd``, ``request_copilot_cmd``,
``COPILOT_LOGINS``), and the check-set wait (``WaitForChecks``, built on the
generic ``_poll_until_settled`` core that step 18's ``WatchBaseBuild`` reuses
for the post-merge build). The open PR for the branch is reused, never
re-created; the Copilot review request is a declared capability probe — a
refused ``gh pr edit --add-reviewer @copilot`` is recorded as ``unavailable``,
not a fallback, and is read back by the later review wait. Later steps grow
this module into the one home of every other gh/remote-git command the
``merge_branch`` pipeline makes (the CI-fix child, review fetch, merge) — see
the plan's Phase 5-6 sections.

Every ``gh`` call elsewhere in this module is pinned to one repository, via
``-R $REPO`` or an explicit ``repos/$REPO/...`` path, once ``ResolveBranch``
has written ``<work>/repo``.

Phase 6 (step 14) adds the two stages that follow the CI-fix hand-off:
``WaitForCopilotReview`` (waits for a Copilot review of the exact head that
will merge) and ``FetchPrReviews`` (gathers every new review/comment entry
from the three REST sources the classifier, step 15, reads), plus the pure
markdown renderer ``render_pr_reviews``.

Step 15 adds the three findings shell builders that follow the classifier's
``Generate`` stage (which lives in ``merge_branch.py``, since it is a plain
prompt, not a gh/git call): ``validate_findings_cmd`` (verdict header, clean
tree, no attribution — before anything is posted), ``post_findings_comment_cmd``
(always a new PR comment, never ``--edit-last``, carrying
``FINDINGS_MARKER_PREFIX``), and ``check_findings_cmd`` (stops the run on a
blocking verdict or a human ``CHANGES_REQUESTED``).

Step 16 (Phase 7) adds the last two per-round stages: ``CheckMergePreconditions``
(re-reads ``gh pr view``, waits out an ``UNKNOWN`` mergeability, and re-checks
the settled check/run set for drift before anything touches the base) and
``merge_cmd`` (the plain fast-forward push of the exact commit the checks ran
on — never ``gh pr merge``, ``--admin``, ``--auto``, or ``--force``).

Step 18 (Phase 8) adds the last stage, ``WatchBaseBuild``: after the merge has
landed, it watches the push-triggered build on the base branch at the merged
sha, reusing ``_poll_until_settled`` and ``_ci_estimate`` the same way
``WaitForChecks`` does. No workflow appearing within ``BASE_RUNS_GRACE`` is a
declared warning (``_merge_state.warn``), not a halt; a red or timed-out build
halts (``base_build_red``/``base_build_timeout``) since the merge already
landed and fixing the base is a new change, never handed to
``check_and_fix_ci``.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from norn.dsl import Pipeline
from norn.models import PipelineContext, StageResult
from norn.pipelines._comment_style import ATTRIBUTION_ERE
from norn.pipelines._merge_state import (
    HaltOnFailure,
    HaltOnTimeout,
    MergePaths,
    halt,
    halt_shell,
    notify,
    warn,
)
from norn.stages.base import BaseStage
from norn.stages.run_command import RunCommand

# Base-resolution candidates tried (in order) once neither ``--arg base=`` nor
# an open PR answers. Deduplicated against the actual default branch name and
# the current branch at call time.
_FORK_POINT_CANDIDATES = ("main", "master", "develop", "development")

# Real login values a Copilot code-review request shows up as, confirmed in
# the E2E run (locked decision 19). Used only to build a static gh ``-q``
# filter at Python build time — never interpolated as a runtime shell value,
# so no external ``jq`` is needed.
COPILOT_LOGINS = {
    "copilot-pull-request-reviewer[bot]",
    "copilot-pull-request-reviewer",
    "Copilot",
}

# `gh run list` event of GitHub's own agent runs (the Copilot code review,
# the Copilot coding agent). They are not the repository's CI, and a Copilot
# review that fails on quota must not read as a red build.
_AGENT_RUN_EVENT = "dynamic"


def _ci_runs(out: str) -> list[dict]:
    """Parse ``gh run list --json ...,event`` output, dropping GitHub's agent runs."""
    return [run for run in json.loads(out) if run.get("event") != _AGENT_RUN_EVENT]


# WaitForChecks constants (Phase 4). Raising one is a deliberate edit, per
# AGENTS.md's "module constants, never knobs".
HEAD_SYNC_TIMEOUT = 60
NO_CHECKS_GRACE = 300
SETTLE_WINDOW = 90

_HEAD_SYNC_POLL_INTERVAL = 5.0
_NO_CHECKS_POLL_INTERVAL = 15.0

# CiFixChild (Phase 5). Each round hands off to at most one check_and_fix_ci
# child, bounded by its own three fix attempts and by this backstop.
CI_FIX_TIMEOUT = 7200

# Floor for the child's ceiling, so a parent that has already spent its whole
# budget still starts the child and lets it report its own budget stop rather
# than handing it a zero or negative limit it cannot act on. Mirrors
# fix_jira_issue.MIN_CHILD_BUDGET.
_MIN_CHILD_BUDGET = 1.0

# WaitForCopilotReview / FetchPrReviews (Phase 6). The deadline is counted
# from the latest `copilot-requested-at`, not from when the wait stage
# starts, so the CI wait already counts toward it (review F1, Q3).
COPILOT_REVIEW_TIMEOUT = 900
_COPILOT_REVIEW_POLL_INTERVAL = 30.0

# CheckMergePreconditions / merge_cmd (Phase 7, step 16). MERGEABLE_WAIT
# bounds the wait for GitHub to finish computing mergeable/mergeStateStatus
# past UNKNOWN; PR_MERGED_WAIT bounds merge_cmd's own poll for `gh pr view`
# to report MERGED after the fast-forward push has already landed.
MERGEABLE_WAIT = 60
PR_MERGED_WAIT = 30
_MERGEABLE_POLL_INTERVAL = 10.0

# WatchBaseBuild (Phase 8, step 18). How long a push-triggered build on the
# base branch gets to appear at all before "no workflow ran" is declared a
# warning rather than a halt (Q6).
BASE_RUNS_GRACE = 120
_BASE_RUNS_POLL_INTERVAL = 15.0

# The pipeline's own marker on a posted findings comment (step 16 prepends
# `<!-- norn:merge_branch findings round <n> -->`). Matched as a prefix here
# so `FetchPrReviews` never re-classifies its own comment as a review finding.
FINDINGS_MARKER_PREFIX = "<!-- norn:merge_branch"

# A check is green when its ``bucket`` is one of these; a run is green when
# its ``conclusion`` is one of these. Kept as module constants since both the
# settlement classifier and the ``## Builds`` summary section need the exact
# same sets.
_CHECK_GREEN_BUCKETS = {"pass", "skipping"}
_RUN_GREEN_CONCLUSIONS = {"success", "skipped", "neutral"}


class GhError(Exception):
    """A ``gh`` or ``git`` invocation exited outside its allowed codes.

    Carries the full argv, the exit code, and stderr, so a caller (or
    ``HaltOnFailure`` turning it into an ``unexpected_failure`` halt) can show
    exactly what ran and how it failed.
    """

    def __init__(self, argv: list[str], rc: int | None, stderr: str) -> None:
        self.argv = argv
        self.rc = rc
        self.stderr = stderr
        super().__init__(f"$ {' '.join(argv)}\nexited {rc}: {stderr.strip()}")


async def _run(argv: list[str], *, cwd: str, env: dict[str, str] | None) -> tuple[int, str, str]:
    """Run *argv* via ``asyncio.create_subprocess_exec`` and capture output.

    Never raises on a non-zero exit — that is ``gh``'s/``git``'s job, since
    only they know which codes are "ok" for a given call.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env={**os.environ, **(env or {})},
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await proc.communicate()
    returncode = proc.returncode if proc.returncode is not None else -1
    return returncode, stdout_b.decode(errors="replace"), stderr_b.decode(errors="replace")


async def gh(
    args: list[str], *, cwd: str, env: dict[str, str] | None, ok: tuple[int, ...] = (0,)
) -> tuple[int, str, str]:
    """Run ``gh <args>`` in *cwd*. Raises ``GhError`` for an exit code outside *ok*."""
    argv = ["gh", *args]
    rc, stdout, stderr = await _run(argv, cwd=cwd, env=env)
    if rc not in ok:
        raise GhError(argv, rc, stderr)
    return rc, stdout, stderr


async def git(
    args: list[str], *, cwd: str, env: dict[str, str] | None = None, ok: tuple[int, ...] = (0,)
) -> tuple[int, str, str]:
    """Run ``git <args>`` in *cwd*. Same contract as :func:`gh`."""
    argv = ["git", *args]
    rc, stdout, stderr = await _run(argv, cwd=cwd, env=env)
    if rc not in ok:
        raise GhError(argv, rc, stderr)
    return rc, stdout, stderr


def _write(path: str, text: str) -> None:
    Path(path).write_text(f"{text}\n")


def _read(path: str) -> str:
    """Return the stripped content of *path*. No fallback: a missing file propagates."""
    return Path(path).read_text().strip()


def _exception_detail(stage: str, exc: Exception) -> str:
    """Build halt detail text from an exception a Python stage raised.

    A deliberate duplicate of ``_merge_state._format_exception_detail``
    (leading-underscore, private to that module) rather than an import across
    the module boundary — same precedent as ``_snapshot_diff.py`` /
    ``_step_snapshot.py`` staying independent copies.
    """
    tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    lines = [
        f"stage: {stage}",
        f"exception: {type(exc).__name__}: {exc}",
        "traceback (last 40 lines):",
        *tb_text.splitlines()[-40:],
    ]
    return "\n".join(lines)


class _Fail(Exception):
    """Internal control-flow sentinel for a deliberate ``ResolveBranch`` failure.

    Never escapes :meth:`ResolveBranch.run` — caught there and turned into a
    plain ``StageResult(success=False, ...)``, per the "nothing has changed
    yet" rule for every stage before ``rebase 1``.
    """


class ResolveBranch(BaseStage):
    """Resolve the base branch to rebase onto, and record the run's identity.

    Every error here is a plain ``StageResult(success=False, ...)`` — or a
    propagating ``GhError`` from an unexpected ``gh``/``git`` failure — never
    a halt: nothing has changed yet, so this stage deliberately does not carry
    ``HaltOnFailure``.
    """

    needs_agent = False

    def __init__(self, *, paths: MergePaths, project_dir: str, base_arg: str, overwrite_remote: str) -> None:
        self._paths = paths
        self._project_dir = project_dir
        self._base_arg = base_arg
        self._overwrite_remote = overwrite_remote

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        paths = self._paths
        project_dir = self._project_dir
        env = ctx.env
        Path(paths.root).mkdir(parents=True, exist_ok=True)

        try:
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
                ["pr", "list", "-R", repo, "--head", branch, "--state", "open", "--json", "number,baseRefName"],
                cwd=project_dir, env=env,
            )
            open_prs = json.loads(out)
            if len(open_prs) > 1:
                listing = "; ".join(f"#{pr['number']} (base {pr['baseRefName']})" for pr in open_prs)
                raise _Fail(f"more than one open PR for {branch}: {listing}")
            open_pr = open_prs[0] if open_prs else None

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
        except _Fail as exc:
            return StageResult(name="", success=False, error=str(exc))

        _write(paths.branch, branch)
        _write(paths.slug, slug)
        _write(paths.repo, repo)
        _write(paths.base, base)
        _write(paths.base_source, source)
        _write(paths.default_branch, default_branch)
        _write(paths.start_head, start_head)
        _write(paths.remote_head, remote_head)

        message = f"base: {base} ({source})"
        if note:
            message = f"{message}\n{note}"
        print(message)

        return StageResult(
            name="",
            success=True,
            output={"base": base, "source": source, "branch": branch, "repo": repo, "note": note},
        )

    # ------------------------------------------------------------------
    # Base resolution
    # ------------------------------------------------------------------

    async def _resolve_base(
        self,
        *,
        branch: str,
        project_dir: str,
        env: dict[str, str] | None,
        default_branch: str,
        open_pr: dict[str, Any] | None,
    ) -> tuple[str, str]:
        """Return ``(base, source)`` per the four resolution levels (see step doc)."""
        base_arg = self._base_arg
        if base_arg:
            rc, _, _ = await git(
                ["rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{base_arg}"],
                cwd=project_dir, env=env, ok=(0, 1),
            )
            if rc != 0:
                raise _Fail(f"--arg base={base_arg}: origin/{base_arg} does not exist")
            if open_pr and open_pr["baseRefName"] != base_arg:
                raise _Fail(
                    f"--arg base={base_arg} conflicts with open PR #{open_pr['number']}, "
                    f"whose base is {open_pr['baseRefName']}"
                )
            return base_arg, "--arg base"

        if open_pr:
            return open_pr["baseRefName"], "open PR"

        candidates: list[str] = []
        seen: set[str] = set()
        for name in (default_branch, *_FORK_POINT_CANDIDATES):
            if name and name != branch and name not in seen:
                seen.add(name)
                candidates.append(name)

        qualified: list[tuple[str, str, int]] = []
        for candidate in candidates:
            rc, _, _ = await git(
                ["rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{candidate}"],
                cwd=project_dir, env=env, ok=(0, 1),
            )
            if rc != 0:
                continue
            rc, mb_out, _ = await git(
                ["merge-base", "HEAD", f"origin/{candidate}"], cwd=project_dir, env=env, ok=(0, 1)
            )
            merge_base = mb_out.strip()
            if rc != 0 or not merge_base:
                continue
            _, count_out, _ = await git(
                ["rev-list", "--count", f"{merge_base}..HEAD"], cwd=project_dir, env=env
            )
            qualified.append((candidate, merge_base, int(count_out.strip())))

        if not qualified:
            return default_branch, "default branch"

        min_distance = min(distance for _, _, distance in qualified)
        tied = [q for q in qualified if q[2] == min_distance]
        if len(tied) == 1:
            return tied[0][0], "fork point"

        merge_bases = {merge_base for _, merge_base, _ in tied}
        names = {name for name, _, _ in tied}
        if len(merge_bases) == 1 and default_branch in names:
            return default_branch, "ambiguous"

        listing = ", ".join(name for name, _, _ in tied)
        raise _Fail(
            f"ambiguous base: {listing} all at distance {min_distance} commit(s) from HEAD "
            "— pass --arg base=<name>"
        )

    # ------------------------------------------------------------------
    # Remote-branch guard (review F3): never silently force-push away a
    # remote commit with no patch-equivalent on the local side.
    # ------------------------------------------------------------------

    async def _check_remote_branch(
        self, *, branch: str, project_dir: str, env: dict[str, str] | None
    ) -> str | None:
        rc, _, _ = await git(
            ["rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"],
            cwd=project_dir, env=env, ok=(0, 1),
        )
        if rc != 0:
            return None  # branch has never been pushed

        rc, _, _ = await git(
            ["merge-base", "--is-ancestor", f"origin/{branch}", "HEAD"],
            cwd=project_dir, env=env, ok=(0, 1),
        )
        if rc == 0:
            return None  # origin/<branch> is already behind (or at) HEAD

        _, out, _ = await git(["rev-parse", f"origin/{branch}"], cwd=project_dir, env=env)
        remote_sha = out.strip()

        _, out, _ = await git(
            ["log", "--cherry-pick", "--right-only", "--format=%h %an %s", f"HEAD...origin/{branch}"],
            cwd=project_dir, env=env,
        )
        unmatched = [line for line in out.splitlines() if line.strip()]

        overwrite = self._overwrite_remote
        if unmatched:
            if overwrite and overwrite == remote_sha:
                listing = "\n".join(unmatched)
                return (
                    f"overwriting {len(unmatched)} unmatched commit(s) on "
                    f"origin/{branch} ({remote_sha}):\n{listing}"
                )
            if overwrite:
                raise _Fail(
                    f"--arg overwrite_remote={overwrite} does not match the current "
                    f"origin/{branch} ({remote_sha})"
                )
            listing = "\n".join(unmatched)
            raise _Fail(
                f"origin/{branch} has commit(s) with no patch-equivalent on HEAD "
                f"(would be force-pushed away):\n{listing}\n"
                f"To overwrite them, rerun with --arg overwrite_remote={remote_sha}"
            )

        if overwrite:
            return f"--arg overwrite_remote={overwrite} was not used: origin/{branch} has no unmatched commit(s)"

        return f"replaced remote {remote_sha[:12]} (patch-equivalent, no commits lost)"


def push_cmd(paths: MergePaths, project_dir: str, n: int) -> str:
    """Return the POSIX ``sh`` fragment for round *n*'s lease-pinned ``push n``.

    The lease is pinned to ``remote-head`` (written by ``resolve branch``, and
    by this fragment on every successful push), never a plain
    ``--force-with-lease``: the rebase's own ``git fetch`` just refreshed the
    tracking ref, so an unpinned lease would protect nothing. An empty
    ``remote-head`` means "the branch must not exist on origin yet" — git's
    own documented meaning for an empty ``--force-with-lease`` expected value.
    Also sets the upstream, so ``@{u}`` resolves for later stages (the
    ``check_and_fix_ci`` child compares against it).
    """
    push_log = shlex.quote(paths.push_log(n))
    branch_path = shlex.quote(paths.branch)
    remote_head_path = shlex.quote(paths.remote_head)
    pushed_head_path = shlex.quote(paths.pushed_head)
    halt_fragment = halt_shell(paths, "push_rejected", f"cat {push_log}")
    return (
        f"cd {shlex.quote(project_dir)} || exit 1\n"
        f"B=$(cat {branch_path}) || exit 1\n"
        f"EXP=$(cat {remote_head_path}) || exit 1\n"
        f'if git push --set-upstream --force-with-lease="refs/heads/$B:$EXP" '
        f'origin "HEAD:refs/heads/$B" > {push_log} 2>&1; then\n'
        f"  git rev-parse HEAD > {remote_head_path} && git rev-parse HEAD > {pushed_head_path}\n"
        "else\n"
        f"{halt_fragment}"
        "fi\n"
    )


def rebase_cmd(paths: MergePaths, project_dir: str, n: int) -> str:
    """Return the POSIX ``sh`` fragment for round *n*'s rebase onto the base.

    Never passes ``--autostash``: ``preflight`` already proved the working
    tree is clean before the round started.
    """
    conflicts_path = paths.rebase_conflicts(n)
    halt_fragment = halt_shell(paths, "rebase_conflict", f"cat {shlex.quote(conflicts_path)}")
    return (
        f"cd {shlex.quote(project_dir)} || exit 1\n"
        "git fetch origin --prune || exit 1\n"
        f"BASE=$(cat {shlex.quote(paths.base)})\n"
        'if ! git rebase "origin/$BASE"; then\n'
        f"  git diff --name-only --diff-filter=U > {shlex.quote(conflicts_path)}\n"
        "  git rebase --abort\n"
        f"{halt_fragment}"
        "fi\n"
    )


def pr_open_or_reuse_cmd(paths: MergePaths, project_dir: str) -> str:
    """Return the POSIX sh fragment for round *n*'s ``open or reuse pr {n}`` stage.

    Finds the branch's own open PR via ``gh pr list --head``; an empty result
    creates one with ``gh pr create --fill`` (title and body come from the
    human's own commits — no assistant text ever enters them) and re-lists to
    learn its number. A reused PR's ``baseRefName`` is compared against
    ``<work>/base`` — reachable as a mismatch only if someone retargeted the
    PR mid-run, since ``resolve branch`` already required agreement — and
    halts ``pr_base_mismatch`` rather than silently proceeding against the
    wrong base. Writes ``pr`` (the number) and ``pr.json`` (the fields later
    stages need: number, url, isDraft, headRefOid) on both paths. Not
    per-round: ``pr``/``pr.json``/``pr-origin`` are fixed files (the same PR
    persists across a round 2 re-rebase and re-push).
    """
    repo_path = shlex.quote(paths.repo)
    branch_path = shlex.quote(paths.branch)
    base_path = shlex.quote(paths.base)
    pr_origin_path = shlex.quote(paths.pr_origin)
    pr_path = shlex.quote(paths.pr)
    pr_json_path = shlex.quote(paths.pr_json)
    mismatch_halt = halt_shell(
        paths,
        "pr_base_mismatch",
        'printf \'PR #%s base is %s, expected %s\\n\' "$NUM" "$BASE_REF" "$BASE"',
    )
    return (
        f"cd {shlex.quote(project_dir)} || exit 1\n"
        f"REPO=$(cat {repo_path}) || exit 1\n"
        f"BRANCH=$(cat {branch_path}) || exit 1\n"
        f"BASE=$(cat {base_path}) || exit 1\n"
        'NUM=$(gh pr list -R "$REPO" --head "$BRANCH" --state open --json number '
        "-q '.[0].number') || exit 1\n"
        'if [ -z "$NUM" ]; then\n'
        '  gh pr create -R "$REPO" --base "$BASE" --head "$BRANCH" --fill || exit 1\n'
        '  NUM=$(gh pr list -R "$REPO" --head "$BRANCH" --state open --json number '
        "-q '.[0].number') || exit 1\n"
        f"  printf 'created\\n' > {pr_origin_path}\n"
        "else\n"
        '  BASE_REF=$(gh pr view "$NUM" -R "$REPO" --json baseRefName -q .baseRefName) || exit 1\n'
        '  if [ "$BASE_REF" != "$BASE" ]; then\n'
        f"{mismatch_halt}"
        "  fi\n"
        f"  printf 'reused\\n' > {pr_origin_path}\n"
        "fi\n"
        f'printf \'%s\\n\' "$NUM" > {pr_path}\n'
        f'gh pr view "$NUM" -R "$REPO" --json number,url,isDraft,headRefOid > {pr_json_path} || exit 1\n'
    )


def pr_not_draft_cmd(paths: MergePaths, project_dir: str) -> str:
    """Return the POSIX sh fragment for round *n*'s ``pr is not draft {n}`` stage.

    Halts ``pr_draft`` when the PR is a draft. Never undrafts it — that is a
    human decision this pipeline does not make on someone's behalf.
    """
    repo_path = shlex.quote(paths.repo)
    pr_path = shlex.quote(paths.pr)
    draft_halt = halt_shell(paths, "pr_draft", f"cat {shlex.quote(paths.pr_json)}")
    return (
        f"cd {shlex.quote(project_dir)} || exit 1\n"
        f"REPO=$(cat {repo_path}) || exit 1\n"
        f"NUM=$(cat {pr_path}) || exit 1\n"
        'IS_DRAFT=$(gh pr view "$NUM" -R "$REPO" --json isDraft -q .isDraft) || exit 1\n'
        'if [ "$IS_DRAFT" = "true" ]; then\n'
        f"{draft_halt}"
        "fi\n"
    )


def _copilot_commit_filter() -> str:
    """Return the gh ``-q`` expression selecting commit ids reviewed by a Copilot login.

    Built once, at Python build time, from the static :data:`COPILOT_LOGINS`
    set — never a runtime shell value — so no external ``jq`` binary is
    needed, only gh's own built-in ``-q``. ``json.dumps`` gives each login a
    valid (gh-``-q``-compatible) double-quoted string literal. gh applies it
    to each ``--paginate`` page on its own (``--slurp`` rejects ``-q``).
    """
    logins = " or ".join(f".user.login == {json.dumps(login)}" for login in sorted(COPILOT_LOGINS))
    return f".[] | select({logins}) | .commit_id"


def request_copilot_cmd(paths: MergePaths, project_dir: str, *, rerequest: bool) -> str:
    """Return the POSIX sh fragment requesting Copilot as a PR reviewer.

    With *rerequest* ``False`` (this step's ``request copilot review {n}``):
    idempotent across reruns and rounds — when a review from a login in
    ``COPILOT_LOGINS`` already covers ``pushed-head`` exactly, this is a
    no-op (no ``pr edit`` call) and writes ``already-reviewed``. Otherwise it
    asks via ``gh pr edit --add-reviewer @copilot`` — a declared capability
    probe (AGENTS.md Principles): the one place a failed command is
    tolerated, never a fallback. A refusal is recorded as ``unavailable`` and
    the stage still exits 0.

    With *rerequest* ``True`` (step 13's ``re-request copilot review {n}``,
    review F1): no ``gh api`` reviews query — the decision is made entirely
    from local state, since ``pushed-head``/``copilot-reviewer`` already say
    everything this needs to know. When ``copilot-reviewer`` is
    ``unavailable`` (Copilot refused earlier — asking again repeats that
    answer) or ``pushed-head`` already equals ``copilot-requested-head``
    (nothing new since the last request), it prints why and exits 0 with no
    ``gh`` call at all. Otherwise it asks the same way, and a refusal halts
    ``copilot_rerequest_failed`` instead of degrading to ``unavailable`` —
    by then a review is already outstanding, and a silent ``unavailable``
    would leave the later review wait with nothing to wait for.

    ``copilot-requested-head`` is written from ``pushed-head`` on every
    outcome except ``unavailable``/the short-circuits above — the head
    Copilot was last asked about.
    """
    repo_path = shlex.quote(paths.repo)
    pr_path = shlex.quote(paths.pr)
    pushed_head_path = shlex.quote(paths.pushed_head)
    reviewer_path = shlex.quote(paths.copilot_reviewer)
    reviewer_log = shlex.quote(paths.copilot_reviewer_log)
    requested_at_path = shlex.quote(paths.copilot_requested_at)
    requested_head_path = shlex.quote(paths.copilot_requested_head)

    if rerequest:
        failure_branch = halt_shell(paths, "copilot_rerequest_failed", f"cat {reviewer_log}")
        return (
            f"cd {shlex.quote(project_dir)} || exit 1\n"
            f"REPO=$(cat {repo_path}) || exit 1\n"
            f"NUM=$(cat {pr_path}) || exit 1\n"
            f'if [ -f {reviewer_path} ] && [ "$(cat {reviewer_path})" = "unavailable" ]; then\n'
            '  echo "copilot review is unavailable for this PR; not re-requesting" >&2\n'
            "  exit 0\n"
            "fi\n"
            f'if [ -f {requested_head_path} ] && '
            f'[ "$(cat {pushed_head_path})" = "$(cat {requested_head_path})" ]; then\n'
            '  echo "copilot was already asked about the current head; not re-requesting" >&2\n'
            "  exit 0\n"
            "fi\n"
            f'if gh pr edit "$NUM" -R "$REPO" --add-reviewer @copilot 2>> {reviewer_log}; then\n'
            f"  printf 'requested\\n' > {reviewer_path}\n"
            f"  date -u +%Y-%m-%dT%H:%M:%SZ > {requested_at_path}\n"
            f"  cp {pushed_head_path} {requested_head_path}\n"
            "else\n"
            f"{failure_branch}"
            "fi\n"
        )

    commit_filter = shlex.quote(_copilot_commit_filter())
    failure_branch = (
        f"    printf 'unavailable\\n' > {reviewer_path}\n"
        f'    echo "copilot review request failed — see {paths.copilot_reviewer_log}" >&2\n'
    )

    return (
        f"cd {shlex.quote(project_dir)} || exit 1\n"
        f"REPO=$(cat {repo_path}) || exit 1\n"
        f"NUM=$(cat {pr_path}) || exit 1\n"
        f"PUSHED_HEAD=$(cat {pushed_head_path}) || exit 1\n"
        f'REVIEWED_COMMITS=$(gh api "repos/$REPO/pulls/$NUM/reviews" --paginate '
        f"-q {commit_filter}) || exit 1\n"
        'if printf \'%s\\n\' "$REVIEWED_COMMITS" | grep -qxF "$PUSHED_HEAD"; then\n'
        f"  printf 'already-reviewed\\n' > {reviewer_path}\n"
        "else\n"
        f'  if gh pr edit "$NUM" -R "$REPO" --add-reviewer @copilot 2>> {reviewer_log}; then\n'
        f"    printf 'requested\\n' > {reviewer_path}\n"
        f"    date -u +%Y-%m-%dT%H:%M:%SZ > {requested_at_path}\n"
        "  else\n"
        f"{failure_branch}"
        "  fi\n"
        "fi\n"
        f'if [ "$(cat {reviewer_path})" != "unavailable" ]; then\n'
        f"  cp {pushed_head_path} {requested_head_path}\n"
        "fi\n"
        "exit 0\n"
    )


# ---------------------------------------------------------------------------
# WaitForChecks — wait until the PR's whole check set has settled (Phase 4)
# ---------------------------------------------------------------------------


def _parse_iso(timestamp: str) -> datetime:
    """Parse a gh-CLI timestamp (ISO-8601 with a trailing ``Z``)."""
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


async def _poll_until_settled(
    *,
    fetch: Callable[[], Awaitable[Any]],
    is_settled: Callable[[Any], bool],
    stability_key: Callable[[Any], Any],
    head_moved: Callable[[Any], bool] | None,
    clock: Callable[[], float],
    sleep: Callable[[float], Awaitable[None]],
    interval: float,
    timeout: float,
    settle_window: float,
    pre_sleep: float = 0.0,
) -> tuple[str, Any]:
    """Poll ``fetch`` until it settles, times out, or reports a moved head.

    The generic polling core shared by ``WaitForChecks`` here and step 18's
    ``WatchBaseBuild``. "Settled" means ``is_settled(state)`` holds AND
    ``stability_key(state)`` has stayed equal for at least *settle_window*
    seconds of polling — any change to the key restarts the window, so e.g. a
    check appearing partway through resets the clock for the whole check-name
    set, not just the new entry. Sleeps *pre_sleep* once, before the first
    fetch, when positive. Returns ``("settled" | "timeout" | "head_moved",
    last_state)`` — never raises for any of those three outcomes; a *fetch*
    that raises propagates unchanged (an unexpected ``gh`` failure is the
    caller's ``HaltOnFailure`` mixin's job, not this function's).
    """
    if pre_sleep > 0:
        await sleep(pre_sleep)

    deadline = clock() + timeout
    state = await fetch()
    if head_moved is not None and head_moved(state):
        return "head_moved", state
    key = stability_key(state)
    stable_since = clock()

    while True:
        if is_settled(state) and (clock() - stable_since) >= settle_window:
            return "settled", state
        if clock() >= deadline:
            return "timeout", state
        await sleep(interval)
        state = await fetch()
        if head_moved is not None and head_moved(state):
            return "head_moved", state
        new_key = stability_key(state)
        if new_key != key:
            key = new_key
            stable_since = clock()


def _classify_checks(checks: list[dict], runs: list[dict]) -> tuple[str, int, list[str]]:
    """Return ``(outcome, passed_count, failing_names)`` for a settled check/run set.

    A check is green when its ``bucket`` is ``pass``/``skipping``; a run is
    green when its ``conclusion`` is ``success``/``skipped``/``neutral`` —
    runs are classified in addition to checks so that a completed run with no
    check of its own still turns the outcome red (review F4). A run whose
    workflow is already represented among ``checks`` by a *failing* check is
    skipped here: it and the matching check are two views of the same
    failure, not two separate ones (review round 2 finding 2). A run whose
    matching check is green, though, is a real, independent failure
    (stale/partial ``gh pr checks`` data, or a job-level check that doesn't
    reflect the whole run) and must not be silently dropped (review round 3
    finding 1 — mirrors the same fix in ``_fixable_workflow_names`` below).
    """
    passed = 0
    failing: list[str] = []
    known_workflows = {c.get("workflow") for c in checks if c.get("bucket") == "fail" and c.get("workflow")}
    for check in checks:
        if check.get("bucket") in _CHECK_GREEN_BUCKETS:
            passed += 1
        else:
            failing.append(check.get("name") or check.get("workflow") or "(unnamed check)")
    for run in runs:
        if run.get("workflowName") in known_workflows:
            continue
        if run.get("conclusion") in _RUN_GREEN_CONCLUSIONS:
            passed += 1
        else:
            failing.append(run.get("workflowName") or f"run {run.get('databaseId')}")
    outcome = "green" if not failing else "red"
    return outcome, passed, failing


def _checks_data(
    outcome: str,
    *,
    head: str | None,
    checks: list[dict],
    runs: list[dict],
    estimate: float | None,
    per_workflow: dict[str, float],
    plan: Any,
) -> dict[str, Any]:
    """Build the JSON body written to ``checks-<n>.json`` / ``checks-<n>-after-fix.json``."""
    return {
        "outcome": outcome,
        "head": head,
        "estimate": estimate,
        "per_workflow_estimate": per_workflow,
        "plan": (
            {"pre_sleep": plan.pre_sleep, "interval": plan.interval, "timeout": plan.timeout}
            if plan is not None
            else None
        ),
        "checks": checks,
        "check_names": sorted({c.get("name") for c in checks if c.get("name")}),
        "runs": runs,
    }


def _fixable_workflow_names(checks: list[dict], runs: list[dict]) -> list[str] | None:
    """Return failing workflow names (check order, deduplicated) fixable by a
    ``check_and_fix_ci`` child, or ``None`` when something can't be routed
    automatically.

    Unroutable: an orphan non-green *run* (review F4 — a run with no
    matching ``checks`` entry of its own carries a ``conclusion`` but no
    ``bucket`` to classify), a failing check in a bucket other than ``fail``
    (e.g. ``cancel`` — a cancelled run needs a human to say why), or a
    failing check with no ``workflow`` (an external app's status, which
    ``check_and_fix_ci`` cannot read logs for). A non-green run whose
    workflow is already represented in ``checks`` by a *failing* check is not
    independently unroutable — it and the matching check are two views of
    the same failure, not two separate ones. A non-green run whose matching
    check is green, though, is a real, independent failure (stale/partial
    ``gh pr checks`` data, or a job-level check that doesn't reflect the
    whole run) and must not be silently dropped (review round 2 finding 3).
    """
    known_workflows = {c.get("workflow") for c in checks if c.get("bucket") == "fail" and c.get("workflow")}
    if any(
        r.get("conclusion") not in _RUN_GREEN_CONCLUSIONS
        and r.get("workflowName") not in known_workflows
        for r in runs
    ):
        return None
    names: list[str] = []
    seen: set[str] = set()
    for check in checks:
        if check.get("bucket") in _CHECK_GREEN_BUCKETS:
            continue
        if check.get("bucket") != "fail" or not check.get("workflow"):
            return None
        workflow = check["workflow"]
        if workflow not in seen:
            seen.add(workflow)
            names.append(workflow)
    return names or None


class WaitForChecks(HaltOnFailure, BaseStage):
    """Wait for a PR's whole check set to settle, then classify the outcome.

    Every outcome other than an unexpected ``gh`` failure returns
    ``StageResult(success=True)`` — red, timed-out, absent, or moved-head are
    data this stage classifies and halts on itself; the default
    ``on_failure=FAIL`` would otherwise raise before any halt gets a chance to
    act (see the step doc).

    ``on_red="halt"`` (phase 4's behaviour) halts ``checks_red`` on any
    non-green outcome. ``on_red="fix"`` (phase 5, used by ``wait for checks
    {n}``) instead hands off to a ``check_and_fix_ci`` child when every
    non-green check is in bucket ``fail`` and has a ``workflow`` that maps to
    exactly one file in ``gh workflow list``: it writes
    ``ci-fix-workflows-<n>``, touches ``CI-RED-<n>``, and returns without
    halting. A ``cancel`` bucket, a failing check with no ``workflow`` (an
    external app's status), or a red *run* with no check of its own (review
    F4 — no bucket to classify) still halts ``checks_red`` even under
    ``on_red="fix"``, since ``check_and_fix_ci`` only knows how to read GitHub
    Actions logs. ``wait for checks after ci fix {n}`` uses ``on_red="halt"``
    — there is one child per round, so that second wait never hands off
    again.

    ``sleep``/``clock`` are injectable (defaulting to real ``asyncio.sleep``/
    ``time.monotonic``) so tests never wait for real time. The polling core
    lives in :func:`_poll_until_settled`.
    """

    needs_agent = False

    def __init__(
        self,
        *,
        n: int,
        out_name: str,
        project_dir: str,
        on_red: Literal["fix", "halt"] = "halt",
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        **kwargs: Any,
    ) -> None:
        self._n = n
        self._out_name = out_name
        self._project_dir = project_dir
        self._on_red = on_red
        self._sleep = sleep
        self._clock = clock
        super().__init__(**kwargs)

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        try:
            return await self._wait(ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._pass_through():
                raise
            halt(self._halt_paths, "unexpected_failure", _exception_detail(self._halt_stage, exc))
            return StageResult(name="", success=True)

    # -- gh calls -----------------------------------------------------

    async def _current_head(
        self, repo: str, pr_num: str, project_dir: str, env: dict[str, str] | None
    ) -> str:
        _, out, _ = await gh(
            ["pr", "view", pr_num, "-R", repo, "--json", "headRefOid"], cwd=project_dir, env=env
        )
        return json.loads(out)["headRefOid"]

    async def _poll_checks(
        self, repo: str, pr_num: str, project_dir: str, env: dict[str, str] | None
    ) -> list[dict] | None:
        """One ``gh pr checks`` call. ``None`` means "no checks reported yet"."""
        argv = [
            "pr", "checks", pr_num, "-R", repo, "--json",
            "name,workflow,state,bucket,link,startedAt,completedAt",
        ]
        rc, out, err = await gh(argv, cwd=project_dir, env=env, ok=(0, 1, 8))
        if rc == 1 and "no checks reported" in err.lower():
            return None
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            raise GhError(["gh", *argv], rc, err)

    async def _run_list(
        self, repo: str, head: str, project_dir: str, env: dict[str, str] | None
    ) -> list[dict]:
        _, out, _ = await gh(
            [
                "run", "list", "-R", repo, "-c", head, "-L", "100", "--json",
                "databaseId,workflowName,status,conclusion,url,event",
            ],
            cwd=project_dir, env=env,
        )
        return _ci_runs(out)

    # -- phases ---------------------------------------------------------

    async def _sync_head(
        self, repo: str, pr_num: str, pushed_head: str, project_dir: str, env: dict[str, str] | None
    ) -> str | None:
        deadline = self._clock() + HEAD_SYNC_TIMEOUT
        while True:
            head = await self._current_head(repo, pr_num, project_dir, env)
            if head == pushed_head:
                return head
            if self._clock() >= deadline:
                return None
            await self._sleep(_HEAD_SYNC_POLL_INTERVAL)

    async def _await_appearance(
        self, repo: str, pr_num: str, project_dir: str, env: dict[str, str] | None
    ) -> list[dict] | None:
        deadline = self._clock() + NO_CHECKS_GRACE
        while True:
            checks = await self._poll_checks(repo, pr_num, project_dir, env)
            if checks is not None:
                return checks
            if self._clock() >= deadline:
                return None
            await self._sleep(_NO_CHECKS_POLL_INTERVAL)

    def _elapsed_since_earliest(self, checks: list[dict]) -> float:
        started = [c["startedAt"] for c in checks if c.get("startedAt")]
        if not started:
            return 0.0
        earliest = min(_parse_iso(s) for s in started)
        return max(0.0, (datetime.now(timezone.utc) - earliest).total_seconds())

    # -- orchestration ----------------------------------------------------

    async def _wait(self, ctx: PipelineContext) -> StageResult:
        paths = self._halt_paths
        project_dir = self._project_dir
        env = ctx.env
        repo = _read(paths.repo)
        pr_num = _read(paths.pr)
        pushed_head = _read(paths.pushed_head)
        branch = _read(paths.branch)
        base = _read(paths.base)
        started_at = self._clock()

        head = await self._sync_head(repo, pr_num, pushed_head, project_dir, env)
        if head is None:
            return await self._conclude(
                "pr_head_moved", head=None, checks=[], runs=[], estimate=None,
                per_workflow={}, plan=None, started_at=started_at,
                detail="the PR head moved before any check was observed",
            )

        checks = await self._await_appearance(repo, pr_num, project_dir, env)
        if checks is None:
            return await self._conclude(
                "checks_none", head=head, checks=[], runs=[], estimate=None,
                per_workflow={}, plan=None, started_at=started_at,
                detail=f"no checks were reported on PR #{pr_num} within {NO_CHECKS_GRACE}s",
            )

        # Local import: _ci_estimate imports `gh` from this module at its own
        # top level, so importing it back here at module load time would be a
        # circular import whose success depends on which of the two modules a
        # caller happens to import first. A call-time import has no such
        # ordering dependency — both modules are always fully loaded by the
        # time this method actually runs.
        from norn.pipelines._ci_estimate import estimate_seconds, poll_plan

        workflows = sorted({c["workflow"] for c in checks if c.get("workflow")})
        estimate, per_workflow = await estimate_seconds(
            repo, workflows, branch, base, clock=self._clock, cwd=project_dir, env=env
        )
        elapsed = self._elapsed_since_earliest(checks)
        plan = poll_plan(estimate, elapsed)
        print(
            f"wait for checks {self._n}: workflows={workflows} estimates={per_workflow} plan={plan}",
            file=sys.stderr,
        )
        has_pending = any(c.get("bucket") == "pending" for c in checks)

        async def _fetch() -> dict[str, Any]:
            current_checks = await self._poll_checks(repo, pr_num, project_dir, env)
            if current_checks is None:
                raise GhError(
                    ["gh", "pr", "checks", pr_num], 1, "checks disappeared after appearing"
                )
            current_head = await self._current_head(repo, pr_num, project_dir, env)
            current_runs = await self._run_list(repo, current_head, project_dir, env)
            return {"checks": current_checks, "runs": current_runs, "head": current_head}

        def _is_settled(state: dict[str, Any]) -> bool:
            no_pending = not any(c.get("bucket") == "pending" for c in state["checks"])
            runs_completed = all(r.get("status") == "completed" for r in state["runs"])
            return no_pending and runs_completed

        def _stability_key(state: dict[str, Any]) -> Any:
            return frozenset((c.get("name"), c.get("bucket")) for c in state["checks"])

        def _head_moved(state: dict[str, Any]) -> bool:
            return state["head"] != head

        outcome, state = await _poll_until_settled(
            fetch=_fetch,
            is_settled=_is_settled,
            stability_key=_stability_key,
            head_moved=_head_moved,
            clock=self._clock,
            sleep=self._sleep,
            interval=plan.interval,
            timeout=plan.timeout,
            settle_window=SETTLE_WINDOW,
            pre_sleep=plan.pre_sleep if has_pending else 0.0,
        )

        if outcome == "head_moved":
            return await self._conclude(
                "pr_head_moved", head=state["head"], checks=state["checks"], runs=state["runs"],
                estimate=estimate, per_workflow=per_workflow, plan=plan, started_at=started_at,
                detail="the PR head moved during the check wait",
            )
        if outcome == "timeout":
            return await self._conclude(
                "checks_timeout", head=head, checks=state["checks"], runs=state["runs"],
                estimate=estimate, per_workflow=per_workflow, plan=plan, started_at=started_at,
                detail=f"checks did not settle within {plan.timeout:.0f}s",
            )

        # `_classify_checks` returns the bare classification ("green"/"red");
        # "red" is not itself a halt kind (see HALTS), so it maps to
        # "checks_red" here.
        classification, _, failing = _classify_checks(state["checks"], state["runs"])

        if classification != "green" and self._on_red == "fix":
            names = _fixable_workflow_names(state["checks"], state["runs"])
            files = await self._resolve_workflow_files(repo, names, project_dir, env) if names else None
            if files:
                return await self._hand_off_to_fix(
                    files, head=head, checks=state["checks"], runs=state["runs"],
                    estimate=estimate, per_workflow=per_workflow, plan=plan, failing=failing,
                )

        kind = "green" if classification == "green" else "checks_red"
        if failing:
            links = {c.get("name"): c.get("link") for c in state["checks"] if c.get("link")}
            detail = "; ".join(
                f"{name} ({links[name]})" if links.get(name) else name for name in failing
            )
        else:
            detail = "(none)"
        return await self._conclude(
            kind, head=head, checks=state["checks"], runs=state["runs"],
            estimate=estimate, per_workflow=per_workflow, plan=plan, started_at=started_at,
            detail=detail,
        )

    async def _conclude(
        self,
        outcome: str,
        *,
        head: str | None,
        checks: list[dict],
        runs: list[dict],
        estimate: float | None,
        per_workflow: dict[str, float],
        plan: Any,
        started_at: float,
        detail: str,
    ) -> StageResult:
        """Write ``<out_name>``, notify (green/checks_red/checks_timeout only), then halt or pass.

        ``green`` is the only outcome that returns without halting — it also
        writes ``green-head``. Every other outcome halts under its own kind
        (never a bare failure) but still returns ``success=True`` — the mixin
        contract every ``HaltOnFailure`` stage follows.
        """
        paths = self._halt_paths
        pr_num = _read(paths.pr)
        data = _checks_data(
            outcome, head=head, checks=checks, runs=runs, estimate=estimate, per_workflow=per_workflow, plan=plan
        )
        Path(self._out_name).write_text(json.dumps(data, indent=2))

        if outcome in ("green", "checks_red", "checks_timeout"):
            _, passed, failing = _classify_checks(checks, runs)
            minutes = max(0, round((self._clock() - started_at) / 60))
            failing_desc = f" ({', '.join(failing)})" if failing else ""
            await notify(
                "PR checks finished",
                f"PR #{pr_num}: {passed} passed, {len(failing)} failed{failing_desc} in {minutes}m",
            )

        if outcome == "green":
            Path(paths.green_head).write_text(f"{head}\n")
            return StageResult(name="", success=True, output=data)

        halt(paths, outcome, detail)
        return StageResult(name="", success=True, output=data)

    # -- on_red="fix" hand-off -------------------------------------------

    async def _resolve_workflow_files(
        self, repo: str, names: list[str], project_dir: str, env: dict[str, str] | None
    ) -> list[str] | None:
        """Map failing check *names* (workflow display names) to workflow file
        basenames via ``gh workflow list``, or ``None`` when a name maps to
        zero or more than one file — not safe to route automatically.
        """
        _, out, _ = await gh(["workflow", "list", "-R", repo, "--json", "name,path"], cwd=project_dir, env=env)
        by_name: dict[str, list[str]] = {}
        for workflow in json.loads(out):
            by_name.setdefault(workflow.get("name"), []).append(workflow.get("path", ""))

        files: list[str] = []
        seen: set[str] = set()
        for name in names:
            candidates = by_name.get(name, [])
            if len(candidates) != 1:
                return None
            basename = candidates[0].rsplit("/", 1)[-1]
            if basename not in seen:
                seen.add(basename)
                files.append(basename)
        return files

    async def _hand_off_to_fix(
        self,
        files: list[str],
        *,
        head: str,
        checks: list[dict],
        runs: list[dict],
        estimate: float | None,
        per_workflow: dict[str, float],
        plan: Any,
        failing: list[str],
    ) -> StageResult:
        """Write ``ci-fix-workflows-<n>``, touch ``CI-RED-<n>``, and return
        without halting — the four ``fix ci {n}`` ... ``wait for checks after
        ci fix {n}`` stages, gated on ``CI-RED-<n>``, take it from here.
        """
        paths = self._halt_paths
        Path(paths.ci_fix_workflows(self._n)).write_text("\n".join(files) + "\n")
        Path(paths.ci_red(self._n)).touch()

        data = _checks_data(
            "ci_fix", head=head, checks=checks, runs=runs, estimate=estimate, per_workflow=per_workflow, plan=plan
        )
        Path(self._out_name).write_text(json.dumps(data, indent=2))

        await notify(
            "PR checks finished",
            f"{len(failing)} failed — handing to check_and_fix_ci",
        )
        return StageResult(name="", success=True, output=data)


# ---------------------------------------------------------------------------
# CiFixChild — run check_and_fix_ci as a child process (Phase 5)
# ---------------------------------------------------------------------------


def ci_fix_child_cmd(project_dir: str, workflows: list[str], test_cmd: str, budget: float) -> str:
    """Return the POSIX sh fragment that runs one ``check_and_fix_ci`` child.

    ``--budget`` (not ``--arg budget=``): ``check_and_fix_ci`` declares no
    budget of its own, so ``dsl.apply_budget_overrides`` gives it one from the
    flag.
    """
    return (
        f"cd {shlex.quote(project_dir)} && "
        f"{shlex.quote(sys.executable)} -m norn run check_and_fix_ci_v1 --non-interactive "
        f"--arg workflow={shlex.quote(','.join(workflows))} "
        f"--arg test_cmd={shlex.quote(test_cmd)} --budget {budget}"
    )


class CiFixChild(HaltOnTimeout):
    """Run ``check_and_fix_ci`` as a child norn run to fix the round's red PR checks.

    The child-run shape ``fix_jira_issue`` already uses for
    ``implement_features_v2``, and for the same reason: ``Pipeline.
    agent_provider()`` has no per-stage override, so a separate process gets
    its own provider, budget and checkpoint. One child per round keeps the
    bound on cost and wall-clock.

    Exit 0 is success: ``ci-fix-<n>.json`` records the outcome and the
    child's newest journal directory. A non-zero, non-timeout exit reads that
    journal with ``_child_run.latest_run_dir`` / ``_child_run.failed_stage_error``
    (imported at call time — the helper is stdlib-only) and halts
    ``ci_fix_failed`` naming the child's failed stage and the tail of its
    error; ``_child_run.report`` is never called — its closing text is
    written for ``fix_jira_issue``. A timeout (the ``HaltOnTimeout`` shape:
    ``returncode is None``) halts ``ci_fix_timeout`` instead.

    ``child_cmd`` overrides the built command outright (tests only); real
    runs always build the command from ``ci-fix-workflows-<n>``, *test_cmd*,
    and the remaining budget, read fresh each run since the ceiling can move
    (an extension granted at a budget prompt, ``--budget`` at launch).
    """

    def __init__(
        self,
        *,
        paths: MergePaths,
        stage: str,
        n: int,
        project_dir: str,
        pipeline: Pipeline,
        test_cmd: str,
        timeout: float = CI_FIX_TIMEOUT,
        child_cmd: str | None = None,
    ) -> None:
        self._n = n
        self._project_dir = project_dir
        self._pipeline = pipeline
        self._test_cmd = test_cmd
        self._child_cmd = child_cmd
        super().__init__(paths=paths, stage=stage, kind="ci_fix_timeout", cmd="", timeout=timeout)

    def _remaining_budget(self, ctx: PipelineContext) -> float:
        """Return the child's ceiling in USD.

        A deliberate duplicate of ``fix_jira_issue.RunChildPipeline.
        remaining_budget`` (fix_jira_issue.py:250-291): that class lives in a
        pipeline module and cannot be imported from here. Read when the stage
        runs, not at import time — the ceiling can move after import
        (``--budget`` at launch, an extension granted at a budget prompt).
        """
        limits = [b.max_cost_usd for b in (self._pipeline.budgets or []) if b.max_cost_usd is not None]
        if not limits:
            return _MIN_CHILD_BUDGET
        spent = ctx.usage_tracker.total_cost_usd or 0.0
        return max(round(min(limits) - spent, 2), _MIN_CHILD_BUDGET)

    def _runs_dir(self) -> Path:
        return Path(self._project_dir) / "check_and_fix_ci_v1.runs"

    def _write_outcome(self, outcome: str, run_dir: Path | None) -> None:
        data = {"outcome": outcome, "journal": str(run_dir) if run_dir else None}
        Path(self._halt_paths.ci_fix(self._n)).write_text(json.dumps(data, indent=2))

    def _failure_detail(self, run_dir: Path | None) -> str:
        from norn.pipelines._child_run import failed_stage_error  # stdlib-only helper

        if run_dir is None:
            return (
                f"check_and_fix_ci_v1 left no journal under {self._runs_dir()} "
                "— it failed before norn started it"
            )
        run_json_path = run_dir / "run.json"
        run_data: dict[str, Any] = {}
        if run_json_path.is_file():
            try:
                run_data = json.loads(run_json_path.read_text())
            except json.JSONDecodeError:
                run_data = {}
        stage_name = run_data.get("failed_stage")
        lines = [f"journal: {run_dir}"]
        if isinstance(stage_name, str) and stage_name:
            lines.append(f"failed stage: {stage_name}")
            error = failed_stage_error(run_dir, stage_name)
            if error:
                lines.append("error (last 40 lines):")
                lines.extend(error.splitlines()[-40:])
        else:
            lines.append("failed, and the journal names no stage")
        return "\n".join(lines)

    def _build_cmd(self, ctx: PipelineContext) -> str:
        """Return the command to run: *child_cmd* verbatim (tests), or the
        real ``check_and_fix_ci`` invocation built from the workflows this
        round handed off and the remaining budget.
        """
        if self._child_cmd is not None:
            return self._child_cmd
        workflows = [w for w in _read(self._halt_paths.ci_fix_workflows(self._n)).splitlines() if w]
        budget = self._remaining_budget(ctx)
        return ci_fix_child_cmd(self._project_dir, workflows, self._test_cmd, budget)

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        from norn.pipelines._child_run import latest_run_dir  # stdlib-only helper

        self.cmd = self._build_cmd(ctx)

        try:
            result = await RunCommand.run(self, ctx, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._pass_through():
                raise
            halt(self._halt_paths, "unexpected_failure", _exception_detail(self._halt_stage, exc))
            return StageResult(name="", success=True)

        run_dir = latest_run_dir(self._runs_dir())

        if result.success:
            self._write_outcome("passed", run_dir)
            return result

        if isinstance(result.output, dict) and result.output.get("returncode") is None:
            self._write_outcome("timeout", run_dir)
            halt(self._halt_paths, self._timeout_kind, result.error or "command timed out")
            return StageResult(name="", success=True)

        self._write_outcome("failed", run_dir)
        halt(self._halt_paths, "ci_fix_failed", self._failure_detail(run_dir))
        return StageResult(name="", success=True)


# ---------------------------------------------------------------------------
# sync after ci fix — defense-in-depth guard behind the child's own attribution
# guard (Phase 5)
# ---------------------------------------------------------------------------


def sync_after_ci_fix_cmd(paths: MergePaths, project_dir: str, n: int) -> str:
    """Return the POSIX sh fragment for round *n*'s ``sync after ci fix {n}`` stage.

    ``ci_fix_unpushed`` when ``origin/<branch>`` is not exactly ``HEAD`` or the
    working tree is not clean — the child must have pushed everything it
    committed and left nothing uncommitted. ``attribution_found`` is defense
    in depth (review F1): ``check_and_fix_ci``'s own commit stages already
    refuse an assistant-attribution line before pushing (step 12); a match
    here means that guard was bypassed. Writes ``HEAD`` to ``remote-head`` and
    ``pushed-head`` on success, so the next stage's idea of "what was pushed"
    moves forward.
    """
    branch_path = shlex.quote(paths.branch)
    pushed_head_path = shlex.quote(paths.pushed_head)
    remote_head_path = shlex.quote(paths.remote_head)
    commits_path = shlex.quote(paths.ci_fix_commits(n))
    pattern = shlex.quote(ATTRIBUTION_ERE)
    unpushed_halt = halt_shell(
        paths, "ci_fix_unpushed",
        'printf \'origin/%s=%s HEAD=%s\\nstatus:\\n%s\\n\' "$B" "$REMOTE" "$LOCAL" "$STATUS"',
    )
    attribution_halt = halt_shell(paths, "attribution_found", f"cat {commits_path}")
    return (
        f"cd {shlex.quote(project_dir)} || exit 1\n"
        "git fetch origin --prune || exit 1\n"
        f"B=$(cat {branch_path}) || exit 1\n"
        'REMOTE=$(git rev-parse "origin/$B") || exit 1\n'
        "LOCAL=$(git rev-parse HEAD) || exit 1\n"
        "STATUS=$(git status --porcelain)\n"
        'if [ "$REMOTE" != "$LOCAL" ] || [ -n "$STATUS" ]; then\n'
        f"{unpushed_halt}"
        "fi\n"
        f"PREV=$(cat {pushed_head_path}) || exit 1\n"
        f'git log --format="%h %s" "$PREV..HEAD" > {commits_path}\n'
        f'if git log --format=%B "$PREV..HEAD" | grep -iE {pattern} >/dev/null || '
        f"git diff \"$PREV..HEAD\" | grep '^+' | grep -iE {pattern} >/dev/null; then\n"
        f"{attribution_halt}"
        "fi\n"
        f"git rev-parse HEAD > {remote_head_path}\n"
        f"git rev-parse HEAD > {pushed_head_path}\n"
    )


# ---------------------------------------------------------------------------
# wait for copilot review / fetch pr reviews (Phase 6)
# ---------------------------------------------------------------------------


async def _gh_paginated(
    path: str, *, repo: str, project_dir: str, env: dict[str, str] | None
) -> list[dict]:
    """Run ``gh api repos/<repo>/<path> --paginate --slurp`` and flatten the pages.

    ``--slurp`` wraps each page in an outer array (the same shape
    ``request_copilot_cmd``'s ``.[][]`` filter flattens in shell); every
    caller here wants a single flat list of entries, so this flattens once
    rather than repeating the double loop at each call site.
    """
    _, out, _ = await gh(
        ["api", f"repos/{repo}/{path}", "--paginate", "--slurp"], cwd=project_dir, env=env
    )
    pages = json.loads(out)
    return [item for page in pages for item in page]


def _find_copilot_review(reviews: list[dict], pushed_head: str) -> dict | None:
    """Return the first review by a ``COPILOT_LOGINS`` login whose ``commit_id``
    equals *pushed_head*, or ``None`` — a Copilot review of an older head
    never matches (review F1), so the caller keeps polling.
    """
    for review in reviews:
        login = (review.get("user") or {}).get("login")
        if login in COPILOT_LOGINS and review.get("commit_id") == pushed_head:
            return review
    return None


class WaitForCopilotReview(HaltOnFailure, BaseStage):
    """Wait for Copilot's review of the exact head that will merge (review F1).

    ``copilot-reviewer`` (written by ``request copilot review {n}`` /
    ``re-request copilot review {n}``) equal to ``unavailable`` — the
    declared capability probe — is a no-op success; there is nothing to wait
    for. Otherwise this polls the PR's reviews every
    ``_COPILOT_REVIEW_POLL_INTERVAL`` seconds until one by a
    ``COPILOT_LOGINS`` login has ``commit_id == pushed-head``, and writes
    ``copilot-review-<n>.json``. An ``already-reviewed`` state (the request
    stage found a matching review itself) passes on the very first poll,
    since the same match condition applies here. A review of an *older* head
    never counts — after a CI fix, ``re-request copilot review <n>`` has
    already asked Copilot about the new head, so only a review of that head
    means the head that merges was actually looked at.

    The deadline is ``COPILOT_REVIEW_TIMEOUT`` seconds counted from the
    latest ``copilot-requested-at`` — not from when this stage starts — so
    the CI-fix wait and the checks wait already count toward it (Q3).
    ``sleep``/``clock`` are injectable so tests never wait for real time; the
    "already elapsed" figure is computed once, from the real wall clock
    against the real ``copilot-requested-at`` timestamp — the same
    precedent ``WaitForChecks._elapsed_since_earliest`` sets for reading a
    real timestamp without an injectable "now": a test controls it by
    writing a ``copilot-requested-at`` that is already old.
    """

    needs_agent = False

    def __init__(
        self,
        *,
        n: int,
        project_dir: str,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        **kwargs: Any,
    ) -> None:
        self._n = n
        self._project_dir = project_dir
        self._sleep = sleep
        self._clock = clock
        super().__init__(**kwargs)

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        try:
            return await self._wait(ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._pass_through():
                raise
            halt(self._halt_paths, "unexpected_failure", _exception_detail(self._halt_stage, exc))
            return StageResult(name="", success=True)

    async def _wait(self, ctx: PipelineContext) -> StageResult:
        paths = self._halt_paths
        project_dir = self._project_dir
        env = ctx.env

        reviewer = _read(paths.copilot_reviewer)
        if reviewer == "unavailable":
            return StageResult(name="", success=True, output={"outcome": "unavailable"})

        repo = _read(paths.repo)
        pr_num = _read(paths.pr)
        pushed_head = _read(paths.pushed_head)
        # `already-reviewed` never requested, so there is no timestamp; its match comes on the first poll.
        if Path(paths.copilot_requested_at).is_file():
            requested_at_text = _read(paths.copilot_requested_at)
            requested_at = _parse_iso(requested_at_text)
            already_elapsed = max(0.0, (datetime.now(timezone.utc) - requested_at).total_seconds())
        else:
            requested_at_text = "(not requested)"
            already_elapsed = 0.0
        deadline = self._clock() + max(COPILOT_REVIEW_TIMEOUT - already_elapsed, 0.0)

        while True:
            reviews = await _gh_paginated(
                f"pulls/{pr_num}/reviews", repo=repo, project_dir=project_dir, env=env
            )
            match = _find_copilot_review(reviews, pushed_head)
            if match is not None:
                Path(paths.copilot_review(self._n)).write_text(json.dumps(match, indent=2) + "\n")
                return StageResult(name="", success=True, output=match)
            if self._clock() >= deadline:
                halt(
                    paths,
                    "copilot_timeout",
                    f"no Copilot review of {pushed_head} within {COPILOT_REVIEW_TIMEOUT}s "
                    f"of the request at {requested_at_text}",
                )
                return StageResult(name="", success=True)
            await self._sleep(_COPILOT_REVIEW_POLL_INTERVAL)


def _normalize_review(review: dict) -> dict:
    return {
        "key": f"review:{review['id']}",
        "kind": "review",
        "login": (review.get("user") or {}).get("login") or "(unknown)",
        "state": review.get("state") or "(none)",
        "created_at": review.get("submitted_at") or "",
        "path": None,
        "line": None,
        "body": review.get("body") or "",
    }


def _normalize_inline_comment(comment: dict) -> dict:
    return {
        "key": f"inline:{comment['id']}@{comment.get('updated_at')}",
        "kind": "inline",
        "login": (comment.get("user") or {}).get("login") or "(unknown)",
        "state": "comment",
        "created_at": comment.get("created_at") or "",
        "path": comment.get("path"),
        "line": comment.get("line"),
        "body": comment.get("body") or "",
    }


def _normalize_issue_comment(comment: dict) -> dict:
    return {
        "key": f"issue:{comment['id']}@{comment.get('updated_at')}",
        "kind": "issue",
        "login": (comment.get("user") or {}).get("login") or "(unknown)",
        "state": "comment",
        "created_at": comment.get("created_at") or "",
        "path": None,
        "line": None,
        "body": comment.get("body") or "",
    }


def _should_drop(entry: dict) -> bool:
    """True when *entry* is noise that must never reach the classifier: the
    pipeline's own marker, or an empty ``COMMENTED`` review (its inline
    comments, if any, arrive through the second REST source instead).
    """
    if FINDINGS_MARKER_PREFIX in entry["body"]:
        return True
    if entry["kind"] == "review" and entry["state"] == "COMMENTED" and not entry["body"].strip():
        return True
    return False


def render_pr_reviews(entries: list[dict]) -> str:
    """Render *entries* (already deduped and filtered, in fetch order) as the
    ``pr-reviews-<n>.md`` body.

    A pure function — testable without ``gh`` — over the normalized entry
    shape ``_normalize_review``/``_normalize_inline_comment``/
    ``_normalize_issue_comment`` produce: ``### <login> (<kind>, <state>)``,
    the ``created_at`` timestamp, ``<path>:<line>`` when a path is present,
    then the body.
    """
    blocks: list[str] = []
    for entry in entries:
        lines = [
            f"### {entry['login']} ({entry['kind']}, {entry['state']})",
            entry["created_at"] or "(no timestamp)",
        ]
        path = entry.get("path")
        if path:
            line_no = entry.get("line")
            lines.append(f"{path}:{line_no}" if line_no is not None else path)
        lines.append("")
        lines.append(entry["body"])
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _clear_findings_body(pr_num: str) -> str:
    """Return the ``VERDICT: CLEAR`` ``findings-<n>.md`` body ``FetchPrReviews``
    writes itself when a fetch turns up zero new entries worth classifying.
    """
    return (
        "VERDICT: CLEAR\n"
        f"PR: {pr_num}\n"
        "Reviewed: 0 new comments\n"
        "\n"
        "## Blocking\n"
        "\n"
        "## Non-blocking\n"
        "\n"
        "## Answered\n"
    )


def _read_seen_keys(path: str) -> set[str]:
    p = Path(path)
    if not p.is_file():
        return set()
    return {line.strip() for line in p.read_text().splitlines() if line.strip()}


class FetchPrReviews(HaltOnFailure, BaseStage):
    """Fetch every new PR review/comment entry for the classifier (step 15).

    Reads the three REST sources that together are the complete set of PR
    feedback — inline review comments (where Copilot's own findings live)
    are not in ``gh pr view --json comments`` — dedupes them against
    ``reviews-seen`` by a per-source id key (review F5: ids are the only
    cursor that agrees across sources, since a comment drafted in a pending
    review keeps its draft ``created_at`` until the review is submitted),
    drops the pipeline's own marker and empty ``COMMENTED`` reviews, and
    writes the remainder to ``pr-reviews-<n>.md`` plus the branch diff the
    classifier (which has no Bash) needs. Zero new entries writes a
    ``VERDICT: CLEAR`` ``findings-<n>.md`` itself and touches
    ``no-new-reviews-<n>``, so the classifier stage's own ``when`` can skip
    it entirely.
    """

    needs_agent = False

    def __init__(self, *, n: int, project_dir: str, **kwargs: Any) -> None:
        self._n = n
        self._project_dir = project_dir
        super().__init__(**kwargs)

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        try:
            return await self._fetch(ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._pass_through():
                raise
            halt(self._halt_paths, "unexpected_failure", _exception_detail(self._halt_stage, exc))
            return StageResult(name="", success=True)

    async def _fetch(self, ctx: PipelineContext) -> StageResult:
        paths = self._halt_paths
        n = self._n
        project_dir = self._project_dir
        env = ctx.env
        repo = _read(paths.repo)
        pr_num = _read(paths.pr)
        base = _read(paths.base)

        reviews = await _gh_paginated(
            f"pulls/{pr_num}/reviews", repo=repo, project_dir=project_dir, env=env
        )
        inline_comments = await _gh_paginated(
            f"pulls/{pr_num}/comments", repo=repo, project_dir=project_dir, env=env
        )
        issue_comments = await _gh_paginated(
            f"issues/{pr_num}/comments", repo=repo, project_dir=project_dir, env=env
        )
        _, out, _ = await gh(
            ["pr", "view", pr_num, "-R", repo, "--json", "reviewDecision"], cwd=project_dir, env=env
        )
        decision = json.loads(out).get("reviewDecision") or ""
        Path(paths.review_decision(n)).write_text(f"{decision}\n")

        entries = (
            [_normalize_review(r) for r in reviews]
            + [_normalize_inline_comment(c) for c in inline_comments]
            + [_normalize_issue_comment(c) for c in issue_comments]
        )

        seen = _read_seen_keys(paths.reviews_seen)
        new_keys: list[str] = []
        new_keys_set: set[str] = set()
        kept: list[dict] = []
        for entry in entries:
            key = entry["key"]
            if key in seen or key in new_keys_set:
                continue
            new_keys.append(key)
            new_keys_set.add(key)
            if not _should_drop(entry):
                kept.append(entry)

        if new_keys:
            with open(paths.reviews_seen, "a") as fh:
                for key in new_keys:
                    fh.write(f"{key}\n")

        _, diff_out, _ = await git(["diff", f"origin/{base}...HEAD"], cwd=project_dir, env=env)
        Path(paths.branch_diff(n)).write_text(diff_out)

        if not kept:
            Path(paths.findings(n)).write_text(_clear_findings_body(pr_num))
            Path(paths.no_new_reviews(n)).touch()
            return StageResult(name="", success=True, output={"new": 0})

        Path(paths.pr_reviews(n)).write_text(render_pr_reviews(kept))
        return StageResult(name="", success=True, output={"new": len(kept)})


# ---------------------------------------------------------------------------
# classify / validate / post / check findings (Phase 6, step 15)
# ---------------------------------------------------------------------------


def validate_findings_cmd(paths: MergePaths, project_dir: str, n: int) -> str:
    """Return the POSIX sh fragment for round *n*'s ``validate findings {n}`` stage.

    Runs only when the classifier ran (same ``when`` as ``classify findings
    {n}``), and only before anything is posted (locked decision 9): a
    malformed verdict header (line 1 is neither ``VERDICT: BLOCKING`` nor
    ``VERDICT: CLEAR``) halts ``findings_malformed``; a dirty working tree —
    the classifier has no ``Bash``, but its ``Write`` tool could still leave
    an untracked or modified file ``git status`` sees — halts
    ``classifier_touched_repo``; an attribution line anywhere in
    ``findings-<n>.md`` halts ``attribution_found``. Any one of these halting
    means ``post findings comment {n}`` (same ``when``) never runs.
    """
    findings_path = shlex.quote(paths.findings(n))
    pattern = shlex.quote(ATTRIBUTION_ERE)
    malformed_halt = halt_shell(paths, "findings_malformed", 'printf \'%s\\n\' "$LINE1"')
    touched_halt = halt_shell(paths, "classifier_touched_repo", 'printf \'%s\\n\' "$STATUS"')
    attribution_halt = halt_shell(paths, "attribution_found", 'printf \'%s\\n\' "$ATTR_MATCH"')
    return (
        f"cd {shlex.quote(project_dir)} || exit 1\n"
        f"FINDINGS={findings_path}\n"
        'LINE1=$(head -n1 "$FINDINGS") || exit 1\n'
        'case "$LINE1" in\n'
        '  "VERDICT: BLOCKING"|"VERDICT: CLEAR") ;;\n'
        f"  *) {malformed_halt}    ;;\n"
        "esac\n"
        'STATUS=$(git status --porcelain)\n'
        'if [ -n "$STATUS" ]; then\n'
        f"{touched_halt}"
        "fi\n"
        f'if ATTR_MATCH=$(grep -iE {pattern} "$FINDINGS"); then\n'
        f"{attribution_halt}"
        "fi\n"
    )


def post_findings_comment_cmd(paths: MergePaths, project_dir: str, n: int) -> str:
    """Return the POSIX sh fragment for round *n*'s ``post findings comment {n}`` stage.

    Prepends the pipeline's own marker (``FINDINGS_MARKER_PREFIX`` plus the
    round number, an HTML comment so it never renders) to
    ``findings-comment-<n>.md``, then posts it as a brand-new PR comment —
    never ``--edit-last``, since ``gh`` runs as the authenticated human and
    that flag would edit the human's own last comment — and records the
    printed URL. The marker is what lets a later round's ``fetch pr reviews``
    (via ``_should_drop``/``FINDINGS_MARKER_PREFIX``) recognise and skip this
    pipeline's own comment.
    """
    repo_path = shlex.quote(paths.repo)
    pr_path = shlex.quote(paths.pr)
    findings_path = shlex.quote(paths.findings(n))
    comment_path = shlex.quote(paths.findings_comment(n))
    url_path = shlex.quote(paths.findings_comment_url(n))
    marker = shlex.quote(f"{FINDINGS_MARKER_PREFIX} findings round {n} -->")
    return (
        f"cd {shlex.quote(project_dir)} || exit 1\n"
        f"REPO=$(cat {repo_path}) || exit 1\n"
        f"NUM=$(cat {pr_path}) || exit 1\n"
        f"{{ printf '%s\\n' {marker}; cat {findings_path}; }} > {comment_path}\n"
        f'URL=$(gh pr comment "$NUM" -R "$REPO" --body-file {comment_path}) || exit 1\n'
        f'printf \'%s\\n\' "$URL" > {url_path}\n'
    )


def check_findings_cmd(paths: MergePaths, project_dir: str, n: int) -> str:
    """Return the POSIX sh fragment for round *n*'s ``check findings {n}`` stage.

    Runs whenever the round reaches this point (``when=gate``), whether or
    not the classifier ran — ``fetch pr reviews {n}`` writes a ``VERDICT:
    CLEAR`` ``findings-<n>.md`` itself on zero new entries, so this always has
    a findings file to read. ``VERDICT: BLOCKING`` halts ``findings_blocking``
    with the ``## Blocking`` section as detail. A ``review-decision-<n>`` of
    ``CHANGES_REQUESTED`` halts ``changes_requested`` regardless of what the
    classifier decided — a human reviewer's request outranks it.
    """
    findings_path = shlex.quote(paths.findings(n))
    decision_path = shlex.quote(paths.review_decision(n))
    blocking_halt = halt_shell(paths, "findings_blocking", 'printf \'%s\\n\' "$BLOCKING"')
    changes_halt = halt_shell(
        paths, "changes_requested", "printf 'a human reviewer requested changes\\n'"
    )
    return (
        f"cd {shlex.quote(project_dir)} || exit 1\n"
        f"FINDINGS={findings_path}\n"
        'LINE1=$(head -n1 "$FINDINGS") || exit 1\n'
        'if [ "$LINE1" = "VERDICT: BLOCKING" ]; then\n'
        "  BLOCKING=$(awk '/^## Blocking/{flag=1; next} /^## /{flag=0} flag' \"$FINDINGS\")\n"
        f"{blocking_halt}"
        "fi\n"
        f"DECISION=$(cat {decision_path}) || exit 1\n"
        'if [ "$DECISION" = "CHANGES_REQUESTED" ]; then\n'
        f"{changes_halt}"
        "fi\n"
    )


# ---------------------------------------------------------------------------
# check merge preconditions / merge pr (Phase 7, step 16)
# ---------------------------------------------------------------------------


def _merge_checks_diff(settled: dict[str, Any], current_checks: list[dict], current_runs: list[dict]) -> str:
    """Return a human-readable diff between the round's settled check/run set
    and a fresh re-read taken right before the merge, or ``""`` when nothing
    differs (review F4 — the merge-time re-read).

    *settled* is whichever wait file the round's last check wait wrote
    (``checks-<n>.json``, or ``checks-<n>-after-fix.json`` after a CI fix),
    already parsed by the caller. Three kinds of drift, all folded into one
    ``checks_changed`` halt: a check name absent from the settled
    ``check_names`` list (something appeared after the checks settled), any
    *current* check whose bucket is not ``pass``/``skipping`` (whether it is
    the new arrival or a settled check that turned red), and any run from a
    fresh ``gh run list -c <green-head>`` that is not a completed green run.
    A check disappearing outright is not itself drift — only an addition or a
    bucket change is.
    """
    settled_names = set(settled.get("check_names") or [])
    added = sorted(
        {c.get("name") for c in current_checks if c.get("name") and c.get("name") not in settled_names}
    )
    not_green_checks = [
        c.get("name") or c.get("workflow") or "(unnamed check)"
        for c in current_checks
        if c.get("bucket") not in _CHECK_GREEN_BUCKETS
    ]
    not_green_runs = [
        r.get("workflowName") or f"run {r.get('databaseId')}"
        for r in current_runs
        if r.get("status") != "completed" or r.get("conclusion") not in _RUN_GREEN_CONCLUSIONS
    ]

    parts: list[str] = []
    if added:
        parts.append(f"added: {', '.join(added)}")
    if not_green_checks:
        parts.append(f"not green: {', '.join(not_green_checks)}")
    if not_green_runs:
        parts.append(f"runs not green: {', '.join(not_green_runs)}")
    return "; ".join(parts)


class CheckMergePreconditions(HaltOnFailure, BaseStage):
    """Check the PR is still fast-forwardable right before ``merge pr {n}`` touches the base.

    Waits up to ``MERGEABLE_WAIT`` seconds (re-reading every
    ``_MERGEABLE_POLL_INTERVAL`` seconds) for GitHub to finish computing
    ``mergeable``/``mergeStateStatus`` past ``UNKNOWN``, writes
    ``merge-state-<n>.json``, then decides in a fixed order (see the step
    doc):

    1. ``reviewDecision`` of ``CHANGES_REQUESTED`` or ``REVIEW_REQUIRED``
       halts ``review_required`` first, so an unmergeable PR never burns a
       round on the checks below.
    2. ``mergeable == CONFLICTING``, or ``mergeStateStatus`` ``DIRTY``/
       ``BEHIND``, touches ``RETRY-<n>`` and succeeds — the conflict path a
       round 2 picks up.
    3. A draft halts ``pr_draft``.
    4. The merge-time re-read (review F4): ``headRefOid`` other than
       ``green-head`` halts ``pr_head_moved``; any drift in the check/run set
       against the round's settled record (:func:`_merge_checks_diff`) halts
       ``checks_changed``.
    5. Only now does a non-``CLEAN`` ``mergeStateStatus`` (``BLOCKED``,
       ``UNSTABLE``, ``HAS_HOOKS``, or a still-``UNKNOWN`` status after the
       wait) halt ``merge_state``, naming the status.
    6. Otherwise it succeeds with no marker, and ``merge pr {n}`` (gated on
       ``RETRY-<n>`` being absent) runs next.

    ``sleep``/``clock`` are injectable (defaulting to real ``asyncio.sleep``/
    ``time.monotonic``) so tests never wait for real time.
    """

    needs_agent = False

    def __init__(
        self,
        *,
        n: int,
        project_dir: str,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        **kwargs: Any,
    ) -> None:
        self._n = n
        self._project_dir = project_dir
        self._sleep = sleep
        self._clock = clock
        super().__init__(**kwargs)

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        try:
            return await self._check(ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._pass_through():
                raise
            halt(self._halt_paths, "unexpected_failure", _exception_detail(self._halt_stage, exc))
            return StageResult(name="", success=True)

    # -- gh calls -----------------------------------------------------

    async def _fetch_pr_state(
        self, repo: str, pr_num: str, project_dir: str, env: dict[str, str] | None
    ) -> dict[str, Any]:
        _, out, _ = await gh(
            [
                "pr", "view", pr_num, "-R", repo, "--json",
                "mergeable,mergeStateStatus,reviewDecision,isDraft,headRefOid",
            ],
            cwd=project_dir, env=env,
        )
        return json.loads(out)

    async def _poll_checks(
        self, repo: str, pr_num: str, project_dir: str, env: dict[str, str] | None
    ) -> list[dict]:
        """One ``gh pr checks`` call. An empty list means "no checks reported"."""
        argv = [
            "pr", "checks", pr_num, "-R", repo, "--json",
            "name,workflow,state,bucket,link,startedAt,completedAt",
        ]
        rc, out, err = await gh(argv, cwd=project_dir, env=env, ok=(0, 1, 8))
        if rc == 1 and "no checks reported" in err.lower():
            return []
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            raise GhError(["gh", *argv], rc, err)

    async def _run_list(
        self, repo: str, head: str, project_dir: str, env: dict[str, str] | None
    ) -> list[dict]:
        _, out, _ = await gh(
            [
                "run", "list", "-R", repo, "-c", head, "-L", "100", "--json",
                "databaseId,workflowName,status,conclusion,url,event",
            ],
            cwd=project_dir, env=env,
        )
        return _ci_runs(out)

    # -- orchestration ----------------------------------------------------

    async def _check(self, ctx: PipelineContext) -> StageResult:
        paths = self._halt_paths
        n = self._n
        project_dir = self._project_dir
        env = ctx.env
        repo = _read(paths.repo)
        pr_num = _read(paths.pr)

        deadline = self._clock() + MERGEABLE_WAIT
        state = await self._fetch_pr_state(repo, pr_num, project_dir, env)
        while (
            (state.get("mergeable") == "UNKNOWN" or state.get("mergeStateStatus") == "UNKNOWN")
            and self._clock() < deadline
        ):
            await self._sleep(_MERGEABLE_POLL_INTERVAL)
            state = await self._fetch_pr_state(repo, pr_num, project_dir, env)

        Path(paths.merge_state(n)).write_text(json.dumps(state, indent=2) + "\n")

        review_decision = state.get("reviewDecision") or ""
        if review_decision in ("CHANGES_REQUESTED", "REVIEW_REQUIRED"):
            halt(paths, "review_required", f"reviewDecision: {review_decision}")
            return StageResult(name="", success=True, output=state)

        mergeable = state.get("mergeable")
        merge_state_status = state.get("mergeStateStatus")
        if mergeable == "CONFLICTING" or merge_state_status in ("DIRTY", "BEHIND"):
            Path(paths.retry(n)).touch()
            return StageResult(name="", success=True, output=state)

        if state.get("isDraft"):
            halt(paths, "pr_draft", json.dumps(state, indent=2))
            return StageResult(name="", success=True, output=state)

        green_head = _read(paths.green_head)
        head_ref = state.get("headRefOid")
        if head_ref != green_head:
            halt(paths, "pr_head_moved", f"headRefOid {head_ref} != green-head {green_head}")
            return StageResult(name="", success=True, output=state)

        checks = await self._poll_checks(repo, pr_num, project_dir, env)
        runs = await self._run_list(repo, green_head, project_dir, env)
        settled_path = paths.checks_after_fix(n) if Path(paths.ci_red(n)).is_file() else paths.checks(n)
        settled = json.loads(_read(settled_path))
        diff = _merge_checks_diff(settled, checks, runs)
        if diff:
            halt(paths, "checks_changed", diff)
            return StageResult(name="", success=True, output=state)

        if merge_state_status != "CLEAN":
            halt(paths, "merge_state", f"mergeStateStatus: {merge_state_status}")
            return StageResult(name="", success=True, output=state)

        return StageResult(name="", success=True, output=state)


def merge_cmd(
    paths: MergePaths,
    project_dir: str,
    n: int,
    *,
    poll_attempts: int = PR_MERGED_WAIT // 5,
    poll_interval: int = 5,
) -> str:
    """Return the POSIX sh fragment for round *n*'s ``merge pr {n}`` stage.

    A plain fast-forward push of the exact commit the checks ran on
    (``git push origin <green-sha>:refs/heads/<base>``) — never ``gh pr
    merge``, ``--admin``, ``--auto``, or ``--force`` on the base (locked
    decision 10). Deviation policy (locked decision 19): a refused
    fast-forward is never "fixed" with a different merge command.

    Order: HEAD must still be ``green-head`` (``head_moved_since_checks``);
    fetch, then check whether the base has already moved past ``green-head``
    — that touches ``RETRY-<n>`` and succeeds, the conflict path a round 2
    picks up; push ``green-head`` onto the base. Only ``non-fast-forward``/
    ``fetch first`` in the push's own output means "the base moved" (also
    ``RETRY-<n>``) — a bare ``rejected`` also matches a branch-protection
    refusal, which must halt (``merge_rejected``) rather than retry. On a
    successful push, ``merged`` is written immediately — the base is already
    fast-forwarded even if everything below fails — then this polls
    ``gh pr view --json state`` (*poll_attempts* times, *poll_interval*
    seconds apart; defaults derived from ``PR_MERGED_WAIT`` so a real run
    waits about that long, both overridable in tests so they never sleep the
    real 30s) until GitHub reports ``MERGED``, halting
    ``merged_pr_not_closed`` otherwise. Finally it deletes the remote branch
    only when it still exists — a repository with "automatically delete head
    branches" has already removed it, so this is idempotence, not a fallback
    — halting ``branch_delete_failed`` on a refusal.

    Every ``gh``/``git`` call here is a plain ``|| exit 1`` (or, in the poll
    loop, `... || exit 1`), so an unpredicted failure — including every
    ``gh pr view`` in the poll loop exiting non-zero — is a plain non-zero
    exit; the stage's own ``HaltingCommand``/``HaltOnFailure`` wrapping turns
    that into an ``unexpected_failure`` halt. ``merged`` may already be on
    disk by then, which is correct — the fast-forward already landed.
    """
    repo_path = shlex.quote(paths.repo)
    pr_path = shlex.quote(paths.pr)
    base_path = shlex.quote(paths.base)
    branch_path = shlex.quote(paths.branch)
    green_head_path = shlex.quote(paths.green_head)
    merge_log = shlex.quote(paths.merge_log(n))
    merged_path = shlex.quote(paths.merged)
    retry_path = shlex.quote(paths.retry(n))

    head_moved_halt = halt_shell(
        paths, "head_moved_since_checks",
        'printf \'HEAD %s != green %s\\n\' "$(git rev-parse HEAD)" "$GREEN"',
    )
    merge_rejected_halt = halt_shell(paths, "merge_rejected", f"cat {merge_log}")
    not_closed_halt = halt_shell(
        paths, "merged_pr_not_closed", 'printf \'PR #%s is %s\\n\' "$NUM" "$STATE"'
    )
    branch_delete_halt = halt_shell(paths, "branch_delete_failed", f"cat {merge_log}")

    return (
        f"cd {shlex.quote(project_dir)} || exit 1\n"
        f"REPO=$(cat {repo_path}) || exit 1\n"
        f"NUM=$(cat {pr_path}) || exit 1\n"
        f"BASE=$(cat {base_path}) || exit 1\n"
        f"BRANCH=$(cat {branch_path}) || exit 1\n"
        f"GREEN=$(cat {green_head_path}) || exit 1\n"
        'if [ "$(git rev-parse HEAD)" != "$GREEN" ]; then\n'
        f"{head_moved_halt}"
        "fi\n"
        "git fetch origin --prune || exit 1\n"
        'if ! git merge-base --is-ancestor "origin/$BASE" "$GREEN"; then\n'
        f"  touch {retry_path}\n"
        "  exit 0\n"
        "fi\n"
        f'if ! git push origin "$GREEN:refs/heads/$BASE" > {merge_log} 2>&1; then\n'
        f"  if grep -qiE 'non-fast-forward|fetch first' {merge_log}; then\n"
        f"    touch {retry_path}\n"
        "    exit 0\n"
        "  fi\n"
        f"{merge_rejected_halt}"
        "fi\n"
        f'printf \'%s\\n\' "$GREEN" > {merged_path}\n'
        "i=0\n"
        'STATE=""\n'
        f"while [ $i -lt {poll_attempts} ]; do\n"
        f'  STATE=$(gh pr view "$NUM" -R "$REPO" --json state -q .state) || exit 1\n'
        '  [ "$STATE" = MERGED ] && break\n'
        "  i=$((i + 1))\n"
        f"  sleep {poll_interval}\n"
        "done\n"
        'if [ "$STATE" != MERGED ]; then\n'
        f"{not_closed_halt}"
        "fi\n"
        'if git ls-remote --exit-code --heads origin "$BRANCH" > /dev/null; then\n'
        f"  if ! git push origin --delete \"$BRANCH\" >> {merge_log} 2>&1; then\n"
        f"{branch_delete_halt}"
        "  fi\n"
        "fi\n"
    )


# ---------------------------------------------------------------------------
# watch base build — post-merge build watch on the base branch (Phase 8, step 18)
# ---------------------------------------------------------------------------


def _base_build_data(
    outcome: str,
    *,
    base: str,
    sha: str,
    runs: list[dict],
    estimate: float | None = None,
    per_workflow: dict[str, float] | None = None,
    plan: Any = None,
) -> dict[str, Any]:
    """Build the JSON body written to ``base-build.json``.

    ``outcome`` is one of ``no_runs``/``passed``/``failed`` — a timed-out
    build is recorded as ``failed`` here (see ``_merge_state.
    _post_merge_build_result``), with the ``timed_out`` flag carrying the
    distinction ``## Why it stopped`` needs (the halt kind itself is
    ``base_build_timeout`` vs ``base_build_red``).
    """
    return {
        "outcome": outcome,
        "base": base,
        "sha": sha,
        "estimate": estimate,
        "per_workflow_estimate": per_workflow or {},
        "plan": (
            {"pre_sleep": plan.pre_sleep, "interval": plan.interval, "timeout": plan.timeout}
            if plan is not None
            else None
        ),
        "runs": runs,
    }


class WatchBaseBuild(HaltOnFailure, BaseStage):
    """Watch the push-triggered build on the base branch at the merged sha.

    Runs only after ``merge pr {n}`` has landed the fast-forward (the stage's
    own ``when=all_of(running(PATHS), file_exists(PATHS.merged))``). The merge
    has already happened by the time this runs, so a red base build is never
    handed to ``check_and_fix_ci`` — fixing the base is a new change, not part
    of getting this branch merged.

    Runs get :data:`BASE_RUNS_GRACE` seconds to appear on ``gh run list``
    filtered to the merged sha and the base branch. None appearing within the
    grace window is the one declared warning outcome (Q6): it writes
    ``base-build.json`` with outcome ``no_runs``, calls
    :func:`norn.pipelines._merge_state.warn`, notifies, and succeeds without
    halting — never a halt, and a failing ``gh run list`` still propagates as
    a ``GhError`` that the ``HaltOnFailure`` mixin turns into
    ``unexpected_failure`` (never folded into "no runs").

    Once runs appear, per-workflow median durations on the base branch itself
    (:func:`norn.pipelines._ci_estimate.estimate_seconds`, called with the
    base as both "branch" and fallback "base") drive the poll plan, and
    :func:`_poll_until_settled` (the same core ``WaitForChecks`` uses) waits
    until every run is ``completed``. Green (every ``conclusion`` in
    ``success``/``skipped``/``neutral``) returns without halting; red halts
    ``base_build_red``; a timeout halts ``base_build_timeout`` — both leave
    ``Status: MERGED_NEEDS_ATTENTION`` (the merge already landed) and wake
    ``escalate``.

    ``sleep``/``clock`` are injectable so tests never wait for real time.
    """

    needs_agent = False

    def __init__(
        self,
        *,
        project_dir: str,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        **kwargs: Any,
    ) -> None:
        self._project_dir = project_dir
        self._sleep = sleep
        self._clock = clock
        super().__init__(**kwargs)

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        try:
            return await self._watch(ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._pass_through():
                raise
            halt(self._halt_paths, "unexpected_failure", _exception_detail(self._halt_stage, exc))
            return StageResult(name="", success=True)

    # -- gh calls -----------------------------------------------------

    async def _run_list(
        self, repo: str, sha: str, base: str, project_dir: str, env: dict[str, str] | None
    ) -> list[dict]:
        _, out, _ = await gh(
            [
                "run", "list", "-R", repo, "-c", sha, "-b", base, "--json",
                "databaseId,workflowName,status,conclusion,url,startedAt,updatedAt,event",
            ],
            cwd=project_dir, env=env,
        )
        return _ci_runs(out)

    async def _await_appearance(
        self, repo: str, sha: str, base: str, project_dir: str, env: dict[str, str] | None
    ) -> list[dict] | None:
        deadline = self._clock() + BASE_RUNS_GRACE
        while True:
            runs = await self._run_list(repo, sha, base, project_dir, env)
            if runs:
                return runs
            if self._clock() >= deadline:
                return None
            await self._sleep(_BASE_RUNS_POLL_INTERVAL)

    # -- orchestration ----------------------------------------------------

    async def _watch(self, ctx: PipelineContext) -> StageResult:
        paths = self._halt_paths
        project_dir = self._project_dir
        env = ctx.env
        repo = _read(paths.repo)
        base = _read(paths.base)
        sha = _read(paths.merged)
        sha7 = sha[:7]

        runs = await self._await_appearance(repo, sha, base, project_dir, env)
        if runs is None:
            data = _base_build_data("no_runs", base=base, sha=sha, runs=[])
            Path(paths.base_build).write_text(json.dumps(data, indent=2))
            text = f"no workflow ran on {base} @ {sha7} within {BASE_RUNS_GRACE} s after the merge"
            warn(paths, text)
            await notify("Post-merge build: warning", text)
            return StageResult(name="", success=True, output=data)

        # Local import: see WaitForChecks._wait for why this is not a
        # module-level import (avoids a circular import with _ci_estimate,
        # which imports `gh` from this module at its own top level).
        from norn.pipelines._ci_estimate import estimate_seconds, poll_plan

        workflows = sorted({r["workflowName"] for r in runs if r.get("workflowName")})
        estimate, per_workflow = await estimate_seconds(
            repo, workflows, base, base, clock=self._clock, cwd=project_dir, env=env
        )
        plan = poll_plan(estimate)

        async def _fetch() -> list[dict]:
            return await self._run_list(repo, sha, base, project_dir, env)

        def _is_settled(state: list[dict]) -> bool:
            return all(r.get("status") == "completed" for r in state)

        def _stability_key(state: list[dict]) -> Any:
            return frozenset((r.get("databaseId"), r.get("status"), r.get("conclusion")) for r in state)

        outcome, state = await _poll_until_settled(
            fetch=_fetch,
            is_settled=_is_settled,
            stability_key=_stability_key,
            head_moved=None,
            clock=self._clock,
            sleep=self._sleep,
            interval=plan.interval,
            timeout=plan.timeout,
            settle_window=SETTLE_WINDOW,
            pre_sleep=plan.pre_sleep,
        )

        return await self._conclude(
            outcome, base=base, sha=sha, sha7=sha7, runs=state, estimate=estimate,
            per_workflow=per_workflow, plan=plan,
        )

    async def _conclude(
        self,
        outcome: str,
        *,
        base: str,
        sha: str,
        sha7: str,
        runs: list[dict],
        estimate: float | None,
        per_workflow: dict[str, float],
        plan: Any,
    ) -> StageResult:
        """Write ``base-build.json``, notify, then halt (red/timeout) or pass (green)."""
        paths = self._halt_paths

        if outcome == "timeout":
            data = _base_build_data(
                "failed", base=base, sha=sha, runs=runs, estimate=estimate,
                per_workflow=per_workflow, plan=plan,
            )
            data["timed_out"] = True
            Path(paths.base_build).write_text(json.dumps(data, indent=2))
            detail = f"{base} @ {sha7}: failed (timed out after {plan.timeout:.0f}s)"
            await notify("Post-merge build finished", detail)
            halt(paths, "base_build_timeout", detail)
            return StageResult(name="", success=True, output=data)

        failing = [
            r.get("workflowName") or f"run {r.get('databaseId')}"
            for r in runs
            if r.get("conclusion") not in _RUN_GREEN_CONCLUSIONS
        ]
        green = not failing
        data = _base_build_data(
            "passed" if green else "failed", base=base, sha=sha, runs=runs, estimate=estimate,
            per_workflow=per_workflow, plan=plan,
        )
        failing_desc = f" ({', '.join(failing)})" if failing else ""
        detail = f"{base} @ {sha7}: {'passed' if green else 'failed'}{failing_desc}"
        Path(paths.base_build).write_text(json.dumps(data, indent=2))
        await notify("Post-merge build finished", detail)

        if not green:
            halt(paths, "base_build_red", detail)
            return StageResult(name="", success=True, output=data)

        return StageResult(name="", success=True, output=data)

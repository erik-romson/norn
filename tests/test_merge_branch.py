"""Tests for norn/pipelines/_merge_gh.py (ResolveBranch, rebase_cmd) and for
the merge_branch pipeline skeleton itself (norn/pipelines/merge_branch.py).

All offline: a fake ``gh`` on PATH (tests.merge_fakes.install_fake_gh) and a
gitpython working repo with a bare ``origin`` (tests.merge_fakes.make_repo_with_remote).
"""
from __future__ import annotations

import importlib
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import git
import pytest
from git import Actor

from norn.dsl import Loop, OnFailure, Parallel, Pipeline, Stage
from norn.model_labels import ModelLabelError
from norn.models import UsageRecord
from norn.pipelines import _ci_estimate, _merge_gh, _merge_state, merge_branch
from norn.pipelines._ci_estimate import PollPlan
from norn.stages.generate import Generate
from tests.merge_fakes import RepoWithRemote, install_fake_gh, make_repo_with_remote, ctx_for


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _repo(tmp_path: Path) -> RepoWithRemote:
    """A repo-with-remote rooted under its own subdir, distinct from the
    pipeline's own work root (``<tmp_path>/norn-work`` below) — both would
    otherwise collide on ``<tmp_path>/work``.
    """
    return make_repo_with_remote(tmp_path / "checkout")


def _paths(tmp_path: Path) -> _merge_state.MergePaths:
    return _merge_state.MergePaths(str(tmp_path / "norn-work"))


def _gh_env(tmp_path: Path, routes: dict[tuple[str, ...], list[tuple[int, str, str]]]) -> dict[str, str]:
    bin_dir, _ = install_fake_gh(tmp_path, routes)
    return {"PATH": f"{bin_dir}:{os.environ['PATH']}"}


def _routes(default_branch: str = "main", open_prs: tuple[dict, ...] = ()) -> dict:
    return {
        ("repo", "view"): [
            (0, json.dumps({"nameWithOwner": "acme/widgets", "defaultBranchRef": {"name": default_branch}}), "")
        ],
        ("pr", "list"): [(0, json.dumps(list(open_prs)), "")],
    }


async def _resolve(repo: RepoWithRemote, paths, env, *, base_arg="", overwrite_remote=""):
    stage = _merge_gh.ResolveBranch(
        paths=paths, project_dir=repo.path, base_arg=base_arg, overwrite_remote=overwrite_remote
    )
    return await stage.run(ctx_for(env=env))


# ---------------------------------------------------------------------------
# ResolveBranch — base resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_base_arg_wins(tmp_path):
    repo = _repo(tmp_path)
    repo.repo.git.checkout("-b", "feature-x")
    (Path(repo.path) / "f.txt").write_text("one\n")
    repo.repo.index.add(["f.txt"])
    repo.repo.index.commit("feature commit")

    paths = _paths(tmp_path)
    env = _gh_env(tmp_path, _routes())

    result = await _resolve(repo, paths, env, base_arg="develop")

    assert result.success is True
    assert result.output["base"] == "develop"
    assert result.output["source"] == "--arg base"
    assert Path(paths.base).read_text().strip() == "develop"
    assert Path(paths.base_source).read_text().strip() == "--arg base"
    assert Path(paths.branch).read_text().strip() == "feature-x"
    assert Path(paths.repo).read_text().strip() == "acme/widgets"


@pytest.mark.asyncio
async def test_base_arg_missing_remote_fails(tmp_path):
    repo = _repo(tmp_path)
    repo.repo.git.checkout("-b", "feature-x")
    (Path(repo.path) / "f.txt").write_text("one\n")
    repo.repo.index.add(["f.txt"])
    repo.repo.index.commit("feature commit")

    paths = _paths(tmp_path)
    env = _gh_env(tmp_path, _routes())

    result = await _resolve(repo, paths, env, base_arg="no-such-branch")

    assert result.success is False
    assert "no-such-branch" in result.error


@pytest.mark.asyncio
async def test_base_arg_conflicts_with_open_pr(tmp_path):
    repo = _repo(tmp_path)
    repo.repo.git.checkout("-b", "feature-x")
    (Path(repo.path) / "f.txt").write_text("one\n")
    repo.repo.index.add(["f.txt"])
    repo.repo.index.commit("feature commit")

    paths = _paths(tmp_path)
    env = _gh_env(tmp_path, _routes(open_prs=({"number": 7, "baseRefName": "develop"},)))

    result = await _resolve(repo, paths, env, base_arg="main")

    assert result.success is False
    assert "7" in result.error
    assert "develop" in result.error
    assert "main" in result.error


@pytest.mark.asyncio
async def test_open_pr_base_wins_over_fork_point(tmp_path):
    repo = _repo(tmp_path)
    repo.repo.git.checkout("-b", "feature-x")
    (Path(repo.path) / "f.txt").write_text("one\n")
    repo.repo.index.add(["f.txt"])
    repo.repo.index.commit("feature commit")

    paths = _paths(tmp_path)
    env = _gh_env(tmp_path, _routes(open_prs=({"number": 3, "baseRefName": "develop"},)))

    result = await _resolve(repo, paths, env)

    assert result.success is True
    assert result.output["base"] == "develop"
    assert result.output["source"] == "open PR"


@pytest.mark.asyncio
async def test_branch_cut_from_develop_resolves_by_fork_point(tmp_path):
    repo = _repo(tmp_path)
    repo.repo.git.checkout("-b", "feature-y", "develop")
    (Path(repo.path) / "g.txt").write_text("g\n")
    repo.repo.index.add(["g.txt"])
    repo.repo.index.commit("feature-y commit")

    paths = _paths(tmp_path)
    env = _gh_env(tmp_path, _routes())

    result = await _resolve(repo, paths, env)

    assert result.success is True
    assert result.output["base"] == "develop"
    assert result.output["source"] == "fork point"


@pytest.mark.asyncio
async def test_develop_equal_main_resolves_ambiguous_to_default_branch(tmp_path):
    repo = _repo(tmp_path)
    # Collapse develop onto main so both candidates tie at the same distance.
    repo.repo.git.push("origin", "main:refs/heads/develop", "--force")
    repo.repo.git.fetch("origin")

    repo.repo.git.checkout("-b", "feature-x")
    (Path(repo.path) / "f.txt").write_text("one\n")
    repo.repo.index.add(["f.txt"])
    repo.repo.index.commit("feature commit")

    paths = _paths(tmp_path)
    env = _gh_env(tmp_path, _routes(default_branch="main"))

    result = await _resolve(repo, paths, env)

    assert result.success is True
    assert result.output["base"] == "main"
    assert result.output["source"] == "ambiguous"


@pytest.mark.asyncio
async def test_tie_excluding_default_branch_fails(tmp_path):
    repo = _repo(tmp_path)
    repo.repo.git.push("origin", "main:refs/heads/develop", "--force")
    repo.repo.git.fetch("origin")

    repo.repo.git.checkout("-b", "feature-x")
    (Path(repo.path) / "f.txt").write_text("one\n")
    repo.repo.index.add(["f.txt"])
    repo.repo.index.commit("feature commit")

    paths = _paths(tmp_path)
    # Default branch reported as "master", which does not exist on origin, so
    # the tie is only between main and develop -- excluding the default branch.
    env = _gh_env(tmp_path, _routes(default_branch="master"))

    result = await _resolve(repo, paths, env)

    assert result.success is False
    assert "main" in result.error
    assert "develop" in result.error
    assert "--arg base=" in result.error


@pytest.mark.asyncio
async def test_branch_equals_base_fails(tmp_path):
    repo = _repo(tmp_path)
    paths = _paths(tmp_path)
    env = _gh_env(tmp_path, _routes())

    result = await _resolve(repo, paths, env, base_arg="main")

    assert result.success is False
    assert "main" in result.error


@pytest.mark.asyncio
async def test_nothing_to_merge_fails(tmp_path):
    repo = _repo(tmp_path)
    repo.repo.git.checkout("-b", "feature-empty")

    paths = _paths(tmp_path)
    env = _gh_env(tmp_path, _routes())

    result = await _resolve(repo, paths, env, base_arg="main")

    assert result.success is False
    assert "nothing to merge" in result.error


@pytest.mark.asyncio
async def test_base_missing_on_origin_fails(tmp_path):
    repo = _repo(tmp_path)
    repo.repo.git.checkout("-b", "feature-x")
    (Path(repo.path) / "f.txt").write_text("one\n")
    repo.repo.index.add(["f.txt"])
    repo.repo.index.commit("feature commit")
    # An empty GitHub repo reports no default branch and origin has no branches.
    git.Repo(repo.origin_path).git.config("receive.denyDeleteCurrent", "ignore")
    repo.repo.git.push("origin", "--delete", "main", "develop")

    paths = _paths(tmp_path)
    env = _gh_env(tmp_path, _routes(default_branch=""))

    result = await _resolve(repo, paths, env)

    assert result.success is False
    assert "does not exist on origin" in result.error


# ---------------------------------------------------------------------------
# ResolveBranch — the remote-only-commit guard
# ---------------------------------------------------------------------------


def _push_then_orphan_commit(repo: RepoWithRemote, *, author: Actor | None = None) -> None:
    """Push a commit to origin/feature-x, then drop it locally and add a
    different local commit so HEAD stays ahead of main without the pushed
    commit's patch.
    """
    repo.repo.git.checkout("-b", "feature-x")
    (Path(repo.path) / "f.txt").write_text("one\n")
    repo.repo.index.add(["f.txt"])
    repo.repo.index.commit("feature commit", author=author)
    repo.repo.remote("origin").push("feature-x")
    repo.repo.git.reset("--hard", "HEAD~1")
    (Path(repo.path) / "g.txt").write_text("two\n")
    repo.repo.index.add(["g.txt"])
    repo.repo.index.commit("a different local commit")


@pytest.mark.asyncio
async def test_remote_only_commit_by_another_author_fails(tmp_path):
    repo = _repo(tmp_path)
    _push_then_orphan_commit(repo, author=Actor("Other Dev", "other@example.com"))

    paths = _paths(tmp_path)
    env = _gh_env(tmp_path, _routes())

    result = await _resolve(repo, paths, env, base_arg="main")

    assert result.success is False
    assert "feature commit" in result.error
    assert "overwrite_remote=" in result.error


@pytest.mark.asyncio
async def test_remote_only_commit_by_same_author_fails(tmp_path):
    # Default commit identity matches make_repo_with_remote's configured
    # user.email -- the "second machine" case (review F3): the author does
    # not matter, only whether the commit exists locally.
    repo = _repo(tmp_path)
    _push_then_orphan_commit(repo)

    paths = _paths(tmp_path)
    env = _gh_env(tmp_path, _routes())

    result = await _resolve(repo, paths, env, base_arg="main")

    assert result.success is False
    assert "feature commit" in result.error


@pytest.mark.asyncio
async def test_overwrite_remote_matching_sha_passes(tmp_path):
    repo = _repo(tmp_path)
    _push_then_orphan_commit(repo)
    remote_sha = repo.repo.git.rev_parse("origin/feature-x")

    paths = _paths(tmp_path)
    env = _gh_env(tmp_path, _routes())

    result = await _resolve(repo, paths, env, base_arg="main", overwrite_remote=remote_sha)

    assert result.success is True
    assert "overwriting" in result.output["note"]
    assert "feature commit" in result.output["note"]


@pytest.mark.asyncio
async def test_overwrite_remote_stale_sha_fails_naming_current(tmp_path):
    repo = _repo(tmp_path)
    _push_then_orphan_commit(repo)
    remote_sha = repo.repo.git.rev_parse("origin/feature-x")

    paths = _paths(tmp_path)
    env = _gh_env(tmp_path, _routes())

    result = await _resolve(repo, paths, env, base_arg="main", overwrite_remote="deadbeef")

    assert result.success is False
    assert remote_sha in result.error


@pytest.mark.asyncio
async def test_overwrite_remote_not_needed_is_reported(tmp_path):
    # origin/feature-x diverges from HEAD (not an ancestor) but is
    # patch-equivalent after the rebase below, so nothing is unmatched --
    # an overwrite_remote value here is simply unnecessary.
    repo = _repo(tmp_path)
    repo.repo.git.checkout("-b", "feature-x")
    (Path(repo.path) / "f.txt").write_text("one\n")
    repo.repo.index.add(["f.txt"])
    repo.repo.index.commit("feature commit")
    repo.repo.remote("origin").push("feature-x")
    remote_sha = repo.repo.git.rev_parse("origin/feature-x")

    repo.repo.git.checkout("main")
    (Path(repo.path) / "m.txt").write_text("m\n")
    repo.repo.index.add(["m.txt"])
    repo.repo.index.commit("main commit 2")
    repo.repo.remote("origin").push("main")

    repo.repo.git.checkout("feature-x")
    repo.repo.git.rebase("main")

    paths = _paths(tmp_path)
    env = _gh_env(tmp_path, _routes())

    result = await _resolve(repo, paths, env, base_arg="main", overwrite_remote=remote_sha)

    assert result.success is True
    assert "was not used" in result.output["note"]


@pytest.mark.asyncio
async def test_patch_equivalent_rebase_passes_with_note(tmp_path):
    repo = _repo(tmp_path)
    repo.repo.git.checkout("-b", "feature-x")
    (Path(repo.path) / "f.txt").write_text("one\n")
    repo.repo.index.add(["f.txt"])
    repo.repo.index.commit("feature commit")
    repo.repo.remote("origin").push("feature-x")

    repo.repo.git.checkout("main")
    (Path(repo.path) / "m.txt").write_text("m\n")
    repo.repo.index.add(["m.txt"])
    repo.repo.index.commit("main commit 2")
    repo.repo.remote("origin").push("main")

    repo.repo.git.checkout("feature-x")
    repo.repo.git.rebase("main")

    paths = _paths(tmp_path)
    env = _gh_env(tmp_path, _routes())

    result = await _resolve(repo, paths, env, base_arg="main")

    assert result.success is True
    assert result.output["note"] is not None
    assert "replaced remote" in result.output["note"]


@pytest.mark.asyncio
async def test_remote_only_merge_commit_fails(tmp_path):
    repo = _repo(tmp_path)
    repo.repo.git.checkout("-b", "feature-x")
    (Path(repo.path) / "f.txt").write_text("one\n")
    repo.repo.index.add(["f.txt"])
    c1 = repo.repo.index.commit("feature commit")
    repo.repo.remote("origin").push("feature-x")

    repo.repo.git.merge("develop", "--no-ff", "-m", "merge develop")
    repo.repo.remote("origin").push("feature-x")
    repo.repo.git.reset("--hard", c1.hexsha)

    paths = _paths(tmp_path)
    env = _gh_env(tmp_path, _routes())

    result = await _resolve(repo, paths, env, base_arg="main")

    assert result.success is False
    assert "merge develop" in result.error


# ---------------------------------------------------------------------------
# rebase_cmd
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rebase_cmd_conflict_halts_and_leaves_clean_tree(tmp_path):
    repo = _repo(tmp_path)
    repo.repo.git.checkout("-b", "feature-x")
    (Path(repo.path) / "README.md").write_text("feature change\n")
    repo.repo.index.add(["README.md"])
    repo.repo.index.commit("feature change")

    repo.repo.git.checkout("main")
    (Path(repo.path) / "README.md").write_text("main change\n")
    repo.repo.index.add(["README.md"])
    repo.repo.index.commit("main change")
    repo.repo.remote("origin").push("main")

    repo.repo.git.checkout("feature-x")

    paths = _paths(tmp_path)
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.base).write_text("main\n")

    script = _merge_gh.rebase_cmd(paths, repo.path, 1)
    result = subprocess.run(["sh", "-c", script], cwd=repo.path, capture_output=True, text=True)

    assert result.returncode == 0
    assert Path(paths.halt).exists()
    reason = Path(paths.halt_reason).read_text()
    assert reason.splitlines()[0] == "KIND: rebase_conflict"

    conflicts = Path(paths.rebase_conflicts(1)).read_text()
    assert "README.md" in conflicts

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo.path, capture_output=True, text=True
    ).stdout
    assert status.strip() == ""


@pytest.mark.asyncio
async def test_rebase_cmd_clean_rebase_does_not_halt(tmp_path):
    repo = _repo(tmp_path)
    repo.repo.git.checkout("-b", "feature-x")
    (Path(repo.path) / "f.txt").write_text("one\n")
    repo.repo.index.add(["f.txt"])
    repo.repo.index.commit("feature commit")

    repo.repo.git.checkout("main")
    (Path(repo.path) / "m.txt").write_text("m\n")
    repo.repo.index.add(["m.txt"])
    repo.repo.index.commit("main commit 2")
    repo.repo.remote("origin").push("main")

    repo.repo.git.checkout("feature-x")

    paths = _paths(tmp_path)
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.base).write_text("main\n")

    script = _merge_gh.rebase_cmd(paths, repo.path, 1)
    result = subprocess.run(["sh", "-c", script], cwd=repo.path, capture_output=True, text=True)

    assert result.returncode == 0
    assert not Path(paths.halt).exists()

    log = subprocess.run(
        ["git", "log", "--oneline", "main..HEAD"], cwd=repo.path, capture_output=True, text=True
    ).stdout
    assert "feature commit" in log


# ---------------------------------------------------------------------------
# merge_branch.py — pipeline skeleton (step 03)
#
# Plain (non-async) structural checks — no @pytest.mark.asyncio needed.
# ---------------------------------------------------------------------------


def _import_merge_branch(argv_tail: list[str] = (), cwd=None):
    """Import (or reload) merge_branch with patched argv and cwd.

    Mirrors the fix_jira_issue reload idiom
    (tests/test_fix_jira_issue_pipeline.py:_import_fix_jira): the module
    reads both ``sys.argv`` (for ``--arg`` flags) and ``os.getcwd()`` (for
    ``PROJECT_DIR``) at import time, so both must be patched before the
    (re)import.
    """
    saved_argv = list(sys.argv)
    saved_cwd = os.getcwd()
    try:
        sys.argv = [saved_argv[0], *argv_tail]
        if cwd is not None:
            os.chdir(str(cwd))
        from norn.pipelines import merge_branch

        return importlib.reload(merge_branch)
    finally:
        sys.argv = saved_argv
        os.chdir(saved_cwd)


# One round's exact top-level-Stage order (the "local tests {n}" Loop sits
# between "rebase {n}" and "local tests green {n}" but is not itself a Stage,
# so it never appears here — see _find_loop for its body). Round 2 is a flat
# copy of round 1, so asserting both against this one template is also what
# proves they have the same shape.
ROUND_ITEM_NAMES = [
    "rebase {n}",
    "local tests green {n}",
    "commit local fixes {n}",
    "push {n}",
    "open or reuse pr {n}",
    "pr is not draft {n}",
    "request copilot review {n}",
    "wait for checks {n}",
    "fix ci {n}",
    "sync after ci fix {n}",
    "re-request copilot review {n}",
    "wait for checks after ci fix {n}",
    "wait for copilot review {n}",
    "fetch pr reviews {n}",
    "classify findings {n}",
    "validate findings {n}",
    "post findings comment {n}",
    "check findings {n}",
    "check merge preconditions {n}",
    "merge pr {n}",
]

# The pipeline's exact top-level-Stage order. Later steps extend this rather
# than rewrite it, so it stays the one place the full expected order lives.
EXPECTED_ITEM_NAMES = [
    "assert launch tree",
    "assert fresh run",
    "preflight",
    "resolve branch",
    *[name.format(n=1) for name in ROUND_ITEM_NAMES],
    *[name.format(n=2) for name in ROUND_ITEM_NAMES],
    "merge reached",
    "watch base build",
    "write merge state",
    "summarize run",
    "finalize merge state",
    "escalate",
]

# One round's loop-body stage order, in the same template form.
LOOP_BODY_NAMES = [
    "count local attempt {n}",
    "run local tests {n}",
    "compress local tests {n}",
    "assess local failure {n}",
    "decide local fix {n}",
    "check local verdict {n}",
]

# Names that must NOT carry HaltOnFailure — every other stage, loop bodies
# included, between "resolve branch" (exclusive) and "write merge state"
# (exclusive) must. "count local attempt {n}" is the mixin's own input
# (`last_attempt=`), so halting there would be circular.
_NON_HALTING_NAMES = {
    "assert launch tree",
    "assert fresh run",
    "preflight",
    "resolve branch",
    "count local attempt 1",
    "count local attempt 2",
    "write merge state",
    "summarize run",
    "finalize merge state",
    "escalate",
}

# The round-2 stages that need a work-root file of their own beyond `RETRY-1`
# before they run: the four gated on `RED-2` (a red local suite) and the four
# gated on `CI-RED-2` (red PR checks handed to a check_and_fix_ci child).
_ROUND_TWO_EXTRA_FILE_GATED = {
    "compress local tests 2": "red",
    "assess local failure 2": "red",
    "decide local fix 2": "red",
    "check local verdict 2": "red",
    "fix ci 2": "ci_red",
    "sync after ci fix 2": "ci_red",
    "re-request copilot review 2": "ci_red",
    "wait for checks after ci fix 2": "ci_red",
}


def _walk_stages(items):
    """Yield every Stage in *items*, recursing into Loop and Parallel bodies."""
    for item in items:
        if isinstance(item, Stage):
            yield item
        elif isinstance(item, (Loop, Parallel)):
            yield from _walk_stages(item.stages)


def _top_stages(items):
    return [item for item in items if isinstance(item, Stage)]


def _find_loop(items, name):
    for item in items:
        if isinstance(item, Loop) and item.name == name:
            return item
    raise KeyError(name)


def test_merge_branch_imports_with_empty_metadata_args(tmp_path):
    mod = _import_merge_branch(cwd=tmp_path)
    assert mod.metadata["args"] == {}


def test_merge_branch_item_order(tmp_path):
    mod = _import_merge_branch(cwd=tmp_path)
    names = [item.name for item in _top_stages(mod.config.items)]
    assert names == EXPECTED_ITEM_NAMES


def test_first_three_items_are_always_run_and_fail(tmp_path):
    mod = _import_merge_branch(cwd=tmp_path)
    stages = {s.name: s for s in _top_stages(mod.config.items)}
    for name in ("assert launch tree", "assert fresh run", "preflight"):
        assert stages[name].always_run is True, name
        assert stages[name].on_failure is OnFailure.FAIL, name


# The only top-level stages after `resolve branch` with no `when`: the
# summary machinery, which always runs regardless of how the run ended.
_NO_WHEN_EXEMPT_NAMES = {"write merge state", "summarize run", "finalize merge state"}


def test_every_stage_after_resolve_branch_except_summary_writers_has_when(tmp_path):
    mod = _import_merge_branch(cwd=tmp_path)
    stages = _top_stages(mod.config.items)
    seen_resolve_branch = False
    for stage in stages:
        if stage.name == "resolve branch":
            seen_resolve_branch = True
            continue
        if not seen_resolve_branch:
            continue
        if stage.name in _NO_WHEN_EXEMPT_NAMES:
            continue
        assert stage.when is not None, stage.name


def test_escalate_is_last_ask_user_and_the_only_one(tmp_path):
    mod = _import_merge_branch(cwd=tmp_path)
    stages = _top_stages(mod.config.items)
    assert stages[-1].name == "escalate"
    assert stages[-1].on_failure is OnFailure.ASK_USER

    ask_user_stages = [s for s in _walk_stages(mod.config.items) if s.on_failure is OnFailure.ASK_USER]
    assert [s.name for s in ask_user_stages] == ["escalate"]


def test_halt_on_failure_boundary(tmp_path):
    """Every stage of both rounds, plus `merge reached`, halts on failure."""
    mod = _import_merge_branch(cwd=tmp_path)
    stages = list(_walk_stages(mod.config.items))
    names = {s.name for s in stages}

    # The boundary is only meaningful if it actually spans both rounds, the
    # loop bodies, and the post-rounds guard.
    assert {"rebase 1", "rebase 2", "check local verdict 2", "merge reached"} <= names

    for stage in stages:
        expect_halting = stage.name not in _NON_HALTING_NAMES
        assert isinstance(stage.impl, _merge_state.HaltOnFailure) is expect_halting, stage.name


def test_unknown_fix_model_raises_model_label_error(tmp_path):
    with pytest.raises(ModelLabelError):
        _import_merge_branch(["--arg", "fix_model=nope"], cwd=tmp_path)


def test_unknown_classify_model_raises_model_label_error(tmp_path):
    with pytest.raises(ModelLabelError):
        _import_merge_branch(["--arg", "classify_model=nope"], cwd=tmp_path)


def test_bad_budget_raises_value_error(tmp_path):
    with pytest.raises(ValueError):
        _import_merge_branch(["--arg", "budget=not-a-number"], cwd=tmp_path)


def test_rerun_cmd_carries_args_except_overwrite_remote(tmp_path):
    mod = _import_merge_branch(
        [
            "--arg", "base=develop",
            "--arg", "budget=20",
            "--arg", "overwrite_remote=deadbeef",
        ],
        cwd=tmp_path,
    )
    assert "base=develop" in mod.RERUN_CMD
    assert "budget=20" in mod.RERUN_CMD
    assert "overwrite_remote" not in mod.RERUN_CMD
    assert "--non-interactive" in mod.RERUN_CMD


def test_rerun_cmd_with_no_args_is_the_bare_command(tmp_path):
    mod = _import_merge_branch(cwd=tmp_path)
    assert mod.RERUN_CMD == "norn run merge_branch --non-interactive"


# ---------------------------------------------------------------------------
# merge_branch.py — local test loop (step 07)
# ---------------------------------------------------------------------------


def _run_sh(script: str, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["sh", "-c", script], cwd=cwd, capture_output=True, text=True)


def _local_key(project_dir: str, test_cmd: str) -> str:
    """Recompute the tree+test_cmd hash the shell builders themselves use."""
    script = (
        f"cd {shlex.quote(project_dir)} && "
        f"{{ git rev-parse HEAD; git diff HEAD; printf '%s' {shlex.quote(test_cmd)}; }} "
        "| git hash-object --stdin"
    )
    result = subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def test_local_tests_loop_shape(tmp_path):
    mod = _import_merge_branch(cwd=tmp_path)
    loop = _find_loop(mod.config.items, "local tests 1")

    assert loop.max_retries == mod.LOCAL_FIX_ATTEMPTS + 1
    assert loop.on_exhaust is OnFailure.FAIL
    assert loop.new_session is True

    assert loop.stages[0].name == "count local attempt 1"
    assert not isinstance(loop.stages[0].impl, _merge_state.HaltOnFailure)

    last_attempt = (1, mod.LOCAL_FIX_ATTEMPTS + 1)
    for stage in loop.stages[1:]:
        assert isinstance(stage.impl, _merge_state.HaltOnFailure), stage.name
        assert stage.impl._halt_last_attempt == last_attempt, stage.name


def test_local_tests_green_is_top_level_and_named_differently(tmp_path):
    mod = _import_merge_branch(cwd=tmp_path)
    loop = _find_loop(mod.config.items, "local tests 1")
    body_names = {s.name for s in loop.stages}

    stages = {s.name: s for s in _top_stages(mod.config.items)}
    green = stages["local tests green 1"]

    assert "local tests green 1" not in body_names
    assert green.on_failure is OnFailure.FAIL
    assert isinstance(green.impl, _merge_state.HaltOnFailure)


def test_decide_local_fix_prompt_and_tooling(tmp_path):
    mod = _import_merge_branch(cwd=tmp_path)
    loop = _find_loop(mod.config.items, "local tests 1")
    decide = next(s for s in loop.stages if s.name == "decide local fix 1")

    assert mod.WORK in decide.impl.add_dirs
    assert decide.impl.cwd is None
    assert mod.COMMENT_RULE in decide.impl.prompt
    assert decide.impl.permission_mode == "acceptEdits"
    assert decide.impl.setting_sources == ["project"]


# --- run_local_tests_cmd ----------------------------------------------------


def test_run_local_tests_cmd_passing_writes_proof(tmp_path):
    repo = _repo(tmp_path)
    paths = _paths(tmp_path)
    Path(paths.root).mkdir(parents=True, exist_ok=True)

    result = _run_sh(merge_branch.run_local_tests_cmd(paths, repo.path, "true", 1), repo.path)

    assert result.returncode == 0
    assert Path(paths.proof(1)).is_file()
    assert not Path(paths.red(1)).exists()
    assert Path(paths.local_log(1)).is_file()


def test_run_local_tests_cmd_failing_touches_red(tmp_path):
    repo = _repo(tmp_path)
    paths = _paths(tmp_path)
    Path(paths.root).mkdir(parents=True, exist_ok=True)

    result = _run_sh(merge_branch.run_local_tests_cmd(paths, repo.path, "false", 1), repo.path)

    assert result.returncode == 0
    assert Path(paths.red(1)).is_file()
    assert not Path(paths.proof(1)).exists()


# --- check_local_verdict_cmd -------------------------------------------------


def test_check_local_verdict_missing_file_halts(tmp_path):
    repo = _repo(tmp_path)
    paths = _paths(tmp_path)
    Path(paths.root).mkdir(parents=True, exist_ok=True)

    result = _run_sh(merge_branch.check_local_verdict_cmd(paths, repo.path, 1, 4), repo.path)

    assert result.returncode == 0
    assert Path(paths.halt).is_file()
    assert Path(paths.halt_reason).read_text().splitlines()[0] == "KIND: local_verdict_missing"


def test_check_local_verdict_malformed_line_halts(tmp_path):
    repo = _repo(tmp_path)
    paths = _paths(tmp_path)
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.verdict(1)).write_text("not a verdict\n")

    result = _run_sh(merge_branch.check_local_verdict_cmd(paths, repo.path, 1, 4), repo.path)

    assert result.returncode == 0
    assert Path(paths.halt_reason).read_text().splitlines()[0] == "KIND: local_verdict_malformed"


def test_check_local_verdict_stop_halts_and_archives(tmp_path):
    repo = _repo(tmp_path)
    paths = _paths(tmp_path)
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.local_head(1)).write_text(repo.repo.head.commit.hexsha)
    Path(paths.verdict(1)).write_text("VERDICT: STOP\nReason: needs a credential\n")

    result = _run_sh(merge_branch.check_local_verdict_cmd(paths, repo.path, 1, 4), repo.path)

    assert result.returncode == 0
    assert Path(paths.halt_reason).read_text().splitlines()[0] == "KIND: local_stop"
    assert Path(paths.verdict_archive(1, 1)).is_file()
    assert "needs a credential" in Path(paths.halt_reason).read_text()


def test_check_local_verdict_fix_with_clean_tree_halts_no_diff(tmp_path):
    repo = _repo(tmp_path)
    paths = _paths(tmp_path)
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.local_head(1)).write_text(repo.repo.head.commit.hexsha)
    Path(paths.verdict(1)).write_text("VERDICT: FIX\nReason: x\n")

    result = _run_sh(merge_branch.check_local_verdict_cmd(paths, repo.path, 1, 4), repo.path)

    assert result.returncode == 0
    assert Path(paths.halt_reason).read_text().splitlines()[0] == "KIND: local_no_diff"


def test_check_local_verdict_fix_with_untracked_file_halts(tmp_path):
    repo = _repo(tmp_path)
    paths = _paths(tmp_path)
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.local_head(1)).write_text(repo.repo.head.commit.hexsha)
    Path(paths.verdict(1)).write_text("VERDICT: FIX\nReason: x\n")
    (Path(repo.path) / "README.md").write_text("hello\nedited\n")
    (Path(repo.path) / "new.txt").write_text("new\n")

    result = _run_sh(merge_branch.check_local_verdict_cmd(paths, repo.path, 1, 4), repo.path)

    assert result.returncode == 0
    text = Path(paths.halt_reason).read_text()
    assert text.splitlines()[0] == "KIND: untracked_files"
    assert "new.txt" in text


def test_check_local_verdict_fix_with_moved_head_halts(tmp_path):
    repo = _repo(tmp_path)
    paths = _paths(tmp_path)
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.local_head(1)).write_text("0" * 40)
    Path(paths.verdict(1)).write_text("VERDICT: FIX\nReason: x\n")

    result = _run_sh(merge_branch.check_local_verdict_cmd(paths, repo.path, 1, 4), repo.path)

    assert result.returncode == 0
    assert Path(paths.halt_reason).read_text().splitlines()[0] == "KIND: local_head_moved"


def test_check_local_verdict_fix_with_attribution_line_halts(tmp_path):
    repo = _repo(tmp_path)
    paths = _paths(tmp_path)
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.local_head(1)).write_text(repo.repo.head.commit.hexsha)
    Path(paths.verdict(1)).write_text("VERDICT: FIX\nReason: x\n")
    (Path(repo.path) / "README.md").write_text(
        "hello\n# Co-Authored-By: Claude <noreply@anthropic.com>\n"
    )

    result = _run_sh(merge_branch.check_local_verdict_cmd(paths, repo.path, 1, 4), repo.path)

    assert result.returncode == 0
    text = Path(paths.halt_reason).read_text()
    assert text.splitlines()[0] == "KIND: attribution_found"
    assert "Co-Authored-By" in text


def test_check_local_verdict_fix_with_tracked_edit_retries(tmp_path):
    repo = _repo(tmp_path)
    paths = _paths(tmp_path)
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.local_head(1)).write_text(repo.repo.head.commit.hexsha)
    Path(paths.verdict(1)).write_text("VERDICT: FIX\nReason: real fix\n")
    Path(paths.local_attempt(1)).write_text("1")
    Path(paths.local_signature_pending(1)).write_text("some-signature")
    (Path(repo.path) / "README.md").write_text("hello\nedited\n")

    result = _run_sh(merge_branch.check_local_verdict_cmd(paths, repo.path, 1, 4), repo.path)

    assert result.returncode == 1
    assert "fix attempt 1 applied" in result.stderr
    assert not Path(paths.halt).exists()
    assert Path(paths.local_signature(1)).read_text() == "some-signature"
    assert not Path(paths.local_signature_pending(1)).exists()
    assert Path(paths.verdict_archive(1, 1)).is_file()


def test_check_local_verdict_fix_on_last_attempt_halts_local_cap(tmp_path):
    repo = _repo(tmp_path)
    paths = _paths(tmp_path)
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.local_head(1)).write_text(repo.repo.head.commit.hexsha)
    Path(paths.verdict(1)).write_text("VERDICT: FIX\nReason: real fix\n")
    Path(paths.local_attempt(1)).write_text("4")
    Path(paths.local_signature_pending(1)).write_text("some-signature")
    (Path(repo.path) / "README.md").write_text("hello\nedited\n")

    result = _run_sh(merge_branch.check_local_verdict_cmd(paths, repo.path, 1, 4), repo.path)

    assert result.returncode == 0
    assert Path(paths.halt).is_file()
    assert Path(paths.halt_reason).read_text().splitlines()[0] == "KIND: local_cap"


# --- local_tests_green_cmd ---------------------------------------------------


def test_local_tests_green_does_not_rerun_on_matching_proof(tmp_path):
    repo = _repo(tmp_path)
    paths = _paths(tmp_path)
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    fail_if_run = "echo SHOULD_NOT_RUN_1 >&2 && false"
    Path(paths.proof(1)).write_text(_local_key(repo.path, fail_if_run))

    result = _run_sh(
        merge_branch.local_tests_green_cmd(paths, repo.path, fail_if_run, 1), repo.path
    )

    assert result.returncode == 0
    assert "not rerun" in result.stdout
    assert "SHOULD_NOT_RUN_1" not in result.stderr
    assert not Path(paths.halt).exists()


def test_local_tests_green_reruns_after_tracked_edit_and_halts_on_red(tmp_path):
    repo = _repo(tmp_path)
    paths = _paths(tmp_path)
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    fails_when_invoked = "echo INVOKED_AGAIN >&2 && false"
    Path(paths.proof(1)).write_text(_local_key(repo.path, fails_when_invoked))

    (Path(repo.path) / "README.md").write_text("hello\nedited\n")
    repo.repo.index.add(["README.md"])
    repo.repo.index.commit("edit")

    result = _run_sh(
        merge_branch.local_tests_green_cmd(paths, repo.path, fails_when_invoked, 1), repo.path
    )

    assert result.returncode == 0
    assert "INVOKED_AGAIN" in result.stderr
    assert Path(paths.halt).is_file()
    assert Path(paths.halt_reason).read_text().splitlines()[0] == "KIND: local_gate_red"


# ---------------------------------------------------------------------------
# merge_branch.py — commit local fixes and push (step 08)
# ---------------------------------------------------------------------------


def test_commit_and_push_follow_local_tests_green_and_are_halting_with_when(tmp_path):
    mod = _import_merge_branch(cwd=tmp_path)
    names = [item.name for item in _top_stages(mod.config.items)]
    green_idx = names.index("local tests green 1")
    assert names[green_idx + 1 : green_idx + 3] == ["commit local fixes 1", "push 1"]

    stages = {s.name: s for s in _top_stages(mod.config.items)}
    for name in ("commit local fixes 1", "push 1"):
        assert isinstance(stages[name].impl, _merge_state.HaltOnFailure), name
        assert stages[name].when is not None, name


# --- commit_local_fixes_cmd ---------------------------------------------------


def _install_rejecting_pre_commit_hook(repo_path: str) -> None:
    hook_path = Path(repo_path) / ".git" / "hooks" / "pre-commit"
    hook_path.write_text("#!/bin/sh\necho 'blocked by hook' >&2\nexit 1\n")
    hook_path.chmod(0o755)


def test_commit_local_fixes_halts_on_untracked_file(tmp_path):
    repo = _repo(tmp_path)
    paths = _paths(tmp_path)
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    (Path(repo.path) / "README.md").write_text("hello\nedited\n")
    (Path(repo.path) / "new.txt").write_text("new\n")

    result = _run_sh(merge_branch.commit_local_fixes_cmd(paths, repo.path, 1), repo.path)

    assert result.returncode == 0
    text = Path(paths.halt_reason).read_text()
    assert text.splitlines()[0] == "KIND: untracked_files"
    assert "new.txt" in text
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo.path, capture_output=True, text=True
    ).stdout
    assert "new.txt" in status
    assert not Path(paths.commit_log(1)).exists()


def test_commit_local_fixes_commits_tracked_edit_with_fixed_subject(tmp_path):
    repo = _repo(tmp_path)
    paths = _paths(tmp_path)
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    before_head = repo.repo.head.commit.hexsha
    (Path(repo.path) / "README.md").write_text("hello\nedited\n")

    result = _run_sh(merge_branch.commit_local_fixes_cmd(paths, repo.path, 1), repo.path)

    assert result.returncode == 0
    assert not Path(paths.halt).exists()
    repo.repo.head.reference = repo.repo.head.reference  # refresh cached state
    new_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo.path, capture_output=True, text=True
    ).stdout.strip()
    assert new_head != before_head

    subject = subprocess.run(
        ["git", "log", "-1", "--format=%B"], cwd=repo.path, capture_output=True, text=True
    ).stdout.strip()
    assert subject == "fix: local test failures (merge_branch round 1)"
    assert Path(paths.commit_log(1)).is_file()


def test_commit_local_fixes_with_nothing_staged_is_a_noop(tmp_path):
    repo = _repo(tmp_path)
    paths = _paths(tmp_path)
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    before_head = repo.repo.head.commit.hexsha

    result = _run_sh(merge_branch.commit_local_fixes_cmd(paths, repo.path, 1), repo.path)

    assert result.returncode == 0
    assert "no local fixes to commit" in result.stdout
    new_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo.path, capture_output=True, text=True
    ).stdout.strip()
    assert new_head == before_head
    assert not Path(paths.commit_log(1)).exists()
    assert not Path(paths.halt).exists()


def test_commit_local_fixes_rejected_by_hook_halts_and_leaves_nothing_staged(tmp_path):
    repo = _repo(tmp_path)
    paths = _paths(tmp_path)
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    _install_rejecting_pre_commit_hook(repo.path)
    (Path(repo.path) / "README.md").write_text("hello\nedited\n")

    result = _run_sh(merge_branch.commit_local_fixes_cmd(paths, repo.path, 1), repo.path)

    assert result.returncode == 0
    text = Path(paths.halt_reason).read_text()
    assert text.splitlines()[0] == "KIND: commit_rejected"
    assert "blocked by hook" in Path(paths.commit_log(1)).read_text()
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"], cwd=repo.path, capture_output=True, text=True
    ).stdout
    assert staged.strip() == ""


# --- push_cmd -----------------------------------------------------------------


def test_push_new_branch_sets_upstream_with_empty_lease(tmp_path):
    repo = _repo(tmp_path)
    repo.repo.git.checkout("-b", "feature-x")
    (Path(repo.path) / "f.txt").write_text("one\n")
    repo.repo.index.add(["f.txt"])
    repo.repo.index.commit("feature commit")
    head = repo.repo.head.commit.hexsha

    paths = _paths(tmp_path)
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.branch).write_text("feature-x\n")
    Path(paths.remote_head).write_text("\n")

    result = _run_sh(_merge_gh.push_cmd(paths, repo.path, 1), repo.path)

    assert result.returncode == 0
    assert not Path(paths.halt).exists()

    upstream = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "@{u}"], cwd=repo.path, capture_output=True, text=True
    ).stdout.strip()
    assert upstream == "origin/feature-x"
    assert Path(paths.pushed_head).read_text().strip() == head
    assert Path(paths.remote_head).read_text().strip() == head


def test_push_rerun_with_unchanged_head_succeeds(tmp_path):
    repo = _repo(tmp_path)
    repo.repo.git.checkout("-b", "feature-x")
    (Path(repo.path) / "f.txt").write_text("one\n")
    repo.repo.index.add(["f.txt"])
    repo.repo.index.commit("feature commit")

    paths = _paths(tmp_path)
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.branch).write_text("feature-x\n")
    Path(paths.remote_head).write_text("\n")

    first = _run_sh(_merge_gh.push_cmd(paths, repo.path, 1), repo.path)
    assert first.returncode == 0
    assert not Path(paths.halt).exists()

    second = _run_sh(_merge_gh.push_cmd(paths, repo.path, 2), repo.path)
    assert second.returncode == 0
    assert not Path(paths.halt).exists()


def test_push_lease_mismatch_halts_and_leaves_remote_unchanged(tmp_path):
    repo = _repo(tmp_path)
    repo.repo.git.checkout("-b", "feature-x")
    (Path(repo.path) / "f.txt").write_text("one\n")
    repo.repo.index.add(["f.txt"])
    repo.repo.index.commit("feature commit")
    repo.repo.remote("origin").push("feature-x")
    remote_sha_before = repo.repo.git.rev_parse("origin/feature-x")

    paths = _paths(tmp_path)
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.branch).write_text("feature-x\n")
    Path(paths.remote_head).write_text(remote_sha_before + "\n")

    # A second clone pushes a new commit to origin/feature-x from under the
    # recorded lease, simulating a second machine racing this one.
    second_clone_path = tmp_path / "second-clone"
    second = git.Repo.clone_from(repo.origin_path, str(second_clone_path), branch="feature-x")
    with second.config_writer() as cw:
        cw.set_value("user", "email", "erik@zenior.no")
        cw.set_value("user", "name", "Erik")
    (second_clone_path / "g.txt").write_text("two\n")
    second.index.add(["g.txt"])
    second.index.commit("second machine commit")
    second.remote("origin").push("feature-x")
    second_sha = second.head.commit.hexsha

    result = _run_sh(_merge_gh.push_cmd(paths, repo.path, 1), repo.path)

    assert result.returncode == 0
    assert Path(paths.halt).is_file()
    assert Path(paths.halt_reason).read_text().splitlines()[0] == "KIND: push_rejected"

    origin_repo = git.Repo(repo.origin_path)
    remote_ref_after = origin_repo.git.rev_parse("refs/heads/feature-x")
    assert remote_ref_after == second_sha


# ---------------------------------------------------------------------------
# pr_open_or_reuse_cmd / pr_not_draft_cmd / request_copilot_cmd (step 09)
# ---------------------------------------------------------------------------


def _run_sh_env(script: str, cwd: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", "-c", script], cwd=cwd, env={**os.environ, **env}, capture_output=True, text=True
    )


def _gh_calls(calls_log: str) -> list[list[str]]:
    return [json.loads(line) for line in Path(calls_log).read_text().splitlines() if line.strip()]


def _write_pr_prereqs(paths, *, repo="acme/widgets", branch="feature-x", base="main") -> None:
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.repo).write_text(repo + "\n")
    Path(paths.branch).write_text(branch + "\n")
    Path(paths.base).write_text(base + "\n")


def _pr_json(number=42, url="https://github.com/acme/widgets/pull/42", is_draft=False, head="deadbeef"):
    return json.dumps({"number": number, "url": url, "isDraft": is_draft, "headRefOid": head})


def test_pr_open_or_reuse_creates_pr_when_none_open(tmp_path):
    paths = _paths(tmp_path)
    _write_pr_prereqs(paths)
    bin_dir, calls_log = install_fake_gh(
        tmp_path,
        {
            ("pr", "list"): [(0, "", ""), (0, "42", "")],
            ("pr", "create"): [(0, "", "")],
            ("pr", "view"): [(0, _pr_json(), "")],
        },
    )
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    result = _run_sh_env(_merge_gh.pr_open_or_reuse_cmd(paths, str(tmp_path)), str(tmp_path), env)

    assert result.returncode == 0
    assert not Path(paths.halt).exists()
    assert Path(paths.pr_origin).read_text().strip() == "created"
    assert Path(paths.pr).read_text().strip() == "42"
    assert json.loads(Path(paths.pr_json).read_text())["number"] == 42

    calls = _gh_calls(calls_log)
    create_calls = [c for c in calls if c[:2] == ["pr", "create"]]
    assert create_calls == [
        ["pr", "create", "-R", "acme/widgets", "--base", "main", "--head", "feature-x", "--fill"]
    ]


def test_pr_open_or_reuse_reuses_open_pr_without_create(tmp_path):
    paths = _paths(tmp_path)
    _write_pr_prereqs(paths)
    bin_dir, calls_log = install_fake_gh(
        tmp_path,
        {
            ("pr", "list"): [(0, "42", "")],
            ("pr", "view"): [(0, "main", ""), (0, _pr_json(), "")],
        },
    )
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    result = _run_sh_env(_merge_gh.pr_open_or_reuse_cmd(paths, str(tmp_path)), str(tmp_path), env)

    assert result.returncode == 0
    assert not Path(paths.halt).exists()
    assert Path(paths.pr_origin).read_text().strip() == "reused"
    assert Path(paths.pr).read_text().strip() == "42"

    calls = _gh_calls(calls_log)
    assert not [c for c in calls if c[:2] == ["pr", "create"]]


def test_pr_open_or_reuse_base_mismatch_halts(tmp_path):
    paths = _paths(tmp_path)
    _write_pr_prereqs(paths, base="main")
    bin_dir, _ = install_fake_gh(
        tmp_path,
        {
            ("pr", "list"): [(0, "42", "")],
            ("pr", "view"): [(0, "develop", "")],
        },
    )
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    result = _run_sh_env(_merge_gh.pr_open_or_reuse_cmd(paths, str(tmp_path)), str(tmp_path), env)

    assert result.returncode == 0
    assert Path(paths.halt).is_file()
    text = Path(paths.halt_reason).read_text()
    assert text.splitlines()[0] == "KIND: pr_base_mismatch"
    assert "develop" in text and "main" in text


def test_pr_not_draft_halts_on_draft(tmp_path):
    paths = _paths(tmp_path)
    _write_pr_prereqs(paths)
    Path(paths.pr).write_text("42\n")
    Path(paths.pr_json).write_text(_pr_json(is_draft=True))
    bin_dir, _ = install_fake_gh(tmp_path, {("pr", "view"): [(0, "true", "")]})
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    result = _run_sh_env(_merge_gh.pr_not_draft_cmd(paths, str(tmp_path)), str(tmp_path), env)

    assert result.returncode == 0
    assert Path(paths.halt).is_file()
    assert Path(paths.halt_reason).read_text().splitlines()[0] == "KIND: pr_draft"


def test_pr_not_draft_passes_when_not_draft(tmp_path):
    paths = _paths(tmp_path)
    _write_pr_prereqs(paths)
    Path(paths.pr).write_text("42\n")
    bin_dir, _ = install_fake_gh(tmp_path, {("pr", "view"): [(0, "false", "")]})
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    result = _run_sh_env(_merge_gh.pr_not_draft_cmd(paths, str(tmp_path)), str(tmp_path), env)

    assert result.returncode == 0
    assert not Path(paths.halt).exists()


def _write_copilot_prereqs(paths, *, pushed_head="cafef00d") -> None:
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.repo).write_text("acme/widgets\n")
    Path(paths.pr).write_text("42\n")
    Path(paths.pushed_head).write_text(pushed_head + "\n")


def test_request_copilot_does_not_combine_slurp_with_jq(tmp_path):
    # gh rejects `--slurp` together with `-q`; the fake gh ignores flags, so pin it here.
    cmd = _merge_gh.request_copilot_cmd(_paths(tmp_path), str(tmp_path), rerequest=False)

    assert "--slurp" not in cmd


def test_request_copilot_already_reviewed_when_review_matches_pushed_head(tmp_path):
    paths = _paths(tmp_path)
    _write_copilot_prereqs(paths, pushed_head="cafef00d")
    bin_dir, calls_log = install_fake_gh(tmp_path, {("api",): [(0, "cafef00d\n", "")]})
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    result = _run_sh_env(
        _merge_gh.request_copilot_cmd(paths, str(tmp_path), rerequest=False), str(tmp_path), env
    )

    assert result.returncode == 0
    assert not Path(paths.halt).exists()
    assert Path(paths.copilot_reviewer).read_text().strip() == "already-reviewed"
    assert Path(paths.copilot_requested_head).read_text().strip() == "cafef00d"

    calls = _gh_calls(calls_log)
    assert not [c for c in calls if c[:2] == ["pr", "edit"]]


def test_request_copilot_calls_pr_edit_when_review_is_older_head(tmp_path):
    paths = _paths(tmp_path)
    _write_copilot_prereqs(paths, pushed_head="cafef00d")
    bin_dir, calls_log = install_fake_gh(
        tmp_path,
        {
            ("api",): [(0, "older111\n", "")],
            ("pr", "edit"): [(0, "", "")],
        },
    )
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    result = _run_sh_env(
        _merge_gh.request_copilot_cmd(paths, str(tmp_path), rerequest=False), str(tmp_path), env
    )

    assert result.returncode == 0
    assert not Path(paths.halt).exists()
    assert Path(paths.copilot_reviewer).read_text().strip() == "requested"
    assert Path(paths.copilot_requested_at).is_file()
    assert Path(paths.copilot_requested_head).read_text().strip() == "cafef00d"

    calls = _gh_calls(calls_log)
    edit_calls = [c for c in calls if c[:2] == ["pr", "edit"]]
    assert edit_calls == [["pr", "edit", "42", "-R", "acme/widgets", "--add-reviewer", "@copilot"]]


def test_request_copilot_pr_edit_failure_gives_unavailable(tmp_path):
    paths = _paths(tmp_path)
    _write_copilot_prereqs(paths, pushed_head="cafef00d")
    bin_dir, _ = install_fake_gh(
        tmp_path,
        {
            ("api",): [(0, "", "")],
            ("pr", "edit"): [(1, "", "error: not permitted\n")],
        },
    )
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    result = _run_sh_env(
        _merge_gh.request_copilot_cmd(paths, str(tmp_path), rerequest=False), str(tmp_path), env
    )

    assert result.returncode == 0
    assert not Path(paths.halt).exists()
    assert Path(paths.copilot_reviewer).read_text().strip() == "unavailable"
    assert not Path(paths.copilot_requested_head).exists()
    assert "not permitted" in Path(paths.copilot_reviewer_log).read_text()
    assert result.stderr.strip() != ""


def test_gh_calls_other_than_api_carry_dash_r(tmp_path):
    paths = _paths(tmp_path)
    _write_pr_prereqs(paths)
    bin_dir, calls_log = install_fake_gh(
        tmp_path,
        {
            ("pr", "list"): [(0, "42", "")],
            ("pr", "view"): [(0, "main", ""), (0, _pr_json(is_draft=False), ""), (0, "false", "")],
            ("api",): [(0, "", "")],
            ("pr", "edit"): [(0, "", "")],
        },
    )
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    r1 = _run_sh_env(_merge_gh.pr_open_or_reuse_cmd(paths, str(tmp_path)), str(tmp_path), env)
    assert r1.returncode == 0 and not Path(paths.halt).exists()

    r2 = _run_sh_env(_merge_gh.pr_not_draft_cmd(paths, str(tmp_path)), str(tmp_path), env)
    assert r2.returncode == 0 and not Path(paths.halt).exists()

    Path(paths.pushed_head).write_text("newhead\n")
    r3 = _run_sh_env(
        _merge_gh.request_copilot_cmd(paths, str(tmp_path), rerequest=False), str(tmp_path), env
    )
    assert r3.returncode == 0

    calls = _gh_calls(calls_log)
    assert calls, "expected at least one recorded gh call"
    for call in calls:
        if call[0] != "api":
            assert "-R" in call, call


def test_pr_stages_follow_push_and_are_halting_with_when(tmp_path):
    mod = _import_merge_branch(cwd=tmp_path)
    names = [item.name for item in _top_stages(mod.config.items)]
    push_idx = names.index("push 1")
    assert names[push_idx + 1 : push_idx + 4] == [
        "open or reuse pr 1",
        "pr is not draft 1",
        "request copilot review 1",
    ]

    stages = {s.name: s for s in _top_stages(mod.config.items)}
    for name in ("open or reuse pr 1", "pr is not draft 1", "request copilot review 1"):
        assert isinstance(stages[name].impl, _merge_state.HaltOnFailure), name
        assert stages[name].when is not None, name


# ---------------------------------------------------------------------------
# WaitForChecks (step 11)
# ---------------------------------------------------------------------------


class _FakeClock:
    """Deterministic clock/sleep pair: ``sleep`` advances the clock instead of
    waiting for real time, so a WaitForChecks poll loop never actually sleeps.
    """

    def __init__(self) -> None:
        self.t = 0.0

    def clock(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


def _write_wait_prereqs(
    paths: _merge_state.MergePaths,
    *,
    repo: str = "acme/widgets",
    pr: str = "42",
    branch: str = "feature-x",
    base: str = "main",
    pushed_head: str = "deadbeef",
) -> None:
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.repo).write_text(repo + "\n")
    Path(paths.pr).write_text(pr + "\n")
    Path(paths.branch).write_text(branch + "\n")
    Path(paths.base).write_text(base + "\n")
    Path(paths.pushed_head).write_text(pushed_head + "\n")


def _wait_stage(
    paths: _merge_state.MergePaths,
    project_dir: str,
    n: int,
    fc: _FakeClock,
    *,
    on_red: str = "halt",
) -> _merge_gh.WaitForChecks:
    return _merge_gh.WaitForChecks(
        paths=paths,
        stage=f"wait for checks {n}",
        n=n,
        out_name=paths.checks(n),
        project_dir=project_dir,
        on_red=on_red,
        sleep=fc.sleep,
        clock=fc.clock,
    )


def _chk(name: str, bucket: str, *, workflow: str = "", link: str = "", started: str = "", completed: str = "") -> dict:
    return {
        "name": name,
        "workflow": workflow,
        "state": "COMPLETED" if bucket in ("pass", "fail", "skipping") else "IN_PROGRESS",
        "bucket": bucket,
        "link": link,
        "startedAt": started,
        "completedAt": completed,
    }


def _run_entry(database_id: int, workflow_name: str, status: str, conclusion: str | None, url: str = "https://x/run") -> dict:
    return {
        "databaseId": database_id,
        "workflowName": workflow_name,
        "status": status,
        "conclusion": conclusion,
        "url": url,
    }


def _record_notify(monkeypatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    async def fake_notify(heading: str, detail: str) -> None:
        calls.append((heading, detail))

    monkeypatch.setattr(_merge_gh, "notify", fake_notify)
    return calls


# ---------------------------------------------------------------------------
# _classify_checks / _fixable_workflow_names (review round 2 findings 2 & 3)
# ---------------------------------------------------------------------------


def test_classify_checks_dedupes_run_reporting_same_workflow_as_check():
    """A failing check and a failing run for the same workflow are two views
    of one failure, not two — the run must not add a second failing name.
    """
    checks = [_chk("build", "fail", workflow="CI")]
    runs = [_run_entry(1, "CI", "completed", "failure")]

    outcome, passed, failing = _merge_gh._classify_checks(checks, runs)

    assert outcome == "red"
    assert passed == 0
    assert failing == ["build"]


def test_classify_checks_does_not_drop_run_whose_check_is_green():
    """A workflow whose captured check is green but whose run is red is a
    real, independent failure (review round 3 finding 1) and must still turn
    the outcome red and appear in ``failing`` — mirrors
    ``test_fixable_workflow_names_does_not_drop_run_whose_check_is_green``.
    """
    checks = [_chk("deploy-job", "pass", workflow="Deploy")]
    runs = [_run_entry(1, "Deploy", "completed", "failure")]

    outcome, passed, failing = _merge_gh._classify_checks(checks, runs)

    assert outcome == "red"
    assert passed == 1
    assert failing == ["Deploy"]


def test_classify_checks_still_counts_orphan_run_with_no_matching_check():
    checks: list[dict] = []
    runs = [_run_entry(7, "deploy", "completed", "failure")]

    outcome, passed, failing = _merge_gh._classify_checks(checks, runs)

    assert outcome == "red"
    assert passed == 0
    assert failing == ["deploy"]


def test_fixable_workflow_names_returns_none_for_orphan_run():
    checks = [_chk("build", "fail", workflow="CI")]
    runs = [_run_entry(1, "deploy", "completed", "failure")]

    assert _merge_gh._fixable_workflow_names(checks, runs) is None


def test_fixable_workflow_names_dedupes_run_matching_a_failing_check():
    checks = [_chk("build", "fail", workflow="CI")]
    runs = [_run_entry(1, "CI", "completed", "failure")]

    assert _merge_gh._fixable_workflow_names(checks, runs) == ["CI"]


def test_fixable_workflow_names_does_not_drop_run_whose_check_is_green():
    """A workflow whose captured check is green but whose run is red is a
    real, independent failure and must not be silently exempted as if it
    were the same failure as the (passing) check.
    """
    checks = [
        _chk("build", "fail", workflow="CI"),
        _chk("deploy-job", "pass", workflow="Deploy"),
    ]
    runs = [_run_entry(1, "Deploy", "completed", "failure")]

    assert _merge_gh._fixable_workflow_names(checks, runs) is None


@pytest.mark.asyncio
async def test_wait_for_checks_settles_green_after_no_checks_then_pending(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    _write_wait_prereqs(paths, pushed_head="deadbeef")
    notify_calls = _record_notify(monkeypatch)

    pending = json.dumps([_chk("build", "pending")])
    green = json.dumps([_chk("build", "pass", link="https://ci/build")])
    bin_dir, _ = install_fake_gh(
        tmp_path,
        {
            ("pr", "view"): [(0, json.dumps({"headRefOid": "deadbeef"}), "")],
            ("pr", "checks"): [
                (1, "", "no checks reported on this PR\n"),
                (1, "", "no checks reported on this PR\n"),
                (0, pending, ""),
                (0, green, ""),
            ],
            ("run", "list"): [(0, "[]", "")],
        },
    )
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    fc = _FakeClock()
    stage = _wait_stage(paths, str(tmp_path), 1, fc)
    result = await stage.run(ctx_for(env=env))

    assert result.success is True
    assert not Path(paths.halt).exists()
    assert Path(paths.green_head).read_text().strip() == "deadbeef"

    data = json.loads(Path(paths.checks(1)).read_text())
    assert data["outcome"] == "green"
    assert data["check_names"] == ["build"]

    assert len(notify_calls) == 1
    heading, detail = notify_calls[0]
    assert heading == "PR checks finished"
    assert "PR #42" in detail


@pytest.mark.asyncio
async def test_wait_for_checks_exit_8_is_treated_as_pending(tmp_path):
    paths = _paths(tmp_path)
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    pending = json.dumps([_chk("build", "pending")])
    bin_dir, _ = install_fake_gh(tmp_path, {("pr", "checks"): [(8, pending, "")]})
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    fc = _FakeClock()
    stage = _wait_stage(paths, str(tmp_path), 1, fc)
    checks = await stage._poll_checks("acme/widgets", "42", str(tmp_path), env)

    assert checks == json.loads(pending)


@pytest.mark.asyncio
async def test_wait_for_checks_failed_bucket_halts_checks_red(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    _write_wait_prereqs(paths, pushed_head="deadbeef")
    notify_calls = _record_notify(monkeypatch)

    checks_json = json.dumps(
        [_chk("build", "pass"), _chk("lint", "fail", link="https://ci/lint")]
    )
    bin_dir, _ = install_fake_gh(
        tmp_path,
        {
            ("pr", "view"): [(0, json.dumps({"headRefOid": "deadbeef"}), "")],
            ("pr", "checks"): [(0, checks_json, "")],
            ("run", "list"): [(0, "[]", "")],
        },
    )
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    fc = _FakeClock()
    stage = _wait_stage(paths, str(tmp_path), 1, fc)
    result = await stage.run(ctx_for(env=env))

    assert result.success is True
    assert Path(paths.halt).is_file()
    reason = Path(paths.halt_reason).read_text()
    assert reason.splitlines()[0] == "KIND: checks_red"
    assert "lint" in reason
    assert not Path(paths.green_head).exists()

    data = json.loads(Path(paths.checks(1)).read_text())
    assert data["outcome"] == "checks_red"
    assert len(notify_calls) == 1


@pytest.mark.asyncio
async def test_wait_for_checks_head_moved_mid_poll_halts_pr_head_moved(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    _write_wait_prereqs(paths, pushed_head="deadbeef")
    notify_calls = _record_notify(monkeypatch)

    pending = json.dumps([_chk("build", "pending")])
    bin_dir, _ = install_fake_gh(
        tmp_path,
        {
            ("pr", "view"): [
                (0, json.dumps({"headRefOid": "deadbeef"}), ""),  # head sync
                (0, json.dumps({"headRefOid": "deadbeef"}), ""),  # first fetch, unchanged
                (0, json.dumps({"headRefOid": "newhead00"}), ""),  # second fetch, moved
            ],
            ("pr", "checks"): [(0, pending, "")],
            ("run", "list"): [(0, "[]", "")],
        },
    )
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    fc = _FakeClock()
    stage = _wait_stage(paths, str(tmp_path), 1, fc)
    result = await stage.run(ctx_for(env=env))

    assert result.success is True
    reason = Path(paths.halt_reason).read_text()
    assert reason.splitlines()[0] == "KIND: pr_head_moved"
    assert not notify_calls


@pytest.mark.asyncio
async def test_wait_for_checks_no_checks_within_grace_halts_checks_none(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    _write_wait_prereqs(paths, pushed_head="deadbeef")
    notify_calls = _record_notify(monkeypatch)

    bin_dir, _ = install_fake_gh(
        tmp_path,
        {
            ("pr", "view"): [(0, json.dumps({"headRefOid": "deadbeef"}), "")],
            ("pr", "checks"): [(1, "", "no checks reported on this PR\n")],
        },
    )
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    fc = _FakeClock()
    stage = _wait_stage(paths, str(tmp_path), 1, fc)
    result = await stage.run(ctx_for(env=env))

    assert result.success is True
    reason = Path(paths.halt_reason).read_text()
    assert reason.splitlines()[0] == "KIND: checks_none"
    assert not notify_calls
    assert fc.t >= _merge_gh.NO_CHECKS_GRACE


@pytest.mark.asyncio
async def test_wait_for_checks_timeout_halts_checks_timeout(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    _write_wait_prereqs(paths, pushed_head="deadbeef")
    notify_calls = _record_notify(monkeypatch)
    monkeypatch.setattr(
        _ci_estimate,
        "poll_plan",
        lambda estimate, elapsed=0.0: PollPlan(pre_sleep=0.0, interval=1.0, timeout=2.0),
    )

    pending = json.dumps([_chk("build", "pending")])
    bin_dir, _ = install_fake_gh(
        tmp_path,
        {
            ("pr", "view"): [(0, json.dumps({"headRefOid": "deadbeef"}), "")],
            ("pr", "checks"): [(0, pending, "")],
            ("run", "list"): [(0, "[]", "")],
        },
    )
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    fc = _FakeClock()
    stage = _wait_stage(paths, str(tmp_path), 1, fc)
    result = await stage.run(ctx_for(env=env))

    assert result.success is True
    reason = Path(paths.halt_reason).read_text()
    assert reason.splitlines()[0] == "KIND: checks_timeout"
    assert len(notify_calls) == 1

    data = json.loads(Path(paths.checks(1)).read_text())
    assert data["plan"]["timeout"] == 2.0


@pytest.mark.asyncio
async def test_poll_until_settled_new_check_resets_stability_window():
    """The generic core (also reused by step 18's ``WatchBaseBuild``): a
    changed stability key restarts the settle window even when ``is_settled``
    was already true, and the changed state still comes back as the final one.
    """
    fc = _FakeClock()
    states = [
        {"names": frozenset({"a"})},
        {"names": frozenset({"a"})},
        {"names": frozenset({"a", "b"})},
        {"names": frozenset({"a", "b"})},
    ]
    calls = iter(states)

    async def fetch():
        return next(calls)

    outcome, state = await _merge_gh._poll_until_settled(
        fetch=fetch,
        is_settled=lambda s: True,
        stability_key=lambda s: s["names"],
        head_moved=None,
        clock=fc.clock,
        sleep=fc.sleep,
        interval=30.0,
        timeout=90.0,
        settle_window=90.0,
    )

    assert outcome == "timeout"
    assert state["names"] == frozenset({"a", "b"})


@pytest.mark.asyncio
async def test_wait_for_checks_waits_for_in_progress_run_to_complete(tmp_path):
    paths = _paths(tmp_path)
    _write_wait_prereqs(paths, pushed_head="deadbeef")

    green_checks = json.dumps([_chk("build", "pass")])
    in_progress_run = json.dumps([_run_entry(1, "deploy", "in_progress", None)])
    completed_run = json.dumps([_run_entry(1, "deploy", "completed", "success")])
    bin_dir, calls_log = install_fake_gh(
        tmp_path,
        {
            ("pr", "view"): [(0, json.dumps({"headRefOid": "deadbeef"}), "")],
            ("pr", "checks"): [(0, green_checks, "")],
            ("run", "list"): [
                (0, in_progress_run, ""),
                (0, in_progress_run, ""),
                (0, in_progress_run, ""),
                (0, in_progress_run, ""),
                (0, completed_run, ""),
            ],
        },
    )
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    fc = _FakeClock()
    stage = _wait_stage(paths, str(tmp_path), 1, fc)
    result = await stage.run(ctx_for(env=env))

    assert result.success is True
    assert not Path(paths.halt).exists()
    data = json.loads(Path(paths.checks(1)).read_text())
    assert data["outcome"] == "green"

    run_list_calls = [c for c in _gh_calls(calls_log) if c[:2] == ["run", "list"]]
    assert len(run_list_calls) >= 5


@pytest.mark.asyncio
async def test_wait_for_checks_completed_failure_run_with_no_check_gives_red(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    _write_wait_prereqs(paths, pushed_head="deadbeef")
    notify_calls = _record_notify(monkeypatch)

    failed_run = json.dumps([_run_entry(7, "deploy", "completed", "failure")])
    bin_dir, _ = install_fake_gh(
        tmp_path,
        {
            ("pr", "view"): [(0, json.dumps({"headRefOid": "deadbeef"}), "")],
            ("pr", "checks"): [(0, "[]", "")],
            ("run", "list"): [(0, failed_run, "")],
        },
    )
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    fc = _FakeClock()
    stage = _wait_stage(paths, str(tmp_path), 1, fc)
    result = await stage.run(ctx_for(env=env))

    assert result.success is True
    reason = Path(paths.halt_reason).read_text()
    assert reason.splitlines()[0] == "KIND: checks_red"
    assert "deploy" in reason
    assert len(notify_calls) == 1

    data = json.loads(Path(paths.checks(1)).read_text())
    assert data["outcome"] == "checks_red"
    assert data["checks"] == []


@pytest.mark.asyncio
async def test_wait_for_checks_ignores_failed_copilot_review_run(tmp_path):
    paths = _paths(tmp_path)
    _write_wait_prereqs(paths, pushed_head="deadbeef")

    green_checks = json.dumps([_chk("build", "pass")])
    runs = json.dumps([
        _run_entry(1, "build", "completed", "success"),
        # The Copilot code review runs as a "dynamic" Actions run and fails when out of quota.
        {**_run_entry(2, "Copilot", "completed", "failure"), "event": "dynamic"},
    ])
    bin_dir, _ = install_fake_gh(
        tmp_path,
        {
            ("pr", "view"): [(0, json.dumps({"headRefOid": "deadbeef"}), "")],
            ("pr", "checks"): [(0, green_checks, "")],
            ("run", "list"): [(0, runs, "")],
        },
    )
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    fc = _FakeClock()
    stage = _wait_stage(paths, str(tmp_path), 1, fc)
    result = await stage.run(ctx_for(env=env))

    assert result.success is True
    assert not Path(paths.halt).exists()
    data = json.loads(Path(paths.checks(1)).read_text())
    assert data["outcome"] == "green"


@pytest.mark.asyncio
async def test_wait_for_checks_gh_exit_4_halts_unexpected_failure(tmp_path):
    paths = _paths(tmp_path)
    _write_wait_prereqs(paths, pushed_head="deadbeef")

    bin_dir, _ = install_fake_gh(tmp_path, {("pr", "view"): [(4, "", "gh: something broke\n")]})
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    fc = _FakeClock()
    stage = _wait_stage(paths, str(tmp_path), 1, fc)
    result = await stage.run(ctx_for(env=env))

    assert result.success is True
    reason = Path(paths.halt_reason).read_text()
    assert reason.splitlines()[0] == "KIND: unexpected_failure"
    assert "wait for checks 1" in reason


def test_wait_for_checks_stage_follows_request_copilot_review_and_has_backstop_timeout(tmp_path):
    mod = _import_merge_branch(cwd=tmp_path)
    names = [item.name for item in _top_stages(mod.config.items)]
    idx = names.index("request copilot review 1")
    assert names[idx + 1] == "wait for checks 1"

    stages = {s.name: s for s in _top_stages(mod.config.items)}
    stage = stages["wait for checks 1"]
    assert isinstance(stage.impl, _merge_gh.WaitForChecks)
    expected_timeout = (
        _merge_gh.HEAD_SYNC_TIMEOUT + _merge_gh.NO_CHECKS_GRACE + 3600 + _merge_gh.SETTLE_WINDOW + 3 * 30 + 120
    )
    assert stage.timeout == expected_timeout


# ---------------------------------------------------------------------------
# WaitForChecks(on_red="fix") — hand red PR checks to check_and_fix_ci (step 13)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_for_checks_on_red_fix_hands_off_fixable_failure(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    _write_wait_prereqs(paths, pushed_head="deadbeef")
    notify_calls = _record_notify(monkeypatch)

    checks_json = json.dumps([_chk("build", "fail", workflow="CI")])
    bin_dir, calls_log = install_fake_gh(
        tmp_path,
        {
            ("pr", "view"): [(0, json.dumps({"headRefOid": "deadbeef"}), "")],
            ("pr", "checks"): [(0, checks_json, "")],
            ("run", "list"): [(0, "[]", "")],
            ("workflow", "list"): [
                (0, json.dumps([{"name": "CI", "path": ".github/workflows/ci.yml"}]), "")
            ],
        },
    )
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    fc = _FakeClock()
    stage = _wait_stage(paths, str(tmp_path), 1, fc, on_red="fix")
    result = await stage.run(ctx_for(env=env))

    assert result.success is True
    assert not Path(paths.halt).exists()
    assert Path(paths.ci_red(1)).is_file()
    assert Path(paths.ci_fix_workflows(1)).read_text().strip() == "ci.yml"

    data = json.loads(Path(paths.checks(1)).read_text())
    assert data["outcome"] == "ci_fix"

    assert len(notify_calls) == 1
    heading, detail = notify_calls[0]
    assert heading == "PR checks finished"
    assert "handing to check_and_fix_ci" in detail

    calls = _gh_calls(calls_log)
    assert [c for c in calls if c[:2] == ["workflow", "list"]]


@pytest.mark.asyncio
async def test_wait_for_checks_on_red_fix_dedupes_and_orders_workflow_files(tmp_path):
    paths = _paths(tmp_path)
    _write_wait_prereqs(paths, pushed_head="deadbeef")

    checks_json = json.dumps(
        [
            _chk("build (linux)", "fail", workflow="CI"),
            _chk("lint", "fail", workflow="Lint"),
            _chk("build (mac)", "fail", workflow="CI"),
        ]
    )
    bin_dir, _ = install_fake_gh(
        tmp_path,
        {
            ("pr", "view"): [(0, json.dumps({"headRefOid": "deadbeef"}), "")],
            ("pr", "checks"): [(0, checks_json, "")],
            ("run", "list"): [(0, "[]", "")],
            ("workflow", "list"): [
                (
                    0,
                    json.dumps(
                        [
                            {"name": "CI", "path": ".github/workflows/ci.yml"},
                            {"name": "Lint", "path": ".github/workflows/lint.yml"},
                        ]
                    ),
                    "",
                )
            ],
        },
    )
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    fc = _FakeClock()
    stage = _wait_stage(paths, str(tmp_path), 1, fc, on_red="fix")
    result = await stage.run(ctx_for(env=env))

    assert result.success is True
    assert Path(paths.ci_fix_workflows(1)).read_text().splitlines() == ["ci.yml", "lint.yml"]


@pytest.mark.asyncio
async def test_wait_for_checks_on_red_fix_external_app_failure_halts_checks_red(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    _write_wait_prereqs(paths, pushed_head="deadbeef")
    _record_notify(monkeypatch)

    checks_json = json.dumps([_chk("codecov/patch", "fail", workflow="")])
    bin_dir, calls_log = install_fake_gh(
        tmp_path,
        {
            ("pr", "view"): [(0, json.dumps({"headRefOid": "deadbeef"}), "")],
            ("pr", "checks"): [(0, checks_json, "")],
            ("run", "list"): [(0, "[]", "")],
        },
    )
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    fc = _FakeClock()
    stage = _wait_stage(paths, str(tmp_path), 1, fc, on_red="fix")
    result = await stage.run(ctx_for(env=env))

    assert result.success is True
    assert Path(paths.halt).is_file()
    assert Path(paths.halt_reason).read_text().splitlines()[0] == "KIND: checks_red"
    assert not Path(paths.ci_red(1)).exists()

    calls = _gh_calls(calls_log)
    assert not [c for c in calls if c[:2] == ["workflow", "list"]]


@pytest.mark.asyncio
async def test_wait_for_checks_on_red_fix_cancel_bucket_halts_checks_red(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    _write_wait_prereqs(paths, pushed_head="deadbeef")
    _record_notify(monkeypatch)

    checks_json = json.dumps([_chk("build", "cancel", workflow="CI")])
    bin_dir, _ = install_fake_gh(
        tmp_path,
        {
            ("pr", "view"): [(0, json.dumps({"headRefOid": "deadbeef"}), "")],
            ("pr", "checks"): [(0, checks_json, "")],
            ("run", "list"): [(0, "[]", "")],
        },
    )
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    fc = _FakeClock()
    stage = _wait_stage(paths, str(tmp_path), 1, fc, on_red="fix")
    result = await stage.run(ctx_for(env=env))

    assert result.success is True
    assert Path(paths.halt).is_file()
    assert Path(paths.halt_reason).read_text().splitlines()[0] == "KIND: checks_red"
    assert not Path(paths.ci_red(1)).exists()


@pytest.mark.asyncio
async def test_wait_for_checks_on_red_halt_still_halts_on_a_fixable_failure(tmp_path, monkeypatch):
    """``on_red="halt"`` (the second wait, after a CI fix) never hands off,
    even for a failure that would otherwise be fixable.
    """
    paths = _paths(tmp_path)
    _write_wait_prereqs(paths, pushed_head="deadbeef")
    _record_notify(monkeypatch)

    checks_json = json.dumps([_chk("build", "fail", workflow="CI")])
    bin_dir, calls_log = install_fake_gh(
        tmp_path,
        {
            ("pr", "view"): [(0, json.dumps({"headRefOid": "deadbeef"}), "")],
            ("pr", "checks"): [(0, checks_json, "")],
            ("run", "list"): [(0, "[]", "")],
        },
    )
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    fc = _FakeClock()
    stage = _wait_stage(paths, str(tmp_path), 1, fc, on_red="halt")
    result = await stage.run(ctx_for(env=env))

    assert result.success is True
    assert Path(paths.halt).is_file()
    assert Path(paths.halt_reason).read_text().splitlines()[0] == "KIND: checks_red"
    assert not Path(paths.ci_red(1)).exists()

    calls = _gh_calls(calls_log)
    assert not [c for c in calls if c[:2] == ["workflow", "list"]]


# ---------------------------------------------------------------------------
# CiFixChild (step 13)
# ---------------------------------------------------------------------------


def _ci_fix_stage(
    tmp_path: Path,
    *,
    budget: float = 15.0,
    child_cmd: str | None = None,
    n: int = 1,
) -> tuple[_merge_state.MergePaths, _merge_gh.CiFixChild]:
    paths = _paths(tmp_path)
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    pipeline = Pipeline("merge_branch_test").budget(max_cost_usd=budget)
    stage = _merge_gh.CiFixChild(
        paths=paths,
        stage=f"fix ci {n}",
        n=n,
        project_dir=str(tmp_path),
        pipeline=pipeline,
        test_cmd="true",
        child_cmd=child_cmd,
    )
    return paths, stage


@pytest.mark.asyncio
async def test_ci_fix_child_success_writes_passed_outcome_no_halt(tmp_path):
    paths, stage = _ci_fix_stage(tmp_path, child_cmd="sh -c 'exit 0'")

    result = await stage.run(ctx_for())

    assert result.success is True
    assert not Path(paths.halt).exists()
    data = json.loads(Path(paths.ci_fix(1)).read_text())
    assert data["outcome"] == "passed"


def _write_child_journal(tmp_path: Path, *, failed_stage: str, error: str) -> Path:
    run_dir = tmp_path / "check_and_fix_ci.runs" / "1"
    (run_dir / "stages").mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"success": False, "failed_stage": failed_stage}))
    (run_dir / "stages" / "0001-stage_x-a1.json").write_text(
        json.dumps({"node_id": f"stage:{failed_stage}", "attempt": 1, "error": error})
    )
    return run_dir


@pytest.mark.asyncio
async def test_ci_fix_child_failure_halts_ci_fix_failed_naming_stage(tmp_path):
    run_dir = _write_child_journal(tmp_path, failed_stage="commit push ci fix", error="pytest failed: 2 tests")
    paths, stage = _ci_fix_stage(tmp_path, child_cmd="sh -c 'exit 3'")

    result = await stage.run(ctx_for())

    assert result.success is True
    assert Path(paths.halt).is_file()
    reason = Path(paths.halt_reason).read_text()
    assert reason.splitlines()[0] == "KIND: ci_fix_failed"
    assert "commit push ci fix" in reason
    assert "pytest failed" in reason

    data = json.loads(Path(paths.ci_fix(1)).read_text())
    assert data["outcome"] == "failed"
    assert data["journal"] == str(run_dir)


@pytest.mark.asyncio
async def test_ci_fix_child_failure_with_no_journal_still_halts(tmp_path):
    paths, stage = _ci_fix_stage(tmp_path, child_cmd="sh -c 'exit 1'")

    result = await stage.run(ctx_for())

    assert result.success is True
    assert Path(paths.halt).is_file()
    assert Path(paths.halt_reason).read_text().splitlines()[0] == "KIND: ci_fix_failed"


def test_ci_fix_child_builds_cmd_with_non_interactive_workflows_and_remaining_budget(tmp_path):
    paths, stage = _ci_fix_stage(tmp_path, budget=15.0)
    Path(paths.ci_fix_workflows(1)).write_text("ci.yml\nrelease.yml\n")

    ctx = ctx_for()
    ctx.usage_tracker.add(UsageRecord(stage_name="x", total_cost_usd=4.0))

    cmd = stage._build_cmd(ctx)

    assert "--non-interactive" in cmd
    assert "workflow=ci.yml,release.yml" in cmd
    assert "--budget 11.0" in cmd


def test_ci_fix_child_remaining_budget_floors_at_minimum(tmp_path):
    paths, stage = _ci_fix_stage(tmp_path, budget=15.0)
    Path(paths.ci_fix_workflows(1)).write_text("ci.yml\n")

    ctx = ctx_for()
    ctx.usage_tracker.add(UsageRecord(stage_name="x", total_cost_usd=20.0))

    cmd = stage._build_cmd(ctx)

    assert "--budget 1.0" in cmd


# ---------------------------------------------------------------------------
# sync_after_ci_fix_cmd (step 13)
# ---------------------------------------------------------------------------


def _sync_prereqs(paths: _merge_state.MergePaths, repo: RepoWithRemote, *, branch: str = "main") -> str:
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.branch).write_text(branch + "\n")
    prev_head = repo.repo.head.commit.hexsha
    Path(paths.pushed_head).write_text(prev_head + "\n")
    return prev_head


def test_sync_after_ci_fix_in_sync_updates_remote_and_pushed_head(tmp_path):
    repo = _repo(tmp_path)
    paths = _paths(tmp_path)
    _sync_prereqs(paths, repo)

    (Path(repo.path) / "fix.txt").write_text("fix\n")
    repo.repo.index.add(["fix.txt"])
    repo.repo.index.commit("fix: ci")
    repo.repo.git.push("origin", "main")

    result = _run_sh(_merge_gh.sync_after_ci_fix_cmd(paths, repo.path, 1), repo.path)

    assert result.returncode == 0, result.stderr
    assert not Path(paths.halt).exists()
    new_head = repo.repo.head.commit.hexsha
    assert Path(paths.remote_head).read_text().strip() == new_head
    assert Path(paths.pushed_head).read_text().strip() == new_head
    assert "fix: ci" in Path(paths.ci_fix_commits(1)).read_text()


def test_sync_after_ci_fix_unpushed_commit_halts(tmp_path):
    repo = _repo(tmp_path)
    paths = _paths(tmp_path)
    _sync_prereqs(paths, repo)

    (Path(repo.path) / "fix.txt").write_text("fix\n")
    repo.repo.index.add(["fix.txt"])
    repo.repo.index.commit("fix: ci")
    # deliberately not pushed

    result = _run_sh(_merge_gh.sync_after_ci_fix_cmd(paths, repo.path, 1), repo.path)

    assert result.returncode == 0, result.stderr
    assert Path(paths.halt).is_file()
    assert Path(paths.halt_reason).read_text().splitlines()[0] == "KIND: ci_fix_unpushed"


def test_sync_after_ci_fix_dirty_worktree_halts(tmp_path):
    repo = _repo(tmp_path)
    paths = _paths(tmp_path)
    _sync_prereqs(paths, repo)

    (Path(repo.path) / "untracked.txt").write_text("oops\n")

    result = _run_sh(_merge_gh.sync_after_ci_fix_cmd(paths, repo.path, 1), repo.path)

    assert result.returncode == 0, result.stderr
    assert Path(paths.halt).is_file()
    assert Path(paths.halt_reason).read_text().splitlines()[0] == "KIND: ci_fix_unpushed"


def test_sync_after_ci_fix_attribution_in_commit_message_halts(tmp_path):
    repo = _repo(tmp_path)
    paths = _paths(tmp_path)
    _sync_prereqs(paths, repo)

    (Path(repo.path) / "fix.txt").write_text("fix\n")
    repo.repo.index.add(["fix.txt"])
    repo.repo.index.commit("fix: ci\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
    repo.repo.git.push("origin", "main")

    result = _run_sh(_merge_gh.sync_after_ci_fix_cmd(paths, repo.path, 1), repo.path)

    assert result.returncode == 0, result.stderr
    assert Path(paths.halt).is_file()
    assert Path(paths.halt_reason).read_text().splitlines()[0] == "KIND: attribution_found"


def test_sync_after_ci_fix_attribution_in_diff_halts(tmp_path):
    repo = _repo(tmp_path)
    paths = _paths(tmp_path)
    _sync_prereqs(paths, repo)

    (Path(repo.path) / "fix.txt").write_text("# Generated with [Claude Code]\n")
    repo.repo.index.add(["fix.txt"])
    repo.repo.index.commit("fix: ci")
    repo.repo.git.push("origin", "main")

    result = _run_sh(_merge_gh.sync_after_ci_fix_cmd(paths, repo.path, 1), repo.path)

    assert result.returncode == 0, result.stderr
    assert Path(paths.halt).is_file()
    assert Path(paths.halt_reason).read_text().splitlines()[0] == "KIND: attribution_found"


# ---------------------------------------------------------------------------
# re-request copilot review (step 13)
# ---------------------------------------------------------------------------


def test_rerequest_copilot_unavailable_makes_no_gh_call(tmp_path):
    paths = _paths(tmp_path)
    _write_copilot_prereqs(paths, pushed_head="cafef00d")
    Path(paths.copilot_reviewer).write_text("unavailable\n")
    bin_dir, calls_log = install_fake_gh(tmp_path, {})
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    result = _run_sh_env(
        _merge_gh.request_copilot_cmd(paths, str(tmp_path), rerequest=True), str(tmp_path), env
    )

    assert result.returncode == 0, result.stderr
    assert not Path(paths.halt).exists()
    assert not _gh_calls(calls_log)


def test_rerequest_copilot_unchanged_head_makes_no_gh_call(tmp_path):
    paths = _paths(tmp_path)
    _write_copilot_prereqs(paths, pushed_head="cafef00d")
    Path(paths.copilot_reviewer).write_text("requested\n")
    Path(paths.copilot_requested_head).write_text("cafef00d\n")
    bin_dir, calls_log = install_fake_gh(tmp_path, {})
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    result = _run_sh_env(
        _merge_gh.request_copilot_cmd(paths, str(tmp_path), rerequest=True), str(tmp_path), env
    )

    assert result.returncode == 0, result.stderr
    assert not Path(paths.halt).exists()
    assert not _gh_calls(calls_log)


def test_rerequest_copilot_moved_head_calls_pr_edit_and_rewrites_state(tmp_path):
    paths = _paths(tmp_path)
    _write_copilot_prereqs(paths, pushed_head="newhead00")
    Path(paths.copilot_reviewer).write_text("requested\n")
    Path(paths.copilot_requested_head).write_text("cafef00d\n")
    bin_dir, calls_log = install_fake_gh(tmp_path, {("pr", "edit"): [(0, "", "")]})
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    result = _run_sh_env(
        _merge_gh.request_copilot_cmd(paths, str(tmp_path), rerequest=True), str(tmp_path), env
    )

    assert result.returncode == 0, result.stderr
    assert not Path(paths.halt).exists()
    assert Path(paths.copilot_reviewer).read_text().strip() == "requested"
    assert Path(paths.copilot_requested_head).read_text().strip() == "newhead00"
    assert Path(paths.copilot_requested_at).is_file()

    calls = _gh_calls(calls_log)
    assert calls == [["pr", "edit", "42", "-R", "acme/widgets", "--add-reviewer", "@copilot"]]


def test_rerequest_copilot_pr_edit_failure_halts(tmp_path):
    paths = _paths(tmp_path)
    _write_copilot_prereqs(paths, pushed_head="newhead00")
    Path(paths.copilot_reviewer).write_text("requested\n")
    Path(paths.copilot_requested_head).write_text("cafef00d\n")
    bin_dir, _ = install_fake_gh(tmp_path, {("pr", "edit"): [(1, "", "error: not permitted\n")]})
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    result = _run_sh_env(
        _merge_gh.request_copilot_cmd(paths, str(tmp_path), rerequest=True), str(tmp_path), env
    )

    assert result.returncode == 0, result.stderr
    assert Path(paths.halt).is_file()
    assert Path(paths.halt_reason).read_text().splitlines()[0] == "KIND: copilot_rerequest_failed"
    assert "not permitted" in Path(paths.halt_reason).read_text()


# ---------------------------------------------------------------------------
# The four hand-off stages (step 13) — structure
# ---------------------------------------------------------------------------


def test_ci_fix_stages_follow_wait_for_checks_in_order(tmp_path):
    mod = _import_merge_branch(cwd=tmp_path)
    names = [item.name for item in _top_stages(mod.config.items)]
    idx = names.index("wait for checks 1")
    assert names[idx + 1 : idx + 5] == [
        "fix ci 1",
        "sync after ci fix 1",
        "re-request copilot review 1",
        "wait for checks after ci fix 1",
    ]


def test_ci_fix_stages_are_gated_on_ci_red(tmp_path):
    mod = _import_merge_branch(cwd=tmp_path)
    stages = {s.name: s for s in _top_stages(mod.config.items)}

    ci_fix_names = ["fix ci 1", "sync after ci fix 1", "re-request copilot review 1", "wait for checks after ci fix 1"]
    for name in ci_fix_names:
        stage = stages[name]
        assert stage.when is not None, name
        assert stage.when(ctx_for()) is False, name

    Path(mod.PATHS.root).mkdir(parents=True, exist_ok=True)
    Path(mod.PATHS.ci_red(1)).touch()
    for name in ci_fix_names:
        assert stages[name].when(ctx_for()) is True, name


def test_fix_ci_stage_has_backstop_timeout_above_ci_fix_timeout(tmp_path):
    mod = _import_merge_branch(cwd=tmp_path)
    stages = {s.name: s for s in _top_stages(mod.config.items)}
    stage = stages["fix ci 1"]
    assert isinstance(stage.impl, _merge_gh.CiFixChild)
    assert stage.timeout > _merge_gh.CI_FIX_TIMEOUT


def test_wait_for_checks_after_ci_fix_uses_on_red_halt_and_after_fix_file(tmp_path):
    mod = _import_merge_branch(cwd=tmp_path)
    stages = {s.name: s for s in _top_stages(mod.config.items)}
    stage = stages["wait for checks after ci fix 1"]
    assert isinstance(stage.impl, _merge_gh.WaitForChecks)
    assert stage.impl._on_red == "halt"
    assert stage.impl._out_name == mod.PATHS.checks_after_fix(1)


# ---------------------------------------------------------------------------
# ## CI fix summary section (step 13)
# ---------------------------------------------------------------------------


def test_render_summary_ci_fix_section_absent_without_ci_red(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.slug).write_text("widgets")
    Path(paths.merged).write_text("deadbeef\n")

    body = _merge_state.render_summary(
        paths, rerun_cmd="norn run merge_branch --non-interactive", max_rounds=1, project_dir=str(tmp_path)
    )

    assert "## CI fix" not in body


def test_render_summary_ci_fix_section_renders_workflows_outcome_and_commits(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.slug).write_text("widgets")
    Path(paths.merged).write_text("deadbeef\n")
    Path(paths.ci_red(1)).touch()
    Path(paths.ci_fix_workflows(1)).write_text("ci.yml\n")
    Path(paths.ci_fix(1)).write_text(
        json.dumps({"outcome": "passed", "journal": "/tmp/check_and_fix_ci.runs/1"})
    )
    Path(paths.ci_fix_commits(1)).write_text("abc1234 fix(ci): auto-fix from check_and_fix_ci\n")

    body = _merge_state.render_summary(
        paths, rerun_cmd="norn run merge_branch --non-interactive", max_rounds=1, project_dir=str(tmp_path)
    )

    assert "## CI fix" in body
    assert "Workflows: ci.yml | Child: passed | Journal: /tmp/check_and_fix_ci.runs/1" in body
    assert "Commits: abc1234 fix(ci): auto-fix from check_and_fix_ci" in body


# ---------------------------------------------------------------------------
# WaitForCopilotReview (step 14)
# ---------------------------------------------------------------------------


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _copilot_wait_stage(
    paths: _merge_state.MergePaths, project_dir: str, n: int, fc: _FakeClock
) -> _merge_gh.WaitForCopilotReview:
    return _merge_gh.WaitForCopilotReview(
        paths=paths,
        stage=f"wait for copilot review {n}",
        n=n,
        project_dir=project_dir,
        sleep=fc.sleep,
        clock=fc.clock,
    )


@pytest.mark.asyncio
async def test_wait_for_copilot_review_unavailable_is_noop(tmp_path):
    paths = _paths(tmp_path)
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.copilot_reviewer).write_text("unavailable\n")
    bin_dir, calls_log = install_fake_gh(tmp_path, {})
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    fc = _FakeClock()
    stage = _copilot_wait_stage(paths, str(tmp_path), 1, fc)
    result = await stage.run(ctx_for(env=env))

    assert result.success is True
    assert not Path(paths.halt).exists()
    assert _gh_calls(calls_log) == []


@pytest.mark.asyncio
async def test_wait_for_copilot_review_older_commit_id_keeps_waiting_then_times_out(tmp_path):
    paths = _paths(tmp_path)
    _write_wait_prereqs(paths, pushed_head="newhead00")
    Path(paths.copilot_reviewer).write_text("requested\n")
    Path(paths.copilot_requested_at).write_text(_iso_now() + "\n")
    older_review = [
        {
            "user": {"login": "copilot-pull-request-reviewer[bot]"},
            "commit_id": "oldhead00",
            "state": "COMMENTED",
            "body": "",
            "submitted_at": "2024-01-01T00:00:00Z",
        }
    ]
    bin_dir, calls_log = install_fake_gh(
        tmp_path,
        {("api", "repos/acme/widgets/pulls/42/reviews"): [(0, json.dumps([older_review]), "")]},
    )
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    fc = _FakeClock()
    stage = _copilot_wait_stage(paths, str(tmp_path), 1, fc)
    result = await stage.run(ctx_for(env=env))

    assert result.success is True
    assert Path(paths.halt).is_file()
    assert Path(paths.halt_reason).read_text().splitlines()[0] == "KIND: copilot_timeout"
    # An older commit_id never matches, however many times it's submitted --
    # the stage kept polling (more than one call) rather than halting on the
    # very first check.
    assert len(_gh_calls(calls_log)) > 1
    assert not Path(paths.copilot_review(1)).exists()


@pytest.mark.asyncio
async def test_wait_for_copilot_review_matching_commit_id_succeeds(tmp_path):
    paths = _paths(tmp_path)
    _write_wait_prereqs(paths, pushed_head="deadbeef")
    # The request stage writes no copilot-requested-at when it finds the review already there.
    Path(paths.copilot_reviewer).write_text("already-reviewed\n")
    review = [
        {
            "user": {"login": "Copilot"},
            "commit_id": "deadbeef",
            "state": "COMMENTED",
            "body": "looks fine",
            "submitted_at": "2024-01-01T00:05:00Z",
        }
    ]
    bin_dir, calls_log = install_fake_gh(
        tmp_path,
        {("api", "repos/acme/widgets/pulls/42/reviews"): [(0, json.dumps([review]), "")]},
    )
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    fc = _FakeClock()
    stage = _copilot_wait_stage(paths, str(tmp_path), 1, fc)
    result = await stage.run(ctx_for(env=env))

    assert result.success is True
    assert not Path(paths.halt).exists()
    # "already-reviewed" passes on the very first poll.
    assert len(_gh_calls(calls_log)) == 1
    data = json.loads(Path(paths.copilot_review(1)).read_text())
    assert data["commit_id"] == "deadbeef"


@pytest.mark.asyncio
async def test_wait_for_copilot_review_deadline_measured_from_requested_at_halts(tmp_path):
    paths = _paths(tmp_path)
    _write_wait_prereqs(paths, pushed_head="deadbeef")
    Path(paths.copilot_reviewer).write_text("requested\n")
    # Already past the deadline when this stage starts -- the CI wait and the
    # checks wait already burned the whole budget.
    old_requested_at = (
        datetime.now(timezone.utc) - timedelta(seconds=_merge_gh.COPILOT_REVIEW_TIMEOUT + 60)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    Path(paths.copilot_requested_at).write_text(old_requested_at + "\n")
    no_match = [
        {
            "user": {"login": "Copilot"},
            "commit_id": "someotherhead",
            "state": "COMMENTED",
            "body": "",
            "submitted_at": "2024-01-01T00:00:00Z",
        }
    ]
    bin_dir, calls_log = install_fake_gh(
        tmp_path,
        {("api", "repos/acme/widgets/pulls/42/reviews"): [(0, json.dumps([no_match]), "")]},
    )
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    fc = _FakeClock()
    stage = _copilot_wait_stage(paths, str(tmp_path), 1, fc)
    result = await stage.run(ctx_for(env=env))

    assert result.success is True
    assert Path(paths.halt).is_file()
    assert Path(paths.halt_reason).read_text().splitlines()[0] == "KIND: copilot_timeout"
    assert len(_gh_calls(calls_log)) == 1


# ---------------------------------------------------------------------------
# FetchPrReviews (step 14)
# ---------------------------------------------------------------------------


def _fetch_stage(paths: _merge_state.MergePaths, project_dir: str, n: int = 1) -> _merge_gh.FetchPrReviews:
    return _merge_gh.FetchPrReviews(paths=paths, stage=f"fetch pr reviews {n}", n=n, project_dir=project_dir)


def _write_fetch_prereqs(
    paths: _merge_state.MergePaths, *, repo: str = "acme/widgets", pr: str = "42", base: str = "main"
) -> None:
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.repo).write_text(repo + "\n")
    Path(paths.pr).write_text(pr + "\n")
    Path(paths.base).write_text(base + "\n")


def _reviews_routes(
    reviews: list[dict], inline_comments: list[dict], issue_comments: list[dict], review_decision: str | None
) -> dict:
    return {
        ("api", "repos/acme/widgets/pulls/42/reviews"): [(0, json.dumps([reviews]), "")],
        ("api", "repos/acme/widgets/pulls/42/comments"): [(0, json.dumps([inline_comments]), "")],
        ("api", "repos/acme/widgets/issues/42/comments"): [(0, json.dumps([issue_comments]), "")],
        ("pr", "view"): [(0, json.dumps({"reviewDecision": review_decision}), "")],
    }


def _feature_repo_with_diff(tmp_path: Path) -> RepoWithRemote:
    repo = _repo(tmp_path)
    repo.repo.git.checkout("-b", "feature-x")
    (Path(repo.path) / "f.txt").write_text("one\n")
    repo.repo.index.add(["f.txt"])
    repo.repo.index.commit("feature commit")
    repo.repo.git.push("origin", "feature-x")
    # A plain `git push` never updates the local `refs/remotes/origin/*`
    # ref; every real pipeline stage before `fetch pr reviews {n}` (`rebase
    # {n}`, `push {n}`, ...) does its own `git fetch origin`, so this fetch
    # here reproduces that -- FetchPrReviews itself never fetches.
    repo.repo.git.fetch("origin")
    return repo


@pytest.mark.asyncio
async def test_fetch_pr_reviews_renders_filters_marker_and_empty_and_writes_diff(tmp_path):
    repo = _feature_repo_with_diff(tmp_path)
    paths = _paths(tmp_path)
    _write_fetch_prereqs(paths)

    reviews = [
        {
            "id": 1,
            "user": {"login": "alice"},
            "state": "APPROVED",
            "body": "lgtm",
            "submitted_at": "2024-01-01T00:00:00Z",
        },
        {
            "id": 2,
            "user": {"login": "bob"},
            "state": "COMMENTED",
            "body": "   ",
            "submitted_at": "2024-01-01T00:01:00Z",
        },
    ]
    inline_comments = [
        {
            "id": 10,
            "user": {"login": "copilot-pull-request-reviewer[bot]"},
            "body": "consider X",
            "created_at": "2024-01-01T00:02:00Z",
            "updated_at": "2024-01-01T00:02:00Z",
            "path": "f.txt",
            "line": 1,
        },
    ]
    issue_comments = [
        {
            "id": 20,
            "user": {"login": "norn-bot"},
            "body": "<!-- norn:merge_branch findings round 1 -->\nold findings",
            "created_at": "2024-01-01T00:03:00Z",
            "updated_at": "2024-01-01T00:03:00Z",
        },
        {
            "id": 21,
            "user": {"login": "carol"},
            "body": "please fix the typo",
            "created_at": "2024-01-01T00:04:00Z",
            "updated_at": "2024-01-01T00:04:00Z",
        },
    ]
    bin_dir, _ = install_fake_gh(tmp_path, _reviews_routes(reviews, inline_comments, issue_comments, None))
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    stage = _fetch_stage(paths, repo.path)
    result = await stage.run(ctx_for(env=env))

    assert result.success is True
    assert not Path(paths.halt).exists()
    assert not Path(paths.no_new_reviews(1)).exists()
    assert not Path(paths.findings(1)).exists()

    body = Path(paths.pr_reviews(1)).read_text()
    assert "### alice (review, APPROVED)" in body
    assert "lgtm" in body
    assert "### copilot-pull-request-reviewer[bot] (inline, comment)" in body
    assert "f.txt:1" in body
    assert "consider X" in body
    assert "### carol (issue, comment)" in body
    assert "please fix the typo" in body

    # Dropped: the empty COMMENTED review and the pipeline's own marker.
    assert "bob" not in body
    assert "old findings" not in body
    assert "norn:merge_branch" not in body

    diff_text = Path(paths.branch_diff(1)).read_text()
    assert "f.txt" in diff_text

    assert Path(paths.review_decision(1)).read_text().strip() == ""

    seen = Path(paths.reviews_seen).read_text().splitlines()
    assert seen == [
        "review:1",
        "review:2",
        "inline:10@2024-01-01T00:02:00Z",
        "issue:20@2024-01-01T00:03:00Z",
        "issue:21@2024-01-01T00:04:00Z",
    ]


@pytest.mark.asyncio
async def test_fetch_pr_reviews_second_run_with_same_data_is_clear(tmp_path):
    repo = _feature_repo_with_diff(tmp_path)
    paths = _paths(tmp_path)
    _write_fetch_prereqs(paths)

    reviews = [
        {"id": 1, "user": {"login": "alice"}, "state": "APPROVED", "body": "lgtm", "submitted_at": "2024-01-01T00:00:00Z"}
    ]
    routes = _reviews_routes(reviews, [], [], "APPROVED")
    bin_dir, _ = install_fake_gh(tmp_path, routes)
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    stage = _fetch_stage(paths, repo.path)
    first = await stage.run(ctx_for(env=env))
    assert first.success is True
    assert not Path(paths.no_new_reviews(1)).exists()

    second = await stage.run(ctx_for(env=env))

    assert second.success is True
    assert Path(paths.no_new_reviews(1)).is_file()
    findings_text = Path(paths.findings(1)).read_text()
    assert findings_text.splitlines()[0] == "VERDICT: CLEAR"
    assert "PR: 42" in findings_text
    assert "Reviewed: 0 new comments" in findings_text
    assert "## Blocking" in findings_text
    assert "## Non-blocking" in findings_text
    assert "## Answered" in findings_text


@pytest.mark.asyncio
async def test_fetch_pr_reviews_seen_keys_ignore_timestamp_order(tmp_path):
    repo = _feature_repo_with_diff(tmp_path)
    paths = _paths(tmp_path)
    _write_fetch_prereqs(paths)

    first_review = {
        "id": 1, "user": {"login": "alice"}, "state": "APPROVED", "body": "lgtm",
        "submitted_at": "2024-01-05T00:00:00Z",
    }
    first_inline = {
        "id": 10, "user": {"login": "bob"}, "body": "nit", "path": "f.txt", "line": 1,
        "created_at": "2024-01-05T00:01:00Z", "updated_at": "2024-01-05T00:01:00Z",
    }
    first_issue = {
        "id": 21, "user": {"login": "carol"}, "body": "please fix",
        "created_at": "2024-01-05T00:02:00Z", "updated_at": "2024-01-05T00:02:00Z",
    }
    bin_dir1, _ = install_fake_gh(
        tmp_path / "gh1", _reviews_routes([first_review], [first_inline], [first_issue], None)
    )
    env1 = {"PATH": f"{bin_dir1}:{os.environ['PATH']}"}
    stage = _fetch_stage(paths, repo.path)
    first_result = await stage.run(ctx_for(env=env1))
    assert first_result.success is True

    # Second fetch: `first_review`/`first_inline` are already seen (dropped).
    # A brand-new inline comment with a `created_at` older than every entry
    # from the first fetch must still render -- there is no timestamp
    # watermark. The issue comment keeps its id but gets a new `updated_at`
    # (an edit) and must render again.
    older_inline = {
        "id": 11, "user": {"login": "dave"}, "body": "older but new", "path": "f.txt", "line": 2,
        "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-01-01T00:00:00Z",
    }
    edited_issue = {
        "id": 21, "user": {"login": "carol"}, "body": "please fix (edited)",
        "created_at": "2024-01-05T00:02:00Z", "updated_at": "2024-01-06T00:00:00Z",
    }
    bin_dir2, _ = install_fake_gh(
        tmp_path / "gh2",
        _reviews_routes([first_review], [first_inline, older_inline], [edited_issue], None),
    )
    env2 = {"PATH": f"{bin_dir2}:{os.environ['PATH']}"}
    second_result = await stage.run(ctx_for(env=env2))
    assert second_result.success is True

    body = Path(paths.pr_reviews(1)).read_text()
    assert "older but new" in body
    assert "please fix (edited)" in body
    assert "lgtm" not in body
    assert "nit" not in body
    # The unedited comment ("please fix") only ever appears as a substring of
    # the edited one ("please fix (edited)") -- it is not re-rendered on its
    # own, since only the id+updated_at key changed, not a brand-new id.
    assert body.count("please fix") == 1


# ---------------------------------------------------------------------------
# render_pr_reviews (pure) and the seen-key helper
# ---------------------------------------------------------------------------


def test_render_pr_reviews_empty_entries_is_empty_string():
    assert _merge_gh.render_pr_reviews([]) == ""


def test_render_pr_reviews_omits_path_line_when_absent():
    entries = [
        {"key": "review:1", "kind": "review", "login": "alice", "state": "APPROVED",
         "created_at": "2024-01-01T00:00:00Z", "path": None, "line": None, "body": "lgtm"},
    ]
    rendered = _merge_gh.render_pr_reviews(entries)
    assert "### alice (review, APPROVED)" in rendered
    assert "f.txt" not in rendered


# ---------------------------------------------------------------------------
# The two Copilot-review stages (step 14) -- structure
# ---------------------------------------------------------------------------


def test_copilot_review_stages_follow_wait_for_checks_after_ci_fix(tmp_path):
    mod = _import_merge_branch(cwd=tmp_path)
    names = [item.name for item in _top_stages(mod.config.items)]
    idx = names.index("wait for checks after ci fix 1")
    assert names[idx + 1 : idx + 3] == ["wait for copilot review 1", "fetch pr reviews 1"]


def test_fetch_pr_reviews_on_failure_is_fail(tmp_path):
    mod = _import_merge_branch(cwd=tmp_path)
    stages = {s.name: s for s in _top_stages(mod.config.items)}
    assert stages["fetch pr reviews 1"].on_failure is OnFailure.FAIL
    assert stages["fetch pr reviews 1"].when is not None


def test_wait_for_copilot_review_has_backstop_timeout_above_copilot_review_timeout(tmp_path):
    mod = _import_merge_branch(cwd=tmp_path)
    stages = {s.name: s for s in _top_stages(mod.config.items)}
    stage = stages["wait for copilot review 1"]
    assert isinstance(stage.impl, _merge_gh.WaitForCopilotReview)
    assert stage.timeout > _merge_gh.COPILOT_REVIEW_TIMEOUT
    assert stage.when is not None


# ---------------------------------------------------------------------------
# ## Copilot review summary section (step 14)
# ---------------------------------------------------------------------------


def test_render_summary_copilot_review_section_defaults_to_none(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.slug).write_text("widgets")
    Path(paths.merged).write_text("deadbeef\n")

    body = _merge_state.render_summary(
        paths, rerun_cmd="norn run merge_branch --non-interactive", max_rounds=1, project_dir=str(tmp_path)
    )

    assert "## Copilot review" in body
    section = body[body.index("## Copilot review") :]
    assert "State: (none)" in section
    assert "Reviewed head: (none)" in section
    assert "Re-requested after CI fix: no" in section
    assert "Arrived: (none)" in section


def test_render_summary_copilot_review_section_renders_state_head_and_arrival(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.slug).write_text("widgets")
    Path(paths.merged).write_text("deadbeef\n")
    Path(paths.copilot_reviewer).write_text("requested\n")
    Path(paths.ci_red(1)).touch()
    Path(paths.copilot_review(1)).write_text(
        json.dumps({"commit_id": "deadbeef", "submitted_at": "2024-01-01T00:05:00Z"})
    )

    body = _merge_state.render_summary(
        paths, rerun_cmd="norn run merge_branch --non-interactive", max_rounds=1, project_dir=str(tmp_path)
    )

    section = body[body.index("## Copilot review") :]
    assert "State: requested" in section
    assert "Reviewed head: deadbeef" in section
    assert "Re-requested after CI fix: yes" in section
    assert "Arrived: 2024-01-01T00:05:00Z" in section


# ---------------------------------------------------------------------------
# classify / validate / post / check findings (step 15)
# ---------------------------------------------------------------------------


def _write_findings(paths, n: int, text: str) -> None:
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.findings(n)).write_text(text)


_BLOCKING_FINDINGS = (
    "VERDICT: BLOCKING\n"
    "PR: 42\n"
    "Reviewed: 1 new comments\n"
    "\n"
    "## Blocking\n"
    "- foo.py:10 (alice): missing null check\n"
    "\n"
    "## Non-blocking\n"
    "\n"
    "## Answered\n"
)

_CLEAR_FINDINGS = (
    "VERDICT: CLEAR\n"
    "PR: 42\n"
    "Reviewed: 1 new comments\n"
    "\n"
    "## Blocking\n"
    "\n"
    "## Non-blocking\n"
    "- foo.py:20 (bob): rename this\n"
    "\n"
    "## Answered\n"
)


# --- validate_findings_cmd ----------------------------------------------------


def test_validate_findings_passes_for_blocking(tmp_path):
    repo = _repo(tmp_path)
    paths = _paths(tmp_path)
    _write_findings(paths, 1, _BLOCKING_FINDINGS)

    result = _run_sh(_merge_gh.validate_findings_cmd(paths, repo.path, 1), repo.path)

    assert result.returncode == 0
    assert not Path(paths.halt).exists()


def test_validate_findings_passes_for_clear(tmp_path):
    repo = _repo(tmp_path)
    paths = _paths(tmp_path)
    _write_findings(paths, 1, _CLEAR_FINDINGS)

    result = _run_sh(_merge_gh.validate_findings_cmd(paths, repo.path, 1), repo.path)

    assert result.returncode == 0
    assert not Path(paths.halt).exists()


def test_validate_findings_halts_on_malformed_verdict(tmp_path):
    repo = _repo(tmp_path)
    paths = _paths(tmp_path)
    _write_findings(paths, 1, "VERDICT: MAYBE\nPR: 42\n")

    result = _run_sh(_merge_gh.validate_findings_cmd(paths, repo.path, 1), repo.path)

    assert result.returncode == 0
    text = Path(paths.halt_reason).read_text()
    assert text.splitlines()[0] == "KIND: findings_malformed"


def test_validate_findings_halts_on_dirty_tree(tmp_path):
    repo = _repo(tmp_path)
    paths = _paths(tmp_path)
    _write_findings(paths, 1, _CLEAR_FINDINGS)
    (Path(repo.path) / "untracked.txt").write_text("oops\n")

    result = _run_sh(_merge_gh.validate_findings_cmd(paths, repo.path, 1), repo.path)

    assert result.returncode == 0
    text = Path(paths.halt_reason).read_text()
    assert text.splitlines()[0] == "KIND: classifier_touched_repo"
    assert "untracked.txt" in text


def test_validate_findings_halts_on_attribution_and_never_calls_gh(tmp_path):
    repo = _repo(tmp_path)
    paths = _paths(tmp_path)
    _write_findings(
        paths,
        1,
        "VERDICT: CLEAR\nPR: 42\nReviewed: 1 new comments\n\n"
        "## Blocking\n\n## Non-blocking\n- Generated with Claude\n\n## Answered\n",
    )
    bin_dir, calls_log = install_fake_gh(tmp_path, {("pr", "comment"): [(0, "https://x/1", "")]})
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    result = _run_sh_env(_merge_gh.validate_findings_cmd(paths, repo.path, 1), repo.path, env)

    assert result.returncode == 0
    text = Path(paths.halt_reason).read_text()
    assert text.splitlines()[0] == "KIND: attribution_found"
    assert Path(calls_log).read_text() == ""


# --- post_findings_comment_cmd ------------------------------------------------


def test_post_findings_comment_records_body_file_and_marker(tmp_path):
    repo = _repo(tmp_path)
    paths = _paths(tmp_path)
    _write_pr_prereqs(paths)
    Path(paths.pr).write_text("42\n")
    _write_findings(paths, 1, _CLEAR_FINDINGS)
    comment_url = "https://github.com/acme/widgets/pull/42#issuecomment-1"
    bin_dir, calls_log = install_fake_gh(tmp_path, {("pr", "comment"): [(0, comment_url + "\n", "")]})
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    result = _run_sh_env(_merge_gh.post_findings_comment_cmd(paths, repo.path, 1), repo.path, env)

    assert result.returncode == 0
    assert Path(paths.findings_comment_url(1)).read_text().strip() == comment_url

    body = Path(paths.findings_comment(1)).read_text()
    assert body.startswith("<!-- norn:merge_branch findings round 1 -->\n")
    assert "VERDICT: CLEAR" in body

    calls = _gh_calls(calls_log)
    comment_calls = [c for c in calls if c[:2] == ["pr", "comment"]]
    assert len(comment_calls) == 1
    call = comment_calls[0]
    assert "--body-file" in call
    assert call[call.index("--body-file") + 1] == paths.findings_comment(1)
    assert "--edit-last" not in call


# --- check_findings_cmd --------------------------------------------------------


def test_check_findings_halts_on_blocking(tmp_path):
    repo = _repo(tmp_path)
    paths = _paths(tmp_path)
    _write_findings(paths, 1, _BLOCKING_FINDINGS)
    Path(paths.review_decision(1)).write_text("APPROVED\n")

    result = _run_sh(_merge_gh.check_findings_cmd(paths, repo.path, 1), repo.path)

    assert result.returncode == 0
    text = Path(paths.halt_reason).read_text()
    assert text.splitlines()[0] == "KIND: findings_blocking"
    assert "missing null check" in text


def test_check_findings_passes_for_clear(tmp_path):
    repo = _repo(tmp_path)
    paths = _paths(tmp_path)
    _write_findings(paths, 1, _CLEAR_FINDINGS)
    Path(paths.review_decision(1)).write_text("\n")

    result = _run_sh(_merge_gh.check_findings_cmd(paths, repo.path, 1), repo.path)

    assert result.returncode == 0
    assert not Path(paths.halt).exists()


def test_check_findings_halts_on_changes_requested_even_when_clear(tmp_path):
    repo = _repo(tmp_path)
    paths = _paths(tmp_path)
    _write_findings(paths, 1, _CLEAR_FINDINGS)
    Path(paths.review_decision(1)).write_text("CHANGES_REQUESTED\n")

    result = _run_sh(_merge_gh.check_findings_cmd(paths, repo.path, 1), repo.path)

    assert result.returncode == 0
    text = Path(paths.halt_reason).read_text()
    assert text.splitlines()[0] == "KIND: changes_requested"


# --- structure: classify findings 1 and its neighbours ------------------------


def test_classify_findings_follows_clear_context_after_fetch_pr_reviews(tmp_path):
    mod = _import_merge_branch(cwd=tmp_path)
    items = mod.config.items
    fetch_idx = next(i for i, item in enumerate(items) if getattr(item, "name", None) == "fetch pr reviews 1")
    assert type(items[fetch_idx + 1]).__name__ == "ClearContext"
    assert items[fetch_idx + 2].name == "classify findings 1"


def test_findings_stages_follow_in_order(tmp_path):
    mod = _import_merge_branch(cwd=tmp_path)
    names = [item.name for item in _top_stages(mod.config.items)]
    idx = names.index("fetch pr reviews 1")
    assert names[idx + 1 : idx + 5] == [
        "classify findings 1",
        "validate findings 1",
        "post findings comment 1",
        "check findings 1",
    ]


def test_classify_findings_has_no_edit_or_bash_and_reads_project_settings(tmp_path):
    mod = _import_merge_branch(cwd=tmp_path)
    stages = {s.name: s for s in _top_stages(mod.config.items)}
    classify = stages["classify findings 1"]

    assert "Edit" not in classify.impl.allowed_tools
    assert "Bash" not in classify.impl.allowed_tools
    assert mod.WORK in classify.impl.add_dirs
    assert classify.impl.cwd is None
    assert mod.COMMENT_RULE in classify.impl.prompt
    assert isinstance(classify.impl, _merge_state.HaltOnFailure)


def test_check_findings_runs_even_when_classify_is_skipped(tmp_path):
    mod = _import_merge_branch(cwd=tmp_path)
    stages = {s.name: s for s in _top_stages(mod.config.items)}
    check = stages["check findings 1"]
    classify = stages["classify findings 1"]

    os.makedirs(mod.WORK, exist_ok=True)
    Path(mod.PATHS.no_new_reviews(1)).touch()
    ctx = ctx_for()

    assert check.on_failure is OnFailure.FAIL
    assert classify.when(ctx) is False
    assert check.when(ctx) is True


# ---------------------------------------------------------------------------
# ## Review findings summary section (step 15)
# ---------------------------------------------------------------------------


def test_render_summary_review_findings_section_defaults_to_none(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.slug).write_text("widgets")
    Path(paths.merged).write_text("deadbeef\n")

    body = _merge_state.render_summary(
        paths, rerun_cmd="norn run merge_branch --non-interactive", max_rounds=1, project_dir=str(tmp_path)
    )

    assert "## Review findings" in body
    section = body[body.index("## Review findings") :]
    assert "(no new reviews)" in section
    assert "Verdict: (none)" in section
    assert "Comment: (none)" in section


def test_render_summary_review_findings_section_renders_authors_verdict_and_comment(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.slug).write_text("widgets")
    Path(paths.merged).write_text("deadbeef\n")
    Path(paths.pr_reviews(1)).write_text(
        "### alice (review, COMMENTED)\n2024-01-01T00:00:00Z\n\nlooks ok\n\n"
        "### bob (inline, comment)\nfoo.py:10\n2024-01-01T00:01:00Z\n\nfix this\n\n"
        "### alice (inline, comment)\nfoo.py:20\n2024-01-01T00:02:00Z\n\nand this\n"
    )
    Path(paths.findings(1)).write_text(_BLOCKING_FINDINGS)
    Path(paths.findings_comment_url(1)).write_text("https://github.com/acme/widgets/pull/42#issuecomment-9\n")

    body = _merge_state.render_summary(
        paths, rerun_cmd="norn run merge_branch --non-interactive", max_rounds=1, project_dir=str(tmp_path)
    )

    section = body[body.index("## Review findings") :]
    assert "- alice: 2" in section
    assert "- bob: 1" in section
    assert "Verdict: BLOCKING" in section
    assert "foo.py:10 (alice): missing null check" in section
    assert "Non-blocking: 0" in section
    assert "Answered: 0" in section
    assert "Comment: https://github.com/acme/widgets/pull/42#issuecomment-9" in section


# ---------------------------------------------------------------------------
# CheckMergePreconditions / merge_cmd (step 16)
# ---------------------------------------------------------------------------


def _write_merge_precondition_prereqs(
    paths: _merge_state.MergePaths,
    *,
    repo: str = "acme/widgets",
    pr: str = "42",
    branch: str = "feature-x",
    base: str = "main",
    green_head: str = "cafef00d",
) -> None:
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.repo).write_text(repo + "\n")
    Path(paths.pr).write_text(pr + "\n")
    Path(paths.branch).write_text(branch + "\n")
    Path(paths.base).write_text(base + "\n")
    Path(paths.green_head).write_text(green_head + "\n")


def _pr_view_state(
    *,
    mergeable: str = "MERGEABLE",
    merge_state_status: str = "CLEAN",
    review_decision: str = "",
    is_draft: bool = False,
    head: str = "cafef00d",
) -> str:
    return json.dumps(
        {
            "mergeable": mergeable,
            "mergeStateStatus": merge_state_status,
            "reviewDecision": review_decision,
            "isDraft": is_draft,
            "headRefOid": head,
        }
    )


def _settled(checks: list[dict], runs: list[dict] = ()) -> str:
    return json.dumps(
        {
            "outcome": "green",
            "head": None,
            "checks": list(checks),
            "check_names": sorted({c.get("name") for c in checks if c.get("name")}),
            "runs": list(runs),
        }
    )


def _preconditions_stage(
    paths: _merge_state.MergePaths, project_dir: str, n: int, fc: _FakeClock
) -> _merge_gh.CheckMergePreconditions:
    return _merge_gh.CheckMergePreconditions(
        paths=paths,
        stage=f"check merge preconditions {n}",
        n=n,
        project_dir=project_dir,
        sleep=fc.sleep,
        clock=fc.clock,
    )


@pytest.mark.asyncio
async def test_check_merge_preconditions_unknown_then_clean_passes(tmp_path):
    paths = _paths(tmp_path)
    _write_merge_precondition_prereqs(paths)
    Path(paths.checks(1)).write_text(_settled([_chk("build", "pass", workflow="ci.yml")]))
    fc = _FakeClock()
    env = _gh_env(
        tmp_path,
        {
            ("pr", "view"): [
                (0, _pr_view_state(mergeable="UNKNOWN", merge_state_status="UNKNOWN"), ""),
                (0, _pr_view_state(), ""),
            ],
            ("pr", "checks"): [(0, json.dumps([_chk("build", "pass", workflow="ci.yml")]), "")],
            ("run", "list"): [(0, "[]", "")],
        },
    )

    result = await _preconditions_stage(paths, str(tmp_path), 1, fc).run(ctx_for(env=env))

    assert result.success is True
    assert not Path(paths.halt).exists()
    assert not Path(paths.retry(1)).exists()
    assert fc.t > 0  # actually slept once between the two polls
    state = json.loads(Path(paths.merge_state(1)).read_text())
    assert state["mergeStateStatus"] == "CLEAN"


@pytest.mark.asyncio
async def test_check_merge_preconditions_review_required_halts_before_retry(tmp_path):
    paths = _paths(tmp_path)
    _write_merge_precondition_prereqs(paths)
    fc = _FakeClock()
    env = _gh_env(
        tmp_path,
        {
            ("pr", "view"): [
                (
                    0,
                    _pr_view_state(
                        mergeable="CONFLICTING", merge_state_status="DIRTY", review_decision="REVIEW_REQUIRED"
                    ),
                    "",
                )
            ],
        },
    )

    result = await _preconditions_stage(paths, str(tmp_path), 1, fc).run(ctx_for(env=env))

    assert result.success is True
    assert Path(paths.halt).is_file()
    assert Path(paths.halt_reason).read_text().splitlines()[0] == "KIND: review_required"
    assert not Path(paths.retry(1)).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mergeable,merge_state_status",
    [("MERGEABLE", "DIRTY"), ("MERGEABLE", "BEHIND"), ("CONFLICTING", "CLEAN")],
)
async def test_check_merge_preconditions_conflict_states_write_retry(tmp_path, mergeable, merge_state_status):
    paths = _paths(tmp_path)
    _write_merge_precondition_prereqs(paths)
    fc = _FakeClock()
    env = _gh_env(
        tmp_path,
        {("pr", "view"): [(0, _pr_view_state(mergeable=mergeable, merge_state_status=merge_state_status), "")]},
    )

    result = await _preconditions_stage(paths, str(tmp_path), 1, fc).run(ctx_for(env=env))

    assert result.success is True
    assert Path(paths.retry(1)).is_file()
    assert not Path(paths.halt).exists()


@pytest.mark.asyncio
async def test_check_merge_preconditions_blocked_halts_merge_state(tmp_path):
    paths = _paths(tmp_path)
    _write_merge_precondition_prereqs(paths)
    Path(paths.checks(1)).write_text(_settled([_chk("build", "pass", workflow="ci.yml")]))
    fc = _FakeClock()
    env = _gh_env(
        tmp_path,
        {
            ("pr", "view"): [(0, _pr_view_state(merge_state_status="BLOCKED"), "")],
            ("pr", "checks"): [(0, json.dumps([_chk("build", "pass", workflow="ci.yml")]), "")],
            ("run", "list"): [(0, "[]", "")],
        },
    )

    result = await _preconditions_stage(paths, str(tmp_path), 1, fc).run(ctx_for(env=env))

    assert result.success is True
    assert Path(paths.halt_reason).read_text().splitlines()[0] == "KIND: merge_state"
    assert not Path(paths.retry(1)).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("review_decision", ["APPROVED", ""])
async def test_check_merge_preconditions_clean_and_reviewed_passes(tmp_path, review_decision):
    paths = _paths(tmp_path)
    _write_merge_precondition_prereqs(paths)
    Path(paths.checks(1)).write_text(_settled([_chk("build", "pass", workflow="ci.yml")]))
    fc = _FakeClock()
    env = _gh_env(
        tmp_path,
        {
            ("pr", "view"): [(0, _pr_view_state(review_decision=review_decision), "")],
            ("pr", "checks"): [(0, json.dumps([_chk("build", "pass", workflow="ci.yml")]), "")],
            ("run", "list"): [(0, "[]", "")],
        },
    )

    result = await _preconditions_stage(paths, str(tmp_path), 1, fc).run(ctx_for(env=env))

    assert result.success is True
    assert not Path(paths.halt).exists()
    assert not Path(paths.retry(1)).exists()


@pytest.mark.asyncio
async def test_check_merge_preconditions_added_check_halts_checks_changed(tmp_path):
    paths = _paths(tmp_path)
    _write_merge_precondition_prereqs(paths)
    Path(paths.checks(1)).write_text(_settled([_chk("build", "pass", workflow="ci.yml")]))
    fc = _FakeClock()
    env = _gh_env(
        tmp_path,
        {
            ("pr", "view"): [(0, _pr_view_state(), "")],
            ("pr", "checks"): [
                (
                    0,
                    json.dumps(
                        [
                            _chk("build", "pass", workflow="ci.yml"),
                            _chk("lint", "pass", workflow="lint.yml"),
                        ]
                    ),
                    "",
                )
            ],
            ("run", "list"): [(0, "[]", "")],
        },
    )

    result = await _preconditions_stage(paths, str(tmp_path), 1, fc).run(ctx_for(env=env))

    assert result.success is True
    text = Path(paths.halt_reason).read_text()
    assert text.splitlines()[0] == "KIND: checks_changed"
    assert "lint" in text


@pytest.mark.asyncio
async def test_check_merge_preconditions_settled_check_turned_fail_halts_checks_changed(tmp_path):
    paths = _paths(tmp_path)
    _write_merge_precondition_prereqs(paths)
    Path(paths.checks(1)).write_text(_settled([_chk("build", "pass", workflow="ci.yml")]))
    fc = _FakeClock()
    env = _gh_env(
        tmp_path,
        {
            ("pr", "view"): [(0, _pr_view_state(), "")],
            ("pr", "checks"): [(0, json.dumps([_chk("build", "fail", workflow="ci.yml")]), "")],
            ("run", "list"): [(0, "[]", "")],
        },
    )

    result = await _preconditions_stage(paths, str(tmp_path), 1, fc).run(ctx_for(env=env))

    assert result.success is True
    text = Path(paths.halt_reason).read_text()
    assert text.splitlines()[0] == "KIND: checks_changed"
    assert "build" in text


@pytest.mark.asyncio
async def test_check_merge_preconditions_head_moved_halts_pr_head_moved(tmp_path):
    paths = _paths(tmp_path)
    _write_merge_precondition_prereqs(paths, green_head="cafef00d")
    fc = _FakeClock()
    env = _gh_env(tmp_path, {("pr", "view"): [(0, _pr_view_state(head="deadbeef"), "")]})

    result = await _preconditions_stage(paths, str(tmp_path), 1, fc).run(ctx_for(env=env))

    assert result.success is True
    assert Path(paths.halt_reason).read_text().splitlines()[0] == "KIND: pr_head_moved"


@pytest.mark.asyncio
async def test_check_merge_preconditions_after_ci_fix_reads_after_fix_file(tmp_path):
    paths = _paths(tmp_path)
    _write_merge_precondition_prereqs(paths)
    Path(paths.ci_red(1)).touch()
    Path(paths.checks(1)).write_text(_settled([_chk("old", "fail", workflow="ci.yml")]))
    Path(paths.checks_after_fix(1)).write_text(_settled([_chk("build", "pass", workflow="ci.yml")]))
    fc = _FakeClock()
    env = _gh_env(
        tmp_path,
        {
            ("pr", "view"): [(0, _pr_view_state(), "")],
            ("pr", "checks"): [(0, json.dumps([_chk("build", "pass", workflow="ci.yml")]), "")],
            ("run", "list"): [(0, "[]", "")],
        },
    )

    result = await _preconditions_stage(paths, str(tmp_path), 1, fc).run(ctx_for(env=env))

    assert result.success is True
    assert not Path(paths.halt).exists()


# --- merge_cmd -----------------------------------------------------------------


def _write_merge_cmd_prereqs(
    paths: _merge_state.MergePaths,
    repo: RepoWithRemote,
    *,
    branch: str,
    base: str = "main",
    green_head: str | None = None,
) -> None:
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.repo).write_text("acme/widgets\n")
    Path(paths.pr).write_text("42\n")
    Path(paths.base).write_text(base + "\n")
    Path(paths.branch).write_text(branch + "\n")
    Path(paths.green_head).write_text((green_head or repo.repo.head.commit.hexsha) + "\n")


def _install_rejecting_pre_receive_hook(origin_path: str, message: str = "protected branch hook declined") -> None:
    hooks_dir = Path(origin_path) / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "pre-receive"
    hook.write_text(f"#!/bin/sh\necho {shlex.quote(message)} >&2\nexit 1\n")
    hook.chmod(hook.stat().st_mode | 0o111)


def test_merge_cmd_clean_fast_forward_pushes_and_deletes_branch(tmp_path):
    repo = _repo(tmp_path)
    repo.repo.git.checkout("-b", "feature-x")
    (Path(repo.path) / "f.txt").write_text("one\n")
    repo.repo.index.add(["f.txt"])
    repo.repo.index.commit("feature commit")
    repo.repo.remote("origin").push("feature-x")
    green = repo.repo.head.commit.hexsha

    paths = _paths(tmp_path)
    _write_merge_cmd_prereqs(paths, repo, branch="feature-x", base="main", green_head=green)
    env = _gh_env(tmp_path, {("pr", "view"): [(0, "MERGED", "")]})

    result = _run_sh_env(_merge_gh.merge_cmd(paths, repo.path, 1), repo.path, env)

    assert result.returncode == 0
    assert not Path(paths.halt).exists()
    assert not Path(paths.retry(1)).exists()
    assert Path(paths.merged).read_text().strip() == green

    origin_repo = git.Repo(repo.origin_path)
    assert origin_repo.git.rev_parse("refs/heads/main") == green
    assert "feature-x" not in [h.name for h in origin_repo.heads]


def test_merge_cmd_moved_base_writes_retry_without_pushing(tmp_path):
    repo = _repo(tmp_path)
    repo.repo.git.checkout("-b", "feature-x")
    (Path(repo.path) / "f.txt").write_text("one\n")
    repo.repo.index.add(["f.txt"])
    repo.repo.index.commit("feature commit")
    green = repo.repo.head.commit.hexsha

    second = git.Repo.clone_from(repo.origin_path, str(tmp_path / "second"), branch="main")
    with second.config_writer() as cw:
        cw.set_value("user", "email", "erik@zenior.no")
        cw.set_value("user", "name", "Erik")
    (Path(second.working_dir) / "g.txt").write_text("two\n")
    second.index.add(["g.txt"])
    second.index.commit("moved base")
    second.remote("origin").push("main")
    main_before = git.Repo(repo.origin_path).git.rev_parse("refs/heads/main")

    paths = _paths(tmp_path)
    _write_merge_cmd_prereqs(paths, repo, branch="feature-x", base="main", green_head=green)

    result = _run_sh(_merge_gh.merge_cmd(paths, repo.path, 1), repo.path)

    assert result.returncode == 0
    assert Path(paths.retry(1)).is_file()
    assert not Path(paths.merged).exists()
    assert not Path(paths.halt).exists()
    main_after = git.Repo(repo.origin_path).git.rev_parse("refs/heads/main")
    assert main_after == main_before


def test_merge_cmd_head_moved_halts_before_push(tmp_path):
    repo = _repo(tmp_path)
    repo.repo.git.checkout("-b", "feature-x")
    (Path(repo.path) / "f.txt").write_text("one\n")
    repo.repo.index.add(["f.txt"])
    repo.repo.index.commit("feature commit")

    paths = _paths(tmp_path)
    _write_merge_cmd_prereqs(paths, repo, branch="feature-x", base="main", green_head="0" * 40)
    main_before = git.Repo(repo.origin_path).git.rev_parse("refs/heads/main")

    result = _run_sh(_merge_gh.merge_cmd(paths, repo.path, 1), repo.path)

    assert result.returncode == 0
    assert Path(paths.halt_reason).read_text().splitlines()[0] == "KIND: head_moved_since_checks"
    assert not Path(paths.merged).exists()
    main_after = git.Repo(repo.origin_path).git.rev_parse("refs/heads/main")
    assert main_after == main_before


def test_merge_cmd_protection_rejection_halts_merge_rejected_branch_not_deleted(tmp_path):
    repo = _repo(tmp_path)
    repo.repo.git.checkout("-b", "feature-x")
    (Path(repo.path) / "f.txt").write_text("one\n")
    repo.repo.index.add(["f.txt"])
    repo.repo.index.commit("feature commit")
    repo.repo.remote("origin").push("feature-x")
    green = repo.repo.head.commit.hexsha

    _install_rejecting_pre_receive_hook(repo.origin_path)

    paths = _paths(tmp_path)
    _write_merge_cmd_prereqs(paths, repo, branch="feature-x", base="main", green_head=green)

    result = _run_sh(_merge_gh.merge_cmd(paths, repo.path, 1), repo.path)

    assert result.returncode == 0
    assert Path(paths.halt_reason).read_text().splitlines()[0] == "KIND: merge_rejected"
    assert "protected branch hook declined" in Path(paths.merge_log(1)).read_text()
    assert not Path(paths.merged).exists()

    origin_repo = git.Repo(repo.origin_path)
    assert "feature-x" in [h.name for h in origin_repo.heads]


def test_merge_cmd_pr_never_merges_halts_merged_pr_not_closed_branch_not_deleted(tmp_path):
    repo = _repo(tmp_path)
    repo.repo.git.checkout("-b", "feature-x")
    (Path(repo.path) / "f.txt").write_text("one\n")
    repo.repo.index.add(["f.txt"])
    repo.repo.index.commit("feature commit")
    repo.repo.remote("origin").push("feature-x")
    green = repo.repo.head.commit.hexsha

    paths = _paths(tmp_path)
    _write_merge_cmd_prereqs(paths, repo, branch="feature-x", base="main", green_head=green)
    env = _gh_env(tmp_path, {("pr", "view"): [(0, "OPEN", "")]})

    script = _merge_gh.merge_cmd(paths, repo.path, 1, poll_attempts=2, poll_interval=0)
    result = _run_sh_env(script, repo.path, env)

    assert result.returncode == 0
    assert Path(paths.merged).read_text().strip() == green
    assert Path(paths.halt_reason).read_text().splitlines()[0] == "KIND: merged_pr_not_closed"

    origin_repo = git.Repo(repo.origin_path)
    assert "feature-x" in [h.name for h in origin_repo.heads]


def test_merge_cmd_branch_already_deleted_is_success(tmp_path):
    repo = _repo(tmp_path)
    repo.repo.git.checkout("-b", "feature-x")
    (Path(repo.path) / "f.txt").write_text("one\n")
    repo.repo.index.add(["f.txt"])
    repo.repo.index.commit("feature commit")
    green = repo.repo.head.commit.hexsha
    # feature-x was never pushed, so origin has no such ref to delete.

    paths = _paths(tmp_path)
    _write_merge_cmd_prereqs(paths, repo, branch="feature-x", base="main", green_head=green)
    env = _gh_env(tmp_path, {("pr", "view"): [(0, "MERGED", "")]})

    result = _run_sh_env(_merge_gh.merge_cmd(paths, repo.path, 1), repo.path, env)

    assert result.returncode == 0
    assert not Path(paths.halt).exists()
    assert Path(paths.merged).read_text().strip() == green


@pytest.mark.asyncio
async def test_merge_cmd_gh_pr_view_failure_after_push_is_unexpected_failure_via_mixin(tmp_path):
    repo = _repo(tmp_path)
    repo.repo.git.checkout("-b", "feature-x")
    (Path(repo.path) / "f.txt").write_text("one\n")
    repo.repo.index.add(["f.txt"])
    repo.repo.index.commit("feature commit")
    repo.repo.remote("origin").push("feature-x")
    green = repo.repo.head.commit.hexsha

    paths = _paths(tmp_path)
    _write_merge_cmd_prereqs(paths, repo, branch="feature-x", base="main", green_head=green)
    env = _gh_env(tmp_path, {("pr", "view"): [(1, "", "error: rate limited")]})

    stage = _merge_state.HaltingCommand(
        cmd=_merge_gh.merge_cmd(paths, repo.path, 1), paths=paths, stage="merge pr 1"
    )
    result = await stage.run(ctx_for(env=env))

    assert result.success is True
    assert Path(paths.merged).read_text().strip() == green
    assert Path(paths.halt).is_file()
    assert Path(paths.halt_reason).read_text().splitlines()[0] == "KIND: unexpected_failure"


# --- structure: check merge preconditions 1 / merge pr 1 -----------------------


def test_merge_preconditions_and_merge_pr_follow_check_findings_in_order(tmp_path):
    mod = _import_merge_branch(cwd=tmp_path)
    names = [item.name for item in _top_stages(mod.config.items)]
    idx = names.index("check findings 1")
    assert names[idx + 1 : idx + 3] == ["check merge preconditions 1", "merge pr 1"]


def test_merge_pr_gated_on_retry_absence(tmp_path):
    mod = _import_merge_branch(cwd=tmp_path)
    stages = {s.name: s for s in _top_stages(mod.config.items)}
    merge_pr = stages["merge pr 1"]
    os.makedirs(mod.WORK, exist_ok=True)
    ctx = ctx_for()

    assert merge_pr.when(ctx) is True

    Path(mod.PATHS.retry(1)).touch()
    assert merge_pr.when(ctx) is False


# ---------------------------------------------------------------------------
# Round 2, `merge reached` and the three-valued Status (step 17)
# ---------------------------------------------------------------------------


def _round_stages(items, n: int) -> list:
    """Every Stage belonging to round *n*, loop body included, in pipeline order.

    Round membership is spelled in the name — every stage a round adds ends
    in `` {n}``, and no stage outside a round does.
    """
    return [stage for stage in _walk_stages(items) if stage.name.endswith(f" {n}")]


def _all_loops(items) -> list:
    return [item for item in items if isinstance(item, Loop)]


def test_round_two_exists_and_is_a_flat_copy_of_round_one(tmp_path):
    mod = _import_merge_branch(cwd=tmp_path)
    top = [stage.name for stage in _top_stages(mod.config.items)]

    for n in (1, 2):
        assert [name for name in top if name.endswith(f" {n}")] == [
            name.format(n=n) for name in ROUND_ITEM_NAMES
        ], n
        loop = _find_loop(mod.config.items, f"local tests {n}")
        assert [s.name for s in loop.stages] == [name.format(n=n) for name in LOOP_BODY_NAMES], n
        assert loop.max_retries == mod.LOCAL_FIX_ATTEMPTS + 1, n
        assert loop.on_exhaust is OnFailure.FAIL, n
        assert loop.new_session is True, n

    assert top.index("rebase 2") > top.index("merge pr 1")


def test_no_loop_is_nested_inside_another_loop(tmp_path):
    """The rounds are flat copies, never nested — a nested Loop would make a
    failed round-2 body stage restart round 1."""
    mod = _import_merge_branch(cwd=tmp_path)
    loops = _all_loops(mod.config.items)
    assert [loop.name for loop in loops] == ["local tests 1", "local tests 2"]
    for loop in loops:
        assert not [item for item in loop.stages if isinstance(item, (Loop, Parallel))], loop.name


def test_stage_names_are_unique_across_items_and_loop_bodies(tmp_path):
    """``ctx.results`` is keyed by stage name, so doubling the round must not
    double a name."""
    mod = _import_merge_branch(cwd=tmp_path)
    names = [stage.name for stage in _walk_stages(mod.config.items)]
    names += [loop.name for loop in _all_loops(mod.config.items)]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    assert duplicates == []


# Every ``MergePaths`` method that takes a round number: a round-2 stage that
# names any of these at round 1 (or the reverse) is reading the wrong round's
# state.
def _per_round_path_builders(paths):
    return [
        paths.retry, paths.red, paths.proof, paths.local_log, paths.local_rc,
        paths.local_attempt, paths.local_signature, paths.local_signature_pending,
        paths.local_head, paths.verdict, paths.rebase_conflicts, paths.commit_log,
        paths.push_log, paths.checks, paths.checks_after_fix, paths.ci_red,
        paths.ci_fix_workflows, paths.ci_fix, paths.ci_fix_commits,
        paths.copilot_review, paths.pr_reviews, paths.branch_diff,
        paths.review_decision, paths.no_new_reviews, paths.findings,
        paths.findings_comment, paths.findings_comment_url, paths.merge_state,
        paths.merge_log,
    ]


def test_each_round_names_only_its_own_round(tmp_path):
    """The `_add_round` audit: no stage of round *n* carries the other round's
    paths, stage name, round index or last-attempt tuple."""
    mod = _import_merge_branch(cwd=tmp_path)
    builders = _per_round_path_builders(mod.PATHS)

    for n, other in ((1, 2), (2, 1)):
        stages = _round_stages(mod.config.items, n)
        assert len(stages) == len(ROUND_ITEM_NAMES) + len(LOOP_BODY_NAMES), n
        for stage in stages:
            impl = stage.impl
            if isinstance(impl, _merge_state.HaltOnFailure):
                assert impl._halt_stage == stage.name, stage.name
            if getattr(impl, "_n", None) is not None:
                assert impl._n == n, stage.name
            last_attempt = getattr(impl, "_halt_last_attempt", None)
            if last_attempt is not None:
                assert last_attempt == (n, mod.LOCAL_FIX_ATTEMPTS + 1), stage.name
            text = f"{getattr(impl, 'cmd', '')}{getattr(impl, 'prompt', '')}"
            for path_of in builders:
                assert path_of(other) not in text, (stage.name, path_of(other))


def test_round_two_stages_are_all_skipped_without_retry_1(tmp_path):
    """Every round-2 stage — the six loop-body stages and the four CI-fix
    stages included — is skipped when round 1 never asked for a retry."""
    mod = _import_merge_branch(cwd=tmp_path)
    os.makedirs(mod.WORK, exist_ok=True)
    assert not Path(mod.PATHS.retry(1)).exists()
    ctx = ctx_for()

    stages = _round_stages(mod.config.items, 2)
    assert len(stages) == len(ROUND_ITEM_NAMES) + len(LOOP_BODY_NAMES)
    for stage in stages:
        assert stage.when is not None, stage.name
        assert stage.when(ctx) is False, stage.name


def test_round_two_ungated_stages_run_once_retry_1_exists(tmp_path):
    """With ``RETRY-1`` present every round-2 stage whose only condition is
    the gate runs; the eight that also need ``RED-2``/``CI-RED-2`` wait for
    their own marker."""
    mod = _import_merge_branch(cwd=tmp_path)
    os.makedirs(mod.WORK, exist_ok=True)
    Path(mod.PATHS.retry(1)).touch()
    ctx = ctx_for()

    for stage in _round_stages(mod.config.items, 2):
        expected = stage.name not in _ROUND_TWO_EXTRA_FILE_GATED
        assert stage.when(ctx) is expected, stage.name

    Path(mod.PATHS.red(2)).touch()
    Path(mod.PATHS.ci_red(2)).touch()
    for stage in _round_stages(mod.config.items, 2):
        assert stage.when(ctx) is True, stage.name


def test_round_two_stages_stay_skipped_after_a_halt(tmp_path):
    """``RETRY-1`` does not resurrect round 2 once the run has halted — the
    gate is ``all_of(running, file_exists(RETRY-1))``."""
    mod = _import_merge_branch(cwd=tmp_path)
    os.makedirs(mod.WORK, exist_ok=True)
    Path(mod.PATHS.retry(1)).touch()
    Path(mod.PATHS.red(2)).touch()
    Path(mod.PATHS.ci_red(2)).touch()
    _merge_state.halt(mod.PATHS, "rebase_conflict", "conflict")
    ctx = ctx_for()

    for stage in _round_stages(mod.config.items, 2):
        assert stage.when(ctx) is False, stage.name


def test_merge_pr_2_gated_on_its_own_retry_marker(tmp_path):
    mod = _import_merge_branch(cwd=tmp_path)
    os.makedirs(mod.WORK, exist_ok=True)
    Path(mod.PATHS.retry(1)).touch()
    merge_pr = {s.name: s for s in _top_stages(mod.config.items)}["merge pr 2"]
    ctx = ctx_for()

    assert merge_pr.when(ctx) is True

    Path(mod.PATHS.retry(2)).touch()
    assert merge_pr.when(ctx) is False


def test_merge_reached_sits_after_round_two_and_before_write_merge_state(tmp_path):
    mod = _import_merge_branch(cwd=tmp_path)
    stages = {s.name: s for s in _top_stages(mod.config.items)}
    names = [s.name for s in _top_stages(mod.config.items)]

    assert names.index("merge pr 2") < names.index("merge reached")
    assert names.index("merge reached") < names.index("write merge state")

    reached = stages["merge reached"]
    assert reached.on_failure is OnFailure.FAIL
    assert isinstance(reached.impl, _merge_state.HaltingCommand)

    os.makedirs(mod.WORK, exist_ok=True)
    ctx = ctx_for()
    assert reached.when(ctx) is True
    _merge_state.halt(mod.PATHS, "rebase_conflict", "conflict")
    assert reached.when(ctx) is False


def test_escalate_is_still_the_only_ask_user_stage_with_two_rounds(tmp_path):
    mod = _import_merge_branch(cwd=tmp_path)
    items = _top_stages(mod.config.items)
    assert items[-1].name == "escalate"
    ask_user_stages = [s.name for s in _walk_stages(mod.config.items) if s.on_failure is OnFailure.ASK_USER]
    assert ask_user_stages == ["escalate"]


# --- merge_reached_cmd ---------------------------------------------------------


def test_merge_reached_passes_when_merged_exists(tmp_path):
    paths = _paths(tmp_path)
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.merged).write_text("deadbeef\n")

    result = _run_sh(merge_branch.merge_reached_cmd(paths, str(tmp_path)), str(tmp_path))

    assert result.returncode == 0, result.stderr
    assert "deadbeef" in result.stdout
    assert not Path(paths.halt).exists()


def test_merge_reached_halts_no_merge_after_rounds_without_merged(tmp_path):
    paths = _paths(tmp_path)
    Path(paths.root).mkdir(parents=True, exist_ok=True)

    result = _run_sh(merge_branch.merge_reached_cmd(paths, str(tmp_path)), str(tmp_path))

    assert result.returncode == 0, result.stderr
    assert Path(paths.halt).is_file()
    reason = Path(paths.halt_reason).read_text().splitlines()
    assert reason[0] == "KIND: no_merge_after_rounds"
    assert reason[1] == _merge_state.HALTS["no_merge_after_rounds"].heading
    assert "no fast-forward merge after 2 round(s)" in reason[2]


# ---------------------------------------------------------------------------
# WatchBaseBuild — post-merge build watch on the base branch (step 18)
# ---------------------------------------------------------------------------


def _watch_prereqs(
    paths: _merge_state.MergePaths,
    *,
    repo: str = "acme/widgets",
    branch: str = "feature-x",
    base: str = "main",
    slug: str = "widgets",
    merged: str = "deadbeef",
) -> None:
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.repo).write_text(repo + "\n")
    Path(paths.branch).write_text(branch + "\n")
    Path(paths.base).write_text(base + "\n")
    Path(paths.slug).write_text(slug + "\n")
    Path(paths.merged).write_text(merged + "\n")


def _watch_stage(
    paths: _merge_state.MergePaths, project_dir: str, fc: _FakeClock
) -> _merge_gh.WatchBaseBuild:
    return _merge_gh.WatchBaseBuild(
        paths=paths,
        stage="watch base build",
        project_dir=project_dir,
        sleep=fc.sleep,
        clock=fc.clock,
    )


def _render_watch_summary(paths: _merge_state.MergePaths, project_dir: str) -> str:
    return _merge_state.render_summary(
        paths,
        rerun_cmd="norn run merge_branch --non-interactive",
        max_rounds=2,
        project_dir=project_dir,
    )


@pytest.mark.asyncio
async def test_watch_base_build_no_runs_within_grace_warns_without_halting(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    _watch_prereqs(paths)
    notify_calls = _record_notify(monkeypatch)

    bin_dir, _ = install_fake_gh(tmp_path, {("run", "list"): [(0, "[]", "")]})
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    fc = _FakeClock()
    stage = _watch_stage(paths, str(tmp_path), fc)
    result = await stage.run(ctx_for(env=env))

    assert result.success is True
    assert not Path(paths.halt).exists()
    assert fc.t >= _merge_gh.BASE_RUNS_GRACE

    data = json.loads(Path(paths.base_build).read_text())
    assert data["outcome"] == "no_runs"

    warnings_text = Path(paths.warnings).read_text()
    assert warnings_text.splitlines() == [
        f"no workflow ran on main @ deadbee within {_merge_gh.BASE_RUNS_GRACE} s after the merge"
    ]

    assert len(notify_calls) == 1
    heading, detail = notify_calls[0]
    assert heading == "Post-merge build: warning"
    assert "no workflow ran on main" in detail

    body = _render_watch_summary(paths, str(tmp_path))
    assert "Status: MERGED\n" in body
    assert "Warnings: 1" in body
    assert "## Warnings" in body
    assert "no workflow ran on main" in body
    assert "no workflow ran within 120 s (warning)" in body


@pytest.mark.asyncio
async def test_watch_base_build_gh_error_during_grace_halts_unexpected_failure(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    _watch_prereqs(paths)
    notify_calls = _record_notify(monkeypatch)

    bin_dir, _ = install_fake_gh(tmp_path, {("run", "list"): [(4, "", "gh: something broke\n")]})
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    fc = _FakeClock()
    stage = _watch_stage(paths, str(tmp_path), fc)
    result = await stage.run(ctx_for(env=env))

    assert result.success is True
    reason = Path(paths.halt_reason).read_text()
    assert reason.splitlines()[0] == "KIND: unexpected_failure"
    assert "watch base build" in reason
    assert not Path(paths.warnings).exists()
    assert not notify_calls


@pytest.mark.asyncio
async def test_watch_base_build_settles_green_after_in_progress(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    _watch_prereqs(paths)
    notify_calls = _record_notify(monkeypatch)

    in_progress = json.dumps([_run_entry(1, "", "in_progress", None)])
    completed = json.dumps([_run_entry(1, "", "completed", "success")])
    bin_dir, _ = install_fake_gh(
        tmp_path,
        {("run", "list"): [(0, in_progress, ""), (0, in_progress, ""), (0, in_progress, ""), (0, completed, "")]},
    )
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    fc = _FakeClock()
    stage = _watch_stage(paths, str(tmp_path), fc)
    result = await stage.run(ctx_for(env=env))

    assert result.success is True
    assert not Path(paths.halt).exists()

    data = json.loads(Path(paths.base_build).read_text())
    assert data["outcome"] == "passed"

    assert len(notify_calls) == 1
    heading, detail = notify_calls[0]
    assert heading == "Post-merge build finished"
    assert "passed" in detail


@pytest.mark.asyncio
async def test_watch_base_build_one_failure_halts_base_build_red(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    _watch_prereqs(paths)
    notify_calls = _record_notify(monkeypatch)

    failed = json.dumps([_run_entry(1, "", "completed", "failure")])
    bin_dir, _ = install_fake_gh(tmp_path, {("run", "list"): [(0, failed, "")]})
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    fc = _FakeClock()
    stage = _watch_stage(paths, str(tmp_path), fc)
    result = await stage.run(ctx_for(env=env))

    assert result.success is True
    assert Path(paths.halt).is_file()
    reason = Path(paths.halt_reason).read_text()
    assert reason.splitlines()[0] == "KIND: base_build_red"
    assert "run 1" in reason

    data = json.loads(Path(paths.base_build).read_text())
    assert data["outcome"] == "failed"

    assert len(notify_calls) == 1
    heading, detail = notify_calls[0]
    assert heading == "Post-merge build finished"
    assert "failed" in detail

    body = _render_watch_summary(paths, str(tmp_path))
    assert "Status: MERGED_NEEDS_ATTENTION" in body


def test_watch_base_build_stage_structure(tmp_path):
    mod = _import_merge_branch(cwd=tmp_path)
    names = [item.name for item in _top_stages(mod.config.items)]
    idx = names.index("merge reached")
    assert names[idx + 1] == "watch base build"
    assert names[idx + 2] == "write merge state"

    stages = {s.name: s for s in _top_stages(mod.config.items)}
    stage = stages["watch base build"]
    assert isinstance(stage.impl, _merge_gh.WatchBaseBuild)
    assert isinstance(stage.impl, _merge_state.HaltOnFailure)

    os.makedirs(mod.WORK, exist_ok=True)
    ctx = ctx_for()
    assert stage.when(ctx) is False  # no `merged` yet

    Path(mod.PATHS.merged).write_text("deadbeef\n")
    assert stage.when(ctx) is True

    _merge_state.halt(mod.PATHS, "rebase_conflict", "conflict")
    assert stage.when(ctx) is False


# ---------------------------------------------------------------------------
# merge_branch.py — the narrated summary (step 19)
# ---------------------------------------------------------------------------


def test_summary_writers_and_escalate_follow_in_order(tmp_path):
    mod = _import_merge_branch(cwd=tmp_path)
    items = mod.config.items
    write_idx = next(
        i for i, item in enumerate(items) if getattr(item, "name", None) == "write merge state"
    )
    assert type(items[write_idx + 1]).__name__ == "ClearContext"
    assert items[write_idx + 2].name == "summarize run"
    assert items[write_idx + 3].name == "finalize merge state"
    assert items[write_idx + 4].name == "escalate"


def test_summarize_run_is_light_generate_with_work_in_add_dirs(tmp_path):
    mod = _import_merge_branch(cwd=tmp_path)
    stages = {s.name: s for s in _top_stages(mod.config.items)}
    stage = stages["summarize run"]

    assert isinstance(stage.impl, Generate)
    assert not isinstance(stage.impl, _merge_state.HaltOnFailure)
    assert stage.impl.model == "light"
    assert stage.impl.add_dirs == [mod.WORK]
    assert stage.impl.permission_mode == "acceptEdits"
    assert set(stage.impl.allowed_tools) == {"Read", "Write"}
    assert stage.impl.max_turns == 10
    assert stage.impl.cwd is None
    assert stage.when is None
    assert stage.on_failure is OnFailure.FAIL


def test_finalize_merge_state_is_plain_write_merge_state_no_when(tmp_path):
    mod = _import_merge_branch(cwd=tmp_path)
    stages = {s.name: s for s in _top_stages(mod.config.items)}
    stage = stages["finalize merge state"]

    assert isinstance(stage.impl, _merge_state.WriteMergeState)
    assert not isinstance(stage.impl, _merge_state.HaltOnFailure)
    assert stage.when is None
    assert stage.on_failure is OnFailure.FAIL


def test_no_generate_stage_anywhere_sets_cwd(tmp_path):
    mod = _import_merge_branch(cwd=tmp_path)
    generate_stages = [s for s in _walk_stages(mod.config.items) if isinstance(s.impl, Generate)]
    assert generate_stages  # sanity: the walk actually found some
    for stage in generate_stages:
        assert stage.impl.cwd is None, stage.name

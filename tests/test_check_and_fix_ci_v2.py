"""Tests for norn.pipelines.check_and_fix_ci_v2.

Step 04 adds the first assertion: check_and_fix_ci.detect_test_cmd is the
same function object as norn.pipelines._test_cmd.detect_test_cmd, not a
copy. Step 12 adds the remaining reload-based tests: comma-separated
``--arg workflow=``, the comment-style rule in both fix prompts, and the
attribution guard in both commit stages.
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys

from norn.dsl import Loop, Parallel, Stage
from norn.pipelines._comment_style import COMMENT_RULE
from tests.merge_fakes import make_repo_with_remote


def _walk_stages(items):
    """Yield every Stage in *items*, recursing into Loop and Parallel bodies."""
    for item in items:
        if isinstance(item, Stage):
            yield item
        elif isinstance(item, (Loop, Parallel)):
            yield from _walk_stages(item.stages)


def _find_stage(items, name):
    for stage in _walk_stages(items):
        if stage.name == name:
            return stage
    raise KeyError(name)


def _import_check_and_fix_ci(argv_tail: list[str] = (), cwd=None):
    """Import (or reload) check_and_fix_ci with patched argv and cwd.

    Mirrors the merge_branch reload idiom (tests/test_merge_branch.py:
    _import_merge_branch): the module reads both ``sys.argv`` (for
    ``--arg`` flags) and ``os.getcwd()`` (for ``PROJECT_DIR``, which also
    feeds ``detect_test_cmd`` at import time) so both must be patched
    before the (re)import. ``_ensure_githubkit()`` is a no-op here because
    githubkit is already in the dev dependency group.
    """
    saved_argv = list(sys.argv)
    saved_cwd = os.getcwd()
    try:
        sys.argv = [saved_argv[0], *argv_tail]
        if cwd is not None:
            os.chdir(str(cwd))
        from norn.pipelines import check_and_fix_ci_v2 as check_and_fix_ci

        return importlib.reload(check_and_fix_ci)
    finally:
        sys.argv = saved_argv
        os.chdir(saved_cwd)


def test_detect_test_cmd_is_the_shared_function(tmp_path) -> None:
    mod = _import_check_and_fix_ci(cwd=tmp_path)

    from norn.pipelines import _test_cmd

    assert mod.detect_test_cmd is _test_cmd.detect_test_cmd


def test_check_and_fix_ci_still_imports(tmp_path) -> None:
    mod = _import_check_and_fix_ci(cwd=tmp_path)

    assert mod.config is not None


# ---------------------------------------------------------------------------
# WORKFLOWS: comma-separated --arg workflow=
# ---------------------------------------------------------------------------


def test_workflows_defaults_to_ci_yml(tmp_path) -> None:
    mod = _import_check_and_fix_ci(cwd=tmp_path)

    assert mod.WORKFLOWS == ["ci.yml"]


def test_workflows_splits_comma_separated_arg(tmp_path) -> None:
    mod = _import_check_and_fix_ci(["--arg", "workflow=a.yml,b.yml"], cwd=tmp_path)

    assert mod.WORKFLOWS == ["a.yml", "b.yml"]


def test_workflows_drops_blank_entries_and_strips_whitespace(tmp_path) -> None:
    mod = _import_check_and_fix_ci(["--arg", "workflow= a.yml , ,b.yml"], cwd=tmp_path)

    assert mod.WORKFLOWS == ["a.yml", "b.yml"]


# ---------------------------------------------------------------------------
# COMMENT_RULE in both fix prompts
# ---------------------------------------------------------------------------


def test_fix_local_prompt_contains_comment_rule(tmp_path) -> None:
    mod = _import_check_and_fix_ci(cwd=tmp_path)

    fix_local = _find_stage(mod.pipeline.items, "fix local")
    assert COMMENT_RULE in fix_local.impl.prompt


def test_fix_ci_prompt_contains_comment_rule(tmp_path) -> None:
    mod = _import_check_and_fix_ci(cwd=tmp_path)

    fix_ci = _find_stage(mod.pipeline.items, "fix ci")
    assert COMMENT_RULE in fix_ci.impl.prompt


# ---------------------------------------------------------------------------
# Commit stages: fixed subjects + attribution_guard_sh
# ---------------------------------------------------------------------------


def test_commit_local_fixes_keeps_fixed_subject_and_has_guard(tmp_path) -> None:
    mod = _import_check_and_fix_ci(cwd=tmp_path)

    stage = _find_stage(mod.pipeline.items, "commit local fixes")
    cmd = stage.impl.cmd
    assert 'git commit -m "fix: auto-fix from check_and_fix_ci local loop"' in cmd
    assert "refusing to commit" in cmd
    # No messages_range for this stage — the unpushed range can hold the
    # human's own commits.
    assert "@{u}..HEAD" not in cmd
    # The guard runs after staging and before the commit.
    assert cmd.index("git add -u") < cmd.index("refusing to commit") < cmd.index("git commit")


def test_commit_push_ci_fix_keeps_fixed_subject_and_has_guard(tmp_path) -> None:
    mod = _import_check_and_fix_ci(cwd=tmp_path)

    stage = _find_stage(mod.pipeline.items, "commit push ci fix")
    cmd = stage.impl.cmd
    assert 'git commit -m "fix(ci): auto-fix from check_and_fix_ci"' in cmd
    assert "refusing to commit" in cmd
    assert "@{u}..HEAD" in cmd
    assert cmd.index("git add -u") < cmd.index("refusing to commit") < cmd.index("git commit")


def test_commit_push_ci_fix_blocks_attribution_line(tmp_path) -> None:
    repo = make_repo_with_remote(tmp_path)
    repo.repo.git.checkout("-b", "feature-x")
    repo.repo.git.push("origin", "feature-x", "--set-upstream")

    with open(repo.path + "/tainted.txt", "w") as f:
        f.write("code\n# Generated with [Claude Code]\n")
    repo.repo.index.add(["tainted.txt"])

    before_head = repo.repo.head.commit.hexsha
    before_remote = repo.repo.git.rev_parse("origin/feature-x")

    mod = _import_check_and_fix_ci(cwd=repo.path)
    stage = _find_stage(mod.pipeline.items, "commit push ci fix")
    cmd = stage.impl.cmd

    result = subprocess.run(
        ["sh", "-c", cmd], cwd=repo.path, capture_output=True, text=True
    )

    assert result.returncode == 1
    assert "refusing to commit" in result.stderr

    after_head = repo.repo.git.rev_parse("HEAD")
    assert after_head == before_head

    after_remote = repo.repo.git.rev_parse("origin/feature-x")
    assert after_remote == before_remote

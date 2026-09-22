"""Tests for the fix_jira_issue bundled pipeline.

Covers pipeline structure (stage names, gate wiring, loop config, prompt paths,
child command construction, model/tools/turns) and the catalog entry.
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from norn.dsl import ClearContext, Loop, OnFailure, Stage
from norn.graph import build_graph
from norn.pipelines._launch_tree import AssertLaunchTree
from norn.stages.generate import Generate
from norn.stages.run_command import RunCommand
from norn.stages.validate import Validate


# ---------------------------------------------------------------------------
# Import helper
# ---------------------------------------------------------------------------


def _import_fix_jira(argv_tail: list[str], cwd: str | Path | None = None):
    """Import fix_jira_issue with patched argv and cwd.

    *argv_tail* contains the positional args (e.g. ``["CBS-2249"]`` or
    ``["CBS-2249", "stop"]``).  *cwd* overrides ``os.getcwd()`` so the module
    resolves artifact paths under a temp directory.
    """
    saved_argv = list(sys.argv)
    saved_cwd = os.getcwd()
    try:
        sys.argv = [saved_argv[0], *argv_tail]
        if cwd is not None:
            os.chdir(str(cwd))
        from norn.pipelines import fix_jira_issue

        return importlib.reload(fix_jira_issue)
    finally:
        sys.argv = saved_argv
        os.chdir(saved_cwd)


def _import_default(tmp_path):
    return _import_fix_jira(["CBS-2249"], cwd=tmp_path)


def _import_stop(tmp_path):
    return _import_fix_jira(["CBS-2249", "stop"], cwd=tmp_path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collect_top_names(items):
    """Return a list of top-level item names (or 'ClearContext')."""
    names = []
    for item in items:
        if isinstance(item, (Stage, Loop)):
            names.append(item.name)
        elif isinstance(item, ClearContext):
            names.append("ClearContext")
    return names


def _find_stage(items, name):
    """Find a top-level Stage by name."""
    for item in items:
        if isinstance(item, Stage) and item.name == name:
            return item
    raise KeyError(f"Stage {name!r} not found")


def _find_loop(items, name):
    """Find a top-level Loop by name."""
    for item in items:
        if isinstance(item, Loop) and item.name == name:
            return item
    raise KeyError(f"Loop {name!r} not found")


def _collect_generates(items):
    """Walk pipeline items and yield all Generate instances."""
    for item in items:
        if isinstance(item, Stage) and isinstance(item.impl, Generate):
            yield item.impl
        elif isinstance(item, Loop):
            yield from _collect_generates(item.stages)


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def test_metadata_has_one_arg_key(tmp_path):
    mod = _import_default(tmp_path)
    assert list(mod.metadata["args"].keys()) == ["args"]


def test_metadata_has_three_env_vars(tmp_path):
    mod = _import_default(tmp_path)
    env = mod.metadata["env_vars"]
    assert "ANTHROPIC_API_KEY" in env
    assert "JIRA_AUTH" in env
    assert "JIRA_BASE" in env
    assert len(env) == 3


# ---------------------------------------------------------------------------
# Top-level stage order
# ---------------------------------------------------------------------------


def test_top_level_stage_names(tmp_path):
    mod = _import_default(tmp_path)
    names = _collect_top_names(mod.config.items)
    assert names == [
        "assert launch tree",
        "preflight environment",
        "prepare branch",
        "fetch issue",
        "check issue files",
        "write brief",
        "brief written",
        "ClearContext",
        # Builder stages (from add_plan_review_stages):
        "preflight",
        "draft plan",
        "resolve open questions",
        "open questions resolved",
        "review plan",
        "review shape ok",
        "ClearContext",
        "apply review",
        "dispositions recorded",
        "ClearContext",
        "split plan",
        "step files written",
        "plan summary",
        # Post-builder stages:
        "implement",
        "diagnose implementation",
        "check implementation",
        "summary",
    ]


def test_graph_builds(tmp_path):
    """Sibling node ids must be unique, or the TUI cannot launch the run."""
    mod = _import_default(tmp_path)
    build_graph(mod.config)


# ---------------------------------------------------------------------------
# Child-run failure handling
# ---------------------------------------------------------------------------


def _ctx_spending(dollars: float):
    """Return a PipelineContext whose usage tracker has already spent *dollars*."""
    from norn.models import PipelineContext, UsageRecord

    ctx = PipelineContext()
    ctx.usage_tracker.add(UsageRecord(stage_name="spent", total_cost_usd=dollars))
    return ctx


def _ctx_with(result_name: str, success: bool):
    """Return a PipelineContext whose results hold one stage outcome."""
    from norn.models import PipelineContext, StageResult

    ctx = PipelineContext()
    ctx.results[result_name] = StageResult(name=result_name, success=success)
    return ctx


def test_check_implementation_is_skipped_when_the_child_failed(tmp_path):
    """The artifact gate must not speak for a child run that never got there."""
    mod = _import_default(tmp_path)
    stage = _find_stage(mod.config.items, "check implementation")
    assert stage.when is not None, "check implementation must be conditional"
    assert not stage.when(_ctx_with("implement", success=False))
    assert stage.when(_ctx_with("implement", success=True))


def test_diagnose_implementation_runs_only_on_a_failed_child(tmp_path):
    mod = _import_default(tmp_path)
    stage = _find_stage(mod.config.items, "diagnose implementation")
    assert stage.when(_ctx_with("implement", success=False))
    assert not stage.when(_ctx_with("implement", success=True))
    assert stage.on_failure is OnFailure.FAIL, (
        "a failed child run must end the pipeline, not prompt again"
    )


def test_diagnose_implementation_reads_the_child_journal(tmp_path):
    """The command must point at the child's state key, not the parent's."""
    mod = _import_default(tmp_path)
    cmd = _find_stage(mod.config.items, "diagnose implementation").impl.cmd
    assert "_child_run.py" in cmd
    assert "--state-key implement_features_v2" in cmd


def test_check_implementation_names_a_missing_review(tmp_path):
    """A child that passes without writing review.md needs its own message."""
    mod = _import_default(tmp_path)
    cmd = _find_stage(mod.config.items, "check implementation").impl.cmd
    assert "wrote no review.md" in cmd


# ---------------------------------------------------------------------------
# on_failure assertions
# ---------------------------------------------------------------------------


def test_brief_written_is_on_failure_fail(tmp_path):
    mod = _import_default(tmp_path)
    stage = _find_stage(mod.config.items, "brief written")
    assert stage.on_failure is OnFailure.FAIL


def test_check_issue_files_is_on_failure_fail(tmp_path):
    mod = _import_default(tmp_path)
    stage = _find_stage(mod.config.items, "check issue files")
    assert stage.on_failure is OnFailure.FAIL


def test_check_implementation_is_on_failure_fail(tmp_path):
    mod = _import_default(tmp_path)
    stage = _find_stage(mod.config.items, "check implementation")
    assert stage.on_failure is OnFailure.FAIL


def test_fetch_issue_is_on_failure_ask_user(tmp_path):
    mod = _import_default(tmp_path)
    stage = _find_stage(mod.config.items, "fetch issue")
    assert stage.on_failure is OnFailure.ASK_USER


def test_implement_is_on_failure_ask_user(tmp_path):
    mod = _import_default(tmp_path)
    stage = _find_stage(mod.config.items, "implement")
    assert stage.on_failure is OnFailure.ASK_USER


# ---------------------------------------------------------------------------
# check implementation details
# ---------------------------------------------------------------------------


def test_check_implementation_reads_verdict_pass(tmp_path):
    """The check implementation RunCommand reads line 1 for exact 'VERDICT: PASS'."""
    mod = _import_default(tmp_path)
    stage = _find_stage(mod.config.items, "check implementation")
    assert isinstance(stage.impl, RunCommand)
    assert 'VERDICT: PASS' in stage.impl.cmd


def test_verdict_needs_fixes_on_line1_fails(tmp_path):
    """A file with VERDICT: NEEDS_FIXES on line 1 and VERDICT: PASS later fails."""
    mod = _import_default(tmp_path)
    stage = _find_stage(mod.config.items, "check implementation")
    cmd = stage.impl.cmd
    # The command checks only line 1; VERDICT: PASS on a later line doesn't matter
    assert 'head -1' in cmd
    assert '"$LINE1" != "VERDICT: PASS"' in cmd


# ---------------------------------------------------------------------------
# Write brief loop config
# ---------------------------------------------------------------------------


def test_write_brief_loop_config(tmp_path):
    mod = _import_default(tmp_path)
    loop = _find_loop(mod.config.items, "write brief")
    assert loop.max_retries == 2
    assert loop.on_exhaust is OnFailure.ASK_USER
    assert len(loop.stages) == 2
    assert loop.stages[0].name == "clean issue"
    assert loop.stages[1].name == "check brief"


# ---------------------------------------------------------------------------
# Haiku stage (clean issue)
# ---------------------------------------------------------------------------


def test_clean_issue_light_model_tools_turns(tmp_path):
    mod = _import_default(tmp_path)
    loop = _find_loop(mod.config.items, "write brief")
    gen = loop.stages[0].impl
    assert isinstance(gen, Generate)
    assert gen.model == "light"
    assert "Read" in gen.allowed_tools
    assert "Write" in gen.allowed_tools
    assert "Glob" in gen.allowed_tools
    assert gen.max_turns == 30


# ---------------------------------------------------------------------------
# wait for plan approval only with stop
# ---------------------------------------------------------------------------


def test_wait_for_plan_approval_absent_without_stop(tmp_path):
    mod = _import_default(tmp_path)
    names = _collect_top_names(mod.config.items)
    assert "wait for plan approval" not in names


def test_wait_for_plan_approval_present_with_stop(tmp_path):
    mod = _import_stop(tmp_path)
    names = _collect_top_names(mod.config.items)
    assert "wait for plan approval" in names


# ---------------------------------------------------------------------------
# Child command
# ---------------------------------------------------------------------------


def test_child_command_contains_implement_features_v2(tmp_path):
    mod = _import_default(tmp_path)
    assert "-m norn run implement_features_v2" in mod.CHILD


def test_child_command_contains_steps_dir(tmp_path):
    mod = _import_default(tmp_path)
    assert mod.STEPS_DIR in mod.CHILD


def test_child_command_contains_non_interactive(tmp_path):
    mod = _import_default(tmp_path)
    assert "--non-interactive" in mod.CHILD


def test_child_command_has_no_static_budget(tmp_path):
    """The ceiling is resolved when the stage runs, not when the module loads."""
    mod = _import_default(tmp_path)
    assert "--arg budget" not in mod.CHILD


def test_child_budget_is_what_is_left_of_the_parent_ceiling(tmp_path):
    mod = _import_fix_jira(["CBS-2249", "--arg", "budget=50"], cwd=tmp_path)
    impl = _find_stage(mod.config.items, "implement").impl

    assert impl.remaining_budget(_ctx_spending(7.6)) == pytest.approx(42.4)


def test_child_budget_follows_a_launch_time_override(tmp_path):
    """`norn run --budget` and the TUI field both land after import."""
    from norn.dsl import apply_budget_overrides

    mod = _import_default(tmp_path)
    impl = _find_stage(mod.config.items, "implement").impl
    apply_budget_overrides(mod.config, max_cost_usd=120.0)

    assert impl.remaining_budget(_ctx_spending(20.0)) == pytest.approx(100.0)


def test_child_budget_never_drops_to_zero(tmp_path):
    """An exhausted parent still starts the child, which reports its own stop."""
    mod = _import_fix_jira(["CBS-2249", "--arg", "budget=10"], cwd=tmp_path)
    impl = _find_stage(mod.config.items, "implement").impl

    assert impl.remaining_budget(_ctx_spending(99.0)) == mod.MIN_CHILD_BUDGET


def test_child_budget_is_omitted_when_the_parent_has_none(tmp_path):
    from norn.models import PipelineContext

    mod = _import_default(tmp_path)
    impl = _find_stage(mod.config.items, "implement").impl
    mod.config.budgets = []
    assert impl.remaining_budget(PipelineContext()) is None


@pytest.mark.asyncio
async def test_child_command_carries_the_budget_at_run_time(tmp_path, monkeypatch):
    """The flag is appended to the command the stage actually shells out."""
    mod = _import_fix_jira(["CBS-2249", "--arg", "budget=50"], cwd=tmp_path)
    impl = _find_stage(mod.config.items, "implement").impl

    from norn.models import StageResult
    from norn.stages.run_command import RunCommand as _RunCommand

    seen = {}

    async def _fake_run(self, ctx, **kwargs):
        seen["cmd"] = self.cmd
        return StageResult(name="implement", success=True)

    monkeypatch.setattr(_RunCommand, "run", _fake_run)

    await impl.run(_ctx_spending(5.0))
    assert seen["cmd"].endswith("--arg budget=45.0")
    assert "--non-interactive" in seen["cmd"]


def test_bad_budget_arg_raises(tmp_path):
    with pytest.raises(ValueError, match="budget"):
        _import_fix_jira(["CBS-2249", "--arg", "budget=lots"], cwd=tmp_path)


def test_implement_timeout_is_none(tmp_path):
    mod = _import_default(tmp_path)
    stage = _find_stage(mod.config.items, "implement")
    assert isinstance(stage.impl, RunCommand)
    assert stage.impl.timeout is None


def test_fetch_timeout_is_default(tmp_path):
    """fetch issue uses the default timeout (not None)."""
    mod = _import_default(tmp_path)
    stage = _find_stage(mod.config.items, "fetch issue")
    assert isinstance(stage.impl, RunCommand)
    # RunCommand default timeout is DEFAULT_TIMEOUT_SECONDS (3600.0)
    from norn.stages.run_command import DEFAULT_TIMEOUT_SECONDS
    assert stage.impl.timeout == DEFAULT_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# Generate stages have no pinned cwd
# ---------------------------------------------------------------------------


def test_generate_stages_have_no_pinned_cwd(tmp_path):
    mod = _import_default(tmp_path)
    generates = list(_collect_generates(mod.config.items))
    assert generates, "expected at least one Generate stage"
    for gen in generates:
        assert gen.cwd is None


# ---------------------------------------------------------------------------
# Preflight names all three ignore paths
# ---------------------------------------------------------------------------


def test_preflight_names_all_ignore_paths(tmp_path):
    mod = _import_default(tmp_path)
    stage = _find_stage(mod.config.items, "preflight environment")
    cmd = stage.impl.cmd
    assert "tmp/jira/CBS-2249" in cmd
    assert "fix_jira_issue.checkpoint" in cmd
    assert "implement_features_v2.checkpoint" in cmd


# ---------------------------------------------------------------------------
# Prepare branch refuses main
# ---------------------------------------------------------------------------


def test_prepare_branch_refuses_main(tmp_path):
    mod = _import_default(tmp_path)
    stage = _find_stage(mod.config.items, "prepare branch")
    cmd = stage.impl.cmd
    assert "main" in cmd
    assert "master" in cmd


# ---------------------------------------------------------------------------
# Prepare branch reuses a branch that already names the issue
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _repo_on_branch(tmp_path, branch: str) -> Path:
    """Init a repo with one commit, checked out on *branch*."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# test\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    if branch != "main":
        _git(repo, "switch", "-c", branch)
    return repo


def _run_prepare_branch(mod, repo: Path) -> subprocess.CompletedProcess:
    cmd = _find_stage(mod.config.items, "prepare branch").impl.cmd
    return subprocess.run(cmd, shell=True, cwd=repo, capture_output=True, text=True)


@pytest.mark.parametrize("branch", [
    "CBS-2249",                      # exactly the key
    "feature/CBS-2249-sorting",      # key inside a path-style name
    "bugfix/cbs-2249",               # lowercased by hand
])
def test_prepare_branch_stays_on_a_branch_naming_the_issue(tmp_path, branch):
    repo = _repo_on_branch(tmp_path, branch)
    mod = _import_fix_jira(["CBS-2249"], cwd=repo)

    r = _run_prepare_branch(mod, repo)

    assert r.returncode == 0, r.stderr
    assert _git(repo, "branch", "--show-current") == branch
    branches = _git(repo, "branch", "--list", "--format=%(refname:short)").split()
    assert sorted(branches) == sorted(["main", branch]), (
        f"no second branch should have been cut, got {branches}"
    )


def test_prepare_branch_creates_the_branch_when_the_name_is_unrelated(tmp_path):
    repo = _repo_on_branch(tmp_path, "scratch")
    mod = _import_fix_jira(["CBS-2249"], cwd=repo)

    r = _run_prepare_branch(mod, repo)

    assert r.returncode == 0, r.stderr
    assert _git(repo, "branch", "--show-current") == "CBS-2249"


def test_prepare_branch_leaves_main_for_the_issue_branch(tmp_path):
    repo = _repo_on_branch(tmp_path, "main")
    mod = _import_fix_jira(["CBS-2249"], cwd=repo)

    r = _run_prepare_branch(mod, repo)

    assert r.returncode == 0, r.stderr
    assert _git(repo, "branch", "--show-current") == "CBS-2249"


def test_prepare_branch_honours_an_explicit_branch_arg(tmp_path):
    """An explicit --arg branch is an instruction, not a default to second-guess."""
    repo = _repo_on_branch(tmp_path, "feature/CBS-2249-sorting")
    mod = _import_fix_jira(["CBS-2249", "--arg", "branch=hotfix-2249"], cwd=repo)

    r = _run_prepare_branch(mod, repo)

    assert r.returncode == 0, r.stderr
    assert _git(repo, "branch", "--show-current") == "hotfix-2249"


def test_prepare_branch_writes_base_sha_when_it_stays_put(tmp_path):
    """The reuse path must still record the base commit later stages read."""
    repo = _repo_on_branch(tmp_path, "CBS-2249")
    mod = _import_fix_jira(["CBS-2249"], cwd=repo)

    r = _run_prepare_branch(mod, repo)

    assert r.returncode == 0, r.stderr
    base_sha = Path(mod.ART.dir + "base.sha")
    assert base_sha.read_text().strip() == _git(repo, "rev-parse", "HEAD")

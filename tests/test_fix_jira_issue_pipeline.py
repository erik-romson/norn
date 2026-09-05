"""Tests for the fix_jira_issue bundled pipeline.

Covers pipeline structure (stage names, gate wiring, loop config, prompt paths,
child command construction, model/tools/turns) and the catalog entry.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from norn.dsl import ClearContext, Loop, OnFailure, Stage
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
        "codex review",
        "check review shape",
        "ClearContext",
        "apply review",
        "dispositions recorded",
        "ClearContext",
        "split plan",
        "step files written",
        "summary",
        # Post-builder stages:
        "implement",
        "check implementation",
        "summary",
    ]


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


def test_clean_issue_haiku_model_tools_turns(tmp_path):
    mod = _import_default(tmp_path)
    loop = _find_loop(mod.config.items, "write brief")
    gen = loop.stages[0].impl
    assert isinstance(gen, Generate)
    assert gen.model == "haiku"
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

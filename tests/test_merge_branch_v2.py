"""Tests for the ``merge_branch_v2`` pipeline module (norn/pipelines/merge_branch_v2.py).

v2 is a parallel copy of the frozen ``merge_branch_v1`` that runs
``check_and_fix_ci_v2`` as its CI-fix child. The shared helpers
(``_merge_gh.py``, ``_merge_state.py``) are tested in the frozen
``tests/test_merge_branch_v1.py`` and ``tests/test_merge_state.py``.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

import pytest

from norn.dsl import Loop, Parallel, Pipeline, Stage
from norn.pipelines import _merge_gh, _merge_state
from tests.merge_fakes import ctx_for


def _import(module_name: str, argv_tail: list[str] = (), cwd=None):
    """Import (or reload) a merge pipeline with patched argv and cwd.

    The module reads ``sys.argv`` and ``os.getcwd()`` at import time.
    """
    saved_argv = list(sys.argv)
    saved_cwd = os.getcwd()
    try:
        sys.argv = [saved_argv[0], *argv_tail]
        if cwd is not None:
            os.chdir(str(cwd))
        module = importlib.import_module(module_name)
        return importlib.reload(module)
    finally:
        sys.argv = saved_argv
        os.chdir(saved_cwd)


def _names(items) -> list[str]:
    out: list[str] = []
    for item in items:
        if isinstance(item, Stage):
            out.append(item.name)
        elif isinstance(item, (Loop, Parallel)):
            out.append(f"{type(item).__name__}:{item.name}")
            out.extend(_names(item.stages))
    return out


def _walk(items):
    for item in items:
        if isinstance(item, Stage):
            yield item
        elif isinstance(item, (Loop, Parallel)):
            yield from _walk(item.stages)


def test_pipeline_name(tmp_path):
    mod = _import("norn.pipelines.merge_branch_v2", cwd=tmp_path)
    assert mod.config.name == "merge_branch_v2"


def test_stage_graph_equals_v1(tmp_path):
    v1 = _import("norn.pipelines.merge_branch_v1", cwd=tmp_path)
    v2 = _import("norn.pipelines.merge_branch_v2", cwd=tmp_path)
    assert _names(v2.config.items) == _names(v1.config.items)
    assert len(_names(v2.config.items)) > 10


def test_rerun_cmd_names_v2(tmp_path):
    mod = _import("norn.pipelines.merge_branch_v2", cwd=tmp_path)
    assert mod.RERUN_CMD == "norn run merge_branch_v2 --non-interactive"


def test_assert_fresh_run_is_built_for_v2(tmp_path):
    mod = _import("norn.pipelines.merge_branch_v2", cwd=tmp_path)
    stage = next(s for s in _walk(mod.config.items) if isinstance(s.impl, _merge_state.AssertFreshRun))
    assert stage.impl._pipeline == "merge_branch_v2"


@pytest.mark.asyncio
async def test_assert_fresh_run_message_names_v2():
    stage = _merge_state.AssertFreshRun(pipeline="merge_branch_v2")
    result = await stage.run(ctx_for(checkpoint=("resolve branch",)))
    assert result.success is False
    assert "merge_branch_v2" in result.error
    assert "merge_branch_v1" not in result.error
    result = await stage.run(ctx_for(params={"args": "x"}))
    assert "merge_branch_v2" in result.error


def _ci_fix_children(mod):
    return [s.impl for s in _walk(mod.config.items) if isinstance(s.impl, _merge_gh.CiFixChild)]


def test_ci_fix_children_use_check_and_fix_ci_v2(tmp_path):
    mod = _import("norn.pipelines.merge_branch_v2", cwd=tmp_path)
    children = _ci_fix_children(mod)
    assert len(children) == 2
    assert all(c._ci_pipeline == "check_and_fix_ci_v2" for c in children)


def test_v1_ci_fix_children_keep_check_and_fix_ci_v1(tmp_path):
    mod = _import("norn.pipelines.merge_branch_v1", cwd=tmp_path)
    children = _ci_fix_children(mod)
    assert children
    assert all(c._ci_pipeline == "check_and_fix_ci_v1" for c in children)


def _child(tmp_path: Path, **kwargs) -> tuple[_merge_state.MergePaths, _merge_gh.CiFixChild]:
    paths = _merge_state.MergePaths(str(tmp_path / "norn-work"))
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    stage = _merge_gh.CiFixChild(
        paths=paths,
        stage="fix ci 1",
        n=1,
        project_dir=str(tmp_path),
        pipeline=Pipeline("merge_branch_test").budget(max_cost_usd=15.0),
        test_cmd="true",
        **kwargs,
    )
    return paths, stage


def test_ci_fix_child_builds_v2_command(tmp_path):
    paths, stage = _child(tmp_path, ci_pipeline="check_and_fix_ci_v2")
    Path(paths.ci_fix_workflows(1)).write_text("ci.yml\n")
    cmd = stage._build_cmd(ctx_for())
    assert "-m norn run check_and_fix_ci_v2 --non-interactive" in cmd
    assert "check_and_fix_ci_v1" not in cmd


def test_ci_fix_child_default_is_v1(tmp_path):
    paths, stage = _child(tmp_path)
    Path(paths.ci_fix_workflows(1)).write_text("ci.yml\n")
    cmd = stage._build_cmd(ctx_for())
    assert "-m norn run check_and_fix_ci_v1 --non-interactive" in cmd
    assert stage._runs_dir() == tmp_path / "check_and_fix_ci_v1.runs"


def test_ci_fix_child_cmd_default_and_override():
    default = _merge_gh.ci_fix_child_cmd("/p", ["ci.yml"], "true", 5.0)
    assert "-m norn run check_and_fix_ci_v1 --non-interactive" in default
    v2 = _merge_gh.ci_fix_child_cmd("/p", ["ci.yml"], "true", 5.0, pipeline="check_and_fix_ci_v2")
    assert v2 == default.replace("check_and_fix_ci_v1", "check_and_fix_ci_v2")


@pytest.mark.asyncio
async def test_ci_fix_child_reads_v2_journal(tmp_path):
    run_dir = tmp_path / "check_and_fix_ci_v2.runs" / "1"
    (run_dir / "stages").mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"success": False, "failed_stage": "commit push ci fix"}))
    (run_dir / "stages" / "0001-stage_x-a1.json").write_text(
        json.dumps({"node_id": "stage:commit push ci fix", "attempt": 1, "error": "pytest failed: 2 tests"})
    )
    paths, stage = _child(tmp_path, ci_pipeline="check_and_fix_ci_v2", child_cmd="sh -c 'exit 3'")

    result = await stage.run(ctx_for())

    assert result.success is True
    reason = Path(paths.halt_reason).read_text()
    assert reason.splitlines()[0] == "KIND: ci_fix_failed"
    assert "commit push ci fix" in reason
    data = json.loads(Path(paths.ci_fix(1)).read_text())
    assert data["journal"] == str(run_dir)


@pytest.mark.asyncio
async def test_ci_fix_child_v2_with_no_journal_names_v2(tmp_path):
    paths, stage = _child(tmp_path, ci_pipeline="check_and_fix_ci_v2", child_cmd="sh -c 'exit 1'")
    await stage.run(ctx_for())
    assert "check_and_fix_ci_v2 left no journal" in Path(paths.halt_reason).read_text()

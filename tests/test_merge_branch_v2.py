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
import subprocess
import sys
from pathlib import Path

import pytest

from norn.dsl import Loop, Parallel, Pipeline, Stage
from norn.pipelines import _merge_gh, _merge_state
from tests.merge_fakes import ctx_for, install_fake_gh


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


# ---------------------------------------------------------------------------
# followups in the PR body, and the learning pointer in the summary (step 17)
# ---------------------------------------------------------------------------

FOLLOWUPS_TEXT = (
    "# Follow-ups — demo\n\n## Unapplied review findings\n\n- minor: rename x\n- nit: spacing\n\n"
    "## Other\n\n- not for the PR\n"
)


def _add_followups_stages(mod) -> list:
    return [s for s in _walk(mod.config.items) if s.name.startswith("add followups to pr")]


def test_no_followups_stage_when_knob_unset(tmp_path):
    mod = _import("norn.pipelines.merge_branch_v2", cwd=tmp_path)
    assert _add_followups_stages(mod) == []


def test_followups_stage_follows_open_or_reuse_in_each_round(tmp_path):
    followups = tmp_path / "followups.md"
    mod = _import("norn.pipelines.merge_branch_v2", ["--arg", f"followups={followups}"], cwd=tmp_path)
    names = _names(mod.config.items)
    for n in (1, 2):
        assert names.index(f"add followups to pr {n}") == names.index(f"open or reuse pr {n}") + 1
    assert mod.FOLLOWUPS == str(followups)
    assert f"followups={followups}" in mod.RERUN_CMD


def test_followups_relative_path_resolves_against_launch_dir(tmp_path):
    mod = _import("norn.pipelines.merge_branch_v2", ["--arg", "followups=out/followups.md"], cwd=tmp_path)
    assert Path(mod.FOLLOWUPS).is_absolute()
    assert mod.FOLLOWUPS.endswith(os.path.join("out", "followups.md"))
    assert Path(mod.FOLLOWUPS).parent.parent.samefile(tmp_path)


def test_followups_stage_skipped_when_file_missing(tmp_path):
    followups = tmp_path / "followups.md"
    mod = _import("norn.pipelines.merge_branch_v2", ["--arg", f"followups={followups}"], cwd=tmp_path)
    stage = _add_followups_stages(mod)[0]
    assert stage.when is not None
    ctx = ctx_for()
    assert stage.when(ctx) is False
    followups.write_text(FOLLOWUPS_TEXT)
    assert stage.when(ctx) is True


def _followups_env(tmp_path: Path, body: str):
    paths = _merge_state.MergePaths(str(tmp_path / "norn-work"))
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.repo).write_text("acme/widgets\n")
    Path(paths.pr).write_text("42\n")
    bin_dir, calls_log = install_fake_gh(
        tmp_path,
        {("pr", "view"): [(0, body, "")], ("pr", "edit"): [(0, "", "")]},
    )
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    return paths, env, calls_log


def _calls(calls_log: str) -> list[list[str]]:
    return [json.loads(line) for line in Path(calls_log).read_text().splitlines() if line.strip()]


def _run_followups(paths, env, tmp_path, followups: Path, n: int = 1):
    cmd = _merge_gh.pr_followups_body_cmd(paths, str(tmp_path), str(followups), n)
    return subprocess.run(["sh", "-c", cmd], cwd=str(tmp_path), env=env, capture_output=True, text=True)


def test_followups_body_appended_to_pr_without_block(tmp_path):
    paths, env, calls_log = _followups_env(tmp_path, "Human written body.\n")
    followups = tmp_path / "followups.md"
    followups.write_text(FOLLOWUPS_TEXT)

    result = _run_followups(paths, env, tmp_path, followups)

    assert result.returncode == 0, result.stderr
    assert not Path(paths.halt).exists()
    edits = [c for c in _calls(calls_log) if c[:2] == ["pr", "edit"]]
    assert len(edits) == 1
    assert edits[0][:5] == ["pr", "edit", "42", "-R", "acme/widgets"]
    body = Path(edits[0][edits[0].index("--body-file") + 1]).read_text()
    assert body.startswith("Human written body.")
    assert body.count(_merge_gh.FOLLOWUPS_START) == 1
    assert body.count(_merge_gh.FOLLOWUPS_END) == 1
    assert "## Unapplied review findings" in body
    assert "- minor: rename x" in body
    assert "not for the PR" not in body
    assert body.index(_merge_gh.FOLLOWUPS_START) < body.index("- nit: spacing") < body.index(_merge_gh.FOLLOWUPS_END)
    assert str(tmp_path / "norn-work") in edits[0][edits[0].index("--body-file") + 1]


def test_followups_body_replaces_old_block(tmp_path):
    old = (
        "Human body.\n\n"
        f"{_merge_gh.FOLLOWUPS_START}\n## Unapplied review findings\n\n- OLD finding\n{_merge_gh.FOLLOWUPS_END}\n\n"
        "Trailer text.\n"
    )
    paths, env, calls_log = _followups_env(tmp_path, old)
    followups = tmp_path / "followups.md"
    followups.write_text(FOLLOWUPS_TEXT)

    result = _run_followups(paths, env, tmp_path, followups, n=2)

    assert result.returncode == 0, result.stderr
    edit = next(c for c in _calls(calls_log) if c[:2] == ["pr", "edit"])
    body = Path(edit[edit.index("--body-file") + 1]).read_text()
    assert "OLD finding" not in body
    assert body.count(_merge_gh.FOLLOWUPS_START) == 1
    assert body.count(_merge_gh.FOLLOWUPS_END) == 1
    assert "- minor: rename x" in body
    assert "Human body." in body and "Trailer text." in body


def test_followups_attribution_halts_without_editing_pr(tmp_path):
    paths, env, calls_log = _followups_env(tmp_path, "Body.\n")
    followups = tmp_path / "followups.md"
    followups.write_text(FOLLOWUPS_TEXT + "\nCo-Authored-By: Claude <noreply@anthropic.com>\n")

    result = _run_followups(paths, env, tmp_path, followups)

    assert result.returncode == 0
    assert Path(paths.halt).exists()
    assert Path(paths.halt_reason).read_text().splitlines()[0] == "KIND: attribution_found"
    assert [c for c in _calls(calls_log) if c[:2] == ["pr", "edit"]] == []


def test_followups_without_section_fails_without_editing_pr(tmp_path):
    paths, env, calls_log = _followups_env(tmp_path, "Body.\n")
    followups = tmp_path / "followups.md"
    followups.write_text("# Follow-ups\n\n## Other\n\n- x\n")

    result = _run_followups(paths, env, tmp_path, followups)

    assert result.returncode != 0
    assert [c for c in _calls(calls_log) if c[:2] == ["pr", "edit"]] == []


def test_summary_learning_pointer_present_for_v2(tmp_path):
    mod = _import("norn.pipelines.merge_branch_v2", cwd=tmp_path)
    journal = tmp_path / "check_and_fix_ci_v2.runs" / "1"
    journal.mkdir(parents=True)
    (journal / "learning.md").write_text("---\ntitle: x\n---\n")
    paths = _merge_state.MergePaths(str(tmp_path / "work"))
    Path(paths.root).mkdir(parents=True)
    Path(paths.ci_red(1)).write_text("")
    Path(paths.merged).write_text("deadbeef\n")
    Path(paths.ci_fix(1)).write_text(json.dumps({"outcome": "ok", "journal": str(journal)}))
    for stage in (s.impl for s in _walk(mod.config.items) if isinstance(s.impl, _merge_state.WriteMergeState)):
        assert stage._learning_pointers is True

    body = _merge_state.render_summary(
        paths,
        rerun_cmd="r",
        max_rounds=2,
        project_dir=str(tmp_path),
        learning_pointers=True,
    )
    assert f"Learning proposal: {journal}/learning.md" in body


def test_summary_learning_pointer_absent_without_proposal_file(tmp_path):
    journal = tmp_path / "runs" / "1"
    journal.mkdir(parents=True)
    paths = _merge_state.MergePaths(str(tmp_path / "work"))
    Path(paths.root).mkdir(parents=True)
    Path(paths.merged).write_text("deadbeef\n")
    Path(paths.ci_fix(1)).write_text(json.dumps({"outcome": "ok", "journal": str(journal)}))
    body = _merge_state.render_summary(
        paths, rerun_cmd="r", max_rounds=2, project_dir=str(tmp_path), learning_pointers=True
    )
    assert "Learning proposal:" not in body

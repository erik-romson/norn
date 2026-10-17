"""Tests for norn/pipelines/_child_cost_v3.py (fix_jira_issue_v3 step 03).

Fake children are shell commands in ``tmp_path``; nothing here reaches a real
agent, ``gh`` or ``osascript``.
"""
from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path

import pytest

from norn.dsl import Pipeline
from norn.metrics import compute_metrics
from norn.models import PipelineContext, UsageRecord
from norn.pipelines import _child_cost_v3, _merge_gh, _merge_state
from norn.pipelines._child_cost_v3 import (
    CI_CHILD_ATTEMPTS,
    ChildCostError,
    CiFixChildV3,
    RunChildPipelineV3,
    child_diagnosis,
    child_usage,
    ci_fix_timeout,
    remaining_budget,
)

PIPELINES = Path(_child_cost_v3.__file__).parent
FAILED_STAGE = "validate step-03 fix-jira-v3-child-cost"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo, check=True, capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    (tmp_path / "tracked.txt").write_text("one\n")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


@pytest.fixture
def notices(monkeypatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    async def fake_notify(title: str, detail: str) -> None:
        calls.append((title, detail))

    monkeypatch.setattr(_child_cost_v3, "notify", fake_notify)
    return calls


def _ctx() -> PipelineContext:
    return PipelineContext(params={}, checkpoint_stages=set(), env={}, working_dir=None)


def _payload(
    root: Path,
    *,
    cost: float | None = 6.09,
    success: bool = True,
    cost_mode: str = "tracked",
    raw_run_json: str | None = None,
    stage_error: str | None = None,
) -> Path:
    """Write the run directory a fake child will copy into its runs dir."""
    payload = root / "payload"
    (payload / "stages").mkdir(parents=True)
    if raw_run_json is not None:
        (payload / "run.json").write_text(raw_run_json)
    else:
        run: dict = {
            "success": success,
            "failed_stage": None if success else FAILED_STAGE,
            "cost_mode": cost_mode,
            "duration_ms": 90_000,
        }
        if cost is not None:
            run["totals"] = {"cost": cost, "output_tokens": 1234, "billed_input_tokens": 14_000_000}
        (payload / "run.json").write_text(json.dumps(run))
    if stage_error is not None:
        (payload / "stages" / "0001-stage_x-a1.json").write_text(
            json.dumps({"node_id": f"stage:{FAILED_STAGE}", "attempt": 1, "error": stage_error})
        )
    return payload


def _child_script(root: Path, runs_dir: Path, payload: Path | None, *, exit_code: int, sleep: int = 0) -> str:
    """Return a shell command that plays a child: make run dir 7 and exit."""
    lines = [f"mkdir -p {runs_dir}"]
    if payload is not None:
        lines.append(f"cp -R {payload} {runs_dir / '7'}")
    if sleep:
        lines.append(f"sleep {sleep}")
    lines.append(f"exit {exit_code}")
    script = root / "child.sh"
    script.write_text("\n".join(lines) + "\n")
    return f"sh {script}"


def _stage(
    root: Path,
    cmd: str,
    *,
    ceiling: float | None = 75.0,
    marker: str | None = None,
) -> RunChildPipelineV3:
    pipeline = Pipeline("parent")
    if ceiling is not None:
        pipeline = pipeline.budget(max_cost_usd=ceiling)
    return RunChildPipelineV3(
        cmd=cmd,
        pipeline=pipeline,
        runs_dir=str(root / "implement_features_v3.runs"),
        rewritten_marker=marker or str(root / "rewritten"),
    )


def _existing_run_six(root: Path) -> Path:
    runs = root / "implement_features_v3.runs"
    (runs / "6").mkdir(parents=True)
    return runs


# ---------------------------------------------------------------------------
# RunChildPipelineV3 against a fake child
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pass_attaches_usage_without_session(repo, notices):
    runs = _existing_run_six(repo)
    cmd = _child_script(repo, runs, _payload(repo), exit_code=0)

    result = await _stage(repo, cmd).run(_ctx())

    assert result.success is True
    assert result.usage is not None
    assert result.usage.total_cost_usd == 6.09
    assert result.usage.session_id is None
    assert result.usage.output_tokens == 1234
    assert result.usage.billed_input_tokens == 0
    assert "child cost $6.09" in result.output["child_cost"]
    assert notices == []


@pytest.mark.asyncio
async def test_failure_names_stage_and_stage_error(repo, notices):
    runs = _existing_run_six(repo)
    payload = _payload(repo, success=False, stage_error="AssertionError: boom\nline two")
    cmd = _child_script(repo, runs, payload, exit_code=1)

    result = await _stage(repo, cmd).run(_ctx())

    assert result.success is False
    assert result.error.splitlines()[0] == f"implement_features_v3 failed at stage '{FAILED_STAGE}'"
    assert "AssertionError: boom" in result.error
    assert str(runs / "7") in result.error
    assert result.usage is not None and result.usage.total_cost_usd == 6.09
    assert "restore" not in result.error.lower()


@pytest.mark.asyncio
async def test_no_new_run_dir_means_no_usage(repo, notices):
    runs = _existing_run_six(repo)
    cmd = _child_script(repo, runs, None, exit_code=1)

    result = await _stage(repo, cmd).run(_ctx())

    assert result.success is False
    assert result.usage is None
    assert "child cost unknown" in result.output["child_cost"]
    assert "before it started its journal" in result.error


@pytest.mark.asyncio
async def test_untracked_cost_mode_means_no_usage(repo, notices):
    runs = _existing_run_six(repo)
    cmd = _child_script(repo, runs, _payload(repo, cost_mode="zero_unknown"), exit_code=0)

    result = await _stage(repo, cmd).run(_ctx())

    assert result.success is True
    assert result.usage is None
    assert "child cost unknown" in result.output["child_cost"]


@pytest.mark.asyncio
async def test_run_json_without_totals_means_no_usage(repo, notices):
    runs = _existing_run_six(repo)
    cmd = _child_script(repo, runs, _payload(repo, cost=None), exit_code=0)

    result = await _stage(repo, cmd).run(_ctx())

    assert result.usage is None
    assert "no totals" in result.output["child_cost"]


@pytest.mark.asyncio
async def test_malformed_run_json_is_a_failed_result_not_an_exception(repo, notices):
    runs = _existing_run_six(repo)
    cmd = _child_script(repo, runs, _payload(repo, raw_run_json="{not json"), exit_code=0)

    result = await _stage(repo, cmd).run(_ctx())

    assert result.success is False
    assert "cannot read the child's" in result.error
    assert str(runs / "7" / "run.json") in result.error


@pytest.mark.asyncio
async def test_dirty_tracked_file_adds_restore_hint(repo, notices):
    runs = _existing_run_six(repo)
    (repo / "tracked.txt").write_text("edited by the failed step\n")
    payload = _payload(repo, success=False, stage_error="tests failed")
    cmd = _child_script(repo, runs, payload, exit_code=1)

    result = await _stage(repo, cmd).run(_ctx())

    assert "git restore ." in result.error
    assert "[r]etry" in result.error


@pytest.mark.asyncio
async def test_timeout_keeps_the_timeout_headline(repo, notices):
    runs = _existing_run_six(repo)
    cmd = _child_script(repo, runs, _payload(repo, success=False), exit_code=0, sleep=5)
    stage = _stage(repo, cmd)
    stage.timeout = 1

    result = await stage.run(_ctx())

    assert result.success is False
    assert result.error.splitlines()[0].startswith("command timed out after 1s")
    assert result.usage is not None


# ---------------------------------------------------------------------------
# Remainder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remainder_is_passed_and_shrinks_after_child_usage(repo, notices):
    runs = _existing_run_six(repo)
    cmd = _child_script(repo, runs, _payload(repo), exit_code=0)
    stage = _stage(repo, cmd, ceiling=75.0)
    ctx = _ctx()
    ctx.usage_tracker.add(UsageRecord(stage_name="plan", total_cost_usd=17.83))

    assert stage.remaining_budget(ctx) == 57.17
    result = await stage.run(ctx)

    assert stage.cmd.endswith("--arg budget=57.17")
    ctx.usage_tracker.add(result.usage)
    assert stage.remaining_budget(ctx) == 51.08


@pytest.mark.asyncio
async def test_no_cost_limit_means_no_budget_arg(repo, notices):
    runs = _existing_run_six(repo)
    cmd = _child_script(repo, runs, _payload(repo), exit_code=0)
    stage = _stage(repo, cmd, ceiling=None)

    assert stage.remaining_budget(_ctx()) is None
    await stage.run(_ctx())

    assert "--arg budget" not in stage.cmd
    assert remaining_budget(Pipeline("p"), _ctx()) is None


def test_remainder_floors_at_one_dollar():
    ctx = _ctx()
    ctx.usage_tracker.add(UsageRecord(stage_name="x", total_cost_usd=80.0))
    assert remaining_budget(Pipeline("p").budget(max_cost_usd=75.0), ctx) == 1.0


# ---------------------------------------------------------------------------
# Ordering of the budget notice
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crossing_the_ceiling_notifies_once_before_returning(repo, notices):
    runs = _existing_run_six(repo)
    cmd = _child_script(repo, runs, _payload(repo, cost=6.09), exit_code=0)
    ctx = _ctx()
    ctx.usage_tracker.add(UsageRecord(stage_name="plan", total_cost_usd=5.0))

    await _stage(repo, cmd, ceiling=10.0).run(ctx)

    assert len(notices) == 1
    assert notices[0][0] == "Budget reached"


@pytest.mark.asyncio
async def test_staying_under_the_ceiling_does_not_notify(repo, notices):
    runs = _existing_run_six(repo)
    cmd = _child_script(repo, runs, _payload(repo, cost=6.09), exit_code=0)

    await _stage(repo, cmd, ceiling=75.0).run(_ctx())

    assert notices == []


# ---------------------------------------------------------------------------
# Rewritten marker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rewritten_marker_fails_without_running_the_command(repo, notices):
    ran = repo / "ran"
    marker = repo / "rewritten"
    marker.write_text("x")
    stage = _stage(repo, f"touch {ran}", marker=str(marker))

    result = await stage.run(_ctx())

    assert result.success is False
    assert "already rewritten" in result.error
    assert "--resume" in result.error
    assert not ran.exists()


# ---------------------------------------------------------------------------
# child_usage / child_diagnosis directly
# ---------------------------------------------------------------------------


def test_child_usage_duration_comes_from_the_journal(tmp_path):
    record, text = child_usage(_payload(tmp_path))
    assert record is not None
    assert record.duration_ms == 90_000
    assert record.num_turns == 0
    assert text == "child cost $6.09"


def test_child_usage_malformed_raises(tmp_path):
    with pytest.raises(ChildCostError):
        child_usage(_payload(tmp_path, raw_run_json="[1, 2"))


def test_child_diagnosis_names_the_next_action_last(repo):
    run_dir = _payload(repo, success=False, stage_error="\n".join(f"e{i}" for i in range(100)))
    text = child_diagnosis(run_dir, str(repo), child="implement_features_v3")
    lines = text.splitlines()
    assert lines[0] == f"implement_features_v3 failed at stage '{FAILED_STAGE}'"
    assert "e99" in lines and "e59" not in lines
    assert lines[-1].startswith("Fix the cause")


# ---------------------------------------------------------------------------
# CiFixChildV3
# ---------------------------------------------------------------------------


def _ci_stage(root: Path, *, ceiling: float = 75.0, ci_timeout_minutes: int = 60):
    paths = _merge_state.MergePaths(str(root / "norn-work"))
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.ci_fix_workflows(1)).write_text("ci.yml\nrelease.yml\n")
    test_cmd_file = root / "test-cmd"
    stage = CiFixChildV3(
        paths=paths,
        stage="fix ci 1",
        n=1,
        project_dir=str(root),
        pipeline=Pipeline("parent").budget(max_cost_usd=ceiling),
        test_cmd_file=str(test_cmd_file),
        ci_timeout_minutes=ci_timeout_minutes,
    )
    return paths, stage, test_cmd_file


def test_ci_child_command_is_built_from_files_read_at_run_time(tmp_path):
    paths, stage, test_cmd_file = _ci_stage(tmp_path)
    test_cmd_file.write_text("make test\n")
    ctx = _ctx()
    ctx.usage_tracker.add(UsageRecord(stage_name="x", total_cost_usd=4.0))

    cmd = stage._build_cmd(ctx)

    assert "check_and_fix_ci_v2" in cmd
    assert "workflow=ci.yml,release.yml" in cmd
    assert "make test" in cmd
    assert "--budget 71.0" in cmd
    assert cmd.endswith("--arg ci_timeout_minutes=60 --arg skip_local=true")
    assert stage._runs_dir() == tmp_path / "check_and_fix_ci_v2.runs"
    assert stage.timeout == 14_400


def test_ci_fix_timeout_formula():
    assert ci_fix_timeout(60) == 14_400
    assert ci_fix_timeout(20) == CI_CHILD_ATTEMPTS * (20 * 60 + 900) + 900


@pytest.mark.asyncio
async def test_ci_child_pass_attaches_usage(tmp_path, notices):
    paths, stage, test_cmd_file = _ci_stage(tmp_path)
    runs = tmp_path / "check_and_fix_ci_v2.runs"
    (runs / "6").mkdir(parents=True)
    stage._build_cmd = lambda ctx: _child_script(tmp_path, runs, _payload(tmp_path), exit_code=0)

    result = await stage.run(_ctx())

    assert result.success is True
    assert not Path(paths.halt).exists()
    assert result.usage.total_cost_usd == 6.09
    assert result.usage.session_id is None


@pytest.mark.asyncio
async def test_ci_child_timeout_halts_and_still_attaches_usage(tmp_path, notices):
    paths, stage, _ = _ci_stage(tmp_path)
    runs = tmp_path / "check_and_fix_ci_v2.runs"
    (runs / "6").mkdir(parents=True)
    stage.timeout = 1
    stage._build_cmd = lambda ctx: _child_script(
        tmp_path, runs, _payload(tmp_path), exit_code=0, sleep=5
    )

    result = await stage.run(_ctx())

    assert result.success is True
    assert Path(paths.halt_reason).read_text().splitlines()[0] == "KIND: ci_fix_timeout"
    assert result.usage is not None and result.usage.total_cost_usd == 6.09


@pytest.mark.asyncio
async def test_ci_child_failure_halts_and_still_attaches_usage(tmp_path, notices):
    paths, stage, _ = _ci_stage(tmp_path)
    runs = tmp_path / "check_and_fix_ci_v2.runs"
    (runs / "6").mkdir(parents=True)
    payload = _payload(tmp_path, success=False, stage_error="push rejected")
    stage._build_cmd = lambda ctx: _child_script(tmp_path, runs, payload, exit_code=3)

    result = await stage.run(_ctx())

    assert result.success is True
    reason = Path(paths.halt_reason).read_text()
    assert reason.splitlines()[0] == "KIND: ci_fix_failed"
    assert FAILED_STAGE in reason
    assert result.usage.total_cost_usd == 6.09


@pytest.mark.asyncio
async def test_ci_child_budget_notice_fires_when_ceiling_crossed(tmp_path, notices):
    paths, stage, _ = _ci_stage(tmp_path, ceiling=10.0)
    runs = tmp_path / "check_and_fix_ci_v2.runs"
    (runs / "6").mkdir(parents=True)
    stage._build_cmd = lambda ctx: _child_script(tmp_path, runs, _payload(tmp_path), exit_code=0)
    ctx = _ctx()
    ctx.usage_tracker.add(UsageRecord(stage_name="x", total_cost_usd=5.0))

    await stage.run(ctx)

    assert len(notices) == 1


# ---------------------------------------------------------------------------
# Guards on what this module copies or mirrors
# ---------------------------------------------------------------------------


def test_ci_fix_child_still_has_the_hooks_the_subclass_uses():
    for name in ("_build_cmd", "_remaining_budget", "_runs_dir"):
        assert callable(getattr(_merge_gh.CiFixChild, name))


def _ci_v2_tree() -> ast.Module:
    return ast.parse((PIPELINES / "check_and_fix_ci_v2.py").read_text())


def test_child_attempts_mirror_check_and_fix_ci_v2():
    values = [
        node.value.value
        for node in ast.walk(_ci_v2_tree())
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "MAX_FIX_ATTEMPTS" for t in node.targets)
        and isinstance(node.value, ast.Constant)
    ]
    assert values == [3]
    assert CI_CHILD_ATTEMPTS == 3


def test_check_and_fix_ci_v2_declares_ci_timeout_minutes():
    keys = [
        key.value
        for node in ast.walk(_ci_v2_tree())
        if isinstance(node, ast.Dict)
        for key in node.keys
        if isinstance(key, ast.Constant)
    ]
    assert "ci_timeout_minutes" in keys


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_metrics_accept_a_command_stage_with_child_usage(tmp_path):
    rd = tmp_path / "test.runs" / "1"
    (rd / "stages").mkdir(parents=True)
    (rd / "run.json").write_text(json.dumps({
        "pipeline": "test", "run_id": 1, "provider": "claude-code", "success": True,
        "duration_ms": 5000, "totals": {"retries": 0},
    }))
    (rd / "stages" / "0001-implement-a1.json").write_text(json.dumps({
        "node_id": "implement", "attempt": 1, "status": "passed", "success": True,
        "duration_ms": 90_000,
        "details": {"cmd": "norn run implement_features_v3", "timeout": None},
        "usage": {
            "input_tokens": 0, "output_tokens": 1234,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
            "total_cost_usd": 6.09, "duration_ms": 90_000, "num_turns": 0,
            "model_id": None, "model": None, "tool_uses": 0, "tool_errors": 0,
            "session_id": None,
        },
    }))

    metrics = compute_metrics(rd)

    assert metrics.cost_usd == pytest.approx(6.09)


# ---------------------------------------------------------------------------
# fix_issue_v1 additions: pipeline name and artifact directory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rewritten_marker_error_names_the_art_dir(repo, notices):
    marker = repo / "rewritten"
    marker.write_text("x")
    stage = RunChildPipelineV3(
        cmd="true",
        pipeline=Pipeline("parent"),
        runs_dir=str(repo / "implement_features_v3.runs"),
        rewritten_marker=str(marker),
        art_dir="/x/tmp/github/123/",
    )

    result = await stage.run(_ctx())

    assert result.success is False
    assert "delete /x/tmp/github/123/ and start from a fresh branch." in result.error
    assert "tmp/jira" not in result.error


@pytest.mark.asyncio
async def test_rewritten_marker_error_default_keeps_the_jira_text(repo, notices):
    marker = repo / "rewritten"
    marker.write_text("x")

    result = await _stage(repo, "true", marker=str(marker)).run(_ctx())

    assert "delete tmp/jira/<KEY>/ and start from a fresh branch." in result.error


@pytest.mark.asyncio
async def test_budget_notice_carries_a_non_default_pipeline_name(repo, monkeypatch):
    calls: list[tuple[str, str, dict]] = []

    async def fake_notify(title, detail, **kwargs):
        calls.append((title, detail, kwargs))

    monkeypatch.setattr(_child_cost_v3, "notify", fake_notify)
    runs = _existing_run_six(repo)
    cmd = _child_script(repo, runs, _payload(repo, cost=6.09), exit_code=0)
    stage = RunChildPipelineV3(
        cmd=cmd,
        pipeline=Pipeline("parent").budget(max_cost_usd=10.0),
        runs_dir=str(repo / "implement_features_v3.runs"),
        rewritten_marker=str(repo / "rewritten"),
        pipeline_name="fix_issue_v1",
    )
    ctx = _ctx()
    ctx.usage_tracker.add(UsageRecord(stage_name="plan", total_cost_usd=5.0))

    await stage.run(ctx)

    assert len(calls) == 1
    assert calls[0][2] == {"pipeline": "fix_issue_v1"}


@pytest.mark.asyncio
async def test_budget_notice_default_still_works_with_a_two_argument_fake(repo, notices):
    runs = _existing_run_six(repo)
    cmd = _child_script(repo, runs, _payload(repo, cost=6.09), exit_code=0)
    ctx = _ctx()
    ctx.usage_tracker.add(UsageRecord(stage_name="plan", total_cost_usd=5.0))

    await _stage(repo, cmd, ceiling=10.0).run(ctx)

    assert [n[0] for n in notices] == ["Budget reached"]


@pytest.mark.asyncio
async def test_ci_child_budget_notice_carries_a_non_default_pipeline_name(tmp_path, monkeypatch):
    calls: list[dict] = []

    async def fake_notify(title, detail, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(_child_cost_v3, "notify", fake_notify)
    paths = _merge_state.MergePaths(str(tmp_path / "norn-work"))
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.ci_fix_workflows(1)).write_text("ci.yml\n")
    stage = CiFixChildV3(
        paths=paths,
        stage="fix ci 1",
        n=1,
        project_dir=str(tmp_path),
        pipeline=Pipeline("parent").budget(max_cost_usd=10.0),
        test_cmd_file=str(tmp_path / "test-cmd"),
        ci_timeout_minutes=60,
        pipeline_name="fix_issue_v1",
    )
    runs = tmp_path / "check_and_fix_ci_v2.runs"
    (runs / "6").mkdir(parents=True)
    stage._build_cmd = lambda ctx: _child_script(tmp_path, runs, _payload(tmp_path), exit_code=0)
    ctx = _ctx()
    ctx.usage_tracker.add(UsageRecord(stage_name="x", total_cost_usd=5.0))

    await stage.run(ctx)

    assert calls == [{"pipeline": "fix_issue_v1"}]

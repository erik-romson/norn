"""Tests for the child-run journal reader used by fix_jira_issue.

The reader turns a child pipeline's exit code back into the stage it actually
died on, so the parent stops reporting a missing artifact as the failure.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from norn.pipelines._child_run import failed_stage_error, latest_run_dir, main, report


# ---------------------------------------------------------------------------
# Journal builder
# ---------------------------------------------------------------------------


def _write_run(
    launch_dir: Path,
    state_key: str,
    run_id: int,
    *,
    success: bool,
    failed_stage: str | None = None,
    stages: list[dict] | None = None,
) -> Path:
    """Write one run directory in norn's journal layout and return it."""
    run_dir = launch_dir / f"{state_key}.runs" / str(run_id)
    (run_dir / "stages").mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({
        "pipeline": state_key,
        "run_id": run_id,
        "success": success,
        "failed_stage": failed_stage,
    }))
    for i, stage in enumerate(stages or [], start=1):
        name = stage["node_id"].split(":")[-1]
        path = run_dir / "stages" / f"{i:04d}-stage_{name}-a{stage.get('attempt', 1)}.json"
        path.write_text(json.dumps(stage))
    return run_dir


# ---------------------------------------------------------------------------
# latest_run_dir
# ---------------------------------------------------------------------------


def test_latest_run_dir_picks_the_highest_number_not_the_newest_name(tmp_path):
    runs = tmp_path / "p.runs"
    for n in (1, 2, 10):
        (runs / str(n)).mkdir(parents=True)
    assert latest_run_dir(runs).name == "10", "run 10 must beat run 2 lexically"


def test_latest_run_dir_ignores_non_numeric_children(tmp_path):
    runs = tmp_path / "p.runs"
    (runs / "1").mkdir(parents=True)
    (runs / "scratch").mkdir()
    assert latest_run_dir(runs).name == "1"


def test_latest_run_dir_is_none_without_a_journal(tmp_path):
    assert latest_run_dir(tmp_path / "missing.runs") is None


# ---------------------------------------------------------------------------
# failed_stage_error
# ---------------------------------------------------------------------------


def test_failed_stage_error_returns_the_last_attempt(tmp_path):
    run_dir = _write_run(
        tmp_path, "child", 1,
        success=False,
        failed_stage="commit step-01",
        stages=[
            {"node_id": "stage:commit step-01", "attempt": 1, "error": "first try"},
            {"node_id": "stage:commit step-01", "attempt": 2, "error": "second try"},
        ],
    )
    assert failed_stage_error(run_dir, "commit step-01") == "second try"


def test_failed_stage_error_matches_a_nested_node_id(tmp_path):
    """A stage inside a loop carries a parent prefix in its node id."""
    run_dir = _write_run(
        tmp_path, "child", 1,
        success=False,
        failed_stage="run validation",
        stages=[{"node_id": "loop:validate/stage:run validation", "error": "tests failed"}],
    )
    assert failed_stage_error(run_dir, "run validation") == "tests failed"


def test_failed_stage_error_is_none_when_no_stage_matches(tmp_path):
    run_dir = _write_run(
        tmp_path, "child", 1,
        success=False,
        failed_stage="ghost",
        stages=[{"node_id": "stage:other", "error": "unrelated"}],
    )
    assert failed_stage_error(run_dir, "ghost") is None


def test_failed_stage_error_survives_a_corrupt_stage_file(tmp_path):
    run_dir = _write_run(
        tmp_path, "child", 1,
        success=False,
        failed_stage="commit",
        stages=[{"node_id": "stage:commit", "error": "real error"}],
    )
    (run_dir / "stages" / "0099-stage_commit-a1.json").write_text("{not json")
    assert failed_stage_error(run_dir, "commit") == "real error"


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def test_report_names_the_failed_stage_and_fails(tmp_path, capsys):
    _write_run(
        tmp_path, "child", 1,
        success=False,
        failed_stage="assert owned diff step-03",
        stages=[{"node_id": "stage:assert owned diff step-03", "error": "changed nothing"}],
    )
    rc = report(tmp_path, "child")
    out = capsys.readouterr().out
    assert rc == 1
    assert "assert owned diff step-03" in out
    assert "changed nothing" in out


def test_report_reads_the_newest_run(tmp_path, capsys):
    _write_run(tmp_path, "child", 1, success=False, failed_stage="old failure")
    _write_run(tmp_path, "child", 2, success=False, failed_stage="new failure")
    report(tmp_path, "child")
    out = capsys.readouterr().out
    assert "new failure" in out
    assert "old failure" not in out


def test_report_passes_when_the_child_succeeded(tmp_path, capsys):
    _write_run(tmp_path, "child", 1, success=True)
    assert report(tmp_path, "child") == 0
    assert "passed" in capsys.readouterr().out


def test_report_fails_loudly_when_there_is_no_journal(tmp_path, capsys):
    """No journal means the child died before norn started it — still a failure."""
    rc = report(tmp_path, "child")
    out = capsys.readouterr().out
    assert rc == 1
    assert "none found" in out


def test_report_tails_a_long_error(tmp_path, capsys):
    _write_run(
        tmp_path, "child", 1,
        success=False,
        failed_stage="noisy",
        stages=[{"node_id": "stage:noisy", "error": "\n".join(f"line {i}" for i in range(200))}],
    )
    report(tmp_path, "child", tail=5)
    out = capsys.readouterr().out
    assert "line 199" in out
    assert "line 100" not in out
    assert "omitted" in out


def test_report_handles_a_failed_run_with_no_named_stage(tmp_path, capsys):
    _write_run(tmp_path, "child", 1, success=False, failed_stage=None)
    assert report(tmp_path, "child") == 1
    assert "names no stage" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_main_reports_via_the_cli(tmp_path, capsys):
    _write_run(tmp_path, "child", 1, success=False, failed_stage="boom")
    rc = main(["_child_run.py", "report", "--launch-dir", str(tmp_path), "--state-key", "child"])
    assert rc == 1
    assert "boom" in capsys.readouterr().out


def test_main_rejects_an_unknown_subcommand(tmp_path):
    with pytest.raises(SystemExit):
        main(["_child_run.py", "explode"])

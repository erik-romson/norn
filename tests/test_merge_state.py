"""Tests for norn/pipelines/_merge_state.py: work root, halt machinery,
HaltOnFailure, AssertFreshRun, Escalate, the summary skeleton, notify, warn.

All offline — no subprocess except a local `sh -c` for the shell-fragment
tests, no network, no agent calls.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from norn.alerts import MacOSChannel
from norn.models import PipelineContext, StageResult
from norn.pipelines import _merge_gh, _merge_state
from norn.stages.base import BaseStage
from norn.stages.compress_test_log import CompressTestLog
from norn.stages.generate import Generate
from tests.merge_fakes import ctx_for, make_repo_with_remote

# ---------------------------------------------------------------------------
# work_root
# ---------------------------------------------------------------------------


def test_work_root_is_deterministic():
    a = _merge_state.work_root("/some/project")
    b = _merge_state.work_root("/some/project")
    assert a == b


def test_work_root_contains_version_marker():
    root = _merge_state.work_root("/some/project")
    assert "/norn-merge/v1/" in (root + "/")


def test_work_root_differs_by_project():
    a = _merge_state.work_root("/some/project")
    b = _merge_state.work_root("/some/other/project")
    assert a != b


# ---------------------------------------------------------------------------
# halt_shell
# ---------------------------------------------------------------------------


def test_halt_shell_writes_kind_heading_detail_and_touches_halt(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    script = _merge_state.halt_shell(paths, "rebase_conflict", detail_cmd="echo 'see conflicts.txt'")
    result = subprocess.run(["sh", "-c", script], cwd=str(tmp_path), capture_output=True, text=True)
    assert result.returncode == 0

    text = Path(paths.halt_reason).read_text()
    lines = text.splitlines()
    assert lines[0] == "KIND: rebase_conflict"
    assert lines[1] == _merge_state.HALTS["rebase_conflict"].heading
    assert "see conflicts.txt" in text
    assert Path(paths.halt).exists()


def test_halt_shell_without_detail_cmd(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    script = _merge_state.halt_shell(paths, "unexpected_failure")
    result = subprocess.run(["sh", "-c", script], cwd=str(tmp_path), capture_output=True, text=True)
    assert result.returncode == 0
    assert Path(paths.halt).exists()
    text = Path(paths.halt_reason).read_text()
    assert text.splitlines()[0] == "KIND: unexpected_failure"


def test_halt_shell_unknown_kind_raises(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    with pytest.raises(KeyError):
        _merge_state.halt_shell(paths, "not_a_real_kind")


# ---------------------------------------------------------------------------
# halt (Python twin)
# ---------------------------------------------------------------------------


def test_halt_unknown_kind_raises(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    with pytest.raises(KeyError):
        _merge_state.halt(paths, "not_a_real_kind", "detail")


def test_halt_writes_reason_and_touches_halt(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    _merge_state.halt(paths, "rebase_conflict", "the detail text")
    text = Path(paths.halt_reason).read_text()
    lines = text.splitlines()
    assert lines[0] == "KIND: rebase_conflict"
    assert lines[1] == _merge_state.HALTS["rebase_conflict"].heading
    assert "the detail text" in text
    assert Path(paths.halt).is_file()


# ---------------------------------------------------------------------------
# predicates
# ---------------------------------------------------------------------------


def test_running_and_halted_predicates(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    ctx = ctx_for()
    assert _merge_state.running(paths)(ctx) is True
    assert _merge_state.halted(paths)(ctx) is False

    Path(paths.halt).touch()

    assert _merge_state.running(paths)(ctx) is False
    assert _merge_state.halted(paths)(ctx) is True


def test_all_of(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    ctx = ctx_for()
    Path(paths.halt).touch()

    assert _merge_state.all_of(_merge_state.halted(paths), lambda c: True)(ctx) is True
    assert _merge_state.all_of(_merge_state.halted(paths), lambda c: False)(ctx) is False
    assert _merge_state.all_of(_merge_state.running(paths), lambda c: True)(ctx) is False


# ---------------------------------------------------------------------------
# AssertFreshRun
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assert_fresh_run_fails_on_checkpoint_stages():
    stage = _merge_state.AssertFreshRun()
    result = await stage.run(ctx_for(checkpoint=("resolve branch",)))
    assert result.success is False
    assert "does not resume" in result.error


@pytest.mark.asyncio
async def test_assert_fresh_run_fails_on_positional_args():
    stage = _merge_state.AssertFreshRun()
    result = await stage.run(ctx_for(params={"args": "some positional text"}))
    assert result.success is False
    assert "positional arguments" in result.error
    assert "some positional text" in result.error


@pytest.mark.asyncio
async def test_assert_fresh_run_passes_otherwise():
    stage = _merge_state.AssertFreshRun()
    result = await stage.run(ctx_for())
    assert result.success is True


# ---------------------------------------------------------------------------
# Escalate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalate_shape(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    _merge_state.halt(paths, "rebase_conflict", "conflict detail")
    summary_file = str(tmp_path / "widgets-merge-state.md")
    Path(paths.summary_path).write_text(summary_file)

    stage = _merge_state.Escalate(paths=paths)
    result = await stage.run(ctx_for())

    assert result.success is False
    assert "needs human intervention" in result.error
    assert _merge_state.HALTS["rebase_conflict"].heading in result.error
    assert summary_file in result.error
    assert "[a]bort" in result.error
    assert "[c]ontinue" in result.error
    assert "[r]etry" in result.error


# ---------------------------------------------------------------------------
# render_summary
# ---------------------------------------------------------------------------


def test_render_summary_halted_work_root(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    Path(paths.repo).write_text("acme/widgets")
    Path(paths.branch).write_text("feature-x")
    Path(paths.base).write_text("main")
    Path(paths.base_source).write_text("default branch")
    Path(paths.start_head).write_text("abc123")
    Path(paths.slug).write_text("widgets")
    _merge_state.halt(paths, "rebase_conflict", "see conflicts.txt")

    body = _merge_state.render_summary(
        paths,
        rerun_cmd="norn run merge_branch_v1 --non-interactive",
        max_rounds=1,
        project_dir=str(tmp_path),
    )

    assert "Status: HALTED" in body
    assert "## Why it stopped" in body
    assert _merge_state.HALTS["rebase_conflict"].next_step in body
    assert "acme/widgets" in body
    assert "feature-x" in body


def test_render_summary_missing_optional_files_render_as_none(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    Path(paths.merged).write_text("deadbeef\n")
    body = _merge_state.render_summary(
        paths, rerun_cmd="norn run merge_branch_v1 --non-interactive", max_rounds=2, project_dir=str(tmp_path)
    )
    assert "Status: MERGED" in body
    assert "(none)" in body


def test_render_summary_local_tests_section_reports_commit_sha(tmp_path):
    repo = make_repo_with_remote(tmp_path / "checkout")
    repo.repo.git.checkout("-b", "feature-x")
    start_head = repo.repo.head.commit.hexsha
    (Path(repo.path) / "f.txt").write_text("one\n")
    repo.repo.index.add(["f.txt"])
    repo.repo.index.commit(_merge_state.commit_subject(1))
    fix_sha = repo.repo.head.commit.hexsha

    paths = _merge_state.MergePaths(str(tmp_path / "work"))
    Path(paths.start_head).parent.mkdir(parents=True, exist_ok=True)
    Path(paths.start_head).write_text(start_head)
    Path(paths.slug).write_text("widgets")
    Path(paths.merged).write_text(fix_sha)

    body = _merge_state.render_summary(
        paths, rerun_cmd="norn run merge_branch_v1 --non-interactive", max_rounds=2, project_dir=repo.path
    )

    assert f"Commit: {fix_sha}" in body


def test_render_summary_local_tests_section_reports_no_commit_when_none_made(tmp_path):
    repo = make_repo_with_remote(tmp_path / "checkout")

    paths = _merge_state.MergePaths(str(tmp_path / "work"))
    Path(paths.start_head).parent.mkdir(parents=True, exist_ok=True)
    Path(paths.start_head).write_text(repo.repo.head.commit.hexsha)
    Path(paths.slug).write_text("widgets")
    Path(paths.merged).write_text(repo.repo.head.commit.hexsha)

    body = _merge_state.render_summary(
        paths, rerun_cmd="norn run merge_branch_v1 --non-interactive", max_rounds=2, project_dir=repo.path
    )

    assert "Commit: (none)" in body


def test_halts_include_wait_for_checks_kinds():
    for kind in ("checks_red", "checks_timeout", "checks_none", "pr_head_moved"):
        assert kind in _merge_state.HALTS
        assert _merge_state.HALTS[kind].heading
        assert _merge_state.HALTS[kind].next_step


def test_halts_include_ci_fix_kinds():
    for kind in ("ci_fix_failed", "ci_fix_timeout", "ci_fix_unpushed", "copilot_rerequest_failed"):
        assert kind in _merge_state.HALTS
        assert _merge_state.HALTS[kind].heading
        assert _merge_state.HALTS[kind].next_step


def test_halts_include_copilot_timeout():
    assert "copilot_timeout" in _merge_state.HALTS
    assert _merge_state.HALTS["copilot_timeout"].heading
    assert _merge_state.HALTS["copilot_timeout"].next_step


def test_render_summary_builds_section_renders_table_and_poll_plan(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.slug).write_text("widgets")
    Path(paths.merged).write_text("deadbeef\n")
    Path(paths.checks(1)).write_text(
        json.dumps(
            {
                "outcome": "green",
                "per_workflow_estimate": {"ci.yml": 120.0},
                "plan": {"pre_sleep": 5.0, "interval": 30.0, "timeout": 600.0},
                "checks": [
                    {
                        "workflow": "ci.yml",
                        "name": "build",
                        "bucket": "pass",
                        "startedAt": "2024-01-01T00:00:00Z",
                        "completedAt": "2024-01-01T00:02:00Z",
                        "link": "https://ci/build",
                    }
                ],
            }
        )
    )

    body = _merge_state.render_summary(
        paths, rerun_cmd="norn run merge_branch_v1 --non-interactive", max_rounds=1, project_dir=str(tmp_path)
    )

    assert "## Builds" in body
    assert "| ci.yml | build | pass | 2m00s | 120s | https://ci/build |" in body
    assert "Poll plan: pre_sleep=5s, interval=30s, timeout=600s" in body


def test_render_summary_builds_section_missing_file_renders_as_none(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.slug).write_text("widgets")
    Path(paths.merged).write_text("deadbeef\n")

    body = _merge_state.render_summary(
        paths, rerun_cmd="norn run merge_branch_v1 --non-interactive", max_rounds=1, project_dir=str(tmp_path)
    )

    builds_index = body.index("## Builds")
    following = body[builds_index : builds_index + 40]
    assert "(none)" in following


def test_render_summary_ci_fix_section_missing_files_degrade_to_none(tmp_path):
    """``CI-RED-1`` present but every other CI-fix file missing (the run
    halted inside ``fix ci 1`` before writing them) still renders — nothing
    raises, and the unwritten fields read as ``(none)``.
    """
    paths = _merge_state.MergePaths(str(tmp_path))
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.slug).write_text("widgets")
    Path(paths.ci_red(1)).touch()
    Path(paths.merged).write_text("deadbeef\n")

    body = _merge_state.render_summary(
        paths, rerun_cmd="norn run merge_branch_v1 --non-interactive", max_rounds=2, project_dir=str(tmp_path)
    )

    assert "## CI fix" in body
    ci_fix_index = body.index("## CI fix")
    following = body[ci_fix_index : ci_fix_index + 300]
    assert "Workflows: (none) | Child: (none) | Journal: (none)" in following
    assert "Commits: (none)" in following


def test_halts_include_merge_preconditions_and_merge_kinds():
    for kind in (
        "review_required",
        "merge_state",
        "checks_changed",
        "head_moved_since_checks",
        "merge_rejected",
        "merged_pr_not_closed",
        "branch_delete_failed",
    ):
        assert kind in _merge_state.HALTS
        assert _merge_state.HALTS[kind].heading
        assert _merge_state.HALTS[kind].next_step


def test_halts_include_post_merge_build_kinds():
    for kind in ("base_build_red", "base_build_timeout"):
        assert kind in _merge_state.HALTS
        assert _merge_state.HALTS[kind].heading
        assert _merge_state.HALTS[kind].next_step


def test_render_summary_merge_section_missing_files_degrade_to_none(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.slug).write_text("widgets")
    # Nothing merged, so the run can only have reached the summary by halting.
    _merge_state.halt(paths, "no_merge_after_rounds", "the base kept moving")

    body = _merge_state.render_summary(
        paths, rerun_cmd="norn run merge_branch_v1 --non-interactive", max_rounds=2, project_dir=str(tmp_path)
    )

    assert "## Merge" in body
    merge_index = body.index("## Merge")
    following = body[merge_index : merge_index + 200]
    assert "Preconditions: (none)" in following
    assert "Merged: (none)" in following
    assert "Branch deleted: (n/a)" in following


def test_render_summary_merge_section_renders_preconditions_and_merged_sha(tmp_path):
    repo = make_repo_with_remote(tmp_path / "checkout")
    repo.repo.git.checkout("-b", "feature-x")
    (Path(repo.path) / "f.txt").write_text("one\n")
    repo.repo.index.add(["f.txt"])
    repo.repo.index.commit("feature commit")
    repo.repo.remote("origin").push("feature-x")
    green = repo.repo.head.commit.hexsha

    paths = _merge_state.MergePaths(str(tmp_path / "work"))
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.slug).write_text("widgets")
    Path(paths.branch).write_text("feature-x\n")
    Path(paths.merge_state(1)).write_text(
        json.dumps(
            {
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
                "reviewDecision": "APPROVED",
                "isDraft": False,
                "headRefOid": green,
            }
        )
    )
    Path(paths.merged).write_text(green + "\n")
    # Delete the remote branch, as `merge pr n` would have done on success.
    repo.repo.remote("origin").push(refspec=":feature-x")

    body = _merge_state.render_summary(
        paths, rerun_cmd="norn run merge_branch_v1 --non-interactive", max_rounds=1, project_dir=repo.path
    )

    assert "## Merge" in body
    section = body[body.index("## Merge") :]
    assert "mergeable=MERGEABLE" in section
    assert "mergeStateStatus=CLEAN" in section
    assert f"Merged: {green}" in section
    assert "Branch deleted: yes" in section


# --- the three-valued Status (step 17) -----------------------------------------


def _status_work_root(tmp_path) -> _merge_state.MergePaths:
    paths = _merge_state.MergePaths(str(tmp_path))
    Path(paths.root).mkdir(parents=True, exist_ok=True)
    Path(paths.repo).write_text("acme/widgets")
    Path(paths.branch).write_text("feature-x")
    Path(paths.base).write_text("main")
    Path(paths.slug).write_text("widgets")
    return paths


def _render(paths, tmp_path) -> str:
    return _merge_state.render_summary(
        paths,
        rerun_cmd="norn run merge_branch_v1 --non-interactive",
        max_rounds=2,
        project_dir=str(tmp_path),
    )


def test_render_summary_status_merged(tmp_path):
    paths = _status_work_root(tmp_path)
    Path(paths.merged).write_text("deadbeef\n")

    body = _render(paths, tmp_path)

    assert "Status: MERGED\n" in body
    assert "MERGED_NEEDS_ATTENTION" not in body


def test_render_summary_status_halted(tmp_path):
    paths = _status_work_root(tmp_path)
    _merge_state.halt(paths, "no_merge_after_rounds", "the base kept moving")

    body = _render(paths, tmp_path)

    assert "Status: HALTED" in body
    assert _merge_state.HALTS["no_merge_after_rounds"].next_step in body


def test_render_summary_status_merged_needs_attention(tmp_path):
    paths = _status_work_root(tmp_path)
    Path(paths.merged).write_text("deadbeef\n")
    _merge_state.halt(paths, "branch_delete_failed", "remote refused the delete")

    body = _render(paths, tmp_path)

    assert "Status: MERGED_NEEDS_ATTENTION" in body
    assert "Merged: deadbeef" in body
    assert _merge_state.HALTS["branch_delete_failed"].next_step in body


def test_render_summary_merged_then_unexpected_failure_is_needs_attention(tmp_path):
    """Review F6: ``merge pr n`` wrote ``merged`` after the fast-forward
    landed and the ``HaltOnFailure`` mixin then halted — the merge is real, so
    the status must not read plain ``HALTED``.
    """
    paths = _status_work_root(tmp_path)
    Path(paths.merged).write_text("deadbeef\n")
    _merge_state.halt(paths, "unexpected_failure", "stage: merge pr 1\nerror: gh pr view exited 1")

    body = _render(paths, tmp_path)

    assert "Status: MERGED_NEEDS_ATTENTION" in body
    assert "gh pr view exited 1" in body


def test_render_summary_without_merged_or_halt_raises(tmp_path):
    """`merge reached` halts when the rounds end without a merge, so this
    combination is unreachable — reaching it is a bug, not a missing file."""
    paths = _status_work_root(tmp_path)

    with pytest.raises(RuntimeError, match="neither"):
        _render(paths, tmp_path)


def test_render_summary_round_header_shows_the_last_round_that_ran(tmp_path):
    paths = _status_work_root(tmp_path)
    Path(paths.merged).write_text("deadbeef\n")

    assert "Round: 1 of 2" in _render(paths, tmp_path)

    Path(paths.retry(1)).touch()
    assert "Round: 2 of 2" in _render(paths, tmp_path)


def test_halts_include_no_merge_after_rounds():
    assert "no_merge_after_rounds" in _merge_state.HALTS
    assert _merge_state.HALTS["no_merge_after_rounds"].heading
    assert _merge_state.HALTS["no_merge_after_rounds"].next_step


class _FakeWriteMergeState:
    pass


@pytest.mark.asyncio
async def test_write_merge_state_writes_summary_and_pointer(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    Path(paths.slug).write_text("widgets")
    _merge_state.halt(paths, "rebase_conflict", "conflict detail")

    stage = _merge_state.WriteMergeState(
        paths=paths,
        rerun_cmd="norn run merge_branch_v1 --non-interactive",
        max_rounds=1,
        project_dir=str(tmp_path),
    )
    result = await stage.run(ctx_for())

    assert result.success is True
    expected_path = paths.summary("widgets")
    assert result.output == expected_path
    assert Path(expected_path).is_file()
    assert Path(paths.summary_path).read_text().strip() == expected_path


@pytest.mark.asyncio
async def test_write_merge_state_raises_without_merged_or_halt(tmp_path):
    """The unreachable fourth state is a plain failure, not a summary that
    quietly claims nothing happened."""
    paths = _merge_state.MergePaths(str(tmp_path))
    Path(paths.slug).write_text("widgets")

    stage = _merge_state.WriteMergeState(
        paths=paths,
        rerun_cmd="norn run merge_branch_v1 --non-interactive",
        max_rounds=2,
        project_dir=str(tmp_path),
    )

    with pytest.raises(RuntimeError):
        await stage.run(ctx_for())


@pytest.mark.asyncio
async def test_finalize_merge_state_splices_what_happened_only(tmp_path, monkeypatch):
    """``finalize merge state`` is a second ``WriteMergeState`` run after
    ``summarize run`` has written ``what-happened.md``. Its re-render must
    change only the ``## What happened`` section — every other section stays
    byte-identical to the first render (``write merge state``'s), so the
    narration can never alter a fact. The ``Finished:`` timestamp is frozen
    so the two renders are directly comparable.
    """
    monkeypatch.setattr(_merge_state, "_now_iso", lambda: "2026-01-01T00:00:00Z")

    paths = _merge_state.MergePaths(str(tmp_path))
    Path(paths.slug).write_text("widgets")
    _merge_state.halt(paths, "rebase_conflict", "conflict detail")

    stage = _merge_state.WriteMergeState(
        paths=paths,
        rerun_cmd="norn run merge_branch_v1 --non-interactive",
        max_rounds=1,
        project_dir=str(tmp_path),
    )

    first = await stage.run(ctx_for())
    first_body = Path(first.output).read_text()
    assert "## What happened" in first_body
    assert "(not written)" in first_body

    narration = "The run rebased cleanly and then hit a local-test conflict, so it halted."
    Path(paths.what_happened).write_text(narration)

    second = await stage.run(ctx_for())
    second_body = Path(second.output).read_text()

    assert second.output == first.output
    assert narration in second_body
    assert "(not written)" not in second_body
    # The only difference between the two renders is the spliced narration.
    assert first_body.replace("(not written)", narration) == second_body


def test_render_summary_round_dependent_sections_follow_the_last_round(tmp_path):
    """With ``RETRY-1`` present the run reached round 2, so the round-keyed
    sections must read round 2's artifacts, not round 1's."""
    paths = _status_work_root(tmp_path)
    Path(paths.merged).write_text("deadbeef\n")
    Path(paths.retry(1)).touch()
    Path(paths.rebase_conflicts(1)).write_text("round one conflict")
    Path(paths.rebase_conflicts(2)).write_text("round two conflict")
    Path(paths.checks(1)).write_text(json.dumps({"checks": [{"name": "round-one-check", "bucket": "pass"}]}))
    Path(paths.checks(2)).write_text(json.dumps({"checks": [{"name": "round-two-check", "bucket": "pass"}]}))

    body = _render(paths, tmp_path)

    assert "round two conflict" in body
    assert "round one conflict" not in body
    assert "round-two-check" in body
    assert "round-one-check" not in body


# --- Warnings / Post-merge build (step 18) ---------------------------------


def test_render_summary_no_warnings_omits_warnings_header_and_section(tmp_path):
    paths = _status_work_root(tmp_path)
    Path(paths.merged).write_text("deadbeef\n")

    body = _render(paths, tmp_path)

    assert "Warnings:" not in body
    assert "## Warnings" not in body


def test_render_summary_with_warnings_shows_header_and_section(tmp_path):
    paths = _status_work_root(tmp_path)
    Path(paths.merged).write_text("deadbeef\n")
    _merge_state.warn(paths, "no workflow ran on main @ deadbee within 120 s after the merge")

    body = _render(paths, tmp_path)

    assert "Warnings: 1" in body
    assert "## Warnings" in body
    assert "no workflow ran on main @ deadbee within 120 s after the merge" in body


def test_render_summary_multiple_warnings_counted(tmp_path):
    paths = _status_work_root(tmp_path)
    Path(paths.merged).write_text("deadbeef\n")
    _merge_state.warn(paths, "first warning")
    _merge_state.warn(paths, "second warning")

    body = _render(paths, tmp_path)

    assert "Warnings: 2" in body
    assert "first warning" in body
    assert "second warning" in body


def test_render_summary_post_merge_build_section_reads_base_build_json(tmp_path):
    paths = _status_work_root(tmp_path)
    Path(paths.merged).write_text("deadbeef\n")
    Path(paths.base_build).write_text(json.dumps({"outcome": "passed", "runs": []}))

    body = _render(paths, tmp_path)

    assert "## Post-merge build" in body
    assert "Result: passed" in body


def test_render_summary_post_merge_build_section_defaults_to_none_when_missing(tmp_path):
    paths = _status_work_root(tmp_path)
    Path(paths.merged).write_text("deadbeef\n")

    body = _render(paths, tmp_path)

    assert "## Post-merge build" in body
    assert "Result: (none)" in body


def test_render_summary_post_merge_build_no_runs_shows_warning_text(tmp_path):
    paths = _status_work_root(tmp_path)
    Path(paths.merged).write_text("deadbeef\n")
    Path(paths.base_build).write_text(json.dumps({"outcome": "no_runs", "runs": []}))

    body = _render(paths, tmp_path)

    assert "Result: no workflow ran within 120 s (warning)" in body


# ---------------------------------------------------------------------------
# notify
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_fires_alert_with_heading_as_title(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_send(self, msg):
        captured["msg"] = msg

    monkeypatch.setattr(MacOSChannel, "send", fake_send)

    await _merge_state.notify("Something happened", "some detail")

    assert captured["msg"].title == "Something happened"


# ---------------------------------------------------------------------------
# warn
# ---------------------------------------------------------------------------


def test_warn_appends_prints_and_never_halts(tmp_path, capsys):
    paths = _merge_state.MergePaths(str(tmp_path))
    _merge_state.warn(paths, "disk almost full")

    text = Path(paths.warnings).read_text()
    assert "disk almost full" in text

    captured = capsys.readouterr()
    assert "WARNING: disk almost full" in captured.err

    assert not Path(paths.halt).exists()


def test_warn_appends_multiple_lines(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    _merge_state.warn(paths, "first")
    _merge_state.warn(paths, "second")
    lines = Path(paths.warnings).read_text().splitlines()
    assert lines == ["first", "second"]


# ---------------------------------------------------------------------------
# HaltingCommand
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_halting_command_halts_on_unexpected_failure(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    stage = _merge_state.HaltingCommand(
        paths=paths, stage="my stage", cmd="echo boom >&2; exit 2"
    )
    result = await stage.run(ctx_for())

    assert result.success is True
    assert Path(paths.halt).is_file()
    text = Path(paths.halt_reason).read_text()
    assert text.splitlines()[0] == "KIND: unexpected_failure"
    assert "my stage" in text
    assert "boom" in text


@pytest.mark.asyncio
async def test_halting_command_passes_through_success(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    stage = _merge_state.HaltingCommand(paths=paths, stage="my stage", cmd="exit 0")
    result = await stage.run(ctx_for())

    assert result.success is True
    assert not Path(paths.halt).exists()


# ---------------------------------------------------------------------------
# HaltOnFailure over a plain Python stage (raising cases)
# ---------------------------------------------------------------------------


class _RaisingStage(BaseStage):
    """Test-only stage that raises whatever exception it's built with."""

    needs_agent = False

    def __init__(self, *, exc: BaseException) -> None:
        self._exc = exc

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        raise self._exc


class _HaltingRaiser(_merge_state.HaltOnFailure, _RaisingStage):
    pass


@pytest.mark.asyncio
async def test_halt_on_failure_halts_on_raised_exception(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    stage = _HaltingRaiser(paths=paths, stage="boom stage", exc=RuntimeError("kaboom"))

    result = await stage.run(ctx_for())

    assert result.success is True
    assert Path(paths.halt).is_file()
    text = Path(paths.halt_reason).read_text()
    assert text.splitlines()[0] == "KIND: unexpected_failure"
    assert "boom stage" in text
    assert "RuntimeError" in text
    assert "kaboom" in text


@pytest.mark.asyncio
async def test_halt_on_failure_halts_on_gh_error(tmp_path):
    """A GhError raised by a gh/git invocation (_merge_gh.py) halts the same
    way any other unpredicted exception does — it is not special-cased.
    """
    paths = _merge_state.MergePaths(str(tmp_path))
    exc = _merge_gh.GhError(["gh", "pr", "list"], 1, "HTTP 502 Bad Gateway")
    stage = _HaltingRaiser(paths=paths, stage="ci stage", exc=exc)

    result = await stage.run(ctx_for())

    assert result.success is True
    assert Path(paths.halt).is_file()
    text = Path(paths.halt_reason).read_text()
    assert text.splitlines()[0] == "KIND: unexpected_failure"
    assert "ci stage" in text
    assert "GhError" in text
    assert "502" in text


@pytest.mark.asyncio
async def test_halt_on_failure_cancelled_error_propagates_without_halting(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    stage = _HaltingRaiser(paths=paths, stage="x", exc=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await stage.run(ctx_for())

    assert not Path(paths.halt).exists()


@pytest.mark.asyncio
async def test_halt_on_failure_propagates_when_halt_itself_raises(tmp_path, monkeypatch):
    paths = _merge_state.MergePaths(str(tmp_path))

    def _raising_halt(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(_merge_state, "halt", _raising_halt)

    stage = _merge_state.HaltingCommand(paths=paths, stage="x", cmd="exit 1")
    with pytest.raises(OSError):
        await stage.run(ctx_for())


# ---------------------------------------------------------------------------
# last_attempt gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_last_attempt_gate_passes_through_before_last_attempt(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    Path(paths.local_attempt(1)).write_text("2")

    stage = _merge_state.HaltingCommand(
        paths=paths, stage="x", cmd="exit 1", last_attempt=(1, 4)
    )
    result = await stage.run(ctx_for())

    assert result.success is False
    assert not Path(paths.halt).exists()


@pytest.mark.asyncio
async def test_last_attempt_gate_halts_on_last_attempt(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    Path(paths.local_attempt(1)).write_text("4")

    stage = _merge_state.HaltingCommand(
        paths=paths, stage="x", cmd="exit 1", last_attempt=(1, 4)
    )
    result = await stage.run(ctx_for())

    assert result.success is True
    assert Path(paths.halt).is_file()


@pytest.mark.asyncio
async def test_last_attempt_gate_no_attempt_file_passes_through(tmp_path):
    """No local-attempt-<n> file yet → not the last attempt → passes through."""
    paths = _merge_state.MergePaths(str(tmp_path))

    stage = _merge_state.HaltingCommand(
        paths=paths, stage="x", cmd="exit 1", last_attempt=(1, 4)
    )
    result = await stage.run(ctx_for())

    assert result.success is False
    assert not Path(paths.halt).exists()


# ---------------------------------------------------------------------------
# HaltingGenerate
# ---------------------------------------------------------------------------


def test_halting_generate_is_a_generate_instance(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    stage = _merge_state.HaltingGenerate(
        paths=paths, stage="x", prompt="do the thing", model="standard"
    )
    assert isinstance(stage, Generate)
    assert stage.needs_agent is True


# ---------------------------------------------------------------------------
# HaltingCompressTestLog
# ---------------------------------------------------------------------------


def test_halting_compress_test_log_is_a_compress_test_log_and_halt_on_failure(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    stage = _merge_state.HaltingCompressTestLog(
        paths=paths, stage="x", source_stage="run local tests 1"
    )
    assert isinstance(stage, CompressTestLog)
    assert isinstance(stage, _merge_state.HaltOnFailure)


@pytest.mark.asyncio
async def test_halting_compress_test_log_reads_source_stage_output_on_failure(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    stage = _merge_state.HaltingCompressTestLog(
        paths=paths, stage="x", source_stage="run local tests 1", summarize_with_haiku=False
    )
    ctx = ctx_for()
    ctx.results["run local tests 1"] = StageResult(
        name="run local tests 1", success=False, error="boom", output="assertion failed: 1 != 2"
    )
    result = await stage.run(ctx)

    assert result.success is True
    assert "assertion failed" in result.output
    assert not Path(paths.halt).exists()


# ---------------------------------------------------------------------------
# HaltOnTimeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_halt_on_timeout_halts_with_named_kind_on_real_timeout(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    stage = _merge_state.HaltOnTimeout(
        paths=paths, stage="run local tests 1", kind="test_timeout",
        cmd="sleep 5", timeout=0.2,
    )
    result = await stage.run(ctx_for())

    assert result.success is True
    assert Path(paths.halt).is_file()
    text = Path(paths.halt_reason).read_text()
    assert text.splitlines()[0] == "KIND: test_timeout"


@pytest.mark.asyncio
async def test_halt_on_timeout_halts_unexpected_failure_on_plain_nonzero_exit(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    stage = _merge_state.HaltOnTimeout(
        paths=paths, stage="run local tests 1", kind="test_timeout",
        cmd="echo boom >&2; exit 1", timeout=5,
    )
    result = await stage.run(ctx_for())

    assert result.success is True
    assert Path(paths.halt).is_file()
    text = Path(paths.halt_reason).read_text()
    assert text.splitlines()[0] == "KIND: unexpected_failure"
    assert "boom" in text


@pytest.mark.asyncio
async def test_halt_on_timeout_passes_through_success(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    stage = _merge_state.HaltOnTimeout(
        paths=paths, stage="x", kind="test_timeout", cmd="exit 0", timeout=5,
    )
    result = await stage.run(ctx_for())

    assert result.success is True
    assert not Path(paths.halt).exists()


# ---------------------------------------------------------------------------
# CountLocalAttempt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_local_attempt_increments_from_one(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    stage = _merge_state.CountLocalAttempt(paths=paths, n=1)

    for _ in range(3):
        result = await stage.run(ctx_for())
        assert result.success is True

    assert Path(paths.local_attempt(1)).read_text() == "3"


# ---------------------------------------------------------------------------
# failure_signature
# ---------------------------------------------------------------------------


def test_failure_signature_normalises_ansi_durations_and_addresses():
    a = "\x1b[31mFAILED\x1b[0m test_foo at 0x1a2b3c after 5.2s"
    b = "FAILED test_foo at 0xffffff after 3.1 seconds"
    assert _merge_state.failure_signature(a) == _merge_state.failure_signature(b)


def test_failure_signature_differs_for_different_failures():
    a = "AssertionError: 1 != 2"
    b = "AssertionError: 3 != 4"
    assert _merge_state.failure_signature(a) != _merge_state.failure_signature(b)


# ---------------------------------------------------------------------------
# AssessLocalFailure
# ---------------------------------------------------------------------------


def _ctx_with_source(source_stage: str, text: str) -> PipelineContext:
    ctx = ctx_for()
    ctx.results[source_stage] = StageResult(name=source_stage, success=True, output=text)
    return ctx


@pytest.mark.asyncio
async def test_assess_local_failure_halts_local_cap_at_max_fixes(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    for k in (1, 2, 3):
        Path(paths.verdict_archive(1, k)).write_text(f"verdict {k}")

    stage = _merge_state.AssessLocalFailure(
        paths=paths, stage="assess local failure 1",
        n=1, source_stage="src", max_fixes=3, project_dir=str(tmp_path),
    )
    result = await stage.run(_ctx_with_source("src", "same old failure"))

    assert result.success is True
    assert Path(paths.halt).is_file()
    text = Path(paths.halt_reason).read_text()
    assert text.splitlines()[0] == "KIND: local_cap"
    assert "same old failure" in text


@pytest.mark.asyncio
async def test_assess_local_failure_halts_local_stuck_on_repeated_signature(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    failure_text = "AssertionError: still broken"
    Path(paths.local_signature(1)).write_text(_merge_state.failure_signature(failure_text))

    stage = _merge_state.AssessLocalFailure(
        paths=paths, stage="assess local failure 1",
        n=1, source_stage="src", max_fixes=3, project_dir=str(tmp_path),
    )
    result = await stage.run(_ctx_with_source("src", failure_text))

    assert result.success is True
    assert Path(paths.halt).is_file()
    assert Path(paths.halt_reason).read_text().splitlines()[0] == "KIND: local_stuck"


@pytest.mark.asyncio
async def test_assess_local_failure_repeat_detection_ignores_timings_and_addresses(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    first = "AssertionError at 0x1111 after 1.0s"
    second = "AssertionError at 0x2222 after 9.9 seconds"
    Path(paths.local_signature(1)).write_text(_merge_state.failure_signature(first))

    stage = _merge_state.AssessLocalFailure(
        paths=paths, stage="assess local failure 1",
        n=1, source_stage="src", max_fixes=3, project_dir=str(tmp_path),
    )
    result = await stage.run(_ctx_with_source("src", second))

    assert result.success is True
    assert Path(paths.halt).is_file()
    assert Path(paths.halt_reason).read_text().splitlines()[0] == "KIND: local_stuck"


@pytest.mark.asyncio
async def test_assess_local_failure_unpromoted_pending_signature_is_not_a_repeat(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    failure_text = "AssertionError: still broken"
    # Only the .pending signature matches — not yet promoted to local-signature-1.
    Path(paths.local_signature_pending(1)).write_text(_merge_state.failure_signature(failure_text))

    repo = make_repo_with_remote(tmp_path)
    stage = _merge_state.AssessLocalFailure(
        paths=paths, stage="assess local failure 1",
        n=1, source_stage="src", max_fixes=3, project_dir=repo.path,
    )
    result = await stage.run(_ctx_with_source("src", failure_text))

    assert result.success is True
    assert not Path(paths.halt).exists()


@pytest.mark.asyncio
async def test_assess_local_failure_success_path_writes_pending_and_head(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    repo = make_repo_with_remote(tmp_path)
    Path(paths.verdict(1)).write_text("VERDICT: FIX\nstale")

    stage = _merge_state.AssessLocalFailure(
        paths=paths, stage="assess local failure 1",
        n=1, source_stage="src", max_fixes=3, project_dir=repo.path,
    )
    result = await stage.run(_ctx_with_source("src", "a brand new failure"))

    assert result.success is True
    assert not Path(paths.halt).exists()
    expected_signature = _merge_state.failure_signature("a brand new failure")
    assert Path(paths.local_signature_pending(1)).read_text() == expected_signature
    assert Path(paths.local_head(1)).read_text() == repo.repo.head.commit.hexsha
    assert not Path(paths.verdict(1)).exists()


@pytest.mark.asyncio
async def test_assess_local_failure_unexpected_exception_halts_unexpected_failure(tmp_path):
    paths = _merge_state.MergePaths(str(tmp_path))
    stage = _merge_state.AssessLocalFailure(
        paths=paths, stage="assess local failure 1",
        n=1, source_stage="src", max_fixes=3, project_dir=str(tmp_path / "not-a-repo"),
    )
    result = await stage.run(_ctx_with_source("src", "a brand new failure"))

    assert result.success is True
    assert Path(paths.halt).is_file()
    assert Path(paths.halt_reason).read_text().splitlines()[0] == "KIND: unexpected_failure"

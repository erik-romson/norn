"""Tests for the shared plan-review builder and AssertLaunchTree helper."""
from __future__ import annotations

import pytest

from norn.dsl import ClearContext, Loop, OnFailure, Pipeline, Stage
from norn.models import PipelineContext
from norn.pipelines._launch_tree import AssertLaunchTree
from norn.pipelines._plan_review import WaitForApproval, add_plan_review_stages


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PREPLAN = "/tmp/x-preplan.md"
_PLAN = "/tmp/x-final-plan.md"
_QUESTIONS = "/tmp/x-plan-questions.md"
_REVIEW = "/tmp/x-plan-review.md"
_RESPONSE = "/tmp/x-plan-review-response.md"
_STEPS_DIR = "/tmp/x-final-plan"
_REPO_DIR = "/tmp/repo"
_SPLIT_SKILL = "split-plan"  # bare name is fine for structural tests


def _build(pause_for_approval: bool = False) -> Pipeline:
    pipeline = Pipeline("test-builder")
    return add_plan_review_stages(
        pipeline,
        preplan=_PREPLAN,
        plan=_PLAN,
        questions=_QUESTIONS,
        review=_REVIEW,
        response=_RESPONSE,
        steps_dir=_STEPS_DIR,
        repo_dir=_REPO_DIR,
        model="opus",
        codex_model=None,
        split_skill=_SPLIT_SKILL,
        pause_for_approval=pause_for_approval,
    )


def _item_names(pipeline: Pipeline) -> list[str]:
    names = []
    for item in pipeline.items:
        if isinstance(item, (Stage, Loop)):
            names.append(item.name)
        elif isinstance(item, ClearContext):
            names.append("ClearContext")
    return names


# ---------------------------------------------------------------------------
# Stage order
# ---------------------------------------------------------------------------


def test_stage_names_without_approval_pause():
    p = _build(pause_for_approval=False)
    assert _item_names(p) == [
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
    ]


def test_stage_names_with_approval_pause():
    p = _build(pause_for_approval=True)
    assert _item_names(p) == [
        "preflight",
        "draft plan",
        "resolve open questions",
        "open questions resolved",
        "wait for plan approval",
        "codex review",
        "check review shape",
        "ClearContext",
        "apply review",
        "dispositions recorded",
        "ClearContext",
        "split plan",
        "step files written",
        "summary",
    ]


def test_preflight_is_first_stage():
    p = _build()
    first = next(item for item in p.items if isinstance(item, Stage))
    assert first.name == "preflight"
    assert first.on_failure is OnFailure.FAIL


def test_wait_for_approval_on_failure_is_ask_user():
    p = _build(pause_for_approval=True)
    stage = next(item for item in p.items if isinstance(item, Stage) and item.name == "wait for plan approval")
    assert stage.on_failure is OnFailure.ASK_USER


def test_no_approval_stage_when_pause_for_approval_false():
    p = _build(pause_for_approval=False)
    names = [item.name for item in p.items if isinstance(item, Stage)]
    assert "wait for plan approval" not in names


# ---------------------------------------------------------------------------
# WaitForApproval behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_for_approval_always_fails():
    stage = WaitForApproval(plan=_PLAN)
    ctx = PipelineContext()
    result = await stage.run(ctx)
    assert result.success is False


@pytest.mark.asyncio
async def test_wait_for_approval_first_line_is_plan_finished():
    stage = WaitForApproval(plan=_PLAN)
    ctx = PipelineContext()
    result = await stage.run(ctx)
    first_line = (result.error or "").splitlines()[0]
    assert first_line == f"Plan finished: {_PLAN}"


# ---------------------------------------------------------------------------
# AssertLaunchTree behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assert_launch_tree_passes_when_working_dir_matches():
    stage = AssertLaunchTree(project_dir="/some/repo")
    ctx = PipelineContext()
    ctx.working_dir = "/some/repo"
    result = await stage.run(ctx)
    assert result.success is True


@pytest.mark.asyncio
async def test_assert_launch_tree_passes_when_working_dir_is_none():
    """working_dir=None means no worktree override; no mismatch possible."""
    stage = AssertLaunchTree(project_dir="/some/repo")
    ctx = PipelineContext()
    ctx.working_dir = None
    result = await stage.run(ctx)
    assert result.success is True


@pytest.mark.asyncio
async def test_assert_launch_tree_fails_when_working_dir_differs():
    stage = AssertLaunchTree(project_dir="/some/repo")
    ctx = PipelineContext()
    ctx.working_dir = "/other/tree"
    result = await stage.run(ctx)
    assert result.success is False
    assert "working_dir=/other/tree" in (result.error or "")
    assert "/some/repo" in (result.error or "")

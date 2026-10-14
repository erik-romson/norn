"""Tests for the shared plan-review builder and AssertLaunchTree helper."""
from __future__ import annotations

import pytest

from norn.dsl import ClearContext, Loop, OnFailure, Pipeline, Stage
from norn.models import PipelineContext
from norn.pipelines._launch_tree import AssertLaunchTree
from norn.pipelines._plan_review_v2 import WaitForApproval, add_plan_review_stages


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
        model="deep",
        codex_label="review",
        split_skill=_SPLIT_SKILL,
        pause_for_approval=pause_for_approval,
    )


def _codex_stage(pipeline: Pipeline) -> Stage:
    """Return the codex review stage from inside the `review plan` loop."""
    loop = next(i for i in pipeline.items if isinstance(i, Loop) and i.name == "review plan")
    return next(s for s in loop.stages if s.name == "codex review")


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
        "review plan",
        "review shape ok",
        "ClearContext",
        "apply review",
        "dispositions recorded",
        "ClearContext",
        "split plan",
        "step files written",
        "plan summary",
    ]


def test_stage_names_with_approval_pause():
    p = _build(pause_for_approval=True)
    assert _item_names(p) == [
        "preflight",
        "draft plan",
        "resolve open questions",
        "open questions resolved",
        "wait for plan approval",
        "review plan",
        "review shape ok",
        "ClearContext",
        "apply review",
        "dispositions recorded",
        "ClearContext",
        "split plan",
        "step files written",
        "plan summary",
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
# Review prompt: codex is read-only, `-o` writes the file
# ---------------------------------------------------------------------------


def test_review_prompt_does_not_ask_codex_to_write_the_file():
    """Regression: codex ran out of turns apologising that it could not write.

    The command is `codex exec --sandbox read-only ... -o <review>`, so codex's
    last message IS the review.  Telling it to write the file made it attempt a
    rejected patch and emit "the workspace is mounted read-only" as its final
    message — which `-o` then wrote to the review path, failing the shape gate.
    """
    from norn.pipelines._plan_review_v2 import review_prompt

    prompt = review_prompt(plan=_PLAN, preplan=_PREPLAN, review=_REVIEW)
    assert "Write your review to" not in prompt
    assert "Do not create or edit any file" in prompt
    assert "final message IS the review" in prompt
    # The shape the gate checks must still be stated.
    assert "## Verdict" in prompt
    assert "## Findings" in prompt


def test_codex_command_is_read_only_and_captures_last_message():
    """The prompt's claims about the sandbox and `-o` must match the command."""
    cmd = _codex_stage(_build()).impl.cmd
    assert "--sandbox read-only" in cmd
    assert f"-o {_REVIEW}" in cmd


def test_split_prompt_forbids_verifying_test_cmds_by_running_them():
    """Regression: the split agent ran full Maven builds in the background to
    check its `test_cmd`s, and read another repo's step files and norn's own
    source, turning a file-writing stage into a 23-minute deep-model session."""
    from norn.pipelines._plan_review_v2 import split_prompt

    prompt = split_prompt(plan=_PLAN, steps_dir=_STEPS_DIR)
    assert "Do not run the build, the tests or any\n`test_cmd`" in prompt
    assert "Do not start\nbackground tasks" in prompt
    assert "Read only this repository and the plan" in prompt


def _generate_stages(pipeline: Pipeline) -> list[Stage]:
    from norn.stages.generate import Generate

    stages: list[Stage] = []
    for item in pipeline.items:
        candidates = item.stages if isinstance(item, Loop) else [item]
        stages += [s for s in candidates if isinstance(s, Stage) and isinstance(s.impl, Generate)]
    return stages


def test_every_planning_agent_pins_its_subagents_to_the_standard_label():
    """Explore declares `model: inherit`, so without the FORCE flag every
    subagent a `deep` planning stage spawns would also run on the deep model."""
    from norn.model_labels import resolve_label

    stages = _generate_stages(_build())
    assert [s.name for s in stages] == ["draft plan", "revise plan", "apply findings", "write step files"]
    for stage in stages:
        assert stage.impl.env == {
            "CLAUDE_CODE_SUBAGENT_MODEL": resolve_label("claude-code", "standard").model,
            "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1",
        }, stage.name


# ---------------------------------------------------------------------------
# Review retry loop
# ---------------------------------------------------------------------------


def test_malformed_review_reruns_codex():
    """The shape check sits inside the loop so a bad review re-runs codex."""
    p = _build()
    loop = next(i for i in p.items if isinstance(i, Loop) and i.name == "review plan")
    assert [s.name for s in loop.stages] == ["codex review", "check review shape"]
    assert loop.max_retries == 2
    assert loop.on_exhaust is OnFailure.ASK_USER


def test_review_shape_is_re_gated_after_the_loop():
    """`[c]` on an exhausted loop counts as success, so the gate is repeated."""
    p = _build()
    gate = next(i for i in p.items if isinstance(i, Stage) and i.name == "review shape ok")
    assert gate.on_failure is OnFailure.FAIL
    # A different name from the in-loop stage: ctx.results is keyed by name.
    assert gate.name != "check review shape"


# ---------------------------------------------------------------------------
# Codex command structure
# ---------------------------------------------------------------------------


def test_codex_review_command_contains_m_flag():
    """The codex command must always include -m <model>."""
    assert "-m " in _codex_stage(_build()).impl.cmd


def test_codex_review_command_contains_effort_flag():
    """The codex command must always include -c model_reasoning_effort=..."""
    assert "model_reasoning_effort=" in _codex_stage(_build()).impl.cmd


def test_codex_label_default_is_review():
    """Default codex_label resolves to the 'review' entry in model_labels.toml."""
    from norn.model_labels import resolve_label
    resolved = resolve_label("codex", "review")
    assert resolved.label == "review"
    assert resolved.effort is not None


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

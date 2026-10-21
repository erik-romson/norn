"""Tests for the fix_jira_issue_v3 plan-review builder (stages 11-30)."""
from __future__ import annotations

from pathlib import Path

import pytest

from norn.dsl import ClearContext, Loop, OnFailure, Pipeline, Stage
from norn.model_labels import resolve_label
from norn.models import PipelineContext
from norn.pipelines import _plan_review_v3
from norn.pipelines._decisions_v3 import AutoDecide, DecisionGate, WriteDecision
from norn.pipelines._plan_review_v2 import draft_prompt
from norn.pipelines._plan_review_v3 import (
    RECOMMENDATION_ADDENDUM,
    REVISE_ADDENDUM,
    add_plan_review_stages_v3,
    draft_prompt_v3,
    recommendations_prompt,
    review_counts,
    revise_prompt_v3,
)
from norn.stages.generate import Generate

_SPLIT_SKILL = "split-plan"

_FILLED_Q = """\
# Open questions — x

STATUS: NEEDS_INPUT

## Q1. Write the patch into the database repo?
**Why it matters:** The pipeline only audits this worktree.
**Assumption if unanswered:** The run does not touch the database repo.
**Recommended answer:** add it to the run report
**Answer:** yes, add it

## Q2. Keep the old flag?
**Why it matters:** Callers may rely on it.
**Assumption if unanswered:** It stays.
**Recommended answer:** none — only the owner knows the callers
**Answer:**
"""

_BLANK_Q = """\
# Open questions — x

STATUS: NEEDS_INPUT

## Q1. Write the patch into the database repo?
**Why it matters:** The pipeline only audits this worktree.
**Assumption if unanswered:** The run does not touch the database repo.
**Recommended answer:** add it to the run report
**Answer:**
"""

_NO_REC_Q = """\
# Open questions — x

STATUS: NEEDS_INPUT

## Q1. Write the patch into the database repo?
**Why it matters:** The pipeline only audits this worktree.
**Assumption if unanswered:** The run does not touch the database repo.
**Answer:**
"""


class _Paths:
    def __init__(self, root: Path) -> None:
        self.preplan = str(root / "x-preplan.md")
        self.plan = str(root / "x-final-plan.md")
        self.questions = str(root / "X-1-plan-questions.md")
        self.review = str(root / "x-plan-review.md")
        self.response = str(root / "x-plan-review-response.md")
        self.steps_dir = str(root / "x-final-plan")
        self.repo_dir = str(root / "repo")
        self.approval = str(root / "X-1-plan-approval.md")


@pytest.fixture
def paths(tmp_path: Path) -> _Paths:
    return _Paths(tmp_path)


def _build(paths: _Paths, *, auto: bool = False, plan_gate: bool = True) -> Pipeline:
    return add_plan_review_stages_v3(
        Pipeline("test-builder-v3"),
        preplan=paths.preplan,
        plan=paths.plan,
        questions=paths.questions,
        review=paths.review,
        response=paths.response,
        steps_dir=paths.steps_dir,
        repo_dir=paths.repo_dir,
        model="deep",
        codex_label="review",
        split_skill=_SPLIT_SKILL,
        approval=paths.approval,
        auto=auto,
        plan_gate=plan_gate,
    )


def _item_names(pipeline: Pipeline) -> list[str]:
    names = []
    for item in pipeline.items:
        if isinstance(item, (Stage, Loop)):
            names.append(item.name)
        elif isinstance(item, ClearContext):
            names.append("ClearContext")
    return names


def _loop(pipeline: Pipeline, name: str) -> Loop:
    return next(i for i in pipeline.items if isinstance(i, Loop) and i.name == name)


def _stage(pipeline: Pipeline, name: str) -> Stage:
    return next(i for i in pipeline.items if isinstance(i, Stage) and i.name == name)


def _generate_stages(pipeline: Pipeline) -> list[Stage]:
    stages: list[Stage] = []
    for item in pipeline.items:
        candidates = item.stages if isinstance(item, Loop) else [item]
        stages += [s for s in candidates if isinstance(s, Stage) and isinstance(s.impl, Generate)]
    return stages


_HEAD = [
    "preflight",
    "check learning store",
    "draft plan",
    "resolve open questions",
    "open questions resolved",
    "complete plan sections",
    "plan sections present",
    "review plan",
    "review shape ok",
    "ClearContext",
    "apply review",
    "dispositions recorded",
    "plan sections kept",
    "write plan approval",
]
_TAIL = [
    "plan approved",
    "plan approval recorded",
    "plan sections approved",
    "ClearContext",
    "split plan",
    "step files written",
    "plan summary",
]


# ---------------------------------------------------------------------------
# Order
# ---------------------------------------------------------------------------


def test_interactive_order(paths):
    assert _item_names(_build(paths)) == _HEAD + _TAIL


def test_auto_order(paths):
    assert _item_names(_build(paths, auto=True)) == _HEAD + ["auto approve plan"] + _TAIL


def test_plan_gate_off_order(paths):
    assert _item_names(_build(paths, plan_gate=False)) == _HEAD + ["auto approve plan"] + _TAIL


def test_loop_body_names(paths):
    p = _build(paths)
    bodies = {
        i.name: [s.name for s in i.stages] for i in p.items if isinstance(i, Loop)
    }
    assert bodies == {
        "resolve open questions": [
            "add recommendations",
            "expand recommended answers",
            "revise plan",
            "check open questions",
        ],
        "complete plan sections": ["add plan sections", "check plan sections"],
        "review plan": ["codex review", "check review shape"],
        "apply review": ["apply findings", "check dispositions"],
        "split plan": ["write step files", "check step files"],
    }


# ---------------------------------------------------------------------------
# Gate 2
# ---------------------------------------------------------------------------


def test_gate_two_placement_and_policies(paths):
    p = _build(paths)
    names = _item_names(p)
    assert "wait for plan approval" not in names
    approved = _stage(p, "plan approved")
    recorded = _stage(p, "plan approval recorded")
    assert approved.on_failure is OnFailure.ASK_USER
    assert isinstance(approved.impl, DecisionGate)
    assert recorded.on_failure is OnFailure.FAIL
    assert recorded.always_run is True
    assert not approved.always_run
    split = names.index("split plan")
    for name in ("plan approved", "plan approval recorded"):
        assert names.index("plan sections kept") < names.index(name) < split
    assert names.index("plan approved") < names.index("plan approval recorded")
    assert names.index("plan approval recorded") < names.index("plan sections approved") < split


def test_gate_two_action_names_the_file_and_retry(paths):
    approved = _stage(_build(paths), "plan approved").impl
    assert approved.action == (
        f"To approve the plan, edit {paths.approval}: change the DECISION line to exactly "
        "`DECISION: approve`, save the file, then [r]etry."
    )
    assert approved.field == "DECISION"
    assert approved.accept == frozenset({"approve", "approved"})


def test_auto_approve_exists_only_in_auto_or_gate_off(paths):
    assert "auto approve plan" not in _item_names(_build(paths))
    assert "auto approve plan" in _item_names(_build(paths, auto=True))
    assert "auto approve plan" in _item_names(_build(paths, plan_gate=False))


def test_auto_approve_notes(paths):
    off = _stage(_build(paths, plan_gate=False), "auto approve plan")
    auto = _stage(_build(paths, auto=True), "auto approve plan")
    both = _stage(_build(paths, auto=True, plan_gate=False), "auto approve plan")
    for stage in (off, auto, both):
        assert isinstance(stage.impl, AutoDecide)
        assert stage.on_failure is OnFailure.FAIL
        assert stage.impl.field == "DECISION"
        assert stage.impl.value == "approve"
    assert off.impl.note == "plan_gate=off"
    assert auto.impl.note == "auto"
    assert both.impl.note == "auto"


def test_review_counts():
    text = "# Review response — x\n\n- F1: accepted — fine\n- F2: rejected — no\n- F3: deferred — later\n- F4: accepted — ok\n"
    assert review_counts(text) == {"accepted": 2, "rejected": 1, "deferred": 1}


@pytest.mark.asyncio
async def test_write_plan_approval_renders_counts(paths):
    Path(paths.response).write_text("- F1: accepted — a\n- F2: rejected — b\n- F3: deferred — c\n- F4: accepted — d\n")
    Path(paths.questions).write_text(
        _BLANK_Q.replace("**Answer:**", "**Answer:** add it to the run report _(auto: recommended answer)_")
        + "\n## Q2. Another?\n**Why it matters:** w\n**Assumption if unanswered:** a\n"
        "**Recommended answer:** none — human\n**Answer:** manual answer\n"
    )
    stage = _stage(_build(paths), "write plan approval")
    assert isinstance(stage.impl, WriteDecision)
    result = await stage.impl.run(PipelineContext())
    assert result.success
    text = Path(paths.approval).read_text()
    assert text.startswith("# Plan approval — X-1\n\nDECISION: pending\n")
    assert f"Edit the plan freely first: {paths.plan}" in text
    assert f"- Codex review: {paths.review} — 4 findings: 2 accepted, 1 rejected, 1 deferred" in text
    assert f"- Questions: {paths.questions} — 2 answered, 1 from a recommendation" in text


@pytest.mark.asyncio
async def test_gate_two_round_trip(paths):
    Path(paths.response).write_text("- F1: accepted — a\n")
    Path(paths.questions).write_text("# Open questions — x\n\nSTATUS: READY\n\nNo open questions.\n")
    p = _build(paths, auto=True)
    ctx = PipelineContext()
    await _stage(p, "write plan approval").impl.run(ctx)
    assert not (await _stage(p, "plan approved").impl.run(ctx)).success
    assert (await _stage(p, "auto approve plan").impl.run(ctx)).success
    assert "DECISION: approve (auto)" in Path(paths.approval).read_text()
    assert (await _stage(p, "plan approved").impl.run(ctx)).success
    assert (await _stage(p, "plan approval recorded").impl.run(ctx)).success


# ---------------------------------------------------------------------------
# Loop 14
# ---------------------------------------------------------------------------


def test_open_questions_loop_retries(paths):
    for auto, expected in ((False, 1), (True, 3)):
        loop = _loop(_build(paths, auto=auto), "resolve open questions")
        assert loop.max_retries == expected
        assert loop.on_exhaust is OnFailure.ASK_USER


def test_expand_fill_blanks_follows_auto(paths):
    for auto in (False, True):
        loop = _loop(_build(paths, auto=auto), "resolve open questions")
        expand = next(s for s in loop.stages if s.name == "expand recommended answers")
        assert expand.impl.fill_blanks is auto


def test_open_questions_hard_twin(paths):
    twin = _stage(_build(paths), "open questions resolved")
    assert twin.on_failure is OnFailure.FAIL


# ---------------------------------------------------------------------------
# Prompts, env, codex
# ---------------------------------------------------------------------------


def test_draft_prompt_contains_v2_verbatim_plus_addendum():
    kw = dict(
        preplan="/p/pre.md", plan="/p/plan.md", questions="/p/q.md",
        store_rel="docs/solutions", finder="/f.py", research_dir="/p/r",
    )
    prompt = draft_prompt_v3(**kw)
    assert draft_prompt(**kw) in prompt
    assert prompt.endswith(RECOMMENDATION_ADDENDUM)
    assert "**Recommended answer:**" in prompt
    assert "directly above" in prompt


def test_revise_prompt_rules():
    prompt = revise_prompt_v3(questions="/p/q.md", plan="/p/plan.md")
    assert prompt.endswith(REVISE_ADDENDUM)
    assert "_(auto: recommended answer)_" in prompt
    assert "_(recommended answer)_" in prompt
    assert "Never edit an answer" in prompt


def test_recommendations_prompt_adds_missing_lines_only():
    prompt = recommendations_prompt(questions="/p/q.md")
    assert "/p/q.md" in prompt
    assert "directly above" in prompt
    assert "Change nothing else" in prompt


def test_every_generate_carries_the_subagent_env(paths):
    stages = _generate_stages(_build(paths))
    assert [s.name for s in stages] == [
        "draft plan",
        "add recommendations",
        "revise plan",
        "add plan sections",
        "apply findings",
        "write step files",
    ]
    expected = {
        "CLAUDE_CODE_SUBAGENT_MODEL": resolve_label("claude-code", "standard").model,
        "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1",
    }
    for stage in stages:
        assert stage.impl.env == expected, stage.name


def test_no_prompt_has_stray_braces(paths):
    for stage in _generate_stages(_build(paths)):
        prompt = stage.impl.prompt.replace("{check plan sections.output}", "")
        assert "{" not in prompt, stage.name
        assert "}" not in prompt, stage.name


def test_codex_command_removes_stale_review_and_is_read_only(paths):
    loop = _loop(_build(paths), "review plan")
    cmd = next(s for s in loop.stages if s.name == "codex review").impl
    assert cmd.cmd.startswith("rm -f ")
    assert paths.review in cmd.cmd.split("&&")[0]
    assert "--sandbox read-only --skip-git-repo-check" in cmd.cmd
    assert f"-o {paths.review}" in cmd.cmd
    assert cmd.timeout == 1800


def test_split_loop_uses_the_skill(paths):
    loop = _loop(_build(paths), "split plan")
    gen = next(s for s in loop.stages if s.name == "write step files").impl
    assert gen.skills == [_SPLIT_SKILL]


# ---------------------------------------------------------------------------
# `when` predicates (files, never results)
# ---------------------------------------------------------------------------


def _when(pipeline: Pipeline, loop: str, stage: str):
    return next(s for s in _loop(pipeline, loop).stages if s.name == stage).when


def test_no_when_uses_stage_results(paths):
    ctx = PipelineContext()  # empty results: a resumed process
    p = _build(paths)
    Path(paths.questions).write_text(_FILLED_Q)
    for name in ("resolve open questions", "complete plan sections"):
        for stage in _loop(p, name).stages:
            if stage.when is not None:
                stage.when(ctx)  # must not consult ctx.results


def test_revise_when_is_false_on_blank_answers_and_true_once_answered(paths):
    when = _when(_build(paths), "resolve open questions", "revise plan")
    ctx = PipelineContext()
    assert when(ctx) is False  # missing file
    Path(paths.questions).write_text(_BLANK_Q)
    assert when(ctx) is False
    Path(paths.questions).write_text(_FILLED_Q)
    assert when(ctx) is True


def test_add_recommendations_when(paths):
    when = _when(_build(paths), "resolve open questions", "add recommendations")
    ctx = PipelineContext()
    assert when(ctx) is True  # missing file counts as needed
    Path(paths.questions).write_text(_BLANK_Q)
    assert when(ctx) is False
    Path(paths.questions).write_text(_NO_REC_Q)
    assert when(ctx) is True


def test_add_plan_sections_when(paths):
    when = _when(_build(paths), "complete plan sections", "add plan sections")
    ctx = PipelineContext()
    assert when(ctx) is True  # missing file
    Path(paths.plan).write_text(
        "# Plan\n\n## Prior learnings\nnone found\n\n## Assumptions\nsome\n\n"
        "## Considered and not built\nnothing\n\n## Edge cases and flows\nfew\n"
    )
    assert when(ctx) is False
    Path(paths.plan).write_text("# Plan\n\n## Assumptions\nsome\n")
    assert when(ctx) is True


# ---------------------------------------------------------------------------
# Resume simulation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_runs_revise_in_attempt_one(paths, monkeypatch):
    """A resumed process has no results; the filled file alone must trigger revise."""
    Path(paths.questions).write_text(_FILLED_Q)
    calls: list[str] = []

    async def fake_run(self, ctx, **kwargs):
        calls.append(self.prompt)
        from norn.models import StageResult

        return StageResult(name="", success=True)

    monkeypatch.setattr(Generate, "run", fake_run)

    p = _build(paths)
    ctx = PipelineContext()
    assert ctx.results == {}
    ran: list[str] = []
    for stage in _loop(p, "resolve open questions").stages:
        if stage.when is not None and not stage.when(ctx):
            continue
        ran.append(stage.name)
        await stage.impl.run(ctx)

    assert "revise plan" in ran
    assert "add recommendations" not in ran
    assert len(calls) == 1
    assert "Re-read the questions file" in calls[0]
    assert REVISE_ADDENDUM in calls[0]
    assert _plan_review_v3.revise_prompt_v3(questions=paths.questions, plan=paths.plan) == calls[0]

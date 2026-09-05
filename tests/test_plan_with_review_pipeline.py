"""Tests for the plan_with_review bundled pipeline.

Covers pipeline structure (stage names, gate wiring, loop config, prompt paths)
and the no-bypass regression test: `[c]ontinue` at a loop pause must not carry
a run past a hard gate whose contract is unmet.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from norn.dsl import ClearContext, Loop, OnFailure, Pipeline, Stage
from norn.event_sink import EventSink
from norn.models import PipelineContext, StageResult
from norn.pipelines._plan_gates import OpenQuestionsGate, ReviewDispositionGate, StepFilesGate
from norn.responder import InputResponder
from norn.runner import PipelineError, run_pipeline
from norn.stages.base import BaseStage
from norn.stages.generate import Generate


FIXTURE_PREPLAN = (
    Path(__file__).parent / "fixtures" / "plan_with_review" / "sample-preplan.md"
)


# ---------------------------------------------------------------------------
# Import helper — mirrors tests/test_pipelines.py:_import_implement_features
# ---------------------------------------------------------------------------


def _import_plan_with_review():
    """Import plan_with_review against the fixture pre-plan.

    The module reads ``sys.argv`` at import time to locate its pre-plan, so a
    plain import under pytest raises ``ValueError``. Patching argv and
    reloading gives every test a stable pipeline to inspect.
    """
    saved = list(sys.argv)
    sys.argv = [saved[0], str(FIXTURE_PREPLAN)]
    try:
        from norn.pipelines import plan_with_review

        return importlib.reload(plan_with_review)
    finally:
        sys.argv = saved


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collect_generates(items):
    """Walk pipeline items and yield all Generate impl instances."""
    for item in items:
        if isinstance(item, Stage) and isinstance(item.impl, Generate):
            yield item.impl
        elif isinstance(item, Loop):
            yield from _collect_generates(item.stages)


class _RecorderStage(BaseStage):
    """Records whether it ran. Never needs an agent."""

    needs_agent = False

    def __init__(self) -> None:
        self.ran = False

    async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
        self.ran = True
        return StageResult(name="", success=True, output="recorded")


class FakeResponder(InputResponder):
    """Never touches stdin. Records all calls for assertion."""

    def __init__(
        self,
        budget_choice: str = "c",
        failure_choice: str = "c",
        step_choice: str = "r",
    ) -> None:
        self._budget_choice = budget_choice
        self._failure_choice = failure_choice
        self._step_choice = step_choice
        self.calls: list[tuple] = []

    async def ask_budget(self, tracker, budget) -> str:
        self.calls.append(("budget",))
        return self._budget_choice

    async def ask_failure(self, name: str, error: str | None) -> str:
        self.calls.append(("failure", name))
        return self._failure_choice

    async def ask_step(self, stage, ctx, *, session_id=None) -> str:
        self.calls.append(("step", stage.name))
        return self._step_choice


# ---------------------------------------------------------------------------
# Structure tests
# ---------------------------------------------------------------------------


def test_metadata_args_has_exactly_one_key():
    mod = _import_plan_with_review()
    assert list(mod.metadata["args"]) == ["args"]


def test_metadata_env_vars_contains_anthropic_key():
    mod = _import_plan_with_review()
    assert "ANTHROPIC_API_KEY" in mod.metadata["env_vars"]


def test_top_level_item_names():
    mod = _import_plan_with_review()
    names = []
    for item in mod.config.items:
        if isinstance(item, (Stage, Loop)):
            names.append(item.name)
        elif isinstance(item, ClearContext):
            names.append("ClearContext")
    assert names == [
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


def test_open_questions_resolved_is_hard_gate():
    mod = _import_plan_with_review()
    stage = [
        item for item in mod.config.items
        if isinstance(item, Stage) and item.name == "open questions resolved"
    ][0]
    assert isinstance(stage.impl, OpenQuestionsGate)
    assert stage.on_failure is OnFailure.FAIL


def test_dispositions_recorded_is_hard_gate():
    mod = _import_plan_with_review()
    stage = [
        item for item in mod.config.items
        if isinstance(item, Stage) and item.name == "dispositions recorded"
    ][0]
    assert isinstance(stage.impl, ReviewDispositionGate)
    assert stage.on_failure is OnFailure.FAIL


def test_hard_gates_share_paths_with_in_loop_twins():
    mod = _import_plan_with_review()

    # Find in-loop gates
    oq_loop = [
        item for item in mod.config.items
        if isinstance(item, Loop) and item.name == "resolve open questions"
    ][0]
    in_loop_oq = [s for s in oq_loop.stages if s.name == "check open questions"][0]

    ar_loop = [
        item for item in mod.config.items
        if isinstance(item, Loop) and item.name == "apply review"
    ][0]
    in_loop_rd = [s for s in ar_loop.stages if s.name == "check dispositions"][0]

    # Find hard gates
    hard_oq = [
        item for item in mod.config.items
        if isinstance(item, Stage) and item.name == "open questions resolved"
    ][0]
    hard_rd = [
        item for item in mod.config.items
        if isinstance(item, Stage) and item.name == "dispositions recorded"
    ][0]

    # OpenQuestionsGate: same QUESTIONS path
    assert in_loop_oq.impl.path == hard_oq.impl.path == mod.QUESTIONS

    # ReviewDispositionGate: same REVIEW and RESPONSE paths
    assert in_loop_rd.impl.review == hard_rd.impl.review == mod.REVIEW
    assert in_loop_rd.impl.response == hard_rd.impl.response == mod.RESPONSE


def test_resolve_open_questions_loop_config():
    mod = _import_plan_with_review()
    loop = [
        item for item in mod.config.items
        if isinstance(item, Loop) and item.name == "resolve open questions"
    ][0]
    assert loop.max_retries == 1
    assert loop.on_exhaust is OnFailure.ASK_USER


def test_apply_review_loop_config():
    mod = _import_plan_with_review()
    loop = [
        item for item in mod.config.items
        if isinstance(item, Loop) and item.name == "apply review"
    ][0]
    assert loop.max_retries == 2


def test_revise_plan_when_predicate():
    """The revise stage has a `when` predicate that is False on empty results
    and True after a failed check open questions."""
    mod = _import_plan_with_review()
    loop = [
        item for item in mod.config.items
        if isinstance(item, Loop) and item.name == "resolve open questions"
    ][0]
    revise = [s for s in loop.stages if s.name == "revise plan"][0]
    assert revise.when is not None

    # False with empty results
    ctx = PipelineContext()
    assert revise.when(ctx) is False

    # True after a failed "check open questions"
    ctx.results["check open questions"] = StageResult(
        name="check open questions", success=False, error="boom"
    )
    assert revise.when(ctx) is True


def test_derived_paths():
    mod = _import_plan_with_review()
    assert mod.PREPLAN == str(FIXTURE_PREPLAN.resolve())
    parent = str(FIXTURE_PREPLAN.resolve().parent)
    assert mod.PLAN == f"{parent}/sample-final-plan.md"
    assert mod.QUESTIONS == f"{parent}/sample-plan-questions.md"
    assert mod.REVIEW == f"{parent}/sample-plan-review.md"
    assert mod.RESPONSE == f"{parent}/sample-plan-review-response.md"


def test_generate_stages_have_no_pinned_cwd():
    mod = _import_plan_with_review()
    generates = list(_collect_generates(mod.config.items))
    assert generates, "expected at least one Generate stage"
    for gen in generates:
        assert gen.cwd is None, (
            f"plan_with_review Generate stage has cwd={gen.cwd!r}; "
            "remove the cwd pin so the stage inherits ctx.working_dir"
        )


def test_draft_prompt_mentions_plan_and_questions_paths():
    mod = _import_plan_with_review()
    assert mod.PLAN in mod._DRAFT_PROMPT
    assert mod.QUESTIONS in mod._DRAFT_PROMPT


def test_apply_prompt_mentions_review_and_response_paths():
    mod = _import_plan_with_review()
    assert mod.REVIEW in mod._APPLY_PROMPT
    assert mod.RESPONSE in mod._APPLY_PROMPT


# ---------------------------------------------------------------------------
# Split stage (step files for implement_features)
# ---------------------------------------------------------------------------


def test_steps_dir_is_plan_path_without_suffix():
    mod = _import_plan_with_review()
    assert mod.STEPS_DIR == mod.PLAN[: -len(".md")]


def test_split_prompt_mentions_plan_and_steps_dir():
    mod = _import_plan_with_review()
    assert mod.PLAN in mod._SPLIT_PROMPT
    assert mod.STEPS_DIR in mod._SPLIT_PROMPT


def test_module_loads_from_a_directory_without_the_skill(tmp_path, monkeypatch):
    """A pipeline that raises at import cannot be listed, let alone run.

    The TUI launcher imports every bundled pipeline to build the run; when
    `norn ui` is started outside this repo there is no `.claude/skills/`
    to resolve, and raising there killed the load with "Could not load
    pipeline" instead of failing the split stage.
    """
    monkeypatch.chdir(tmp_path)
    mod = _import_plan_with_review()
    assert mod.config.items


def test_split_skill_falls_back_to_the_norn_checkout(tmp_path, monkeypatch):
    from norn.skills import Skill

    monkeypatch.chdir(tmp_path)
    mod = _import_plan_with_review()
    skill = mod._split_skill()
    assert isinstance(skill, Skill)
    assert "step-NN-name.md" in skill.content


def test_split_skill_degrades_to_the_bare_name(tmp_path, monkeypatch):
    """Nothing resolves: Generate gets the name and the stage reports the miss."""
    monkeypatch.chdir(tmp_path)
    mod = _import_plan_with_review()
    assert mod._split_skill(fallback=tmp_path / "absent" / "SKILL.md") == "split-plan"


def test_write_step_files_carries_the_split_plan_skill():
    mod = _import_plan_with_review()
    loop = [
        item for item in mod.config.items
        if isinstance(item, Loop) and item.name == "split plan"
    ][0]
    write = [s for s in loop.stages if s.name == "write step files"][0]
    assert write.impl.skills == [mod.SPLIT_SKILL]
    assert "step-NN-name.md" in mod.SPLIT_SKILL.content


def test_step_files_written_is_hard_gate_sharing_the_loop_path():
    mod = _import_plan_with_review()
    loop = [
        item for item in mod.config.items
        if isinstance(item, Loop) and item.name == "split plan"
    ][0]
    in_loop = [s for s in loop.stages if s.name == "check step files"][0]
    hard = [
        item for item in mod.config.items
        if isinstance(item, Stage) and item.name == "step files written"
    ][0]
    assert isinstance(hard.impl, StepFilesGate)
    assert hard.on_failure is OnFailure.FAIL
    assert in_loop.impl.steps_dir == hard.impl.steps_dir == mod.STEPS_DIR


# ---------------------------------------------------------------------------
# No-bypass regression test (review finding F1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_continue_at_loop_pause_cannot_bypass_hard_gate(tmp_path):
    """Choosing [c]ontinue at the loop pause must not carry the run past
    a hard gate whose contract is unmet.

    Builds a miniature pipeline with the same shape as plan_with_review's
    open-questions section: a loop with a gate, followed by the same gate
    as a top-level stage with on_failure=FAIL.
    """
    # Write a questions file that can never pass
    bad = tmp_path / "questions.md"
    bad.write_text("STATUS: NEEDS_INPUT\n\n## Q1. Unanswered\n**Answer:**\n")

    recorder = _RecorderStage()
    responder = FakeResponder(failure_choice="c")
    sink = EventSink()

    p = (
        Pipeline("t")
        .loop("gate loop", max_retries=1, on_exhaust=OnFailure.ASK_USER, stages=[
            Stage("check", OpenQuestionsGate(path=str(bad))),
        ])
        .stage("hard gate", OpenQuestionsGate(path=str(bad)), on_failure=OnFailure.FAIL)
        .stage("downstream", recorder)
    )

    with pytest.raises(PipelineError):
        await run_pipeline(p, event_sink=sink, input_responder=responder)

    # The recorder must NOT have run — the hard gate stopped the run.
    assert not recorder.ran
    # The responder was asked exactly once (the loop pause).
    assert len([c for c in responder.calls if c[0] == "failure"]) == 1


@pytest.mark.asyncio
async def test_retry_with_fixed_file_passes_hard_gate(tmp_path):
    """When the human fixes the file and retries, the run passes through
    both the loop gate and the hard gate, reaching downstream."""
    questions = tmp_path / "questions.md"
    questions.write_text("STATUS: NEEDS_INPUT\n\n## Q1. Open\n**Answer:**\n")

    recorder = _RecorderStage()

    class _FixAndRetryResponder(InputResponder):
        """First failure: fix the file and retry. Subsequent: continue."""

        def __init__(self) -> None:
            self.calls: list[tuple] = []
            self._fixed = False

        async def ask_budget(self, tracker, budget) -> str:
            return "c"

        async def ask_failure(self, name: str, error: str | None) -> str:
            self.calls.append(("failure", name))
            if not self._fixed:
                self._fixed = True
                # Fix the file so the gate passes on the next attempt
                questions.write_text(
                    "STATUS: READY\n\n## Q1. Open\n**Answer:** answered\n"
                )
                return "r"
            return "c"

        async def ask_step(self, stage, ctx, *, session_id=None) -> str:
            return "r"

    responder = _FixAndRetryResponder()
    sink = EventSink()

    p = (
        Pipeline("t")
        .loop("gate loop", max_retries=1, on_exhaust=OnFailure.ASK_USER, stages=[
            Stage("check", OpenQuestionsGate(path=str(questions))),
        ])
        .stage("hard gate", OpenQuestionsGate(path=str(questions)), on_failure=OnFailure.FAIL)
        .stage("downstream", recorder)
    )

    await run_pipeline(p, event_sink=sink, input_responder=responder)

    # The recorder MUST have run — the hard gate passed.
    assert recorder.ran

"""Tests for dogfooding/implement_plan/implementation_plan.py."""

from __future__ import annotations

import pytest

from dogfooding.common import (
    clean_worktree,
    first_h1,
    parse_front_matter,
    preflight,
    record_start,
)
from dogfooding.implement_plan.implementation_plan import (
    ImplementationPlan,
    PlanStep,
)
from dogfooding.implement_plan.pipeline import build_pipeline
from norn.dsl import Pipeline
from norn.stages.generate import Generate
from norn.stages.run_command import RunCommand
from norn.testing import (
    MockStage,
    PipelineTestRunner,
    verify,
)


# ---------------------------------------------------------------------------
# PlanStep
# ---------------------------------------------------------------------------


def _make_step(
    name: str = "step-01-hooks",
    body: str = "# Add hooks\n\nSome body.",
    source_path: str = "tmp/steps/step-01-hooks.md",
    test_cmd: str = "uv run python -m pytest tests/ -v",
    bats_cmd: str = "bats -r bats/ -v",
    add_cmd: str = "git add -u",
    commit_subject: str = "refactor: step-01-hooks \u2014 Add hooks",
) -> PlanStep:
    return PlanStep(
        name=name,
        body=body,
        source_path=source_path,
        test_cmd=test_cmd,
        bats_cmd=bats_cmd,
        add_cmd=add_cmd,
        commit_subject=commit_subject,
    )


class TestPlanStep:
    def test_fields(self):
        step = _make_step()
        assert step.name == "step-01-hooks"
        assert step.commit_subject == "refactor: step-01-hooks \u2014 Add hooks"

    def test_test_failed_predicate_returns_callable(self):
        step = _make_step()
        pred = step.test_failed()
        assert callable(pred)


# ---------------------------------------------------------------------------
# Front-matter parser
# ---------------------------------------------------------------------------


class TestParseFrontMatter:
    def test_no_front_matter(self):
        fm, body = parse_front_matter("just text")
        assert fm == {}
        assert body == "just text"

    def test_simple_key_value(self):
        text = "---\ntest_cmd: pytest -v\n---\nbody here"
        fm, body = parse_front_matter(text)
        assert fm["test_cmd"] == "pytest -v"
        assert body == "body here"

    def test_list_values(self):
        text = "---\npaths:\n  - src/\n  - tests/\n---\nbody"
        fm, body = parse_front_matter(text)
        assert fm["paths"] == ["src/", "tests/"]
        assert body == "body"


class TestFirstH1:
    def test_finds_h1(self):
        assert first_h1("some text\n# My Title\nmore") == "My Title"

    def test_no_h1(self):
        assert first_h1("no heading here") is None


# ---------------------------------------------------------------------------
# ImplementationPlan.from_steps
# ---------------------------------------------------------------------------


class TestImplementationPlanFromSteps:
    def _plan(self, steps: list[PlanStep] | None = None) -> ImplementationPlan:
        if steps is None:
            steps = [_make_step()]
        return ImplementationPlan(steps, project_dir="/tmp/proj")

    def test_iteration_yields_steps(self):
        steps = [_make_step(name="a"), _make_step(name="b")]
        plan = ImplementationPlan(steps, project_dir="/tmp")
        assert [s.name for s in plan] == ["a", "b"]

    def test_clean_worktree_returns_run_command(self):
        stage = clean_worktree("/tmp/proj")
        assert isinstance(stage, RunCommand)
        assert "git status --porcelain" in stage.cmd

    def test_preflight_returns_run_command(self):
        stage = preflight("/tmp/proj", "uv", "python3")
        assert isinstance(stage, RunCommand)
        assert "command -v" in stage.cmd
        assert "uv" in stage.cmd
        assert "python3" in stage.cmd

    def test_record_start_returns_run_command(self):
        stage = record_start("/tmp/proj")
        assert isinstance(stage, RunCommand)
        assert "git rev-parse HEAD" in stage.cmd

    def test_implement_returns_generate(self):
        step = _make_step()
        plan = self._plan([step])
        stage = plan.implement(step)
        assert isinstance(stage, Generate)
        assert step.source_path in stage.prompt
        assert "## Step to implement" in stage.prompt
        assert stage.permission_mode == "acceptEdits"
        assert "Write" in stage.allowed_tools

    def test_fix_returns_generate_with_test_placeholders(self):
        step = _make_step()
        plan = self._plan([step])
        stage = plan.fix(step)
        assert isinstance(stage, Generate)
        assert "{test step-01-hooks.output}" in stage.prompt
        assert "{bats step-01-hooks.output}" in stage.prompt

    def test_test_returns_run_command(self):
        step = _make_step(test_cmd="pytest -x")
        plan = self._plan([step])
        stage = plan.test(step)
        assert isinstance(stage, RunCommand)
        assert "pytest -x" in stage.cmd

    def test_bats_returns_run_command(self):
        step = _make_step(bats_cmd="bats tests/")
        plan = self._plan([step])
        stage = plan.bats(step)
        assert isinstance(stage, RunCommand)
        assert "bats tests/" in stage.cmd

    def test_commit_returns_run_command(self):
        step = _make_step()
        plan = self._plan([step])
        stage = plan.commit(step)
        assert isinstance(stage, RunCommand)
        assert step.add_cmd in stage.cmd
        assert "git commit -F -" in stage.cmd

    def test_summarize_returns_generate_read_only(self):
        step = _make_step()
        plan = self._plan([step])
        stage = plan.summarize(step)
        assert isinstance(stage, Generate)
        assert "Write" not in stage.allowed_tools
        assert "Edit" not in stage.allowed_tools
        assert "Read" in stage.allowed_tools

    def test_summarize_accumulates_prior_summaries(self):
        s1 = _make_step(name="step-01")
        s2 = _make_step(name="step-02")
        plan = self._plan([s1, s2])
        plan.summarize(s1)
        # After summarizing step-01, implement for step-02 should include prior context
        gen = plan.implement(s2)
        assert "## What was done in prior steps" in gen.prompt
        assert "{summarize step-01.output}" in gen.prompt

    def test_review_returns_generate_read_only(self):
        plan = self._plan()
        stage = plan.review()
        assert isinstance(stage, Generate)
        assert "Write" not in stage.allowed_tools
        assert "{record start.output}" in stage.prompt

    def test_handoff_returns_generate_read_only(self):
        plan = self._plan()
        stage = plan.handoff()
        assert isinstance(stage, Generate)
        assert "Write" not in stage.allowed_tools
        assert "handoff.md" in stage.prompt

    def test_shared_context_in_implement_prompt(self):
        step = _make_step()
        plan = ImplementationPlan(
            [step], project_dir="/tmp", shared_context="Some shared info"
        )
        gen = plan.implement(step)
        assert "Some shared info" in gen.prompt

    def test_generate_sets_cwd_and_setting_sources(self):
        step = _make_step()
        plan = ImplementationPlan([step], project_dir="/my/project")
        gen = plan.implement(step)
        assert gen.cwd == "/my/project"
        assert gen.setting_sources == ["project"]


# ---------------------------------------------------------------------------
# Pipeline integration tests (PipelineTestRunner)
# ---------------------------------------------------------------------------


def _make_test_step(
    name: str = "step-01",
    body: str = "# Add hooks\nImplement git hooks.",
) -> PlanStep:
    return PlanStep(
        name=name,
        body=body,
        source_path=f"/tmp/{name}.md",
        test_cmd="uv run python -m pytest tests/ -v",
        bats_cmd="bats -r bats/ -v",
        add_cmd="git add -u",
        commit_subject=f"refactor: {name}",
    )


def _build(steps: list[PlanStep] | None = None) -> Pipeline:
    """Build a pipeline using the real structure from pipeline.py."""
    plan = ImplementationPlan(steps or [_make_test_step()], project_dir="/tmp")
    return build_pipeline(plan)


@pytest.mark.asyncio
async def test_single_step_happy_path():
    """Implement succeeds, tests pass, commit runs — fix never called."""
    implement = MockStage().returns("implemented").as_agent()
    fix = MockStage().returns("fixed").as_agent()
    test_py = MockStage().returns({"stdout": "3 passed", "stderr": "", "returncode": 0})
    bats = MockStage().returns({"stdout": "5 tests", "stderr": "", "returncode": 0})
    commit = MockStage().returns({"stdout": "committed", "stderr": "", "returncode": 0})
    summarize = MockStage().returns("summary").as_agent()
    record = MockStage().returns({"stdout": "abc123", "stderr": "", "returncode": 0})

    result = await (
        PipelineTestRunner(_build())
        .patch("record start", record)
        .patch("implement step-01", implement)
        .patch("fix step-01", fix)
        .patch("test step-01", test_py)
        .patch("bats step-01", bats)
        .patch("commit step-01", commit)
        .patch("summarize step-01", summarize)
        .run()
    )

    result.assert_completed()
    verify(implement).called(times=1)
    verify(fix).never_called()
    verify(test_py).called(times=1)
    verify(bats).called(times=1)


@pytest.mark.asyncio
async def test_pytest_fails_then_fix_retries():
    """When pytest fails, the loop retries with fix stage running."""
    implement = MockStage().returns("code").as_agent()
    fix = MockStage().returns("fixed code").as_agent()
    test_py = MockStage().fails(times=1, error="FAILED test_foo").then_returns(
        {"stdout": "3 passed", "stderr": "", "returncode": 0}
    )
    bats = MockStage().returns({"stdout": "ok", "stderr": "", "returncode": 0})
    commit = MockStage().returns({"stdout": "ok", "stderr": "", "returncode": 0})
    summarize = MockStage().returns("summary").as_agent()
    record = MockStage().returns({"stdout": "abc123", "stderr": "", "returncode": 0})

    result = await (
        PipelineTestRunner(_build())
        .patch("record start", record)
        .patch("implement step-01", implement)
        .patch("fix step-01", fix)
        .patch("test step-01", test_py)
        .patch("bats step-01", bats)
        .patch("commit step-01", commit)
        .patch("summarize step-01", summarize)
        .run()
    )

    result.assert_completed()
    verify(fix).called(times=1)
    verify(test_py).called(times=2)
    verify(test_py).on_attempt(1).failed()
    verify(test_py).on_attempt(2).succeeded()


@pytest.mark.asyncio
async def test_bats_fails_then_fix_retries():
    """When bats fails, the loop retries with fix stage running."""
    implement = MockStage().returns("code").as_agent()
    fix = MockStage().returns("fixed").as_agent()
    test_py = MockStage().returns({"stdout": "passed", "stderr": "", "returncode": 0})
    bats = MockStage().fails(times=1, error="FAILED bats test").then_returns(
        {"stdout": "ok", "stderr": "", "returncode": 0}
    )
    commit = MockStage().returns({"stdout": "ok", "stderr": "", "returncode": 0})
    summarize = MockStage().returns("summary").as_agent()
    record = MockStage().returns({"stdout": "abc123", "stderr": "", "returncode": 0})

    result = await (
        PipelineTestRunner(_build())
        .patch("record start", record)
        .patch("implement step-01", implement)
        .patch("fix step-01", fix)
        .patch("test step-01", test_py)
        .patch("bats step-01", bats)
        .patch("commit step-01", commit)
        .patch("summarize step-01", summarize)
        .run()
    )

    result.assert_completed()
    verify(fix).called(times=1)
    verify(bats).called(times=2)
    verify(bats).on_attempt(1).failed()
    verify(bats).on_attempt(2).succeeded()


@pytest.mark.asyncio
async def test_exhausts_retries_raises():
    """Pipeline fails when max_retries (5) exhausted with tests always failing."""
    implement = MockStage().returns("code").as_agent()
    fix = MockStage().returns("attempt").as_agent()
    test_py = MockStage().always_fails(error="FAILED")
    bats = MockStage().returns({"stdout": "ok", "stderr": "", "returncode": 0})
    commit = MockStage().returns({"stdout": "ok", "stderr": "", "returncode": 0})
    summarize = MockStage().returns("summary").as_agent()
    record = MockStage().returns({"stdout": "abc123", "stderr": "", "returncode": 0})

    with pytest.raises(Exception):
        await (
            PipelineTestRunner(_build())
            .patch("record start", record)
            .patch("implement step-01", implement)
            .patch("fix step-01", fix)
            .patch("test step-01", test_py)
            .patch("bats step-01", bats)
            .patch("commit step-01", commit)
            .patch("summarize step-01", summarize)
            .run()
        )

    verify(implement).called(times=1)
    verify(test_py).called(at_least=2)


@pytest.mark.asyncio
async def test_original_impl_preserved():
    """Patching preserves original stage implementations for inspection."""
    implement = MockStage().returns("code").as_agent()
    fix = MockStage().returns("fixed").as_agent()
    test_py = MockStage().returns({"stdout": "ok", "stderr": "", "returncode": 0})
    bats = MockStage().returns({"stdout": "ok", "stderr": "", "returncode": 0})
    commit = MockStage().returns({"stdout": "ok", "stderr": "", "returncode": 0})
    summarize = MockStage().returns("summary").as_agent()
    record = MockStage().returns({"stdout": "abc123", "stderr": "", "returncode": 0})

    result = await (
        PipelineTestRunner(_build())
        .patch("record start", record)
        .patch("implement step-01", implement)
        .patch("fix step-01", fix)
        .patch("test step-01", test_py)
        .patch("bats step-01", bats)
        .patch("commit step-01", commit)
        .patch("summarize step-01", summarize)
        .run()
    )

    assert isinstance(result.mock("implement step-01").original_impl, Generate)
    assert isinstance(result.mock("fix step-01").original_impl, Generate)
    assert isinstance(result.mock("summarize step-01").original_impl, Generate)
    assert isinstance(result.mock("test step-01").original_impl, RunCommand)
    assert isinstance(result.mock("bats step-01").original_impl, RunCommand)
    assert isinstance(result.mock("commit step-01").original_impl, RunCommand)


@pytest.mark.asyncio
async def test_implement_prompt_contains_step_body():
    """The implement stage's Generate.prompt includes the step body text."""
    step = _make_test_step()
    implement = MockStage().returns("code").as_agent()
    fix = MockStage().returns("fixed").as_agent()
    test_py = MockStage().returns({"stdout": "ok", "stderr": "", "returncode": 0})
    bats = MockStage().returns({"stdout": "ok", "stderr": "", "returncode": 0})
    commit = MockStage().returns({"stdout": "ok", "stderr": "", "returncode": 0})
    summarize = MockStage().returns("summary").as_agent()
    record = MockStage().returns({"stdout": "abc123", "stderr": "", "returncode": 0})

    result = await (
        PipelineTestRunner(_build([step]))
        .patch("record start", record)
        .patch("implement step-01", implement)
        .patch("fix step-01", fix)
        .patch("test step-01", test_py)
        .patch("bats step-01", bats)
        .patch("commit step-01", commit)
        .patch("summarize step-01", summarize)
        .run()
    )

    gen = result.mock("implement step-01").original_impl
    assert isinstance(gen, Generate)
    assert "Implement git hooks." in gen.prompt


@pytest.mark.asyncio
async def test_fix_prompt_references_test_outputs():
    """The fix stage's prompt references test output placeholders."""
    step = _make_test_step()
    implement = MockStage().returns("code").as_agent()
    fix = MockStage().returns("fixed").as_agent()
    test_py = MockStage().fails(times=1, error="FAILED").then_returns(
        {"stdout": "ok", "stderr": "", "returncode": 0}
    )
    bats = MockStage().returns({"stdout": "ok", "stderr": "", "returncode": 0})
    commit = MockStage().returns({"stdout": "ok", "stderr": "", "returncode": 0})
    summarize = MockStage().returns("summary").as_agent()
    record = MockStage().returns({"stdout": "abc123", "stderr": "", "returncode": 0})

    result = await (
        PipelineTestRunner(_build([step]))
        .patch("record start", record)
        .patch("implement step-01", implement)
        .patch("fix step-01", fix)
        .patch("test step-01", test_py)
        .patch("bats step-01", bats)
        .patch("commit step-01", commit)
        .patch("summarize step-01", summarize)
        .run()
    )

    fix_gen = result.mock("fix step-01").original_impl
    assert isinstance(fix_gen, Generate)
    assert "{test step-01.output}" in fix_gen.prompt
    assert "{bats step-01.output}" in fix_gen.prompt


@pytest.mark.asyncio
async def test_custom_test_cmd():
    """Step with custom test_cmd is reflected in the test stage's RunCommand."""
    step = PlanStep(
        name="step-01",
        body="# Custom\nCustom test.",
        source_path="/tmp/step-01.md",
        test_cmd="uv run python -m pytest tests/custom/ -v --tb=short",
        bats_cmd="bats -r bats/ -v",
        add_cmd="git add -u",
        commit_subject="refactor: step-01",
    )
    implement = MockStage().returns("code").as_agent()
    fix = MockStage().returns("fixed").as_agent()
    test_py = MockStage().returns({"stdout": "ok", "stderr": "", "returncode": 0})
    bats = MockStage().returns({"stdout": "ok", "stderr": "", "returncode": 0})
    commit = MockStage().returns({"stdout": "ok", "stderr": "", "returncode": 0})
    summarize = MockStage().returns("summary").as_agent()
    record = MockStage().returns({"stdout": "abc123", "stderr": "", "returncode": 0})

    result = await (
        PipelineTestRunner(_build([step]))
        .patch("record start", record)
        .patch("implement step-01", implement)
        .patch("fix step-01", fix)
        .patch("test step-01", test_py)
        .patch("bats step-01", bats)
        .patch("commit step-01", commit)
        .patch("summarize step-01", summarize)
        .run()
    )

    test_cmd_stage = result.mock("test step-01").original_impl
    assert isinstance(test_cmd_stage, RunCommand)
    assert "tests/custom/" in test_cmd_stage.cmd


@pytest.mark.asyncio
async def test_commit_subject_from_h1():
    """Commit RunCommand.cmd contains the H1 text from the step body."""
    step = _make_test_step(body="# Add hooks\nImplement git hooks.")
    # commit_subject is set by _make_test_step as "refactor: step-01"
    # but a real step with H1 would have "refactor: step-01 — Add hooks"
    step = PlanStep(
        name="step-01",
        body="# Add hooks\nImplement git hooks.",
        source_path="/tmp/step-01.md",
        test_cmd="uv run python -m pytest tests/ -v",
        bats_cmd="bats -r bats/ -v",
        add_cmd="git add -u",
        commit_subject="refactor: step-01 — Add hooks",
    )
    implement = MockStage().returns("code").as_agent()
    fix = MockStage().returns("fixed").as_agent()
    test_py = MockStage().returns({"stdout": "ok", "stderr": "", "returncode": 0})
    bats = MockStage().returns({"stdout": "ok", "stderr": "", "returncode": 0})
    commit = MockStage().returns({"stdout": "ok", "stderr": "", "returncode": 0})
    summarize = MockStage().returns("summary").as_agent()
    record = MockStage().returns({"stdout": "abc123", "stderr": "", "returncode": 0})

    result = await (
        PipelineTestRunner(_build([step]))
        .patch("record start", record)
        .patch("implement step-01", implement)
        .patch("fix step-01", fix)
        .patch("test step-01", test_py)
        .patch("bats step-01", bats)
        .patch("commit step-01", commit)
        .patch("summarize step-01", summarize)
        .run()
    )

    commit_stage = result.mock("commit step-01").original_impl
    assert isinstance(commit_stage, RunCommand)
    assert "Add hooks" in commit_stage.cmd


@pytest.mark.asyncio
async def test_multi_step_runs_all_steps():
    """Two steps — verify both implement stages are called."""
    s1 = _make_test_step(name="step-01", body="# Hooks\nAdd hooks.")
    s2 = _make_test_step(name="step-02", body="# Tests\nAdd tests.")

    impl1 = MockStage().returns("code1").as_agent()
    impl2 = MockStage().returns("code2").as_agent()
    fix1 = MockStage().returns("fix1").as_agent()
    fix2 = MockStage().returns("fix2").as_agent()
    test1 = MockStage().returns({"stdout": "ok", "stderr": "", "returncode": 0})
    test2 = MockStage().returns({"stdout": "ok", "stderr": "", "returncode": 0})
    bats1 = MockStage().returns({"stdout": "ok", "stderr": "", "returncode": 0})
    bats2 = MockStage().returns({"stdout": "ok", "stderr": "", "returncode": 0})
    commit1 = MockStage().returns({"stdout": "ok", "stderr": "", "returncode": 0})
    commit2 = MockStage().returns({"stdout": "ok", "stderr": "", "returncode": 0})
    sum1 = MockStage().returns("summary1").as_agent()
    sum2 = MockStage().returns("summary2").as_agent()
    record = MockStage().returns({"stdout": "abc123", "stderr": "", "returncode": 0})

    result = await (
        PipelineTestRunner(_build([s1, s2]))
        .patch("record start", record)
        .patch("implement step-01", impl1)
        .patch("implement step-02", impl2)
        .patch("fix step-01", fix1)
        .patch("fix step-02", fix2)
        .patch("test step-01", test1)
        .patch("test step-02", test2)
        .patch("bats step-01", bats1)
        .patch("bats step-02", bats2)
        .patch("commit step-01", commit1)
        .patch("commit step-02", commit2)
        .patch("summarize step-01", sum1)
        .patch("summarize step-02", sum2)
        .run()
    )

    result.assert_completed()
    verify(impl1).called(times=1)
    verify(impl2).called(times=1)
    verify(impl1).called_before(impl2)

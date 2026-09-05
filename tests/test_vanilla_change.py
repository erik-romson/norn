"""Tests for norn/pipelines/vanilla_change.py pipeline."""

from __future__ import annotations

import pytest

from norn.stages.generate import Generate
from norn.stages.run_command import RunCommand
from norn.testing import (
    MockStage,
    PipelineTestRunner,
    verify,
)


@pytest.mark.asyncio
async def test_happy_path_all_tests_pass_first_try():
    """Pipeline completes when implement succeeds and both tests pass."""
    implement = MockStage().returns("implemented").as_agent()
    fix = MockStage().returns("fixed").as_agent()
    test_py = MockStage().returns({"stdout": "3 passed", "stderr": "", "returncode": 0})
    test_bats = MockStage().returns({"stdout": "5 tests", "stderr": "", "returncode": 0})

    result = await (
        PipelineTestRunner("norn/pipelines/vanilla_change.py")
        .patch("implement", implement)
        .patch("fix", fix)
        .patch("test python", test_py)
        .patch("test bats", test_bats)
        .with_param("args", "add a --dry-run flag")
        .run()
    )

    result.assert_completed()
    result.stage("implement").assert_success()
    result.stage("test python").assert_success()
    result.stage("test bats").assert_success()

    verify(implement).called(times=1)
    # fix should be skipped on first iteration (no prior test failure)
    verify(fix).never_called()
    verify(test_py).called(times=1)
    verify(test_bats).called(times=1)

    # implement runs before tests
    verify(implement).called_before(test_py)


@pytest.mark.asyncio
async def test_pytest_fails_then_fix_retries():
    """When pytest fails, the loop retries with fix stage running."""
    implement = MockStage().returns("code").as_agent()
    fix = MockStage().returns("fixed code").as_agent()
    test_py = MockStage().fails(times=1, error="FAILED test_foo").then_returns(
        {"stdout": "3 passed", "stderr": "", "returncode": 0}
    )
    test_bats = MockStage().returns({"stdout": "ok", "stderr": "", "returncode": 0})

    result = await (
        PipelineTestRunner("norn/pipelines/vanilla_change.py")
        .patch("implement", implement)
        .patch("fix", fix)
        .patch("test python", test_py)
        .patch("test bats", test_bats)
        .with_param("args", "some change")
        .run()
    )

    result.assert_completed()

    verify(implement).called(times=1)
    # fix runs on the retry iteration
    verify(fix).called(times=1)
    # test python: fail once + pass once = 2 calls
    verify(test_py).called(times=2)
    verify(test_py).on_attempt(1).failed()
    verify(test_py).on_attempt(2).succeeded()


@pytest.mark.asyncio
async def test_bats_fails_then_fix_retries():
    """When bats fails, the loop retries with fix stage running."""
    implement = MockStage().returns("code").as_agent()
    fix = MockStage().returns("fixed").as_agent()
    test_py = MockStage().returns({"stdout": "passed", "stderr": "", "returncode": 0})
    test_bats = MockStage().fails(times=1, error="FAILED bats test").then_returns(
        {"stdout": "ok", "stderr": "", "returncode": 0}
    )

    result = await (
        PipelineTestRunner("norn/pipelines/vanilla_change.py")
        .patch("implement", implement)
        .patch("fix", fix)
        .patch("test python", test_py)
        .patch("test bats", test_bats)
        .with_param("args", "some change")
        .run()
    )

    result.assert_completed()

    verify(fix).called(times=1)
    verify(test_bats).called(times=2)
    verify(test_bats).on_attempt(1).failed()
    verify(test_bats).on_attempt(2).succeeded()


@pytest.mark.asyncio
async def test_exhausts_retries_raises():
    """Pipeline fails when max_retries (5) exhausted with tests always failing."""
    implement = MockStage().returns("code").as_agent()
    fix = MockStage().returns("attempt").as_agent()
    test_py = MockStage().always_fails(error="FAILED")
    test_bats = MockStage().returns({"stdout": "ok", "stderr": "", "returncode": 0})

    with pytest.raises(Exception):
        await (
            PipelineTestRunner("norn/pipelines/vanilla_change.py")
            .patch("implement", implement)
            .patch("fix", fix)
            .patch("test python", test_py)
            .patch("test bats", test_bats)
            .with_param("args", "doomed change")
            .run()
        )

    # implement once + test_and_fix loop runs up to 6 times (1 initial + 5 retries)
    verify(implement).called(times=1)
    verify(test_py).called(at_least=2)


@pytest.mark.asyncio
async def test_original_impl_preserved_via_patch():
    """Patching preserves the original stage implementations for inspection."""
    implement = MockStage().returns("code").as_agent()
    fix = MockStage().returns("fixed").as_agent()
    test_py = MockStage().returns({"stdout": "ok", "stderr": "", "returncode": 0})
    test_bats = MockStage().returns({"stdout": "ok", "stderr": "", "returncode": 0})

    result = await (
        PipelineTestRunner("norn/pipelines/vanilla_change.py")
        .patch("implement", implement)
        .patch("fix", fix)
        .patch("test python", test_py)
        .patch("test bats", test_bats)
        .with_param("args", "change")
        .run()
    )

    # Original impl types are preserved
    assert isinstance(result.mock("implement").original_impl, Generate)
    assert isinstance(result.mock("fix").original_impl, Generate)
    assert isinstance(result.mock("test python").original_impl, RunCommand)
    assert isinstance(result.mock("test bats").original_impl, RunCommand)


@pytest.mark.asyncio
async def test_implement_prompt_contains_param_args():
    """The implement stage's original prompt references {param.args}."""
    implement = MockStage().returns("code").as_agent()
    test_py = MockStage().returns({"stdout": "ok", "stderr": "", "returncode": 0})
    test_bats = MockStage().returns({"stdout": "ok", "stderr": "", "returncode": 0})

    result = await (
        PipelineTestRunner("norn/pipelines/vanilla_change.py")
        .patch("implement", implement)
        .patch("fix", MockStage().returns("fixed").as_agent())
        .patch("test python", test_py)
        .patch("test bats", test_bats)
        .with_param("args", "test change")
        .run()
    )

    gen = result.mock("implement").original_impl
    assert isinstance(gen, Generate)
    assert "{param.args}" in gen.prompt


@pytest.mark.asyncio
async def test_fix_prompt_references_test_outputs():
    """The fix stage's original prompt references test output placeholders."""
    implement = MockStage().returns("code").as_agent()
    fix = MockStage().returns("fixed").as_agent()
    test_py = MockStage().fails(times=1, error="FAILED").then_returns(
        {"stdout": "ok", "stderr": "", "returncode": 0}
    )
    test_bats = MockStage().returns({"stdout": "ok", "stderr": "", "returncode": 0})

    result = await (
        PipelineTestRunner("norn/pipelines/vanilla_change.py")
        .patch("implement", implement)
        .patch("fix", fix)
        .patch("test python", test_py)
        .patch("test bats", test_bats)
        .with_param("args", "test change")
        .run()
    )

    fix_gen = result.mock("fix").original_impl
    assert isinstance(fix_gen, Generate)
    assert "{test python.output}" in fix_gen.prompt
    assert "{test bats.output}" in fix_gen.prompt

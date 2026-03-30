"""Tests for pipeline configs using mockito-based mock stages.

Exercises dogfooding/vanilla_change.py pipeline logic without calling
the Claude SDK or running real subprocesses.
"""

from typing import Any

import pytest
from mockito import unstub

from norn.models import PipelineContext, StageResult
from norn.runner import RetriesExhaustedError
from norn.testing import (
    mock_generate,
    mock_run_command,
    patch_stages,
    run_test,
)


@pytest.fixture(autouse=True)
def _cleanup_mockito():
    """Unstub mockito after each test."""
    yield
    unstub()


# ---------------------------------------------------------------------------
# vanilla_change.py — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vanilla_change_all_pass_first_try():
    """All stages pass on the first attempt — no fix stage triggered."""
    impl = mock_generate(output="implemented code")
    fix = mock_generate(output="should not run")
    test_py = mock_run_command(stdout="6 passed")
    test_bats = mock_run_command(stdout="3 tests, 0 failures")

    ctx = await run_test(
        "dogfooding/vanilla_change.py",
        patches={
            "implement": impl,
            "fix": fix,
            "test python": test_py,
            "test bats": test_bats,
        },
        params={"args": "add feature X"},
    )

    assert impl._call_count == 1
    assert test_py._call_count == 1
    assert test_bats._call_count == 1
    assert fix._call_count == 0  # fix never triggered
    assert ctx.results["implement"].success
    assert ctx.results["test python"].success


@pytest.mark.asyncio
async def test_vanilla_change_pytest_fails_then_fix():
    """pytest fails once, fix stage runs on retry, then passes."""
    impl = mock_generate(output="initial code")
    fix = mock_generate(output="fixed code")
    test_py = mock_run_command(fail_count=1, stderr="FAILED test_foo")
    test_bats = mock_run_command(stdout="ok")

    await run_test(
        "dogfooding/vanilla_change.py",
        patches={
            "implement": impl,
            "fix": fix,
            "test python": test_py,
            "test bats": test_bats,
        },
        params={"args": "fix bug"},
    )

    assert impl._call_count == 1
    assert fix._call_count == 1  # fix runs on retry
    assert test_py._call_count == 2  # fail + pass
    assert test_bats._call_count == 1  # only runs on passing attempt


@pytest.mark.asyncio
async def test_vanilla_change_bats_fails_then_fix():
    """bats fails once, fix stage triggers on retry."""
    impl = mock_generate(output="code")
    fix = mock_generate(output="fixed")
    test_py = mock_run_command(stdout="passed")
    test_bats = mock_run_command(fail_count=1, stderr="not ok 1 test failed")

    await run_test(
        "dogfooding/vanilla_change.py",
        patches={
            "implement": impl,
            "fix": fix,
            "test python": test_py,
            "test bats": test_bats,
        },
        params={"args": "fix bats"},
    )

    assert fix._call_count == 1
    assert test_bats._call_count == 2


# ---------------------------------------------------------------------------
# vanilla_change.py — exhausted retries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vanilla_change_retries_exhausted():
    """All retries fail — raises RetriesExhaustedError."""
    impl = mock_generate(output="code")
    fix = mock_generate(output="still broken")
    test_py = mock_run_command(fail_count=99, stderr="always fails")
    test_bats = mock_run_command(stdout="ok")

    with pytest.raises(RetriesExhaustedError):
        await run_test(
            "dogfooding/vanilla_change.py",
            patches={
                "implement": impl,
                "fix": fix,
                "test python": test_py,
                "test bats": test_bats,
            },
            params={"args": "impossible task"},
        )

    # implement once + fix on retries 2-5 = 4 fix calls
    assert fix._call_count == 4
    assert test_py._call_count == 5


# ---------------------------------------------------------------------------
# Session continuity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vanilla_change_session_flows_from_implement_to_fix():
    """The fix stage receives the session_id from the implement stage."""
    impl = mock_generate(output="code", session_id="impl-session")
    fix = mock_generate(output="fixed", session_id="impl-session")
    test_py = mock_run_command(fail_count=1, stderr="fail")
    test_bats = mock_run_command()

    await run_test(
        "dogfooding/vanilla_change.py",
        patches={
            "implement": impl,
            "fix": fix,
            "test python": test_py,
            "test bats": test_bats,
        },
        params={"args": "test"},
    )

    # fix should receive the session from implement
    assert fix._received_sessions[0] == "impl-session"


# ---------------------------------------------------------------------------
# Context inspection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vanilla_change_fix_sees_failed_test_in_context():
    """The fix stage sees the failed pytest result when it runs."""
    snapshots: list[dict] = []

    # Wrap mock_generate to capture context snapshots
    fix = mock_generate(output="fixed")
    original_run = fix.run

    async def capturing_run(ctx: PipelineContext, **kwargs: Any) -> StageResult:
        snapshots.append({
            name: (r.success, r.error)
            for name, r in ctx.results.items()
        })
        return await original_run(ctx, **kwargs)

    fix.run = capturing_run

    impl = mock_generate(output="code")
    test_py = mock_run_command(fail_count=1, stderr="AssertionError: expected 1 got 2")
    test_bats = mock_run_command(stdout="ok")

    await run_test(
        "dogfooding/vanilla_change.py",
        patches={
            "implement": impl,
            "fix": fix,
            "test python": test_py,
            "test bats": test_bats,
        },
        params={"args": "test"},
    )

    # fix ran once on attempt 2, snapshot shows failed pytest from attempt 1
    assert len(snapshots) == 1
    success, error = snapshots[0]["test python"]
    assert not success
    assert "AssertionError" in error


# ---------------------------------------------------------------------------
# patch_stages utility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_stages_replaces_by_name():
    """patch_stages replaces specific stages while leaving others intact."""
    from norn.dsl import Stage
    from norn.loader import load_pipeline
    from norn.stages.generate import Generate

    pipeline = load_pipeline("dogfooding/vanilla_change.py")

    # Before patching, implement is a real Generate
    impl_stage = pipeline.items[0]
    assert isinstance(impl_stage, Stage)
    assert isinstance(impl_stage.impl, Generate)

    # After patching
    m = mock_generate()
    patch_stages(pipeline, {"implement": m})
    impl_stage = pipeline.items[0]
    assert isinstance(impl_stage, Stage)
    assert impl_stage.impl is m

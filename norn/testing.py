"""Testing utilities for norn using mockito.

Provides factory functions that create mockito mocks for pipeline stages,
plus utilities for patching stages in loaded pipeline configs.

Usage::

    from mockito import verify, unstub
    from norn.testing import mock_generate, mock_run_command, patch_stages, run_test

    gen = mock_generate(output="done")
    cmd = mock_run_command(fail_count=2, stderr="test failed")

    ctx = await run_test(
        "dogfooding/vanilla_change.py",
        patches={"implement": gen, "test python": cmd},
        params={"args": "add feature X"},
    )

    # mockito verify
    verify(gen, times=1).run(...)
    verify(cmd, times=3).run(...)

    unstub()
"""

from __future__ import annotations

from typing import Any

from mockito import mock

from norn.dsl import Loop, Parallel, Pipeline, Stage
from norn.loader import load_pipeline
from norn.models import PipelineContext, StageResult, UsageRecord
from norn.runner import run_pipeline
from norn.stages.base import BaseStage


def _make_success_result(
    output: Any = "mock output",
    session_id: str = "mock-session-1",
    cost_usd: float = 0.01,
    artifacts: list[str] | None = None,
    attempt: int = 1,
) -> StageResult:
    """Build a successful StageResult with usage."""
    usage = UsageRecord(
        stage_name="",
        session_id=session_id,
        input_tokens=100,
        output_tokens=50,
        total_cost_usd=cost_usd,
        duration_ms=500,
        duration_api_ms=400,
        num_turns=1,
        attempt=attempt,
    )
    return StageResult(
        name="", success=True, output=output, usage=usage,
        artifacts=list(artifacts or []),
    )


def _make_failure_result(
    error: str = "mock failure",
    session_id: str = "mock-session-1",
    cost_usd: float = 0.01,
    attempt: int = 1,
) -> StageResult:
    """Build a failed StageResult with usage."""
    usage = UsageRecord(
        stage_name="",
        session_id=session_id,
        input_tokens=100,
        output_tokens=50,
        total_cost_usd=cost_usd,
        duration_ms=500,
        duration_api_ms=400,
        num_turns=1,
        attempt=attempt,
    )
    return StageResult(name="", success=False, error=error, usage=usage)


def mock_generate(
    *,
    output: Any = "mock output",
    fail_count: int = 0,
    error: str = "mock failure",
    session_id: str = "mock-session-1",
    cost_usd: float = 0.01,
    artifacts: list[str] | None = None,
) -> BaseStage:
    """Create a mockito mock for a Generate stage.

    Returns a mock ``BaseStage`` with ``needs_agent = True`` whose ``run()``
    method fails ``fail_count`` times then succeeds.

    Args:
        output: Value to return as ``StageResult.output`` on success.
        fail_count: Number of initial calls that fail before succeeding.
        error: Error message for failed calls.
        session_id: Session ID to report in usage records.
        cost_usd: Simulated cost per call.
        artifacts: File paths to report as artifacts.

    Example::

        gen = mock_generate(output="code", fail_count=1, error="syntax error")
        # Call 1: fails with "syntax error"
        # Call 2+: succeeds with output "code"

        verify(gen, times=2).run(...)
    """
    stage = mock(BaseStage, strict=False)
    stage.needs_agent = True
    stage._call_count = 0
    stage._received_sessions = []
    stage._received_attempts = []

    async def _run(ctx: PipelineContext, **kwargs: Any) -> StageResult:
        stage._call_count += 1
        stage._received_sessions.append(kwargs.get("session_id"))
        stage._received_attempts.append(kwargs.get("attempt", 1))
        attempt = kwargs.get("attempt", 1)

        if stage._call_count <= fail_count:
            return _make_failure_result(
                error=error, session_id=session_id, cost_usd=cost_usd, attempt=attempt,
            )
        return _make_success_result(
            output=output, session_id=session_id, cost_usd=cost_usd,
            artifacts=artifacts, attempt=attempt,
        )

    stage.run = _run
    return stage


def mock_run_command(
    *,
    stdout: str = "",
    stderr: str = "mock test failure",
    fail_count: int = 0,
    returncode_on_fail: int = 1,
) -> BaseStage:
    """Create a mockito mock for a RunCommand stage.

    Returns a mock ``BaseStage`` whose ``run()`` method fails ``fail_count``
    times (returning ``returncode_on_fail``) then succeeds.

    Args:
        stdout: Simulated stdout on success.
        stderr: Simulated stderr on failure.
        fail_count: Number of initial calls that fail before succeeding.
        returncode_on_fail: Exit code for failed calls.

    Example::

        cmd = mock_run_command(fail_count=1, stderr="FAILED test_foo")
        # Call 1: fails with returncode 1
        # Call 2+: succeeds with returncode 0
    """
    stage = mock(BaseStage, strict=False)
    stage.needs_agent = False
    stage._call_count = 0

    async def _run(ctx: PipelineContext, **kwargs: Any) -> StageResult:
        stage._call_count += 1

        if stage._call_count <= fail_count:
            output = {"stdout": "", "stderr": stderr, "returncode": returncode_on_fail}
            return StageResult(name="", success=False, output=output, error=stderr)

        output = {"stdout": stdout, "stderr": "", "returncode": 0}
        return StageResult(name="", success=True, output=output)

    stage.run = _run
    return stage


def mock_read_file(*, content: str = "mock file content") -> BaseStage:
    """Create a mockito mock for a ReadFile stage.

    Args:
        content: File content to return.

    Example::

        rf = mock_read_file(content="spec contents")
    """
    stage = mock(BaseStage, strict=False)
    stage.needs_agent = False
    stage._call_count = 0

    async def _run(ctx: PipelineContext, **kwargs: Any) -> StageResult:
        stage._call_count += 1
        return StageResult(name="", success=True, output=content)

    stage.run = _run
    return stage


def _patch_items(items: list, patches: dict[str, BaseStage]) -> None:
    """Recursively replace stage implementations by name in a pipeline's items."""
    for i, item in enumerate(items):
        if isinstance(item, Stage) and item.name in patches:
            items[i] = Stage(
                name=item.name,
                impl=patches[item.name],
                on_failure=item.on_failure,
                when=item.when,
                timeout=item.timeout,
            )
        elif isinstance(item, Loop):
            _patch_items(item.stages, patches)
        elif isinstance(item, Parallel):
            _patch_items(item.stages, patches)


def patch_stages(pipeline: Pipeline, patches: dict[str, BaseStage]) -> Pipeline:
    """Replace stage implementations by name in a pipeline.

    Returns the same pipeline instance (mutated) with matched stages
    swapped to the provided mock implementations.

    Args:
        pipeline: The pipeline to patch.
        patches: Mapping of stage name to mock implementation.

    Example::

        from norn.testing import mock_generate, mock_run_command, patch_stages

        pipeline = load_pipeline("dogfooding/vanilla_change.py")
        patch_stages(pipeline, {
            "implement": mock_generate(output="code"),
            "test python": mock_run_command(),
        })
    """
    _patch_items(pipeline.items, patches)
    return pipeline


async def run_test(
    config_path: str,
    *,
    patches: dict[str, BaseStage],
    params: dict[str, Any] | None = None,
    resume_session: str | None = None,
) -> PipelineContext:
    """Load a pipeline config, patch stages, and run it.

    Convenience function that combines ``load_pipeline``, ``patch_stages``,
    and ``run_pipeline`` into a single call.

    Args:
        config_path: Path to the pipeline ``.py`` config file.
        patches: Stage name → mock implementation mapping.
        params: Pipeline parameters (e.g. ``{"args": "do something"}``).
        resume_session: Optional session ID to resume.

    Returns:
        The final ``PipelineContext`` after the pipeline completes.

    Example::

        ctx = await run_test(
            "dogfooding/vanilla_change.py",
            patches={
                "implement": mock_generate(output="code"),
                "fix": mock_generate(output="fixed"),
                "test python": mock_run_command(fail_count=1),
                "test bats": mock_run_command(),
            },
            params={"args": "add feature X"},
        )
        assert ctx.results["test python"].success
    """
    pipeline = load_pipeline(config_path)
    patch_stages(pipeline, patches)
    return await run_pipeline(
        pipeline,
        params=params,
        resume_session=resume_session,
    )

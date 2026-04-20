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

import copy
import inspect
import itertools
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mockito import mock

from norn.dsl import Loop, Parallel, Pipeline, Stage
from norn.loader import load_pipeline
from norn.models import PipelineContext, StageResult, UsageRecord
from norn.runner import run_pipeline
from norn.stages.base import BaseStage

_global_call_counter = itertools.count()


def reset_call_counter() -> None:
    """Reset the global call counter. Called between tests."""
    global _global_call_counter
    _global_call_counter = itertools.count()


@dataclass
class CallRecord:
    """A single recorded invocation of a MockStage."""

    index: int
    ctx: PipelineContext
    kwargs: dict[str, Any]
    result: StageResult
    timestamp: float
    original_impl: BaseStage | None = None

    @property
    def session_id(self) -> str | None:
        """Return the session_id kwarg, if present."""
        return self.kwargs.get("session_id")

    @property
    def attempt(self) -> int:
        """Return the attempt kwarg, defaulting to 1."""
        return self.kwargs.get("attempt", 1)

    @property
    def succeeded(self) -> bool:
        """Return whether the result was successful."""
        return self.result.success

    def context_had(self, stage_name: str) -> bool:
        """Check whether a stage had completed before this call."""
        return stage_name in self.ctx.results


class MockStage(BaseStage):
    """A programmable stage that records all calls for later verification."""

    def __init__(self) -> None:
        self._output: Any = "mock output"
        self._fail_count: int = 0
        self._fail_error: str = "mock failure"
        self._always_fail: bool = False
        self._success_output: Any | None = None  # set by then_returns()
        self._side_effect: Callable | None = None
        self._artifacts: list[str] = []
        self._agent_session_id: str | None = None
        self._agent_cost_usd: float = 0.01
        self._calls: list[CallRecord] = []
        self._call_count: int = 0
        self.original_impl: BaseStage | None = None

    # --- Stubbing (fluent) ---

    def returns(self, output: Any) -> MockStage:
        """Set the output value for successful calls."""
        self._output = output
        return self

    def fails(self, *, times: int = 1, error: str = "mock failure") -> MockStage:
        """Configure the mock to fail for the first `times` calls."""
        self._fail_count = times
        self._fail_error = error
        return self

    def always_fails(self, error: str = "mock failure") -> MockStage:
        """Configure the mock to always fail."""
        self._always_fail = True
        self._fail_error = error
        return self

    def then_returns(self, output: Any) -> MockStage:
        """Set the output value after failures are exhausted."""
        self._success_output = output
        return self

    def as_agent(self, session_id: str | None = None, cost_usd: float = 0.01) -> MockStage:
        """Mark this mock as an agent stage with usage tracking."""
        self.needs_agent = True
        self._agent_session_id = session_id
        self._agent_cost_usd = cost_usd
        return self

    def with_side_effect(self, fn: Callable) -> MockStage:
        """Set a side-effect function called instead of normal stubbing logic."""
        self._side_effect = fn
        return self

    def with_artifacts(self, artifacts: list[str]) -> MockStage:
        """Set artifacts returned on successful calls."""
        self._artifacts = artifacts
        return self

    # --- Recording ---

    @property
    def calls(self) -> list[CallRecord]:
        """Return all recorded calls."""
        return self._calls

    @property
    def call_count(self) -> int:
        """Return how many times this mock was called."""
        return self._call_count

    @property
    def last_call(self) -> CallRecord:
        """Return the most recent call record."""
        if not self._calls:
            raise AssertionError("MockStage was never called")
        return self._calls[-1]

    # --- Execution ---

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        """Execute the mock stage, recording the call."""
        self._call_count += 1
        attempt = kwargs.get("attempt", 1)

        # Snapshot context
        ctx_snapshot = PipelineContext(
            results=dict(ctx.results),
            params=dict(ctx.params),
        )

        # Side effect
        if self._side_effect is not None:
            if inspect.iscoroutinefunction(self._side_effect):
                result = await self._side_effect(ctx, **kwargs)
            else:
                result = self._side_effect(ctx, **kwargs)
            record = CallRecord(
                index=next(_global_call_counter),
                ctx=ctx_snapshot,
                kwargs=dict(kwargs),
                result=result,
                timestamp=time.monotonic(),
                original_impl=self.original_impl,
            )
            self._calls.append(record)
            return result

        # Determine success/failure
        if self._always_fail or self._call_count <= self._fail_count:
            session_id = self._agent_session_id or "mock-session-1"
            result = _make_failure_result(
                error=self._fail_error,
                session_id=session_id,
                cost_usd=self._agent_cost_usd,
                attempt=attempt,
            ) if self.needs_agent else StageResult(
                name="", success=False, error=self._fail_error,
            )
        else:
            output = (
                self._success_output
                if (self._success_output is not None
                    and self._call_count > self._fail_count
                    and self._fail_count > 0)
                else self._output
            )
            session_id = self._agent_session_id or "mock-session-1"
            result = _make_success_result(
                output=output,
                session_id=session_id,
                cost_usd=self._agent_cost_usd,
                artifacts=self._artifacts or None,
                attempt=attempt,
            ) if self.needs_agent else StageResult(
                name="", success=True, output=output,
            )

        record = CallRecord(
            index=next(_global_call_counter),
            ctx=ctx_snapshot,
            kwargs=dict(kwargs),
            result=result,
            timestamp=time.monotonic(),
            original_impl=self.original_impl,
        )
        self._calls.append(record)
        return result


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
            mock = patches[item.name]
            # Stash original impl on MockStage instances for input inspection
            if isinstance(mock, MockStage):
                mock.original_impl = item.impl
            items[i] = Stage(
                name=item.name,
                impl=mock,
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

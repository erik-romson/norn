from __future__ import annotations

import os
import time

import pytest

from norn.models import PipelineContext
from norn.stages.run_command import RunCommand


@pytest.mark.asyncio
async def test_run_command_success():
    stage = RunCommand(cmd="echo hello")
    result = await stage.run(PipelineContext())
    assert result.success
    assert "hello" in result.output["stdout"]


@pytest.mark.asyncio
async def test_run_command_failure():
    stage = RunCommand(cmd="exit 1")
    result = await stage.run(PipelineContext())
    assert not result.success
    assert result.output["returncode"] == 1


@pytest.mark.asyncio
async def test_run_command_env_injected():
    """Stage-level env dict values are available in the subprocess environment."""
    stage = RunCommand(cmd="echo $MY_VAR", env={"MY_VAR": "hello_from_env"})
    result = await stage.run(PipelineContext())
    assert result.success
    assert "hello_from_env" in result.output["stdout"]


@pytest.mark.asyncio
async def test_run_command_secret_in_env_resolved():
    """``{secret.NAME}`` placeholders in stage env are resolved from ctx.secrets."""
    ctx = PipelineContext()
    ctx.secrets = {"TOKEN": "secret_val_xyz"}
    stage = RunCommand(cmd="echo $TOKEN", env={"TOKEN": "{secret.TOKEN}"})
    result = await stage.run(ctx)
    assert result.success
    assert "secret_val_xyz" in result.output["stdout"]


@pytest.mark.asyncio
async def test_run_command_pipeline_env_injected():
    """Pipeline-level env (ctx.env) is available to stages without explicit env."""
    ctx = PipelineContext()
    ctx.env = {"PIPELINE_VAR": "pipeline_val_abc"}
    stage = RunCommand(cmd="echo $PIPELINE_VAR")
    result = await stage.run(ctx)
    assert result.success
    assert "pipeline_val_abc" in result.output["stdout"]


@pytest.mark.asyncio
async def test_run_command_stage_env_overrides_pipeline_env():
    """Stage env takes precedence over pipeline-level env for the same key."""
    ctx = PipelineContext()
    ctx.env = {"VAR": "from_pipeline"}
    stage = RunCommand(cmd="echo $VAR", env={"VAR": "from_stage"})
    result = await stage.run(ctx)
    assert result.success
    assert "from_stage" in result.output["stdout"]


@pytest.mark.asyncio
async def test_run_command_error_includes_both_stdout_and_stderr():
    """Failure error must surface stdout even when stderr is non-empty.

    Tools like ``pg_isready`` write diagnostics to stdout; suppressing it
    would hide the actual failure cause.
    """
    stage = RunCommand(cmd="echo on_stdout; echo on_stderr 1>&2; exit 3")
    result = await stage.run(PipelineContext())
    assert not result.success
    assert "stdout:\non_stdout" in result.error
    assert "stderr:\non_stderr" in result.error


@pytest.mark.asyncio
async def test_run_command_error_surfaces_last_xtrace_line():
    """When ``set -x`` is active, the last traced command appears as a hint."""
    stage = RunCommand(cmd="sh -ex -c 'true; false'")
    result = await stage.run(PipelineContext())
    assert not result.success
    assert "last command: + false" in result.error


@pytest.mark.asyncio
async def test_run_command_times_out_fast_instead_of_hanging():
    """A command that exceeds its timeout fails promptly with a timeout error
    rather than blocking the pipeline indefinitely."""
    started = time.monotonic()
    stage = RunCommand(cmd="sleep 30", timeout=0.3)
    result = await stage.run(PipelineContext())
    assert not result.success
    assert "timed out" in result.error
    assert result.output["returncode"] is None
    # Must return near the timeout, nowhere near the 30s sleep.
    assert time.monotonic() - started < 5


@pytest.mark.asyncio
async def test_run_command_timeout_kills_backgrounded_child(tmp_path):
    """The backstop must reap a backgrounded child that inherited our pipes —
    otherwise communicate() would never see EOF. Relies on the process-group
    kill enabled by ``start_new_session=True``."""
    pidfile = tmp_path / "child.pid"
    # Background a long sleep (inherits stdout/stderr), then keep the parent
    # alive so the whole command overruns the timeout.
    stage = RunCommand(
        cmd=f"sleep 30 & echo $! > {pidfile}; sleep 30",
        timeout=0.4,
    )
    result = await stage.run(PipelineContext())
    assert not result.success

    child_pid = int(pidfile.read_text().strip())
    # killpg should have reaped the backgrounded child too; poll briefly.
    deadline = time.monotonic() + 3.0
    alive = True
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)  # signal 0 = liveness probe
            time.sleep(0.05)
        except ProcessLookupError:
            alive = False
            break
    assert not alive, f"backgrounded child {child_pid} survived the group kill"

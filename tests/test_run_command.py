from __future__ import annotations

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

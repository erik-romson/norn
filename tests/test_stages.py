import pytest

from norn.models import PipelineContext
from norn.stages.read_file import ReadFile
from norn.stages.run_command import RunCommand


@pytest.mark.asyncio
async def test_read_file_success(tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("hello world")
    stage = ReadFile(path=str(f))
    result = await stage.run(PipelineContext())
    assert result.success
    assert result.output == "hello world"


@pytest.mark.asyncio
async def test_read_file_missing():
    stage = ReadFile(path="/nonexistent/file.txt")
    result = await stage.run(PipelineContext())
    assert not result.success
    assert result.error is not None


@pytest.mark.asyncio
async def test_run_command_success():
    stage = RunCommand(cmd="echo hello")
    result = await stage.run(PipelineContext())
    assert result.success
    assert "hello" in result.output["stdout"]
    assert result.output["returncode"] == 0


@pytest.mark.asyncio
async def test_run_command_failure():
    stage = RunCommand(cmd="false")
    result = await stage.run(PipelineContext())
    assert not result.success
    assert result.output["returncode"] != 0

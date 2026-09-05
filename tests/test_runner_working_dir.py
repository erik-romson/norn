from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

from norn.dsl import Pipeline, Stage
from norn.models import PipelineContext, StageResult
from norn.runner import resolve_run_path, run_pipeline
from norn.stages.base import BaseStage
from norn.stages.read_file import ReadFile
from norn.stages.run_command import RunCommand
from norn.stages.validate import Contains, FileExists, Validate


# ---------------------------------------------------------------------------
# resolve_run_path
# ---------------------------------------------------------------------------


def test_resolve_run_path_absolute_unchanged():
    ctx = PipelineContext()
    abs_path = "/tmp/some/absolute/path"
    result = resolve_run_path(ctx, abs_path)
    assert result == Path(abs_path)


def test_resolve_run_path_relative_under_working_dir():
    ctx = PipelineContext()
    ctx.working_dir = "/my/working/dir"
    result = resolve_run_path(ctx, "subdir/file.txt")
    assert result == Path("/my/working/dir/subdir/file.txt")


def test_resolve_run_path_relative_falls_back_to_cwd_when_none():
    ctx = PipelineContext()
    assert ctx.working_dir is None
    result = resolve_run_path(ctx, "relative/path")
    assert result == Path.cwd() / "relative/path"


def test_resolve_run_path_absolute_passes_through_even_with_working_dir():
    ctx = PipelineContext()
    ctx.working_dir = "/my/working/dir"
    abs_path = "/absolute/override"
    result = resolve_run_path(ctx, abs_path)
    assert result == Path(abs_path)


# ---------------------------------------------------------------------------
# run_pipeline working_dir + run_id kwargs
# ---------------------------------------------------------------------------


class CapturingStage(BaseStage):
    """Records the context it was called with."""

    captured: PipelineContext | None = None

    async def run(self, ctx: PipelineContext) -> StageResult:  # type: ignore[override]
        CapturingStage.captured = ctx
        return StageResult(name="", success=True, output="captured")


@pytest.mark.asyncio
async def test_run_pipeline_sets_working_dir():
    CapturingStage.captured = None
    p = Pipeline("wd-test").stage("s", CapturingStage())
    await run_pipeline(p, working_dir="/some/dir")
    assert CapturingStage.captured is not None
    assert CapturingStage.captured.working_dir == "/some/dir"


@pytest.mark.asyncio
async def test_run_pipeline_working_dir_none_by_default():
    CapturingStage.captured = None
    p = Pipeline("wd-default").stage("s", CapturingStage())
    ctx = await run_pipeline(p)
    assert ctx.working_dir is None


@pytest.mark.asyncio
async def test_run_pipeline_seeds_run_id():
    p = Pipeline("rid-test").stage("s", CapturingStage())
    ctx = await run_pipeline(p, run_id="my-custom-run-id")
    assert ctx.run_id == "my-custom-run-id"


@pytest.mark.asyncio
async def test_run_pipeline_generates_run_id_when_omitted():
    p = Pipeline("rid-auto").stage("s", CapturingStage())
    ctx = await run_pipeline(p)
    assert ctx.run_id  # truthy — a UUID was generated
    assert ctx.run_id != "my-custom-run-id"


# ---------------------------------------------------------------------------
# RunCommand honours ctx.working_dir
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_command_executes_in_working_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        # Use a Python one-liner so the test is cross-platform
        cmd = f"{sys.executable} -c \"import os; print(os.getcwd())\""
        p = Pipeline("cwd-test").stage("getcwd", RunCommand(cmd=cmd))
        ctx = await run_pipeline(p, working_dir=tmpdir)
        result = ctx.get("getcwd")
        printed_cwd = result["stdout"].strip()
        # tempfile.TemporaryDirectory may return a symlink path on macOS;
        # resolve both sides so we compare real paths.
        assert Path(printed_cwd).resolve() == Path(tmpdir).resolve()


@pytest.mark.asyncio
async def test_run_command_no_working_dir_uses_process_cwd():
    """When working_dir is None, cwd=None is passed and the subprocess inherits process cwd."""
    cmd = f"{sys.executable} -c \"import os; print(os.getcwd())\""
    p = Pipeline("cwd-default").stage("getcwd", RunCommand(cmd=cmd))
    ctx = await run_pipeline(p)  # no working_dir
    result = ctx.get("getcwd")
    printed_cwd = result["stdout"].strip()
    assert Path(printed_cwd).resolve() == Path.cwd().resolve()


# ---------------------------------------------------------------------------
# ReadFile honours ctx.working_dir
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_file_resolves_relative_under_working_dir():
    """ReadFile resolves a relative path under ctx.working_dir."""
    with tempfile.TemporaryDirectory() as tmpdir:
        target = Path(tmpdir) / "data.txt"
        target.write_text("hello from worktree")
        p = Pipeline("rf-wd").stage("read", ReadFile(path="data.txt"))
        ctx = await run_pipeline(p, working_dir=tmpdir)
        result = ctx.get("read")
        assert result == "hello from worktree"


@pytest.mark.asyncio
async def test_read_file_absolute_path_ignores_working_dir():
    """ReadFile leaves an absolute path untouched even when working_dir is set."""
    with tempfile.TemporaryDirectory() as tmpdir:
        target = Path(tmpdir) / "abs.txt"
        target.write_text("absolute content")
        other_dir = tempfile.mkdtemp()
        try:
            p = Pipeline("rf-abs").stage("read", ReadFile(path=str(target)))
            ctx = await run_pipeline(p, working_dir=other_dir)
            result = ctx.get("read")
            assert result == "absolute content"
        finally:
            import shutil
            shutil.rmtree(other_dir)


@pytest.mark.asyncio
async def test_read_file_no_working_dir_uses_process_cwd():
    """When working_dir is None, ReadFile resolves relative to process cwd."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Write a file in process cwd with a unique name to avoid collisions
        target = Path.cwd() / "__norn_test_read_file_no_wd__.txt"
        target.write_text("process cwd content")
        try:
            p = Pipeline("rf-no-wd").stage("read", ReadFile(path="__norn_test_read_file_no_wd__.txt"))
            ctx = await run_pipeline(p)  # no working_dir
            result = ctx.get("read")
            assert result == "process cwd content"
        finally:
            target.unlink()


# ---------------------------------------------------------------------------
# context(file=...) and context_cmd(...) honour ctx.working_dir
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_file_globs_under_working_dir():
    """A relative file context spec globs under ctx.working_dir."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Write a file in the worktree
        (Path(tmpdir) / "ctx.txt").write_text("worktree context")

        class ContextCaptureStage(BaseStage):
            captured = None

            async def run(self, ctx: PipelineContext) -> StageResult:
                ContextCaptureStage.captured = ctx.injected_context
                return StageResult(name="", success=True, output="ok")

        p = Pipeline("ctx-file").context("ctx.txt", label="myctx").stage("cap", ContextCaptureStage())
        ctx = await run_pipeline(p, working_dir=tmpdir)

    assert ContextCaptureStage.captured is not None
    labels = [label for label, _ in ContextCaptureStage.captured]
    contents = [content for _, content in ContextCaptureStage.captured]
    assert "myctx" in labels
    assert "worktree context" in contents


# ---------------------------------------------------------------------------
# Validate honours ctx.working_dir
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_resolves_relative_under_working_dir():
    """Validate resolves relative check paths under ctx.working_dir."""
    with tempfile.TemporaryDirectory() as tmpdir:
        target = Path(tmpdir) / "check.txt"
        target.write_text("expected content")
        p = Pipeline("val-wd").stage(
            "check",
            Validate(checks=[FileExists("check.txt"), Contains("check.txt", patterns=["expected"])]),
        )
        ctx = await run_pipeline(p, working_dir=tmpdir)
        result = ctx.get("check")
        assert result == []  # all checks passed


@pytest.mark.asyncio
async def test_validate_absolute_path_ignores_working_dir():
    """Validate leaves absolute check paths untouched even when working_dir is set."""
    with tempfile.TemporaryDirectory() as tmpdir:
        target = Path(tmpdir) / "abs.txt"
        target.write_text("absolute content")
        other_dir = tempfile.mkdtemp()
        try:
            p = Pipeline("val-abs").stage(
                "check",
                Validate(checks=[FileExists(str(target)), Contains(str(target), patterns=["absolute"])]),
            )
            ctx = await run_pipeline(p, working_dir=other_dir)
            result = ctx.get("check")
            assert result == []  # absolute paths found despite working_dir pointing elsewhere
        finally:
            import shutil
            shutil.rmtree(other_dir)


@pytest.mark.asyncio
async def test_validate_no_working_dir_uses_process_cwd():
    """When working_dir is None, Validate resolves relative paths against process cwd."""
    target = Path.cwd() / "__norn_test_validate_no_wd__.txt"
    target.write_text("cwd content")
    try:
        p = Pipeline("val-cwd").stage(
            "check",
            Validate(checks=[FileExists("__norn_test_validate_no_wd__.txt")]),
        )
        ctx = await run_pipeline(p)  # no working_dir
        result = ctx.get("check")
        assert result == []  # found in process cwd
    finally:
        target.unlink()


@pytest.mark.asyncio
async def test_context_cmd_runs_in_working_dir():
    """A context_cmd runs with cwd=ctx.working_dir."""
    with tempfile.TemporaryDirectory() as tmpdir:
        class ContextCaptureStage(BaseStage):
            captured = None

            async def run(self, ctx: PipelineContext) -> StageResult:
                ContextCaptureStage.captured = ctx.injected_context
                return StageResult(name="", success=True, output="ok")

        # Print the real cwd from within the subprocess
        cmd = f"{sys.executable} -c \"import os; print(os.path.realpath(os.getcwd()))\""
        p = (
            Pipeline("ctx-cmd")
            .context_cmd(cmd, label="cmdctx")
            .stage("cap", ContextCaptureStage())
        )
        await run_pipeline(p, working_dir=tmpdir)

    assert ContextCaptureStage.captured is not None
    captured_cwd = ContextCaptureStage.captured[0][1].strip()
    assert captured_cwd == str(Path(tmpdir).resolve())

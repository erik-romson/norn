from __future__ import annotations

import os
import time

import pytest

from norn.event_sink import EventSink
from norn.events import CommandOutput
from norn.models import PipelineContext
from norn.ui import register_secrets
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


# ---------------------------------------------------------------------------
# Live output streaming
# ---------------------------------------------------------------------------


def _streaming_ctx() -> tuple[PipelineContext, list]:
    """A context whose sink records every event it receives."""
    seen: list = []
    sink = EventSink(on_event=seen.append)
    return PipelineContext(event_sink=sink, run_id="run-1"), seen


def _command_output(seen: list) -> list[CommandOutput]:
    return [e for e in seen if isinstance(e, CommandOutput)]


@pytest.mark.asyncio
async def test_run_command_streams_output_as_command_output_events():
    """Output reaches the seam while the command runs, tagged by stream."""
    ctx, seen = _streaming_ctx()
    stage = RunCommand(cmd="echo out-line; echo err-line >&2")
    result = await stage.run(ctx, node_id="stage:build", attempt=1)

    assert result.success
    events = _command_output(seen)
    assert [(e.stream, e.text) for e in events] == [
        ("stdout", "out-line"),
        ("stderr", "err-line"),
    ]


@pytest.mark.asyncio
async def test_streamed_events_carry_the_stage_node_id_and_ordered_seq():
    """Events key to the runner's graph node so the TUI files them correctly."""
    ctx, seen = _streaming_ctx()
    stage = RunCommand(cmd="printf 'a\\nb\\nc\\n'")
    await stage.run(ctx, node_id="loop:tests/stage:run", attempt=2)

    events = _command_output(seen)
    assert events, "expected streamed output"
    assert {e.key.stage_id for e in events} == {"loop:tests/stage:run"}
    assert {e.key.attempt for e in events} == {2}
    assert {e.key.run_id for e in events} == {"run-1"}
    seqs = [e.key.seq for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


@pytest.mark.asyncio
async def test_streaming_does_not_change_captured_output():
    """StageResult still carries the command's complete output."""
    ctx, _ = _streaming_ctx()
    stage = RunCommand(cmd="printf 'one\\ntwo\\n'; printf 'no-newline-tail'")
    result = await stage.run(ctx, node_id="stage:x")

    assert result.output["stdout"] == "one\ntwo\nno-newline-tail"
    assert result.output["returncode"] == 0


@pytest.mark.asyncio
async def test_trailing_line_without_newline_is_still_streamed():
    """The final flush emits a partial last line rather than swallowing it."""
    ctx, seen = _streaming_ctx()
    stage = RunCommand(cmd="printf 'partial'")
    await stage.run(ctx, node_id="stage:x")

    assert [e.text for e in _command_output(seen)] == ["partial"]


@pytest.mark.asyncio
async def test_streamed_output_is_redacted_at_the_seam():
    """A registered secret never reaches a consumer through streamed output."""
    register_secrets(["hunter2-swordfish"])
    ctx, seen = _streaming_ctx()
    stage = RunCommand(cmd="echo token=hunter2-swordfish")
    result = await stage.run(ctx, node_id="stage:x")

    streamed = "".join(e.text for e in _command_output(seen))
    assert "hunter2-swordfish" not in streamed
    # The captured result is the runner's own copy and is masked where it is
    # displayed or persisted, not here.
    assert "hunter2-swordfish" in result.output["stdout"]


@pytest.mark.asyncio
async def test_long_line_beyond_the_readline_limit_survives():
    """Chunked reads avoid StreamReader's 64KiB line-length limit."""
    ctx, _ = _streaming_ctx()
    size = 200_000
    stage = RunCommand(cmd=f"printf 'x%.0s' $(seq 1 {size})")
    result = await stage.run(ctx, node_id="stage:x")

    assert result.success
    assert len(result.output["stdout"]) == size


@pytest.mark.asyncio
async def test_timeout_returns_the_output_captured_before_the_kill():
    """Partial output is the only clue to where a wedged command stopped."""
    ctx, seen = _streaming_ctx()
    stage = RunCommand(cmd="echo before-hang; sleep 30", timeout=0.6)
    result = await stage.run(ctx, node_id="stage:x")

    assert not result.success
    assert "timed out" in result.error
    assert "before-hang" in result.output["stdout"]
    assert [e.text for e in _command_output(seen)] == ["before-hang"]


@pytest.mark.asyncio
async def test_no_streaming_without_a_node_id():
    """Driven outside the runner there is no node to attribute output to."""
    ctx, seen = _streaming_ctx()
    stage = RunCommand(cmd="echo unattributed")
    result = await stage.run(ctx)

    assert result.output["stdout"] == "unattributed\n"
    assert _command_output(seen) == []

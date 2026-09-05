from __future__ import annotations

import asyncio
import codecs
import contextlib
import itertools
import os
import signal
from typing import Any, Callable

from norn.events import CommandOutput, EventKey
from norn.models import PipelineContext, StageResult
from norn.secrets import resolve_env
from norn.stages.base import BaseStage

# Backstop timeout (seconds) for any single command. A command that never
# returns — e.g. one that backgrounds a server which inherits our stdout/stderr
# pipe — would otherwise wedge the whole pipeline forever: proc.communicate()
# blocks until EOF on the pipe, which a lingering child never delivers.
# Generous on purpose so real builds and test suites finish well inside it.
# Pass ``timeout=None`` to wait indefinitely, or a smaller value to tighten it.
DEFAULT_TIMEOUT_SECONDS = 3600.0

# How often streamed output is flushed to the event seam. Output is batched
# rather than emitted per line: a verbose build emits thousands of lines a
# second, and one event per line would swamp the TUI's per-event refresh.
# One flush every 150ms keeps the transcript feeling live while capping the
# event rate at ~7/s no matter how loud the command is.
FLUSH_INTERVAL_SECONDS = 0.15

# Read size for the process pipes. Chunked reads (rather than ``readline()``)
# avoid StreamReader's 64KiB line-length limit, which raises ValueError on a
# command that emits one very long line — e.g. a minified bundle or a base64
# payload.
READ_CHUNK_BYTES = 65536


class _OutputStream:
    """Captures one process pipe and hands out whole lines for live display.

    Two jobs, deliberately separate:

    * **Capture** — every decoded chunk is kept so ``StageResult.output``
      carries the command's complete output, exactly as before streaming.
    * **Display** — ``take_lines()`` returns only the text up to the last
      newline, holding a partial trailing line back until it completes, so
      the transcript never shows a half-written line as its own entry.

    Decoding is incremental so a multi-byte character split across two reads
    is reassembled instead of becoming two replacement characters.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._captured: list[str] = []
        self._pending = ""

    def feed(self, data: bytes) -> None:
        """Decode and store *data*, queueing it for display."""
        text = self._decoder.decode(data)
        if text:
            self._captured.append(text)
            self._pending += text

    def take_lines(self, *, final: bool = False) -> str:
        """Return queued whole lines, or everything queued when *final*."""
        if final:
            tail = self._decoder.decode(b"", final=True)
            if tail:
                self._captured.append(tail)
                self._pending += tail
            pending, self._pending = self._pending, ""
            return pending
        head, sep, rest = self._pending.rpartition("\n")
        if not sep:
            return ""
        self._pending = rest
        return head

    @property
    def text(self) -> str:
        """The complete decoded output seen so far."""
        return "".join(self._captured)


class RunCommand(BaseStage):
    """Run a shell command and return stdout, stderr, and exit code.

    Pure Python — no agent session, no SDK dependency.
    Executed via ``asyncio.create_subprocess_shell``.

    Args:
        cmd: Shell command string to execute.
        env: Optional extra environment variables. Values may contain
            ``{secret.NAME}`` and ``{param.NAME}`` placeholders which are
            resolved at runtime. Merged with pipeline-level env (stage
            values take precedence).
        timeout: Max seconds to wait before the command's process group is
            killed and the stage fails. Defaults to
            ``DEFAULT_TIMEOUT_SECONDS`` (1h) as a hang backstop; pass ``None``
            to wait indefinitely.

    Output:
        ``StageResult.output`` is a dict::

            {"stdout": str, "stderr": str, "returncode": int}

        The stage succeeds when ``returncode == 0``.

    Example::

        Stage("test", RunCommand(cmd="python -m pytest tests/ -v"))
        Stage("deploy", RunCommand(
            cmd="./deploy.sh",
            env={"TOKEN": "{secret.DEPLOY_TOKEN}"},
        ))
    """

    # Tells the runner to pass node_id/attempt so streamed output can be
    # keyed to this stage's graph node (see BaseStage.emits_events).
    emits_events = True

    def __init__(
        self,
        *,
        cmd: str,
        env: dict[str, str] | None = None,
        timeout: float | None = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.cmd = cmd
        self.env = env
        self.timeout = timeout

    async def run(
        self,
        ctx: PipelineContext,
        *,
        node_id: str | None = None,
        attempt: int = 1,
        **kwargs: Any,
    ) -> StageResult:
        # Build subprocess env: process env + pipeline env + stage env (with secret resolution)
        subprocess_env: dict[str, str] | None = None
        if self.env or ctx.env:
            subprocess_env = {**os.environ, **ctx.env}
            if self.env:
                subprocess_env.update(resolve_env(self.env, ctx))

        # start_new_session=True runs the command as its own process-group
        # leader. That lets us tear down the *entire* tree (a backgrounded
        # server, its reloader, any grandchildren) with one killpg when the
        # command overruns its timeout or the run is aborted — a lone
        # proc.kill() would leave those orphaned and still holding our capture
        # pipe open, the exact deadlock that wedges proc.communicate().
        proc = await asyncio.create_subprocess_shell(
            self.cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=subprocess_env,
            start_new_session=True,
        )

        try:
        # Read both pipes concurrently rather than via proc.communicate(), so
        # output can be forwarded to the event seam while the command is still
        # running. Reading both at once is what keeps communicate()'s deadlock
        # guarantee: a command that fills one pipe while we block on the other
        # would stall forever.
        #
        # Decoding is defensive: a command may emit non-UTF-8 bytes (e.g. an
        # e2e script dumping a decrypted binary payload). Strict decode would
        # raise UnicodeDecodeError and crash the pipeline before the command's
        # pass/fail could be reported. errors="replace" preserves all valid
        # text and substitutes U+FFFD for stray bytes.
        out = _OutputStream("stdout")
        err = _OutputStream("stderr")
        emit = self._make_emitter(ctx, node_id=node_id, attempt=attempt)
        flusher = asyncio.create_task(self._flush_loop(emit, out, err))
        timed_out = False

            if self.timeout is not None:
                await asyncio.wait_for(self._pump(proc, out, err), timeout=self.timeout)
            else:
                await self._pump(proc, out, err)
        except asyncio.TimeoutError:
            # Command overran its backstop. Kill the whole group and report a
            # clean failure so the loop/fix machinery reacts instead of hanging.
            self._terminate_group(proc)
            timed_out = True
            with contextlib.suppress(Exception):
                await proc.wait()
            cmd_preview = self.cmd if len(self.cmd) <= 500 else self.cmd[:500] + "…"
        except asyncio.CancelledError:
            # External cancellation (pipeline abort, or a Stage-level timeout in
            # the runner). Reap the tree before propagating so nothing is left
            # holding our pipes open.
            self._terminate_group(proc)
            raise
        finally:
            # Stop the periodic flush and push whatever is still queued —
            # including a trailing line with no newline — so the transcript
            # ends with everything the command actually printed. Runs on the
            # timeout and cancellation paths too.
            flusher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await flusher
            self._emit_pending(emit, out, err, final=True)

        if timed_out:
            return StageResult(
                name="",
                success=False,
                # Partial output, not empty: what the command managed to print
                # before it wedged is usually the only clue as to where.
                output={"stdout": out.text, "stderr": err.text, "returncode": None},
                error=(
                    f"command timed out after {self.timeout:g}s and was killed\n"
                    f"$ {cmd_preview}"
                ),
            )

        output = {
            "stdout": out.text,
            "stderr": err.text,
            "returncode": proc.returncode,
        }
        success = proc.returncode == 0
        error = self._format_error(output) if not success else None
        return StageResult(name="", success=success, output=output, error=error)

    @staticmethod
    # ------------------------------------------------------------------
    # Live output streaming
    # ------------------------------------------------------------------

    @staticmethod
    def _make_emitter(
        ctx: PipelineContext,
        *,
        node_id: str | None,
        attempt: int,
    ) -> Callable[[str, str], None]:
        """Return a function that emits one ``CommandOutput`` per call.

        *node_id* is the fully-qualified graph node id the runner computed for
        this stage, so streamed output lands in the same transcript spool as
        the stage's other events.

        It is ``None`` when ``RunCommand`` is driven directly rather than
        through the runner — then output is not streamed at all. There is no
        node to attribute it to, so every consumer would drop it while the
        sink kept spooling it under a key nothing reads.
        """
        if node_id is None:
            return lambda stream, text: None

        sink = ctx.event_sink
        run_id = ctx.run_id
        unit_id = ctx.unit_id
        counter = itertools.count(1)

        def emit(stream: str, text: str) -> None:
            sink.emit(CommandOutput(
                key=EventKey(
                    run_id=run_id,
                    unit_id=unit_id,
                    stage_id=node_id,
                    attempt=attempt,
                    seq=next(counter),
                ),
                text=text,
                stream=stream,
            ))

        return emit

    @staticmethod
    async def _read_stream(stream: asyncio.StreamReader, sink: _OutputStream) -> None:
        """Drain *stream* into *sink* until EOF."""
        while True:
            data = await stream.read(READ_CHUNK_BYTES)
            if not data:
                return
            sink.feed(data)

    async def _pump(
        self,
        proc: asyncio.subprocess.Process,
        out: _OutputStream,
        err: _OutputStream,
    ) -> None:
        """Drain both pipes to EOF, then reap the process.

        ``proc.stdout``/``proc.stderr`` are always set because both were
        opened with ``PIPE`` at spawn time.
        """
        assert proc.stdout is not None and proc.stderr is not None
        await asyncio.gather(
            self._read_stream(proc.stdout, out),
            self._read_stream(proc.stderr, err),
        )
        await proc.wait()

    @staticmethod
    def _emit_pending(
        emit: Callable[[str, str], None],
        out: _OutputStream,
        err: _OutputStream,
        *,
        final: bool = False,
    ) -> None:
        """Emit whatever display text each stream has queued."""
        for stream in (out, err):
            text = stream.take_lines(final=final)
            if text:
                # Each event renders as its own transcript entry, which already
                # implies a line break; a trailing newline would render as an
                # extra blank line. The non-final path is already stripped (the
                # newline is the separator rpartition drops), so this only bites
                # on the final flush.
                emit(stream.name, text.removesuffix("\n"))

    async def _flush_loop(
        self,
        emit: Callable[[str, str], None],
        out: _OutputStream,
        err: _OutputStream,
    ) -> None:
        """Emit queued output every ``FLUSH_INTERVAL_SECONDS`` until cancelled."""
        while True:
            await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
            self._emit_pending(emit, out, err)

    def _terminate_group(proc: asyncio.subprocess.Process) -> None:
        """SIGKILL the command's whole process group (best effort).

        Relies on ``start_new_session=True`` at spawn time, which makes the
        child a process-group leader, so killing its group reaches every
        descendant. No-op once the process has already exited.
        """
        if proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)

    def _format_error(self, output: dict) -> str:
        """Build a useful error message from a failed command's output.

        Always includes exit code and the command. Shows both stdout and
        stderr when non-empty (some tools log diagnostics only to stdout —
        e.g. ``pg_isready``). When ``set -x`` traces are detected in stderr,
        surfaces the last traced command as a concise ``last command`` hint
        so chained ``&&`` failures are easy to localize.
        """
        cmd_preview = self.cmd if len(self.cmd) <= 500 else self.cmd[:500] + "…"
        parts = [
            f"command exited with status {output['returncode']}",
            f"$ {cmd_preview}",
        ]
        stderr = output["stderr"].rstrip()
        stdout = output["stdout"].rstrip()
        last_traced = self._last_xtrace_line(stderr)
        if last_traced:
            parts.append(f"last command: {last_traced}")
        if stderr:
            parts.append(f"stderr:\n{stderr}")
        if stdout:
            parts.append(f"stdout:\n{stdout}")
        return "\n".join(parts)

    @staticmethod
    def _last_xtrace_line(stderr: str) -> str | None:
        """Return the last ``set -x`` trace line (``+ ...``), if any."""
        for line in reversed(stderr.splitlines()):
            stripped = line.lstrip()
            if stripped.startswith("+ ") or stripped.startswith("++ "):
                return stripped
        return None

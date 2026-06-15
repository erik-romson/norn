from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from typing import Any

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

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
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
            if self.timeout is not None:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self.timeout
                )
            else:
                stdout, stderr = await proc.communicate()
        except asyncio.TimeoutError:
            # Command overran its backstop. Kill the whole group and report a
            # clean failure so the loop/fix machinery reacts instead of hanging.
            self._terminate_group(proc)
            with contextlib.suppress(Exception):
                await proc.wait()
            cmd_preview = self.cmd if len(self.cmd) <= 500 else self.cmd[:500] + "…"
            return StageResult(
                name="",
                success=False,
                output={"stdout": "", "stderr": "", "returncode": None},
                error=(
                    f"command timed out after {self.timeout:g}s and was killed\n"
                    f"$ {cmd_preview}"
                ),
            )
        except asyncio.CancelledError:
            # External cancellation (pipeline abort, or a Stage-level timeout in
            # the runner). Reap the tree before propagating so nothing is left
            # holding our pipes open.
            self._terminate_group(proc)
            raise

        # Decode defensively: a test_cmd may emit non-UTF-8 bytes on stdout/stderr
        # (e.g. an e2e script that dumps a decrypted binary payload). Strict decode
        # would raise UnicodeDecodeError here and crash the whole pipeline before the
        # command's pass/fail can be reported. errors="replace" preserves all valid
        # text and substitutes U+FFFD for stray bytes.
        output = {
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
            "returncode": proc.returncode,
        }
        success = proc.returncode == 0
        error = self._format_error(output) if not success else None
        return StageResult(name="", success=success, output=output, error=error)

    @staticmethod
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

from __future__ import annotations

import asyncio
import os
from typing import Any

from norn.models import PipelineContext, StageResult
from norn.secrets import resolve_env
from norn.stages.base import BaseStage


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

    def __init__(self, *, cmd: str, env: dict[str, str] | None = None) -> None:
        self.cmd = cmd
        self.env = env

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        # Build subprocess env: process env + pipeline env + stage env (with secret resolution)
        subprocess_env: dict[str, str] | None = None
        if self.env or ctx.env:
            subprocess_env = {**os.environ, **ctx.env}
            if self.env:
                subprocess_env.update(resolve_env(self.env, ctx))

        proc = await asyncio.create_subprocess_shell(
            self.cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=subprocess_env,
        )
        stdout, stderr = await proc.communicate()
        output = {
            "stdout": stdout.decode(),
            "stderr": stderr.decode(),
            "returncode": proc.returncode,
        }
        success = proc.returncode == 0
        error = output["stderr"] if not success else None
        return StageResult(name="", success=success, output=output, error=error)

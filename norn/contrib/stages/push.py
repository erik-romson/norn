from __future__ import annotations

import asyncio
import shlex
from typing import TYPE_CHECKING

from norn.models import StageResult
from norn.stages.base import BaseStage

if TYPE_CHECKING:
    from norn.models import PipelineContext


class Push(BaseStage):
    needs_agent = False

    def __init__(
        self,
        *,
        commit_format: str = "[{issue_key}] {summary}",
    ) -> None:
        self.commit_format = commit_format

    async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
        r = ctx.results.get("read_issue")
        if r is None:
            return StageResult(name="", success=False, error="No read_issue result")
        issue_ctx = r.output

        repo_path = issue_ctx.local_path
        branch = issue_ctx.branch
        if not repo_path or not branch:
            return StageResult(name="", success=False, error="No local_path or branch on IssueContext")

        message = self.commit_format.format(
            issue_key=issue_ctx.key,
            summary=issue_ctx.summary,
        )

        cmds = [
            f"git -C {shlex.quote(str(repo_path))} add -A",
            (
                f"git -C {shlex.quote(str(repo_path))} diff --cached --quiet || "
                f"git -C {shlex.quote(str(repo_path))} commit -m {shlex.quote(message)}"
            ),
            f"git -C {shlex.quote(str(repo_path))} push -u origin {shlex.quote(branch)}",
        ]

        for cmd in cmds:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                return StageResult(
                    name="", success=False,
                    error=f"Push failed: {stderr.decode()}",
                    output={"cmd": cmd, "stderr": stderr.decode()},
                )

        return StageResult(name="", success=True, output={"branch": branch})

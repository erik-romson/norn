from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

from norn.models import StageResult
from norn.stages.base import BaseStage

if TYPE_CHECKING:
    from norn.models import PipelineContext

log = logging.getLogger(__name__)


class CI(BaseStage):
    needs_agent = False

    def __init__(
        self,
        *,
        poll_interval: int = 30,
        timeout_minutes: int = 30,
    ) -> None:
        self.poll_interval = poll_interval
        self.timeout_minutes = timeout_minutes

    async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
        r = ctx.results.get("read_issue")
        if r is None:
            return StageResult(name="", success=False, error="No read_issue result")
        issue_ctx = r.output

        repo = issue_ctx.repo
        branch = issue_ctx.branch
        if not repo or not branch:
            return StageResult(name="", success=False, error="No repo or branch on IssueContext")

        deadline = time.time() + self.timeout_minutes * 60

        while time.time() < deadline:
            status = await _check_ci_status(repo, branch)
            conclusion = status.get("conclusion")

            if conclusion == "success":
                return StageResult(name="", success=True, output=status)
            if conclusion in ("failure", "cancelled"):
                return StageResult(
                    name="", success=False,
                    error=f"CI {conclusion}: {status.get('name', '')}",
                    output=status,
                )
            # Still running or pending
            log.debug("CI status: %s — waiting %ds", status.get("status"), self.poll_interval)
            await asyncio.sleep(self.poll_interval)

        return StageResult(
            name="", success=False,
            error=f"CI timed out after {self.timeout_minutes} minutes",
        )


async def _check_ci_status(repo: str, branch: str) -> dict:
    """Check GitHub Actions workflow status via gh CLI."""
    proc = await asyncio.create_subprocess_shell(
        f"gh run list -R {repo} -b {branch} --json status,conclusion,name,databaseId -L 5",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"gh run list failed: {stderr.decode()}")

    runs = json.loads(stdout.decode())
    if not runs:
        return {"status": "pending", "conclusion": None}

    run = runs[0]
    if run["status"] == "completed":
        return {
            "status": "completed",
            "conclusion": run["conclusion"],
            "run_id": run["databaseId"],
            "name": run["name"],
        }
    return {"status": run["status"], "conclusion": None}

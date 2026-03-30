from __future__ import annotations

import asyncio
import shlex
from typing import TYPE_CHECKING

from norn.models import StageResult
from norn.stages.base import BaseStage
from norn.contrib.models.pipeline_result import PipelineResult
from norn.contrib.notifications.base import NotifyChannel

if TYPE_CHECKING:
    from norn.models import PipelineContext


class Ship(BaseStage):
    needs_agent = False

    def __init__(
        self,
        *,
        pr_title_format: str = "[{issue_key}] {summary}",
        pr_body_includes: list[str] | None = None,
        draft: bool = False,
        notify: list[NotifyChannel] | None = None,
    ) -> None:
        self.pr_title_format = pr_title_format
        self.pr_body_includes = pr_body_includes or ["jira_link", "analysis"]
        self.draft = draft
        self.notify = notify or []

    async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
        r = ctx.results.get("read_issue")
        if r is None:
            return StageResult(name="", success=False, error="No read_issue result")
        issue_ctx = r.output

        plan_r = ctx.results.get("plan")
        fix_plan = plan_r.output if plan_r else None

        title = self.pr_title_format.format(
            issue_key=issue_ctx.key,
            summary=issue_ctx.summary,
        )
        body = self._build_pr_body(issue_ctx, fix_plan)

        pr_url = await _create_pr(
            issue_ctx.repo, issue_ctx.branch, title, body, draft=self.draft,
        )

        for channel in self.notify:
            await channel.send(issue_ctx, pr_url)

        result = PipelineResult(
            jira_key=issue_ctx.key,
            pr_url=pr_url,
            status="success",
            summary=f"PR created: {pr_url}",
        )
        return StageResult(name="", success=True, output=result)

    def _build_pr_body(self, issue, plan) -> str:
        sections: list[str] = []
        if "jira_link" in self.pr_body_includes:
            sections.append(f"**Jira:** {issue.key}")
        if "analysis" in self.pr_body_includes and plan:
            sections.append(f"## Analysis\n{plan.analysis}")
        return "\n\n".join(sections)


async def _create_pr(
    repo: str, branch: str, title: str, body: str, *, draft: bool = False,
) -> str:
    """Create PR via gh CLI. Returns PR URL."""
    cmd = (
        f"gh pr create -R {shlex.quote(repo)} --head {shlex.quote(branch)} "
        f"--title {shlex.quote(title)} "
        f"--body {shlex.quote(body)}"
    )
    if draft:
        cmd += " --draft"

    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"PR creation failed: {stderr.decode()}")
    return stdout.decode().strip()

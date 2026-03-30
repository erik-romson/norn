from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from norn.contrib.matchers.base import MatchResult, RepoMatcher

if TYPE_CHECKING:
    from norn.models import PipelineContext
    from norn.contrib.models.issue_context import IssueContext

log = logging.getLogger(__name__)


async def list_org_repos(org: str | None) -> list[str]:
    if not org:
        return []
    cmd = f"gh repo list {org} -L 200 --json nameWithOwner"
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0 or not stdout:
            return []
        repos = json.loads(stdout)
        return [r["nameWithOwner"] for r in repos]
    except Exception as e:
        log.debug("Failed to list repos for org %r: %s", org, e)
        return []


class KeywordMatcher(RepoMatcher):
    async def match(self, issue: IssueContext, ctx: PipelineContext) -> MatchResult | None:
        org = ctx.params.get("github_org")
        repos = await list_org_repos(org)
        text = f"{issue.summary} {issue.description}".lower()
        for repo_name in repos:
            short = repo_name.split("/")[-1].lower()
            if short and short in text:
                return MatchResult(repo_name, 0.6, "keyword")
        return None

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from norn.contrib.extractors.class_names import extract_class_names
from norn.contrib.matchers.base import MatchResult, RepoMatcher

if TYPE_CHECKING:
    from norn.models import PipelineContext
    from norn.contrib.models.issue_context import IssueContext

log = logging.getLogger(__name__)


async def github_code_search(cls_name: str, github_org: str | None) -> list[str]:
    """Search GitHub for a class name using gh CLI. Returns list of repo full names."""
    cmd = f"gh search code {cls_name!r}"
    if github_org:
        cmd += f" --owner {github_org}"
    cmd += " --json repository --limit 5"
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0 or not stdout:
            return []
        results = json.loads(stdout)
        return [r["repository"]["nameWithOwner"] for r in results if "repository" in r]
    except Exception as e:
        log.debug("GitHub code search failed for %r: %s", cls_name, e)
        return []


class StacktraceMatcher(RepoMatcher):
    def __init__(self, github_org: str | None = None) -> None:
        self._github_org = github_org

    async def match(self, issue: IssueContext, ctx: PipelineContext) -> MatchResult | None:
        if not issue.stacktraces:
            return None
        org = self._github_org or ctx.params.get("github_org")
        classes = extract_class_names(issue.stacktraces)
        if not classes:
            return None
        repo_hits: dict[str, int] = {}
        for cls in classes[:10]:
            repos = await github_code_search(cls, org)
            for repo in repos:
                repo_hits[repo] = repo_hits.get(repo, 0) + 1
        if not repo_hits:
            return None
        best = max(repo_hits, key=lambda k: repo_hits[k])
        confidence = min(repo_hits[best] / max(len(classes), 1), 1.0)
        return MatchResult(best, confidence, "stacktrace")

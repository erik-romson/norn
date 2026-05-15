from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from norn.contrib.matchers.base import MatchResult, RepoMatcher
from norn.contrib.matchers.keyword_matcher import list_org_repos

if TYPE_CHECKING:
    from norn.models import PipelineContext
    from norn.contrib.models.issue_context import IssueContext

log = logging.getLogger(__name__)


class LLMMatcher(RepoMatcher):
    def __init__(self, model: str = "sonnet") -> None:
        self._model = model

    async def match(self, issue: IssueContext, ctx: PipelineContext) -> MatchResult | None:
        org = ctx.params.get("github_org")
        repos = await list_org_repos(org)
        if not repos:
            return None

        from norn.agents.complete import complete_text

        prompt = f"""Given this Jira issue, which GitHub repository is most likely affected?

Issue key: {issue.key}
Summary: {issue.summary}
Description: {issue.description[:500]}
Stacktraces: {issue.stacktraces[:2]}

Available repositories:
{json.dumps(repos, indent=2)}

Return JSON only: {{"repo": "<owner/name>", "confidence": 0.0, "reasoning": "..."}}"""

        raw = await complete_text(
            prompt,
            provider=ctx.agent_provider,
            model=self._model,
        )
        if not raw:
            return None

        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if not m:
            return None
        try:
            parsed = json.loads(m.group(0))
            return MatchResult(parsed["repo"], float(parsed.get("confidence", 0.5)), "llm")
        except (json.JSONDecodeError, KeyError) as e:
            log.warning("LLMMatcher failed to parse response: %s", e)
            return None

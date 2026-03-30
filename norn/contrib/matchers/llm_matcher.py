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
    def __init__(self, model: str = "claude-sonnet-4-6") -> None:
        self._model = model

    async def match(self, issue: IssueContext, ctx: PipelineContext) -> MatchResult | None:
        org = ctx.params.get("github_org")
        repos = await list_org_repos(org)
        if not repos:
            return None

        try:
            from claude_agent_sdk import query, ClaudeAgentOptions
        except ImportError:
            log.warning("claude-agent-sdk not available for LLMMatcher")
            return None

        prompt = f"""Given this Jira issue, which GitHub repository is most likely affected?

Issue key: {issue.key}
Summary: {issue.summary}
Description: {issue.description[:500]}
Stacktraces: {issue.stacktraces[:2]}

Available repositories:
{json.dumps(repos, indent=2)}

Return JSON only: {{"repo": "<owner/name>", "confidence": 0.0, "reasoning": "..."}}"""

        chunks: list[str] = []
        try:
            from claude_agent_sdk import AssistantMessage
            async for msg in query(prompt=prompt, options=ClaudeAgentOptions(model=self._model)):
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if hasattr(block, "text"):
                            chunks.append(block.text)
        except Exception as e:
            log.warning("LLMMatcher query failed: %s", e)
            return None

        raw = "".join(chunks)
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if not m:
            return None
        try:
            parsed = json.loads(m.group(0))
            return MatchResult(parsed["repo"], float(parsed.get("confidence", 0.5)), "llm")
        except (json.JSONDecodeError, KeyError) as e:
            log.warning("LLMMatcher failed to parse response: %s", e)
            return None

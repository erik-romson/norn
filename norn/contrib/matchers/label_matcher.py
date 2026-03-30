from __future__ import annotations

from typing import TYPE_CHECKING

from norn.contrib.matchers.base import MatchResult, RepoMatcher

if TYPE_CHECKING:
    from norn.models import PipelineContext
    from norn.contrib.models.issue_context import IssueContext


class LabelMatcher(RepoMatcher):
    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = mapping

    async def match(self, issue: IssueContext, ctx: PipelineContext) -> MatchResult | None:
        for label in issue.labels:
            if label in self._mapping:
                return MatchResult(self._mapping[label], 0.9, "label")
        return None

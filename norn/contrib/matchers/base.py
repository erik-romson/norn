from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from norn.models import PipelineContext
    from norn.contrib.models.issue_context import IssueContext


@dataclass
class MatchResult:
    repo: str
    confidence: float
    method: str


class RepoMatcher(ABC):
    @abstractmethod
    async def match(self, issue: IssueContext, ctx: PipelineContext) -> MatchResult | None: ...


class MatcherChain:
    def __init__(self, threshold: float = 0.7) -> None:
        self.threshold = threshold
        self._matchers: list[RepoMatcher] = []

    def add(self, matcher: RepoMatcher) -> MatcherChain:
        self._matchers.append(matcher)
        return self

    async def match(self, issue: IssueContext, ctx: PipelineContext) -> MatchResult | None:
        for matcher in self._matchers:
            result = await matcher.match(issue, ctx)
            if result and result.confidence >= self.threshold:
                return result
        return None

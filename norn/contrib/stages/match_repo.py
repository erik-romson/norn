from __future__ import annotations

from typing import TYPE_CHECKING

from norn.models import StageResult
from norn.stages.base import BaseStage
from norn.contrib.matchers.base import MatcherChain

if TYPE_CHECKING:
    from norn.models import PipelineContext


class MatchRepo(BaseStage):
    needs_agent = False

    def __init__(self, matcher_chain: MatcherChain) -> None:
        self.matcher_chain = matcher_chain

    async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
        issue_ctx = ctx.get("read_issue")
        if issue_ctx is None:
            return StageResult(name="", success=False, error="No read_issue result in context")
        match = await self.matcher_chain.match(issue_ctx, ctx)
        if match is None:
            return StageResult(
                name="", success=False,
                error="No repo match above confidence threshold",
            )
        issue_ctx.repo = match.repo
        issue_ctx.match_confidence = match.confidence
        issue_ctx.match_method = match.method
        return StageResult(name="", success=True, output=issue_ctx)

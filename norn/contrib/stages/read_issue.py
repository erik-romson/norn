from __future__ import annotations

from typing import TYPE_CHECKING

from norn.models import StageResult
from norn.stages.base import BaseStage
from norn.contrib.sources.base import IssueSource

if TYPE_CHECKING:
    from norn.models import PipelineContext


class ReadIssue(BaseStage):
    needs_agent = False

    def __init__(self, source: IssueSource) -> None:
        self.source = source

    async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
        issue_key = ctx.params.get("issue_key") or ctx.params.get("args", "").strip()
        if not issue_key:
            return StageResult(name="", success=False, error="No issue_key in params")
        issue_context = await self.source.fetch(issue_key, ctx)
        return StageResult(name="", success=True, output=issue_context)

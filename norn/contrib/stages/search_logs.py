from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from norn.models import StageResult
from norn.stages.base import BaseStage
from norn.contrib.search.elastic import ElasticClient

if TYPE_CHECKING:
    from norn.models import PipelineContext

log = logging.getLogger(__name__)


class SearchLogs(BaseStage):
    needs_agent = False

    def __init__(
        self,
        *,
        url: str,
        index: str,
        query_template: str | None = None,
        time_range: str = "7d",
        max_results: int = 100,
        auth: str = "api_key",
        use_opensearch: bool = False,
    ) -> None:
        self.url = url
        self.index = index
        self.query_template = query_template
        self.time_range = time_range
        self.max_results = max_results
        self.auth = auth
        self.use_opensearch = use_opensearch

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        r = ctx.results.get("read_issue")
        if r is None:
            return StageResult(name="", success=False, error="No read_issue result")
        issue_ctx = r.output

        query = self._build_query(issue_ctx)
        client = ElasticClient(url=self.url, auth=self.auth, use_opensearch=self.use_opensearch)
        results = await client.search(
            index=self.index,
            query=query,
            size=self.max_results,
            secrets=ctx.secrets,
        )

        return StageResult(
            name="", success=True,
            output={"hits": len(results), "logs": results},
        )

    def _build_query(self, issue) -> dict:
        """Build ES query from issue context.

        If ``query_template`` was provided at construction time, it is used
        as a Lucene query string with ``{key}`` substituted by the issue key.
        Otherwise a ``bool`` query is built from the issue key and the first
        few stacktrace lines.
        """
        if self.query_template is not None:
            return {"query_string": {"query": self.query_template.format(key=issue.key)}}

        should: list[dict] = [{"match": {"message": issue.key}}]
        for trace in issue.stacktraces[:3]:
            lines = trace.strip().split("\n")
            if lines:
                should.append({"match_phrase": {"message": lines[0][:200]}})
        return {
            "bool": {
                "should": should,
                "minimum_should_match": 1,
                "filter": [
                    {"range": {"@timestamp": {"gte": f"now-{self.time_range}"}}}
                ],
            }
        }

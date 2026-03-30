from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


class ElasticClient:
    """Async wrapper around Elasticsearch or OpenSearch client.

    Supports auth modes:

    - ``api_key``: API key from ``secrets["ES_API_KEY"]``. For Elasticsearch,
      the value may be ``"id:secret"`` (split on first ``:``). For OpenSearch
      the full value is used as a password with an empty username.
    - ``basic``: Username/password from ``secrets["ES_USER"]`` and
      ``secrets["ES_PASSWORD"]``.
    - ``aws_sigv4``: AWS IAM authentication (OpenSearch only). Region read
      from ``secrets["AWS_REGION"]``, falls back to ``"us-east-1"``.
      Requires the ``aws`` optional dependency (``aioboto3`` / ``boto3``).
    """

    def __init__(
        self,
        *,
        url: str,
        auth: str = "api_key",
        use_opensearch: bool = False,
    ) -> None:
        self.url = url
        self.auth = auth
        self.use_opensearch = use_opensearch

    async def search(
        self,
        *,
        index: str,
        query: dict,
        size: int = 100,
        secrets: dict[str, str] | None = None,
    ) -> list[dict]:
        """Execute a search query and return ``_source`` documents.

        Args:
            index: Index name or pattern (e.g. ``"app-logs-*"``).
            query: Elasticsearch/OpenSearch query DSL dict.
            size: Maximum number of hits to return.
            secrets: Secret values used for authentication.

        Returns:
            List of ``_source`` dicts from matching hits, newest first.
        """
        s = secrets or {}
        if self.use_opensearch:
            return await self._opensearch(index, query, size, s)
        return await self._elasticsearch(index, query, size, s)

    async def _elasticsearch(
        self, index: str, query: dict, size: int, secrets: dict[str, str]
    ) -> list[dict]:
        from elasticsearch import AsyncElasticsearch

        kwargs: dict[str, Any] = {"hosts": [self.url]}
        if self.auth == "api_key":
            api_key = secrets.get("ES_API_KEY", "")
            if ":" in api_key:
                kid, ksecret = api_key.split(":", 1)
                kwargs["api_key"] = (kid, ksecret)
            elif api_key:
                kwargs["api_key"] = api_key
        elif self.auth == "basic":
            kwargs["basic_auth"] = (
                secrets.get("ES_USER", ""),
                secrets.get("ES_PASSWORD", ""),
            )

        es = AsyncElasticsearch(**kwargs)
        try:
            resp = await es.search(
                index=index,
                query=query,
                size=size,
                sort=[{"@timestamp": "desc"}],
            )
            return [h["_source"] for h in resp["hits"]["hits"]]
        finally:
            await es.close()

    async def _opensearch(
        self, index: str, query: dict, size: int, secrets: dict[str, str]
    ) -> list[dict]:
        from opensearchpy import AsyncOpenSearch

        kwargs: dict[str, Any] = {"hosts": [self.url]}
        if self.auth == "basic":
            kwargs["http_auth"] = (
                secrets.get("ES_USER", ""),
                secrets.get("ES_PASSWORD", ""),
            )
        elif self.auth == "api_key":
            api_key = secrets.get("ES_API_KEY", "")
            kwargs["http_auth"] = ("", api_key)
        elif self.auth == "aws_sigv4":
            import boto3
            from opensearchpy import RequestsAWSV4SignerAuth

            creds = boto3.Session().get_credentials()
            region = secrets.get("AWS_REGION", "us-east-1")
            kwargs["http_auth"] = RequestsAWSV4SignerAuth(creds, region, "es")
            kwargs["use_ssl"] = True

        client = AsyncOpenSearch(**kwargs)
        try:
            resp = await client.search(
                index=index,
                body={"query": query, "size": size, "sort": [{"@timestamp": "desc"}]},
            )
            return [h["_source"] for h in resp["hits"]["hits"]]
        finally:
            await client.close()

from __future__ import annotations

import json
import os

from claude_agent_sdk import tool

from norn.contrib.search.elastic import ElasticClient

_SCHEMA_SEARCH_LOGS = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "index": {"type": "string"},
        "limit": {"type": "integer"},
    },
    "required": ["query"],
}


@tool(
    "search_logs",
    "Search application logs in Elasticsearch or OpenSearch using Lucene query syntax.",
    _SCHEMA_SEARCH_LOGS,
)
async def search_logs(args: dict) -> dict:
    """Search application logs in Elasticsearch.

    Reads connection settings from environment variables:

    - ``ES_URL``: Elasticsearch/OpenSearch base URL (required).
    - ``ES_AUTH``: Auth mode — ``api_key`` (default), ``basic``, or ``aws_sigv4``.
    - ``ES_USE_OPENSEARCH``: Set to ``"true"`` or ``"1"`` to use the OpenSearch client.
    - ``ES_API_KEY``: API key (for ``api_key`` mode).
    - ``ES_USER`` / ``ES_PASSWORD``: Credentials (for ``basic`` mode).
    - ``AWS_REGION``: AWS region (for ``aws_sigv4`` mode, default ``us-east-1``).
    """
    query_str: str = args["query"]
    index: str = args.get("index", "app-logs-*")
    limit: int = int(args.get("limit", 20))

    url = os.environ.get("ES_URL", "")
    if not url:
        return {
            "content": [{"type": "text", "text": "Error: ES_URL must be set"}],
            "is_error": True,
        }

    auth = os.environ.get("ES_AUTH", "api_key")
    use_opensearch = os.environ.get("ES_USE_OPENSEARCH", "").lower() in ("1", "true")
    secrets = {k: v for k, v in os.environ.items() if k.startswith(("ES_", "AWS_"))}

    client = ElasticClient(url=url, auth=auth, use_opensearch=use_opensearch)
    query = {"query_string": {"query": query_str}}
    results = await client.search(index=index, query=query, size=limit, secrets=secrets)
    return {"content": [{"type": "text", "text": json.dumps(results, indent=2)}]}

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from norn.models import PipelineContext, StageResult, UsageTracker
from norn.contrib.models.issue_context import IssueContext


def _make_ctx(issue: IssueContext | None = None) -> PipelineContext:
    ctx = PipelineContext(results={}, usage_tracker=UsageTracker(), params={})
    if issue is not None:
        ctx.results["read_issue"] = StageResult(name="read_issue", success=True, output=issue)
    return ctx


def _make_issue(**overrides) -> IssueContext:
    defaults = dict(key="PROJ-42", summary="Fix bug", description="desc")
    defaults.update(overrides)
    return IssueContext(**defaults)


# ---------------------------------------------------------------------------
# ElasticClient — Elasticsearch path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_elastic_client_elasticsearch_api_key():
    from norn.contrib.search.elastic import ElasticClient

    mock_es_class = MagicMock()
    mock_es = AsyncMock()
    mock_es.search = AsyncMock(return_value={
        "hits": {"hits": [{"_source": {"message": "error"}}]}
    })
    mock_es.close = AsyncMock()
    mock_es_class.return_value = mock_es

    with patch("norn.contrib.search.elastic.AsyncElasticsearch", mock_es_class, create=True):
        with patch.dict("sys.modules", {"elasticsearch": MagicMock(AsyncElasticsearch=mock_es_class)}):
            client = ElasticClient(url="http://es:9200", auth="api_key")
            results = await client.search(
                index="logs-*",
                query={"match": {"message": "PROJ-42"}},
                secrets={"ES_API_KEY": "myid:mysecret"},
            )

    assert results == [{"message": "error"}]
    call_kwargs = mock_es_class.call_args.kwargs
    assert call_kwargs["api_key"] == ("myid", "mysecret")


@pytest.mark.asyncio
async def test_elastic_client_elasticsearch_basic_auth():
    from norn.contrib.search.elastic import ElasticClient

    mock_es_class = MagicMock()
    mock_es = AsyncMock()
    mock_es.search = AsyncMock(return_value={"hits": {"hits": []}})
    mock_es.close = AsyncMock()
    mock_es_class.return_value = mock_es

    with patch.dict("sys.modules", {"elasticsearch": MagicMock(AsyncElasticsearch=mock_es_class)}):
        client = ElasticClient(url="http://es:9200", auth="basic")
        await client.search(
            index="logs-*",
            query={"match_all": {}},
            secrets={"ES_USER": "admin", "ES_PASSWORD": "s3cr3t"},
        )

    call_kwargs = mock_es_class.call_args.kwargs
    assert call_kwargs["basic_auth"] == ("admin", "s3cr3t")


# ---------------------------------------------------------------------------
# ElasticClient — OpenSearch path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_elastic_client_opensearch_api_key():
    from norn.contrib.search.elastic import ElasticClient

    mock_os_class = MagicMock()
    mock_os = AsyncMock()
    mock_os.search = AsyncMock(return_value={
        "hits": {"hits": [{"_source": {"message": "warn"}}]}
    })
    mock_os.close = AsyncMock()
    mock_os_class.return_value = mock_os

    with patch.dict("sys.modules", {"opensearchpy": MagicMock(AsyncOpenSearch=mock_os_class)}):
        client = ElasticClient(url="http://os:9200", auth="api_key", use_opensearch=True)
        results = await client.search(
            index="logs-*",
            query={"match": {"message": "err"}},
            secrets={"ES_API_KEY": "thekey"},
        )

    assert results == [{"message": "warn"}]
    call_kwargs = mock_os_class.call_args.kwargs
    assert call_kwargs["http_auth"] == ("", "thekey")


@pytest.mark.asyncio
async def test_elastic_client_opensearch_basic_auth():
    from norn.contrib.search.elastic import ElasticClient

    mock_os_class = MagicMock()
    mock_os = AsyncMock()
    mock_os.search = AsyncMock(return_value={"hits": {"hits": []}})
    mock_os.close = AsyncMock()
    mock_os_class.return_value = mock_os

    with patch.dict("sys.modules", {"opensearchpy": MagicMock(AsyncOpenSearch=mock_os_class)}):
        client = ElasticClient(url="http://os:9200", auth="basic", use_opensearch=True)
        await client.search(
            index="logs-*",
            query={"match_all": {}},
            secrets={"ES_USER": "user", "ES_PASSWORD": "pass"},
        )

    call_kwargs = mock_os_class.call_args.kwargs
    assert call_kwargs["http_auth"] == ("user", "pass")


# ---------------------------------------------------------------------------
# SearchLogs stage — run() with mocked ElasticClient
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_logs_run_returns_hits():
    from norn.contrib.stages.search_logs import SearchLogs

    issue = _make_issue(stacktraces=["NullPointerException at Foo.bar()"])
    ctx = _make_ctx(issue=issue)

    mock_client = AsyncMock()
    mock_client.search = AsyncMock(return_value=[
        {"message": "PROJ-42 error occurred"},
        {"message": "another log line"},
    ])

    with patch("norn.contrib.stages.search_logs.ElasticClient", return_value=mock_client):
        stage = SearchLogs(url="http://es:9200", index="logs-*")
        result = await stage.run(ctx)

    assert result.success
    assert result.output["hits"] == 2
    assert len(result.output["logs"]) == 2


@pytest.mark.asyncio
async def test_search_logs_passes_secrets_to_client():
    from norn.contrib.stages.search_logs import SearchLogs

    issue = _make_issue()
    ctx = _make_ctx(issue=issue)
    ctx.secrets["ES_API_KEY"] = "mykey"

    mock_client = AsyncMock()
    mock_client.search = AsyncMock(return_value=[])

    with patch("norn.contrib.stages.search_logs.ElasticClient", return_value=mock_client) as MockClient:
        stage = SearchLogs(url="http://es:9200", index="logs-*", auth="api_key")
        await stage.run(ctx)

    MockClient.assert_called_once_with(url="http://es:9200", auth="api_key", use_opensearch=False)
    mock_client.search.assert_called_once()
    call_kwargs = mock_client.search.call_args.kwargs
    assert call_kwargs["secrets"]["ES_API_KEY"] == "mykey"


# ---------------------------------------------------------------------------
# SearchLogs — query_template
# ---------------------------------------------------------------------------


def test_search_logs_query_template():
    from norn.contrib.stages.search_logs import SearchLogs

    stage = SearchLogs(url="http://es:9200", index="logs-*", query_template="service:payment AND {key}")
    issue = _make_issue()
    query = stage._build_query(issue)

    assert query == {"query_string": {"query": "service:payment AND PROJ-42"}}


def test_search_logs_default_query_no_stacktraces():
    from norn.contrib.stages.search_logs import SearchLogs

    stage = SearchLogs(url="http://es:9200", index="logs-*")
    issue = _make_issue()
    query = stage._build_query(issue)

    assert query["bool"]["should"][0]["match"]["message"] == "PROJ-42"
    assert len(query["bool"]["should"]) == 1


# ---------------------------------------------------------------------------
# log_tools — search_logs MCP tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_tool_missing_url(monkeypatch):
    monkeypatch.delenv("ES_URL", raising=False)

    from norn.contrib.tools.log_tools import search_logs

    result = await search_logs.handler({"query": "error"})

    assert result.get("is_error") is True
    assert "ES_URL" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_log_tool_calls_elastic_client(monkeypatch):
    monkeypatch.setenv("ES_URL", "http://es:9200")
    monkeypatch.setenv("ES_API_KEY", "k")

    mock_client = AsyncMock()
    mock_client.search = AsyncMock(return_value=[{"message": "hit"}])

    with patch("norn.contrib.tools.log_tools.ElasticClient", return_value=mock_client):
        from norn.contrib.tools.log_tools import search_logs

        result = await search_logs.handler({"query": "error", "index": "my-logs-*", "limit": 5})

    assert result["content"][0]["type"] == "text"
    import json
    data = json.loads(result["content"][0]["text"])
    assert data == [{"message": "hit"}]

    mock_client.search.assert_called_once()
    call_kwargs = mock_client.search.call_args.kwargs
    assert call_kwargs["index"] == "my-logs-*"
    assert call_kwargs["size"] == 5

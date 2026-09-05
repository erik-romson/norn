from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from norn.models import PipelineContext, StageResult, UsageTracker
from norn.contrib.models.issue_context import IssueContext
from norn.contrib.sources.base import IssueSource


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(**params) -> PipelineContext:
    return PipelineContext(results={}, usage_tracker=UsageTracker(), params=params)


def _make_ctx_with_secrets(**secrets) -> PipelineContext:
    ctx = PipelineContext(results={}, usage_tracker=UsageTracker(), params={})
    ctx.secrets.update(secrets)
    return ctx


def _make_issue(**overrides) -> IssueContext:
    defaults = dict(key="PROJ-1", summary="Fix null ptr", description="NPE in Foo.bar()")
    defaults.update(overrides)
    return IssueContext(**defaults)


class FakeSource(IssueSource):
    def __init__(self, issue: IssueContext) -> None:
        self._issue = issue

    async def fetch(self, issue_key: str, ctx=None) -> IssueContext:
        return self._issue


# ---------------------------------------------------------------------------
# ReadIssue stage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_issue_missing_key():
    from norn.contrib.stages.read_issue import ReadIssue

    stage = ReadIssue(FakeSource(_make_issue()))
    ctx = _make_ctx()  # no issue_key or args
    result = await stage.run(ctx)

    assert not result.success
    assert "issue_key" in result.error


@pytest.mark.asyncio
async def test_read_issue_key_from_issue_key_param():
    from norn.contrib.stages.read_issue import ReadIssue

    issue = _make_issue(key="PROJ-42")
    stage = ReadIssue(FakeSource(issue))
    ctx = _make_ctx(issue_key="PROJ-42")
    result = await stage.run(ctx)

    assert result.success
    assert result.output.key == "PROJ-42"


@pytest.mark.asyncio
async def test_read_issue_key_from_args_param():
    from norn.contrib.stages.read_issue import ReadIssue

    issue = _make_issue(key="PROJ-99")
    stage = ReadIssue(FakeSource(issue))
    ctx = _make_ctx(args="PROJ-99")
    result = await stage.run(ctx)

    assert result.success
    assert result.output.key == "PROJ-99"


# ---------------------------------------------------------------------------
# JiraSource
# ---------------------------------------------------------------------------


def _make_jira_mock(
    *,
    key: str = "PROJ-1",
    summary: str = "Bug",
    description: str = "Details",
    labels: list[str] | None = None,
    components: list[str] | None = None,
    comments: list[str] | None = None,
    attachment_filenames: list[str] | None = None,
    linked_keys: list[str] | None = None,
) -> MagicMock:
    """Build a minimal JIRA client mock."""
    issue_mock = MagicMock()
    issue_mock.key = key
    issue_mock.fields.summary = summary
    issue_mock.fields.description = description
    issue_mock.fields.labels = labels or []
    issue_mock.fields.attachment = []
    issue_mock.fields.issuelinks = []

    comp_mocks = []
    for c in (components or []):
        m = MagicMock()
        m.name = c
        comp_mocks.append(m)
    issue_mock.fields.components = comp_mocks

    comment_mocks = []
    for body in (comments or []):
        m = MagicMock()
        m.body = body
        comment_mocks.append(m)

    att_mocks = []
    for i, fname in enumerate(attachment_filenames or []):
        m = MagicMock()
        m.id = str(10000 + i)
        m.filename = fname
        m.get.return_value = b"binary content"
        att_mocks.append(m)
    issue_mock.fields.attachment = att_mocks

    link_mocks = []
    for lkey in (linked_keys or []):
        m = MagicMock()
        inner = MagicMock()
        inner.key = lkey
        m.outwardIssue = inner
        link_mocks.append(m)
    issue_mock.fields.issuelinks = link_mocks

    client = MagicMock()
    client.issue.return_value = issue_mock
    client.comments.return_value = comment_mocks
    return client


@pytest.mark.asyncio
async def test_jira_source_basic_fields():
    from norn.contrib.sources.jira_source import JiraSource

    client = _make_jira_mock(
        key="PROJ-1",
        summary="Fix null ptr",
        description="NPE in Foo.bar()",
        labels=["backend", "critical"],
        components=["auth"],
    )

    source = JiraSource(url="https://example.atlassian.net", include_attachments=False)
    with patch.object(source, "_make_client", return_value=client):
        result = await source.fetch("PROJ-1")

    assert result.key == "PROJ-1"
    assert result.summary == "Fix null ptr"
    assert result.description == "NPE in Foo.bar()"
    assert "backend" in result.labels
    assert "auth" in result.components


@pytest.mark.asyncio
async def test_jira_source_comments_included():
    from norn.contrib.sources.jira_source import JiraSource

    client = _make_jira_mock(
        comments=["First comment", "Second comment"],
    )

    source = JiraSource(url="https://example.atlassian.net", include_comments=True, include_attachments=False)
    with patch.object(source, "_make_client", return_value=client):
        result = await source.fetch("PROJ-1")

    assert len(result.comments) == 2
    assert "First comment" in result.comments


@pytest.mark.asyncio
async def test_jira_source_comments_excluded():
    from norn.contrib.sources.jira_source import JiraSource

    client = _make_jira_mock(comments=["Should not appear"])

    source = JiraSource(url="https://example.atlassian.net", include_comments=False, include_attachments=False)
    with patch.object(source, "_make_client", return_value=client):
        result = await source.fetch("PROJ-1")

    assert result.comments == []


@pytest.mark.asyncio
async def test_jira_source_extracts_stacktrace():
    from norn.contrib.sources.jira_source import JiraSource

    stacktrace_text = (
        "java.lang.NullPointerException: Cannot invoke\n"
        "\tat com.acme.Foo.bar(Foo.java:10)\n"
        "\tat com.acme.Baz.run(Baz.java:20)\n"
    )
    client = _make_jira_mock(description=stacktrace_text)

    source = JiraSource(
        url="https://example.atlassian.net",
        extract_stacktraces_flag=True,
        include_attachments=False,
    )
    with patch.object(source, "_make_client", return_value=client):
        result = await source.fetch("PROJ-1")

    assert len(result.stacktraces) >= 1
    assert "com.acme.Foo.bar" in result.stacktraces[0]


@pytest.mark.asyncio
async def test_jira_source_no_stacktrace_extraction():
    from norn.contrib.sources.jira_source import JiraSource

    client = _make_jira_mock(description="No stacktrace here.")

    source = JiraSource(
        url="https://example.atlassian.net",
        extract_stacktraces_flag=False,
        include_attachments=False,
    )
    with patch.object(source, "_make_client", return_value=client):
        result = await source.fetch("PROJ-1")

    assert result.stacktraces == []


@pytest.mark.asyncio
async def test_jira_source_downloads_attachments(tmp_path):
    from norn.contrib.sources.jira_source import JiraSource

    client = _make_jira_mock(attachment_filenames=["report.txt"])

    source = JiraSource(
        url="https://example.atlassian.net",
        include_attachments=True,
        attachment_dir=str(tmp_path),
    )
    with patch.object(source, "_make_client", return_value=client):
        result = await source.fetch("PROJ-1")

    assert len(result.attachments) == 1
    # On-disk filename is prefixed with the attachment id to avoid collisions.
    assert result.attachments[0].name == "10000_report.txt"
    assert result.attachments[0].read_bytes() == b"binary content"


@pytest.mark.asyncio
async def test_jira_source_credentials_from_ctx():
    from norn.contrib.sources.jira_source import JiraSource

    client = _make_jira_mock()
    captured: list[tuple[str, str]] = []

    def _capture_client(email, token):
        captured.append((email, token))
        return client

    ctx = _make_ctx_with_secrets(JIRA_EMAIL="user@example.com", JIRA_TOKEN="secret")
    source = JiraSource(url="https://example.atlassian.net", include_attachments=False)

    def _make_client_mock(email, token):
        captured.append((email, token))
        return client

    with patch.object(source, "_make_client", side_effect=_make_client_mock):
        await source.fetch("PROJ-1", ctx)

    assert captured[0] == ("user@example.com", "secret")


@pytest.mark.asyncio
async def test_jira_source_linked_issues():
    from norn.contrib.sources.jira_source import JiraSource

    client = _make_jira_mock(linked_keys=["PROJ-2", "PROJ-3"])

    source = JiraSource(url="https://example.atlassian.net", include_attachments=False)
    with patch.object(source, "_make_client", return_value=client):
        result = await source.fetch("PROJ-1")

    assert "PROJ-2" in result.linked_issues
    assert "PROJ-3" in result.linked_issues


# ---------------------------------------------------------------------------
# Jira DSL builder
# ---------------------------------------------------------------------------


def test_jira_builder_defaults():
    from norn.contrib.dsl.jira import Jira

    source = Jira("https://acme.atlassian.net").build()

    assert source.url == "https://acme.atlassian.net"
    assert source.include_comments is True
    assert source.include_attachments is True
    assert source.extract_stacktraces_flag is True
    assert source.projects == []


def test_jira_builder_fluent():
    from norn.contrib.dsl.jira import Jira

    source = (
        Jira("https://acme.atlassian.net")
        .projects("BACKEND", "FRONTEND")
        .auth("pat")
        .include_comments(False)
        .include_attachments(False)
        .extract_stacktraces(False)
        .attachment_dir("/tmp/custom")
        .build()
    )

    assert source.projects == ["BACKEND", "FRONTEND"]
    assert source.auth_method == "pat"
    assert source.include_comments is False
    assert source.include_attachments is False
    assert source.extract_stacktraces_flag is False
    assert str(source.attachment_dir) == "/tmp/custom"


def test_jira_builder_returns_jira_source():
    from norn.contrib.dsl.jira import Jira
    from norn.contrib.sources.jira_source import JiraSource

    source = Jira("https://acme.atlassian.net").build()
    assert isinstance(source, JiraSource)

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from norn.models import PipelineContext, StageResult, UsageTracker
from norn.contrib.matchers.base import MatcherChain, MatchResult
from norn.contrib.matchers.component_matcher import ComponentMatcher
from norn.contrib.matchers.keyword_matcher import KeywordMatcher
from norn.contrib.matchers.label_matcher import LabelMatcher
from norn.contrib.matchers.stacktrace_matcher import StacktraceMatcher
from norn.contrib.models.issue_context import IssueContext
from norn.contrib.stages.match_repo import MatchRepo


def _make_issue(**overrides) -> IssueContext:
    defaults = dict(key="PROJ-1", summary="Bug", description="desc")
    defaults.update(overrides)
    return IssueContext(**defaults)


def _make_ctx() -> PipelineContext:
    return PipelineContext(results={}, usage_tracker=UsageTracker(), params={})


@pytest.mark.asyncio
async def test_component_matcher_hit():
    matcher = ComponentMatcher({"auth-service": "acme/auth-service"})
    issue = _make_issue(components=["auth-service"])
    result = await matcher.match(issue, _make_ctx())
    assert result is not None
    assert result.repo == "acme/auth-service"
    assert result.confidence == 1.0
    assert result.method == "component"


@pytest.mark.asyncio
async def test_component_matcher_miss():
    matcher = ComponentMatcher({"auth-service": "acme/auth-service"})
    issue = _make_issue(components=["billing"])
    result = await matcher.match(issue, _make_ctx())
    assert result is None


@pytest.mark.asyncio
async def test_label_matcher_hit():
    matcher = LabelMatcher({"backend": "acme/api-server"})
    issue = _make_issue(labels=["backend"])
    result = await matcher.match(issue, _make_ctx())
    assert result is not None
    assert result.repo == "acme/api-server"
    assert result.confidence == 0.9
    assert result.method == "label"


@pytest.mark.asyncio
async def test_label_matcher_miss():
    matcher = LabelMatcher({"backend": "acme/api-server"})
    issue = _make_issue(labels=["frontend"])
    result = await matcher.match(issue, _make_ctx())
    assert result is None


@pytest.mark.asyncio
async def test_matcher_chain_first_wins():
    chain = MatcherChain(threshold=0.7)
    chain.add(ComponentMatcher({"auth": "acme/auth"}))
    chain.add(LabelMatcher({"backend": "acme/api"}))
    issue = _make_issue(components=["auth"], labels=["backend"])
    result = await chain.match(issue, _make_ctx())
    assert result is not None
    assert result.repo == "acme/auth"
    assert result.method == "component"


@pytest.mark.asyncio
async def test_matcher_chain_threshold():
    chain = MatcherChain(threshold=0.95)
    chain.add(LabelMatcher({"backend": "acme/api"}))  # confidence=0.9 < 0.95
    issue = _make_issue(labels=["backend"])
    result = await chain.match(issue, _make_ctx())
    assert result is None  # Below threshold


@pytest.mark.asyncio
async def test_matcher_chain_no_match():
    chain = MatcherChain(threshold=0.5)
    chain.add(ComponentMatcher({}))
    issue = _make_issue()
    result = await chain.match(issue, _make_ctx())
    assert result is None


# ---------------------------------------------------------------------------
# StacktraceMatcher
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stacktrace_matcher_hit():
    stacktrace = (
        "at com.acme.auth.TokenValidator.validate(TokenValidator.java:42)\n"
        "at com.acme.auth.AuthService.login(AuthService.java:101)"
    )
    issue = _make_issue(stacktraces=[stacktrace])
    with patch(
        "norn.contrib.matchers.stacktrace_matcher.github_code_search",
        new=AsyncMock(return_value=["acme/auth-service"]),
    ):
        matcher = StacktraceMatcher(github_org="acme")
        result = await matcher.match(issue, _make_ctx())
    assert result is not None
    assert result.repo == "acme/auth-service"
    assert result.method == "stacktrace"
    assert 0.0 < result.confidence <= 1.0


@pytest.mark.asyncio
async def test_stacktrace_matcher_no_stacktraces():
    issue = _make_issue(stacktraces=[])
    matcher = StacktraceMatcher(github_org="acme")
    result = await matcher.match(issue, _make_ctx())
    assert result is None


@pytest.mark.asyncio
async def test_stacktrace_matcher_no_search_results():
    stacktrace = "at com.acme.SomeService.doThing(SomeService.java:10)"
    issue = _make_issue(stacktraces=[stacktrace])
    with patch(
        "norn.contrib.matchers.stacktrace_matcher.github_code_search",
        new=AsyncMock(return_value=[]),
    ):
        matcher = StacktraceMatcher(github_org="acme")
        result = await matcher.match(issue, _make_ctx())
    assert result is None


@pytest.mark.asyncio
async def test_stacktrace_matcher_uses_ctx_org():
    stacktrace = "at com.acme.SomeService.doThing(SomeService.java:10)"
    issue = _make_issue(stacktraces=[stacktrace])
    ctx = _make_ctx()
    ctx.params["github_org"] = "ctx-org"
    calls: list[tuple] = []

    async def fake_search(cls_name: str, github_org: str | None) -> list[str]:
        calls.append((cls_name, github_org))
        return ["ctx-org/some-service"]

    with patch(
        "norn.contrib.matchers.stacktrace_matcher.github_code_search",
        new=fake_search,
    ):
        matcher = StacktraceMatcher()  # no org set
        await matcher.match(issue, ctx)

    assert all(org == "ctx-org" for _, org in calls)


# ---------------------------------------------------------------------------
# KeywordMatcher
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_keyword_matcher_hit():
    issue = _make_issue(summary="auth-service is broken", description="crashes on startup")
    ctx = _make_ctx()
    ctx.params["github_org"] = "acme"
    with patch(
        "norn.contrib.matchers.keyword_matcher.list_org_repos",
        new=AsyncMock(return_value=["acme/auth-service", "acme/billing"]),
    ):
        matcher = KeywordMatcher()
        result = await matcher.match(issue, ctx)
    assert result is not None
    assert result.repo == "acme/auth-service"
    assert result.confidence == 0.6
    assert result.method == "keyword"


@pytest.mark.asyncio
async def test_keyword_matcher_miss():
    issue = _make_issue(summary="something unrelated", description="no match here")
    ctx = _make_ctx()
    ctx.params["github_org"] = "acme"
    with patch(
        "norn.contrib.matchers.keyword_matcher.list_org_repos",
        new=AsyncMock(return_value=["acme/auth-service", "acme/billing"]),
    ):
        matcher = KeywordMatcher()
        result = await matcher.match(issue, ctx)
    assert result is None


@pytest.mark.asyncio
async def test_keyword_matcher_no_org():
    issue = _make_issue(summary="auth-service crash")
    ctx = _make_ctx()  # no github_org param
    with patch(
        "norn.contrib.matchers.keyword_matcher.list_org_repos",
        new=AsyncMock(return_value=[]),
    ):
        matcher = KeywordMatcher()
        result = await matcher.match(issue, ctx)
    assert result is None


# ---------------------------------------------------------------------------
# LLMMatcher
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_llm_matcher_hit():
    issue = _make_issue(summary="Login broken", description="users cannot log in")
    ctx = _make_ctx()
    ctx.params["github_org"] = "acme"

    async def fake_complete(prompt, *, provider, model, system_prompt=None, cwd=None, env=None):
        return '{"repo": "acme/auth-service", "confidence": 0.85, "reasoning": "login"}'

    with patch(
        "norn.contrib.matchers.llm_matcher.list_org_repos",
        new=AsyncMock(return_value=["acme/auth-service"]),
    ), patch(
        "norn.agents.complete.complete_text",
        new=fake_complete,
    ):
        from norn.contrib.matchers.llm_matcher import LLMMatcher
        matcher = LLMMatcher()
        result = await matcher.match(issue, ctx)

    assert result is not None
    assert result.repo == "acme/auth-service"
    assert result.confidence == 0.85
    assert result.method == "llm"


@pytest.mark.asyncio
async def test_llm_matcher_no_repos():
    issue = _make_issue(summary="some bug")
    ctx = _make_ctx()
    ctx.params["github_org"] = "acme"
    with patch(
        "norn.contrib.matchers.llm_matcher.list_org_repos",
        new=AsyncMock(return_value=[]),
    ):
        from norn.contrib.matchers.llm_matcher import LLMMatcher
        matcher = LLMMatcher()
        result = await matcher.match(issue, ctx)
    assert result is None


@pytest.mark.asyncio
async def test_llm_matcher_passes_ctx_provider():
    """LLMMatcher should pass ctx.agent_provider to complete_text."""
    issue = _make_issue(summary="Login broken", description="users cannot log in")
    ctx = _make_ctx()
    ctx.params["github_org"] = "acme"
    ctx.agent_provider = "opencode"

    captured: dict = {}

    async def fake_complete(prompt, *, provider, model, system_prompt=None, cwd=None, env=None):
        captured["provider"] = provider
        captured["model"] = model
        return '{"repo": "acme/auth-service", "confidence": 0.9, "reasoning": "test"}'

    with patch(
        "norn.contrib.matchers.llm_matcher.list_org_repos",
        new=AsyncMock(return_value=["acme/auth-service"]),
    ), patch(
        "norn.agents.complete.complete_text",
        new=fake_complete,
    ):
        from norn.contrib.matchers.llm_matcher import LLMMatcher
        matcher = LLMMatcher()
        await matcher.match(issue, ctx)

    assert captured["provider"] == "opencode"


@pytest.mark.asyncio
async def test_llm_matcher_complete_text_returns_none():
    """LLMMatcher returns None when complete_text returns None."""
    issue = _make_issue(summary="Login broken", description="users cannot log in")
    ctx = _make_ctx()
    ctx.params["github_org"] = "acme"

    async def fake_complete(prompt, *, provider, model, system_prompt=None, cwd=None, env=None):
        return None

    with patch(
        "norn.contrib.matchers.llm_matcher.list_org_repos",
        new=AsyncMock(return_value=["acme/auth-service"]),
    ), patch(
        "norn.agents.complete.complete_text",
        new=fake_complete,
    ):
        from norn.contrib.matchers.llm_matcher import LLMMatcher
        matcher = LLMMatcher()
        result = await matcher.match(issue, ctx)

    assert result is None


# ---------------------------------------------------------------------------
# MatchRepo stage
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_match_repo_stage_success():
    issue = _make_issue(components=["auth-service"])
    ctx = _make_ctx()
    ctx.results["read_issue"] = StageResult(name="read_issue", success=True, output=issue)

    chain = MatcherChain(threshold=0.7)
    chain.add(ComponentMatcher({"auth-service": "acme/auth-service"}))

    stage = MatchRepo(chain)
    result = await stage.run(ctx)

    assert result.success
    updated_issue: IssueContext = result.output
    assert updated_issue.repo == "acme/auth-service"
    assert updated_issue.match_confidence == 1.0
    assert updated_issue.match_method == "component"


@pytest.mark.asyncio
async def test_match_repo_stage_no_match():
    issue = _make_issue(components=[])
    ctx = _make_ctx()
    ctx.results["read_issue"] = StageResult(name="read_issue", success=True, output=issue)

    chain = MatcherChain(threshold=0.7)
    chain.add(ComponentMatcher({}))

    stage = MatchRepo(chain)
    result = await stage.run(ctx)

    assert not result.success
    assert "confidence threshold" in result.error


@pytest.mark.asyncio
async def test_match_repo_stage_missing_issue():
    ctx = _make_ctx()  # read_issue present but output is None
    ctx.results["read_issue"] = StageResult(name="read_issue", success=False, output=None)

    chain = MatcherChain(threshold=0.7)
    stage = MatchRepo(chain)
    result = await stage.run(ctx)

    assert not result.success
    assert "read_issue" in result.error

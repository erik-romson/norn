"""Tests for capability-based feature gating.

Proves that:
- opencode still rejects hooks / mcp / non-project setting_sources / fork /
  structured output (behavior preserved from the old provider-name check).
- claude-code accepts all of those features.
- The gate is driven by the capability descriptor, NOT the provider name: a
  fake provider whose capabilities forbid hooks is rejected even when its name
  is not "opencode".
- validate_capabilities() raises ValueError with human-readable messages.
"""

from __future__ import annotations

import pytest

from norn.agents.base import AgentRequest
from norn.agents.capabilities import AgentCapabilities, CostMode, validate_capabilities
from norn.agents.opencode import OpenCodeError, OpenCodeProvider
from norn.models import PipelineContext, StageResult
from norn.stages.generate import Generate
from norn.agents import registry as agent_registry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _opencode_request(**overrides) -> AgentRequest:
    defaults = dict(prompt="hello", stage_name="test_stage", provider="opencode")
    defaults.update(overrides)
    return AgentRequest(**defaults)


def _minimal_caps(**overrides) -> AgentCapabilities:
    """Return an AgentCapabilities with all features False, applying *overrides*."""
    defaults = dict(
        block_kinds=frozenset({"text"}),
        cost_mode=CostMode.ZERO_UNKNOWN,
        supports_structured_output=False,
        supports_fork=False,
        supports_hooks=False,
        supports_mcp=False,
        supports_thinking=False,
        file_edit_without_terminal=False,
        session_resumable=False,
        session_forkable=False,
        session_attachable=False,
        live_model_switch=False,
        model_alias_table="fake",
        can_list_models=False,
        supports_setting_sources=False,
    )
    defaults.update(overrides)
    return AgentCapabilities(**defaults)


def _full_caps(**overrides) -> AgentCapabilities:
    """Return an AgentCapabilities with all features True (except attachable/live_switch)."""
    defaults = dict(
        block_kinds=frozenset({"text", "tool_use", "tool_result", "thinking"}),
        cost_mode=CostMode.TRACKED,
        supports_structured_output=True,
        supports_fork=True,
        supports_hooks=True,
        supports_mcp=True,
        supports_thinking=True,
        file_edit_without_terminal=True,
        session_resumable=True,
        session_forkable=True,
        session_attachable=False,
        live_model_switch=False,
        model_alias_table="fake",
        can_list_models=False,
        supports_setting_sources=True,
    )
    defaults.update(overrides)
    return AgentCapabilities(**defaults)


# ---------------------------------------------------------------------------
# validate_capabilities — unit tests
# ---------------------------------------------------------------------------


def test_validate_caps_structured_output_rejected():
    caps = _minimal_caps()
    request = _opencode_request(output_format={"type": "json_schema", "schema": {}})
    with pytest.raises(ValueError, match="Structured output"):
        validate_capabilities(request, caps, "fake-provider")


def test_validate_caps_fork_rejected():
    caps = _minimal_caps()
    request = _opencode_request(fork_session=True)
    with pytest.raises(ValueError, match="fork_session"):
        validate_capabilities(request, caps, "fake-provider")


def test_validate_caps_setting_sources_rejected():
    caps = _minimal_caps()
    request = _opencode_request(setting_sources=["user"])
    with pytest.raises(ValueError, match="setting_sources"):
        validate_capabilities(request, caps, "fake-provider")


def test_validate_caps_hooks_rejected():
    caps = _minimal_caps()
    request = _opencode_request(hooks={"PreToolUse": []})
    with pytest.raises(ValueError, match="hooks"):
        validate_capabilities(request, caps, "fake-provider")


def test_validate_caps_mcp_servers_rejected():
    caps = _minimal_caps()
    request = _opencode_request(mcp_servers={"srv": {}})
    with pytest.raises(ValueError, match="mcp_servers"):
        validate_capabilities(request, caps, "fake-provider")


def test_validate_caps_mcp_tools_rejected():
    caps = _minimal_caps()
    request = _opencode_request(mcp_tools=["tool"])
    with pytest.raises(ValueError, match="mcp_tools"):
        validate_capabilities(request, caps, "fake-provider")


def test_validate_caps_thinking_rejected():
    caps = _minimal_caps()
    request = _opencode_request(thinking={"type": "enabled", "budget_tokens": 5000})
    with pytest.raises(ValueError, match="thinking"):
        validate_capabilities(request, caps, "fake-provider")


def test_validate_caps_provider_name_appears_in_message():
    caps = _minimal_caps()
    request = _opencode_request(hooks={"PreToolUse": []})
    with pytest.raises(ValueError, match="my-special-provider"):
        validate_capabilities(request, caps, "my-special-provider")


def test_validate_caps_full_caps_allows_everything():
    caps = _full_caps()
    request = _opencode_request(
        output_format={"type": "json_schema", "schema": {}},
        fork_session=True,
        setting_sources=["user"],
        hooks={"PreToolUse": []},
        mcp_servers={"srv": {}},
        mcp_tools=["tool"],
        thinking={"type": "enabled", "budget_tokens": 5000},
    )
    # Should not raise
    validate_capabilities(request, caps, "full-provider")


# ---------------------------------------------------------------------------
# OpenCodeProvider._reject_unsupported — still rejects the same features
# ---------------------------------------------------------------------------


def test_opencode_rejects_hooks_via_capabilities():
    provider = OpenCodeProvider()
    request = _opencode_request(hooks={"PostToolUse": []})
    with pytest.raises(OpenCodeError, match="hooks"):
        provider._reject_unsupported(request)


def test_opencode_rejects_mcp_servers_via_capabilities():
    provider = OpenCodeProvider()
    request = _opencode_request(mcp_servers={"srv": {}})
    with pytest.raises(OpenCodeError, match="mcp_servers"):
        provider._reject_unsupported(request)


def test_opencode_rejects_mcp_tools_via_capabilities():
    provider = OpenCodeProvider()
    request = _opencode_request(mcp_tools=["tool"])
    with pytest.raises(OpenCodeError, match="mcp_tools"):
        provider._reject_unsupported(request)


def test_opencode_rejects_fork_via_capabilities():
    provider = OpenCodeProvider()
    request = _opencode_request(fork_session=True)
    with pytest.raises(OpenCodeError, match="fork_session"):
        provider._reject_unsupported(request)


def test_opencode_rejects_structured_output_via_capabilities():
    provider = OpenCodeProvider()
    request = _opencode_request(output_format={"type": "json_schema", "schema": {}})
    with pytest.raises(OpenCodeError, match="Structured output"):
        provider._reject_unsupported(request)


def test_opencode_rejects_thinking_via_capabilities():
    provider = OpenCodeProvider()
    request = _opencode_request(thinking={"type": "enabled", "budget_tokens": 5000})
    with pytest.raises(OpenCodeError, match="thinking"):
        provider._reject_unsupported(request)


def test_opencode_allows_plain_request():
    provider = OpenCodeProvider()
    request = _opencode_request()
    # Should not raise
    provider._reject_unsupported(request)


# ---------------------------------------------------------------------------
# Generate.run() — gates features via capabilities, not provider name
# ---------------------------------------------------------------------------


class _FakeRestrictedProvider:
    """A test provider whose capabilities forbid hooks and mcp — not named opencode."""

    name = "_fake-restricted-provider"
    capabilities: AgentCapabilities = _minimal_caps()

    async def run(self, request: AgentRequest):
        return
        yield  # noqa: unreachable


class _FakePermissiveProvider:
    """A test provider whose capabilities allow everything — not named claude-code."""

    name = "_fake-permissive-provider"
    capabilities: AgentCapabilities = _full_caps()

    async def run(self, request: AgentRequest):
        return
        yield  # noqa: unreachable


@pytest.fixture()
def restricted_provider():
    p = _FakeRestrictedProvider()
    agent_registry.register(p)
    try:
        yield p
    finally:
        agent_registry._registry.pop(p.name, None)


@pytest.fixture()
def permissive_provider():
    p = _FakePermissiveProvider()
    agent_registry.register(p)
    try:
        yield p
    finally:
        agent_registry._registry.pop(p.name, None)


@pytest.mark.asyncio
async def test_generate_rejects_hooks_for_restricted_provider(restricted_provider):
    """A non-opencode provider with supports_hooks=False is still rejected."""
    hooks = {"PreToolUse": [{"matcher": {}, "hook": {"type": "command", "command": "exit 0"}}]}
    gen = Generate(prompt="do stuff", hooks=hooks)
    ctx = PipelineContext()
    ctx.agent_provider = _FakeRestrictedProvider.name
    result = await gen.run(ctx)
    assert not result.success
    assert "hooks" in result.error.lower()
    assert _FakeRestrictedProvider.name in result.error


@pytest.mark.asyncio
async def test_generate_rejects_mcp_tools_for_restricted_provider(restricted_provider):
    """A non-opencode provider with supports_mcp=False rejects mcp_tools."""
    gen = Generate(prompt="do stuff")
    ctx = PipelineContext()
    ctx.agent_provider = _FakeRestrictedProvider.name
    result = await gen.run(ctx, mcp_tools=["fake_tool"])
    assert not result.success
    assert "mcp_tools" in result.error.lower()
    assert _FakeRestrictedProvider.name in result.error


@pytest.mark.asyncio
async def test_generate_rejects_setting_sources_for_restricted_provider(restricted_provider):
    """A provider with supports_setting_sources=False rejects non-project sources."""
    gen = Generate(prompt="do stuff", setting_sources=["user"])
    ctx = PipelineContext()
    ctx.agent_provider = _FakeRestrictedProvider.name
    result = await gen.run(ctx)
    assert not result.success
    assert "'user'" in result.error
    assert _FakeRestrictedProvider.name in result.error


@pytest.mark.asyncio
async def test_generate_allows_hooks_for_permissive_provider(permissive_provider):
    """A non-claude-code provider with supports_hooks=True passes hooks through."""
    hooks = {"PreToolUse": [{"matcher": {}, "hook": {"type": "command", "command": "exit 0"}}]}
    gen = Generate(prompt="do stuff", hooks=hooks)
    ctx = PipelineContext()
    ctx.agent_provider = _FakePermissiveProvider.name
    result = await gen.run(ctx)
    # The permissive provider ran — result is success (empty output is fine)
    assert result.success


@pytest.mark.asyncio
async def test_generate_allows_mcp_tools_for_permissive_provider(permissive_provider):
    """A non-claude-code provider with supports_mcp=True passes mcp_tools through."""
    gen = Generate(prompt="do stuff")
    ctx = PipelineContext()
    ctx.agent_provider = _FakePermissiveProvider.name
    result = await gen.run(ctx, mcp_tools=["fake_tool"])
    assert result.success


@pytest.mark.asyncio
async def test_generate_allows_setting_sources_for_permissive_provider(permissive_provider):
    """A provider with supports_setting_sources=True passes non-project sources through."""
    gen = Generate(prompt="do stuff", setting_sources=["user"])
    ctx = PipelineContext()
    ctx.agent_provider = _FakePermissiveProvider.name
    result = await gen.run(ctx)
    assert result.success


# ---------------------------------------------------------------------------
# opencode (registered provider) — accept/reject matrix via Generate.run()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_opencode_rejects_hooks_in_generate():
    hooks = {"PreToolUse": [{"matcher": {}, "hook": {"type": "command", "command": "exit 0"}}]}
    gen = Generate(prompt="do stuff", hooks=hooks)
    ctx = PipelineContext()
    ctx.agent_provider = "opencode"
    result = await gen.run(ctx)
    assert not result.success
    assert "hooks" in result.error.lower()
    assert "opencode" in result.error


@pytest.mark.asyncio
async def test_opencode_rejects_mcp_tools_in_generate():
    gen = Generate(prompt="do stuff")
    ctx = PipelineContext()
    ctx.agent_provider = "opencode"
    result = await gen.run(ctx, mcp_tools=["tool"])
    assert not result.success
    assert "mcp_tools" in result.error.lower()
    assert "opencode" in result.error


@pytest.mark.asyncio
async def test_opencode_rejects_setting_sources_in_generate():
    gen = Generate(prompt="do stuff", setting_sources=["user"])
    ctx = PipelineContext()
    ctx.agent_provider = "opencode"
    result = await gen.run(ctx)
    assert not result.success
    assert "'user'" in result.error
    assert "opencode" in result.error


@pytest.mark.asyncio
async def test_claude_code_accepts_hooks_in_generate():
    hooks = {"PreToolUse": [{"matcher": {}, "hook": {"type": "command", "command": "exit 0"}}]}
    gen = Generate(prompt="do stuff", hooks=hooks)
    ctx = PipelineContext()
    ctx.agent_provider = "claude-code"

    class _FakeClaude:
        name = "claude-code"
        last_request: AgentRequest | None = None

        async def run(self, request: AgentRequest):
            _FakeClaude.last_request = request
            return
            yield

    original = agent_registry._registry.get("claude-code")
    agent_registry.register(_FakeClaude())
    try:
        result = await gen.run(ctx)
        assert _FakeClaude.last_request is not None, "provider was never reached"
        assert _FakeClaude.last_request.hooks is hooks
    finally:
        if original:
            agent_registry.register(original)
        else:
            agent_registry._registry.pop("claude-code", None)


@pytest.mark.asyncio
async def test_claude_code_accepts_setting_sources_in_generate():
    gen = Generate(prompt="do stuff", setting_sources=["user"])
    ctx = PipelineContext()
    ctx.agent_provider = "claude-code"

    class _FakeClaude:
        name = "claude-code"
        last_request: AgentRequest | None = None

        async def run(self, request: AgentRequest):
            _FakeClaude.last_request = request
            return
            yield

    original = agent_registry._registry.get("claude-code")
    agent_registry.register(_FakeClaude())
    try:
        result = await gen.run(ctx)
        assert _FakeClaude.last_request is not None, "provider was never reached"
        assert _FakeClaude.last_request.setting_sources == ["user"]
    finally:
        if original:
            agent_registry.register(original)
        else:
            agent_registry._registry.pop("claude-code", None)

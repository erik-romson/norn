"""Tests for the AgentCapabilities descriptor and per-provider values.

Validates that:
- The descriptor shape (fields and types) is correct.
- Each registered provider exposes a ``capabilities`` attribute.
- The two built-in providers declare distinct, accurate capability sets.
"""

from __future__ import annotations

import pytest

from norn.agents.capabilities import AgentCapabilities, CostMode
from norn.agents.registry import get_provider


# ---------------------------------------------------------------------------
# Descriptor shape
# ---------------------------------------------------------------------------


def test_agent_capabilities_is_frozen():
    caps = AgentCapabilities(
        block_kinds=frozenset({"text"}),
        cost_mode=CostMode.TRACKED,
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
        model_alias_table="test",
        can_list_models=False,
    )
    with pytest.raises((AttributeError, TypeError)):
        caps.supports_hooks = True  # type: ignore[misc]


def test_cost_mode_enum_values():
    assert CostMode.TRACKED is not CostMode.ZERO_UNKNOWN
    assert CostMode.TRACKED is not CostMode.SUBSCRIPTION
    assert CostMode.ZERO_UNKNOWN is not CostMode.SUBSCRIPTION


# ---------------------------------------------------------------------------
# claude-code capabilities
# ---------------------------------------------------------------------------


def test_claude_code_has_capabilities():
    provider = get_provider("claude-code")
    assert hasattr(provider, "capabilities")
    assert isinstance(provider.capabilities, AgentCapabilities)


def test_claude_code_block_kinds():
    caps = get_provider("claude-code").capabilities
    assert caps.block_kinds == frozenset({"text", "tool_use", "tool_result", "thinking"})


def test_claude_code_cost_mode():
    caps = get_provider("claude-code").capabilities
    assert caps.cost_mode is CostMode.TRACKED


def test_claude_code_full_feature_flags():
    caps = get_provider("claude-code").capabilities
    assert caps.supports_structured_output is True
    assert caps.supports_fork is True
    assert caps.supports_hooks is True
    assert caps.supports_mcp is True
    assert caps.supports_thinking is True
    assert caps.file_edit_without_terminal is True
    assert caps.session_resumable is True
    assert caps.session_forkable is True
    assert caps.session_attachable is False
    assert caps.live_model_switch is False
    assert caps.model_alias_table == "claude-code"
    assert caps.can_list_models is False
    assert caps.supports_setting_sources is True


# ---------------------------------------------------------------------------
# opencode capabilities
# ---------------------------------------------------------------------------


def test_opencode_has_capabilities():
    provider = get_provider("opencode")
    assert hasattr(provider, "capabilities")
    assert isinstance(provider.capabilities, AgentCapabilities)


def test_opencode_block_kinds():
    caps = get_provider("opencode").capabilities
    assert caps.block_kinds == frozenset({"text"})


def test_opencode_cost_mode():
    caps = get_provider("opencode").capabilities
    assert caps.cost_mode is CostMode.ZERO_UNKNOWN


def test_opencode_full_feature_flags():
    caps = get_provider("opencode").capabilities
    assert caps.supports_structured_output is False
    assert caps.supports_fork is False
    assert caps.supports_hooks is False
    assert caps.supports_mcp is False
    assert caps.supports_thinking is False
    assert caps.file_edit_without_terminal is False
    assert caps.session_resumable is True
    assert caps.session_forkable is False
    assert caps.session_attachable is False
    assert caps.live_model_switch is False
    assert caps.model_alias_table == "opencode"
    assert caps.can_list_models is False
    assert caps.supports_setting_sources is False


# ---------------------------------------------------------------------------
# Differentiation between providers
# ---------------------------------------------------------------------------


def test_providers_differ_on_block_kinds():
    cc = get_provider("claude-code").capabilities
    oc = get_provider("opencode").capabilities
    assert cc.block_kinds != oc.block_kinds
    assert "tool_use" in cc.block_kinds
    assert "tool_use" not in oc.block_kinds


def test_providers_differ_on_hooks():
    cc = get_provider("claude-code").capabilities
    oc = get_provider("opencode").capabilities
    assert cc.supports_hooks is True
    assert oc.supports_hooks is False


def test_providers_differ_on_mcp():
    cc = get_provider("claude-code").capabilities
    oc = get_provider("opencode").capabilities
    assert cc.supports_mcp is True
    assert oc.supports_mcp is False


def test_providers_differ_on_fork():
    cc = get_provider("claude-code").capabilities
    oc = get_provider("opencode").capabilities
    assert cc.supports_fork is True
    assert oc.supports_fork is False


def test_providers_differ_on_structured_output():
    cc = get_provider("claude-code").capabilities
    oc = get_provider("opencode").capabilities
    assert cc.supports_structured_output is True
    assert oc.supports_structured_output is False


def test_providers_differ_on_model_alias_table():
    cc = get_provider("claude-code").capabilities
    oc = get_provider("opencode").capabilities
    assert cc.model_alias_table == "claude-code"
    assert oc.model_alias_table == "opencode"

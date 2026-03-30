from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from norn.models import PipelineContext
from norn.profiles import (
    ANALYSIS,
    CODING,
    SHIPPING,
    SessionProfile,
    build_block_hooks,
)
from norn.stages.generate import Generate


def test_analysis_profile_values():
    assert ANALYSIS.name == "analysis"
    assert ANALYSIS.permission_mode == "plan"
    assert ANALYSIS.allowed_tools == ["Read", "Grep", "Glob"]
    assert ANALYSIS.blocked_patterns is None


def test_coding_profile_values():
    assert CODING.name == "coding"
    assert CODING.permission_mode == "bypassPermissions"
    assert "Read" in CODING.allowed_tools
    assert "Edit" in CODING.allowed_tools
    assert "Bash" in CODING.allowed_tools
    assert CODING.blocked_patterns is not None
    assert "rm -rf /" in CODING.blocked_patterns
    assert "git push --force" in CODING.blocked_patterns


def test_shipping_profile_values():
    assert SHIPPING.name == "shipping"
    assert SHIPPING.permission_mode == "default"
    assert "Read" in SHIPPING.allowed_tools
    assert "Bash" in SHIPPING.allowed_tools
    assert SHIPPING.blocked_patterns is not None
    assert "git checkout" in SHIPPING.blocked_patterns


def test_session_profile_is_dataclass():
    profile = SessionProfile(name="test", permission_mode="default")
    assert profile.name == "test"
    assert profile.permission_mode == "default"
    assert profile.allowed_tools is None
    assert profile.blocked_patterns is None
    assert profile.env is None
    assert profile.max_turns is None


def test_build_block_hooks_returns_dict():
    result = build_block_hooks(["rm -rf /", "DROP TABLE"])
    assert isinstance(result, dict)
    assert "PreToolUse" in result


def test_build_block_hooks_structure():
    result = build_block_hooks(["dangerous_cmd"])
    hooks = result["PreToolUse"]
    assert isinstance(hooks, list)
    assert len(hooks) == 1
    hook_entry = hooks[0]
    assert hook_entry["matcher"]["tool_name"] == "Bash"
    assert hook_entry["hook"]["type"] == "command"
    assert "dangerous_cmd" in hook_entry["hook"]["command"]


def test_build_block_hooks_empty_patterns():
    result = build_block_hooks([])
    assert "PreToolUse" in result
    command = result["PreToolUse"][0]["hook"]["command"]
    assert "exit 0" in command


def test_generate_profile_applies_allowed_tools_and_permission_mode():
    """profile= on Generate sets allowed_tools and permission_mode at init time."""
    gen = Generate(prompt="hi", profile=ANALYSIS)
    assert gen.allowed_tools == ANALYSIS.allowed_tools
    assert gen.permission_mode == ANALYSIS.permission_mode


def test_generate_profile_builds_hooks_from_blocked_patterns():
    """profile= with blocked_patterns creates PreToolUse hooks on the instance."""
    gen = Generate(prompt="hi", profile=CODING)
    assert gen.hooks is not None
    assert "PreToolUse" in gen.hooks


def test_generate_stage_settings_override_profile():
    """Explicit stage-level settings take precedence over the profile."""
    gen = Generate(
        prompt="hi",
        profile=ANALYSIS,
        permission_mode="bypassPermissions",
        allowed_tools=["Read", "Write"],
    )
    assert gen.permission_mode == "bypassPermissions"
    assert gen.allowed_tools == ["Read", "Write"]


@pytest.mark.asyncio
async def test_pipeline_profile_applied_to_generate_via_context():
    """ctx.pipeline_profile is used as fallback when stage has no explicit settings."""
    captured_kwargs: dict = {}

    async def fake_query(*, prompt, options):
        return
        yield

    def fake_options_cls(**kw):
        captured_kwargs.update(kw)
        return kw

    gen = Generate(prompt="do stuff")
    ctx = PipelineContext()
    ctx.pipeline_profile = ANALYSIS

    with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock()}):
        import claude_agent_sdk as sdk_mod

        sdk_mod.query = fake_query
        sdk_mod.AssistantMessage = type("AssistantMessage", (), {})
        sdk_mod.ResultMessage = type("ResultMessage", (), {})
        sdk_mod.ClaudeAgentOptions = fake_options_cls

        await gen.run(ctx)

    assert captured_kwargs.get("allowed_tools") == ANALYSIS.allowed_tools
    assert captured_kwargs.get("permission_mode") == ANALYSIS.permission_mode


@pytest.mark.asyncio
async def test_stage_settings_take_precedence_over_pipeline_profile():
    """Stage-level allowed_tools overrides ctx.pipeline_profile at run time."""
    captured_kwargs: dict = {}

    async def fake_query(*, prompt, options):
        return
        yield

    def fake_options_cls(**kw):
        captured_kwargs.update(kw)
        return kw

    gen = Generate(prompt="do stuff", allowed_tools=["Read"], permission_mode="default")
    ctx = PipelineContext()
    ctx.pipeline_profile = CODING  # would set bypassPermissions + more tools

    with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock()}):
        import claude_agent_sdk as sdk_mod

        sdk_mod.query = fake_query
        sdk_mod.AssistantMessage = type("AssistantMessage", (), {})
        sdk_mod.ResultMessage = type("ResultMessage", (), {})
        sdk_mod.ClaudeAgentOptions = fake_options_cls

        await gen.run(ctx)

    assert captured_kwargs.get("allowed_tools") == ["Read"]
    assert captured_kwargs.get("permission_mode") == "default"


@pytest.mark.asyncio
async def test_pipeline_profile_blocked_patterns_become_hooks():
    """ctx.pipeline_profile with blocked_patterns adds PreToolUse hooks to opt_kwargs."""
    captured_kwargs: dict = {}

    async def fake_query(*, prompt, options):
        return
        yield

    def fake_options_cls(**kw):
        captured_kwargs.update(kw)
        return kw

    gen = Generate(prompt="do stuff")
    ctx = PipelineContext()
    ctx.pipeline_profile = CODING  # has blocked_patterns

    with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock()}):
        import claude_agent_sdk as sdk_mod

        sdk_mod.query = fake_query
        sdk_mod.AssistantMessage = type("AssistantMessage", (), {})
        sdk_mod.ResultMessage = type("ResultMessage", (), {})
        sdk_mod.ClaudeAgentOptions = fake_options_cls

        await gen.run(ctx)

    hooks = captured_kwargs.get("hooks", {})
    assert "PreToolUse" in hooks

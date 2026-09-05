"""Tests for SDK content-block → neutral AgentEvent.block mapping.

The claude_agent_sdk is mocked entirely offline; no real Claude calls are made.
"""
from __future__ import annotations

import dataclasses
import sys
import types as stdlib_types

import pytest
from unittest.mock import MagicMock, patch

from norn.agents.base import (
    AgentEvent,
    AgentRequest,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from norn.agents.claude_code import ClaudeCodeProvider


# ---------------------------------------------------------------------------
# Fake SDK types that stand in for claude_agent_sdk classes.
#
# Field sets MUST match the real SDK's field lists exactly — no extra `type`
# attribute.  Confirmed real SDK fields:
#   TextBlock      ['text']
#   ThinkingBlock  ['thinking', 'signature']
#   ToolUseBlock   ['id', 'name', 'input']
#   ToolResultBlock['tool_use_id', 'content', 'is_error']
# ---------------------------------------------------------------------------


class _FakeAssistantMessage:
    """Minimal stand-in for claude_agent_sdk.AssistantMessage."""

    def __init__(self, content: list) -> None:
        self.content = content


class _FakeUserMessage:
    """Minimal stand-in for claude_agent_sdk.UserMessage."""

    def __init__(self, content) -> None:
        self.content = content


class _FakeResultMessage:
    """Minimal stand-in for claude_agent_sdk.ResultMessage."""

    session_id = "sess-blocks-test"
    total_cost_usd = 0.0
    duration_ms = 100
    duration_api_ms = 80
    num_turns = 1
    is_error = False
    structured_output = None
    usage = {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }


class _FakeTextBlock:
    """Stand-in for claude_agent_sdk.types.TextBlock — field: text."""

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeToolUseBlock:
    """Stand-in for claude_agent_sdk.types.ToolUseBlock — fields: id, name, input."""

    def __init__(self, name: str, input_data: dict) -> None:
        self.id = "fake-id"
        self.name = name
        self.input = input_data


class _FakeToolResultBlock:
    """Stand-in for claude_agent_sdk.types.ToolResultBlock — fields: tool_use_id, content, is_error."""

    def __init__(self, content: str, *, is_error: bool = False) -> None:
        self.tool_use_id = "fake-tool-use-id"
        self.content = content
        self.is_error = is_error


class _FakeThinkingBlock:
    """Stand-in for claude_agent_sdk.types.ThinkingBlock — fields: thinking, signature."""

    def __init__(self, thinking: str) -> None:
        self.thinking = thinking
        self.signature = ""


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_sdk(messages: list) -> tuple[MagicMock, MagicMock]:
    """Build minimal claude_agent_sdk and claude_agent_sdk.types mocks.

    Returns:
        (sdk_mock, types_mock) — both must be injected into sys.modules.
    """
    mock_sdk = MagicMock()
    # isinstance() checks in the provider compare against these
    mock_sdk.AssistantMessage = _FakeAssistantMessage
    mock_sdk.UserMessage = _FakeUserMessage
    mock_sdk.ResultMessage = _FakeResultMessage

    async def _fake_query(prompt, options):
        for msg in messages:
            yield msg

    mock_sdk.query = _fake_query

    # claude_agent_sdk.types submodule: isinstance() dispatch in the provider
    # uses the classes imported from here.
    mock_types = stdlib_types.SimpleNamespace(
        TextBlock=_FakeTextBlock,
        ThinkingBlock=_FakeThinkingBlock,
        ToolUseBlock=_FakeToolUseBlock,
        ToolResultBlock=_FakeToolResultBlock,
    )

    return mock_sdk, mock_types


def _make_request(**overrides) -> AgentRequest:
    defaults: dict = dict(
        prompt="test prompt",
        stage_name="test_stage",
        provider="claude-code",
    )
    defaults.update(overrides)
    return AgentRequest(**defaults)


async def _run(messages: list) -> list[AgentEvent]:
    """Drive ClaudeCodeProvider.run() with mocked SDK and collect all events."""
    sdk, sdk_types = _make_sdk(messages)
    with patch.dict(sys.modules, {
        "claude_agent_sdk": sdk,
        "claude_agent_sdk.types": sdk_types,
    }):
        return [e async for e in ClaudeCodeProvider().run(_make_request())]


# ---------------------------------------------------------------------------
# Field-name drift guard — this test catches the original defect
# ---------------------------------------------------------------------------


def test_fake_field_names_match_sdk_classes():
    """Fake block classes must NOT have a 'type' field and must expose the same
    instance attributes the real SDK dataclasses expose.

    This test imports the real SDK (not the mock) and would have caught the
    original bug: fakes had ``type = "tool_use"`` etc. which the real SDK
    dataclasses do not have.
    """
    from claude_agent_sdk.types import (  # real SDK, not patched
        TextBlock as RealTextBlock,
        ThinkingBlock as RealThinkingBlock,
        ToolUseBlock as RealToolUseBlock,
        ToolResultBlock as RealToolResultBlock,
    )

    # Gather field names from real SDK dataclasses
    def _fields(cls) -> set[str]:
        return {f.name for f in dataclasses.fields(cls)}

    real_text_fields = _fields(RealTextBlock)
    real_thinking_fields = _fields(RealThinkingBlock)
    real_tool_use_fields = _fields(RealToolUseBlock)
    real_tool_result_fields = _fields(RealToolResultBlock)

    # None of the real SDK block dataclasses should have a 'type' field.
    # If this assertion fails the SDK changed; update fakes accordingly.
    assert "type" not in real_text_fields, "SDK TextBlock gained 'type'; update fakes"
    assert "type" not in real_thinking_fields, "SDK ThinkingBlock gained 'type'; update fakes"
    assert "type" not in real_tool_use_fields, "SDK ToolUseBlock gained 'type'; update fakes"
    assert "type" not in real_tool_result_fields, "SDK ToolResultBlock gained 'type'; update fakes"

    # Our fakes must also not carry a 'type' class attribute
    assert not hasattr(_FakeTextBlock, "type"), "Fake TextBlock must not have 'type' attribute"
    assert not hasattr(_FakeThinkingBlock, "type"), "Fake ThinkingBlock must not have 'type' attribute"
    assert not hasattr(_FakeToolUseBlock, "type"), "Fake ToolUseBlock must not have 'type' attribute"
    assert not hasattr(_FakeToolResultBlock, "type"), "Fake ToolResultBlock must not have 'type' attribute"

    # Key fields from the real SDK must exist on our fakes
    fake_tool_use = _FakeToolUseBlock("Read", {"file_path": "/x"})
    assert hasattr(fake_tool_use, "name")
    assert hasattr(fake_tool_use, "input")

    fake_thinking = _FakeThinkingBlock("reasoning here")
    assert hasattr(fake_thinking, "thinking")  # SDK field name is 'thinking', not 'text'

    fake_tool_result = _FakeToolResultBlock("output", is_error=False)
    assert hasattr(fake_tool_result, "content")
    assert hasattr(fake_tool_result, "is_error")


# ---------------------------------------------------------------------------
# Text blocks — existing behaviour unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_block_yields_text_event():
    """A text SDK block produces AgentEvent.text."""
    events = await _run([
        _FakeAssistantMessage([_FakeTextBlock("Hello world")]),
        _FakeResultMessage(),
    ])
    text_events = [e for e in events if e.text is not None]
    assert len(text_events) == 1
    assert text_events[0].text == "Hello world"


@pytest.mark.asyncio
async def test_text_block_does_not_emit_block_field():
    """Text blocks do NOT produce AgentEvent.block — only AgentEvent.text."""
    events = await _run([
        _FakeAssistantMessage([_FakeTextBlock("hi")]),
        _FakeResultMessage(),
    ])
    block_events = [e for e in events if e.block is not None]
    assert block_events == []


# ---------------------------------------------------------------------------
# Tool-use blocks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_use_block_yields_tool_use_block():
    """A tool_use SDK block produces AgentEvent(block=ToolUseBlock(...))."""
    events = await _run([
        _FakeAssistantMessage([
            _FakeToolUseBlock("Read", {"file_path": "/src/foo.py"}),
        ]),
        _FakeResultMessage(),
    ])
    block_events = [e for e in events if e.block is not None]
    assert len(block_events) == 1
    blk = block_events[0].block
    assert isinstance(blk, ToolUseBlock)
    assert blk.name == "Read"


@pytest.mark.asyncio
async def test_tool_use_input_summary_extracts_file_path():
    """file_path key in tool input → input_summary equals the file path."""
    events = await _run([
        _FakeAssistantMessage([
            _FakeToolUseBlock("Write", {"file_path": "/project/main.py", "content": "..."}),
        ]),
        _FakeResultMessage(),
    ])
    blk = next(e.block for e in events if e.block is not None)
    assert isinstance(blk, ToolUseBlock)
    assert blk.input_summary == "/project/main.py"


@pytest.mark.asyncio
async def test_tool_use_input_summary_extracts_command():
    """'command' key in tool input → input_summary equals the command string."""
    events = await _run([
        _FakeAssistantMessage([
            _FakeToolUseBlock("Bash", {"command": "ls -la /tmp"}),
        ]),
        _FakeResultMessage(),
    ])
    blk = next(e.block for e in events if e.block is not None)
    assert isinstance(blk, ToolUseBlock)
    assert blk.input_summary == "ls -la /tmp"


@pytest.mark.asyncio
async def test_tool_use_input_summary_falls_back_to_str():
    """No known path key → input_summary is a non-empty truncated str(input)."""
    events = await _run([
        _FakeAssistantMessage([
            _FakeToolUseBlock("Search", {"pattern": "foo", "recursive": True}),
        ]),
        _FakeResultMessage(),
    ])
    blk = next(e.block for e in events if e.block is not None)
    assert isinstance(blk, ToolUseBlock)
    assert blk.input_summary  # non-empty
    assert len(blk.input_summary) <= 200


@pytest.mark.asyncio
async def test_tool_use_input_summary_truncated_to_200():
    """input_summary never exceeds 200 characters."""
    long_value = "x" * 500
    events = await _run([
        _FakeAssistantMessage([
            _FakeToolUseBlock("Bash", {"command": long_value}),
        ]),
        _FakeResultMessage(),
    ])
    blk = next(e.block for e in events if e.block is not None)
    assert isinstance(blk, ToolUseBlock)
    assert len(blk.input_summary) <= 200


# ---------------------------------------------------------------------------
# Tool-result blocks — arrive on UserMessage.content, not AssistantMessage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_result_ok_when_not_error():
    """Successful tool result on UserMessage → ToolResultBlock(ok=True)."""
    events = await _run([
        _FakeUserMessage([_FakeToolResultBlock("file contents here", is_error=False)]),
        _FakeResultMessage(),
    ])
    block_events = [e for e in events if e.block is not None]
    assert len(block_events) == 1
    blk = block_events[0].block
    assert isinstance(blk, ToolResultBlock)
    assert blk.ok is True


@pytest.mark.asyncio
async def test_tool_result_not_ok_when_error():
    """Failed tool result on UserMessage → ToolResultBlock(ok=False)."""
    events = await _run([
        _FakeUserMessage([_FakeToolResultBlock("Permission denied", is_error=True)]),
        _FakeResultMessage(),
    ])
    blk = next(e.block for e in events if e.block is not None)
    assert isinstance(blk, ToolResultBlock)
    assert blk.ok is False


@pytest.mark.asyncio
async def test_tool_result_summary_from_string_content():
    """String content in tool result → summary contains that string."""
    events = await _run([
        _FakeUserMessage([_FakeToolResultBlock("the tool output", is_error=False)]),
        _FakeResultMessage(),
    ])
    blk = next(e.block for e in events if e.block is not None)
    assert isinstance(blk, ToolResultBlock)
    assert "the tool output" in blk.summary


@pytest.mark.asyncio
async def test_user_message_plain_string_content_emits_no_blocks():
    """A UserMessage with plain string content (no list) emits no block events."""
    events = await _run([
        _FakeUserMessage("raw string content"),
        _FakeResultMessage(),
    ])
    block_events = [e for e in events if e.block is not None]
    assert block_events == []


# ---------------------------------------------------------------------------
# Thinking blocks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thinking_block_yields_thinking_block():
    """A thinking SDK block produces AgentEvent(block=ThinkingBlock(...))."""
    events = await _run([
        _FakeAssistantMessage([
            _FakeThinkingBlock("Let me think step by step..."),
        ]),
        _FakeResultMessage(),
    ])
    block_events = [e for e in events if e.block is not None]
    assert len(block_events) == 1
    blk = block_events[0].block
    assert isinstance(blk, ThinkingBlock)
    # SDK ThinkingBlock.thinking maps to norn ThinkingBlock.text
    assert blk.text == "Let me think step by step..."


# ---------------------------------------------------------------------------
# Mixed blocks — order preserved across message types
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mixed_blocks_emitted_in_stream_order():
    """Block events across AssistantMessage and UserMessage are emitted in stream order."""
    events = await _run([
        _FakeAssistantMessage([
            _FakeThinkingBlock("plan here"),
            _FakeTextBlock("I'll help you."),
            _FakeToolUseBlock("Read", {"file_path": "/x.py"}),
        ]),
        # Tool result arrives on UserMessage (real SDK behaviour)
        _FakeUserMessage([_FakeToolResultBlock("file content", is_error=False)]),
        _FakeResultMessage(),
    ])

    # Text event present
    text_events = [e for e in events if e.text is not None]
    assert text_events[0].text == "I'll help you."

    # Block events: ThinkingBlock → ToolUseBlock → ToolResultBlock (stream order)
    block_events = [e for e in events if e.block is not None]
    assert len(block_events) == 3
    block_type_names = [type(e.block).__name__ for e in block_events]
    assert block_type_names == ["ThinkingBlock", "ToolUseBlock", "ToolResultBlock"]


@pytest.mark.asyncio
async def test_thinking_block_precedes_tool_use_block_in_stream():
    """ThinkingBlock event appears before ToolUseBlock event in the stream."""
    events = await _run([
        _FakeAssistantMessage([
            _FakeThinkingBlock("thinking text"),
            _FakeToolUseBlock("Edit", {"file_path": "/a.py"}),
        ]),
        _FakeResultMessage(),
    ])
    indices = {type(e.block).__name__: i for i, e in enumerate(events) if e.block is not None}
    assert indices["ThinkingBlock"] < indices["ToolUseBlock"]


# ---------------------------------------------------------------------------
# No block events for non-block messages (ResultMessage only)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_result_message_only_no_block_events():
    """A stream with only a ResultMessage produces no block events."""
    events = await _run([_FakeResultMessage()])
    assert all(e.block is None for e in events)

from __future__ import annotations

import pytest

from norn.agents.base import (
    AgentEvent,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)


# ---------------------------------------------------------------------------
# Block dataclass construction
# ---------------------------------------------------------------------------


def test_text_block():
    b = TextBlock(text="hello")
    assert b.text == "hello"


def test_tool_use_block():
    b = ToolUseBlock(name="Edit", input_summary="src/x.py")
    assert b.name == "Edit"
    assert b.input_summary == "src/x.py"


def test_tool_result_block_ok():
    b = ToolResultBlock(ok=True, summary="written 42 bytes")
    assert b.ok is True
    assert b.summary == "written 42 bytes"


def test_tool_result_block_error():
    b = ToolResultBlock(ok=False, summary="permission denied")
    assert b.ok is False


def test_thinking_block():
    b = ThinkingBlock(text="let me think…")
    assert b.text == "let me think…"


def test_blocks_are_frozen():
    """Frozen dataclasses must not allow attribute mutation."""
    b = TextBlock(text="x")
    with pytest.raises((AttributeError, TypeError)):
        b.text = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# AgentEvent backward-compatibility
# ---------------------------------------------------------------------------


def test_agent_event_default_construction():
    """AgentEvent() with no args must still work; block defaults to None."""
    ev = AgentEvent()
    assert ev.text is None
    assert ev.session_id is None
    assert ev.usage is None
    assert ev.structured_output is None
    assert ev.artifact is None
    assert ev.block is None


def test_agent_event_text_only():
    """Existing text-only construction is unaffected."""
    ev = AgentEvent(text="hi")
    assert ev.text == "hi"
    assert ev.block is None


def test_agent_event_carries_tool_use_block():
    b = ToolUseBlock(name="Edit", input_summary="src/x.py")
    ev = AgentEvent(block=b)
    assert ev.block is b
    assert isinstance(ev.block, ToolUseBlock)
    assert ev.block.name == "Edit"


def test_agent_event_carries_tool_result_block():
    b = ToolResultBlock(ok=True, summary="done")
    ev = AgentEvent(block=b)
    assert ev.block is b


def test_agent_event_carries_thinking_block():
    b = ThinkingBlock(text="thinking…")
    ev = AgentEvent(block=b)
    assert ev.block is b


def test_agent_event_carries_text_block():
    b = TextBlock(text="streamed chunk")
    ev = AgentEvent(text="streamed chunk", block=b)
    assert ev.text == "streamed chunk"
    assert ev.block is b

from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from norn.agents.base import (
    AgentError,
    AgentEvent,
    AgentProvider,
    AgentRequest,
    AgentUsage,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from norn.agents.capabilities import AgentCapabilities, CostMode
from norn.agents.models import resolve_model

log = logging.getLogger(__name__)

# The SDK emits an INFO-level "Using bundled Claude Code CLI: <path>" line on
# every transport spawn.  Pin the SDK logger to WARNING to keep real errors but
# drop the spawn chatter.
logging.getLogger("claude_agent_sdk").setLevel(logging.WARNING)

_INPUT_SUMMARY_MAX = 200
_RESULT_SUMMARY_MAX = 200

# Ordered list of input-dict keys to try when building a short summary for a
# tool-use block.  Earlier entries are preferred over later ones.
_PREFERRED_INPUT_KEYS = ("file_path", "path", "command", "query")


def _build_input_summary(name: str, input_data: dict) -> str:
    """Return a short, redaction-friendly summary of a tool's input dict."""
    for key in _PREFERRED_INPUT_KEYS:
        if key in input_data:
            return str(input_data[key])[:_INPUT_SUMMARY_MAX]
    return str(input_data)[:_INPUT_SUMMARY_MAX]


def _build_result_summary(content: Any) -> str:
    """Extract a short text summary from tool-result content."""
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(item.get("text", ""))
            elif hasattr(item, "text"):
                parts.append(item.text)
        text = " ".join(p for p in parts if p)
    elif isinstance(content, str):
        text = content
    else:
        text = str(content) if content is not None else ""
    return text[:_RESULT_SUMMARY_MAX]


class ClaudeCodeError(AgentError):
    """Wraps exceptions from the Claude Agent SDK with captured stderr context.

    Extends ``AgentError`` so ``Generate.run()`` can catch all provider errors
    uniformly via the base class.
    """


class ClaudeCodeProvider:
    """AgentProvider implementation backed by the Claude Agent SDK.

    Translates an ``AgentRequest`` into ``claude_agent_sdk`` calls, streaming
    ``AgentEvent`` objects back to the caller. Handles session resumption,
    fork semantics, artifact tracking via PostToolUse hooks, and stderr capture.
    """

    name: str = "claude-code"

    capabilities: AgentCapabilities = AgentCapabilities(
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
        model_alias_table="claude-code",
        can_list_models=False,
        supports_setting_sources=True,
    )

    async def run(self, request: AgentRequest) -> AsyncIterator[AgentEvent]:
        """Execute a Claude Agent SDK query, yielding provider-neutral events."""
        try:
            from claude_agent_sdk import (
                AssistantMessage,
                ClaudeAgentOptions,
                ResultMessage,
                UserMessage,
                query,
            )
            from claude_agent_sdk.types import (
                TextBlock as SdkTextBlock,
                ThinkingBlock as SdkThinkingBlock,
                ToolResultBlock as SdkToolResultBlock,
                ToolUseBlock as SdkToolUseBlock,
            )
        except ImportError as exc:
            raise ImportError(
                "claude-agent-sdk is not installed. Install with: uv add claude-agent-sdk"
            ) from exc

        # Resolve model alias to provider-facing ID
        resolved_model = resolve_model("claude-code", request.model)

        # Artifact tracking state (shared with the hook closure)
        artifacts: list[str] = []
        stderr_lines: list[str] = []

        async def _track_artifacts(input_data: dict, tool_use_id: str, context: Any) -> dict:
            if input_data.get("tool_name") in ("Write", "Edit", "NotebookEdit"):
                file_path = input_data.get("tool_input", {}).get("file_path")
                if file_path and file_path not in artifacts:
                    artifacts.append(file_path)
            return {}

        # Build options kwargs
        opt_kwargs: dict[str, Any] = {}
        if request.session_id:
            opt_kwargs["resume"] = request.session_id
        if request.fork_session and request.session_id:
            opt_kwargs["fork_session"] = True
        if request.allowed_tools:
            opt_kwargs["allowed_tools"] = request.allowed_tools
        if request.permission_mode:
            opt_kwargs["permission_mode"] = request.permission_mode
        if request.max_turns is not None:
            opt_kwargs["max_turns"] = request.max_turns
        if request.cwd:
            opt_kwargs["cwd"] = request.cwd
        if request.setting_sources:
            opt_kwargs["setting_sources"] = request.setting_sources
        if request.add_dirs:
            opt_kwargs["add_dirs"] = request.add_dirs
        if resolved_model:
            opt_kwargs["model"] = resolved_model
        if request.thinking:
            opt_kwargs["thinking"] = request.thinking
        if request.system_prompt:
            opt_kwargs["system_prompt"] = request.system_prompt
        if request.output_format:
            opt_kwargs["output_format"] = request.output_format
        # MCP servers: mcp_tools takes precedence (server is created here);
        # explicit mcp_servers is supported for direct pass-through.
        if request.mcp_tools:
            from claude_agent_sdk import create_sdk_mcp_server
            mcp_server = create_sdk_mcp_server(request.stage_name, tools=request.mcp_tools)
            all_mcp_servers: dict = {request.stage_name: mcp_server}
            if request.mcp_servers:
                all_mcp_servers.update(request.mcp_servers)
            opt_kwargs["mcp_servers"] = all_mcp_servers
        elif request.mcp_servers:
            opt_kwargs["mcp_servers"] = request.mcp_servers
        if request.env:
            opt_kwargs["env"] = request.env

        opt_kwargs["stderr"] = lambda line: stderr_lines.append(line)

        # Build hooks: merge caller-provided hooks with artifact tracker
        artifact_hook = {"hooks": [_track_artifacts]}
        if request.hooks:
            merged = dict(request.hooks)
            existing = merged.get("PostToolUse", [])
            merged["PostToolUse"] = [*existing, artifact_hook]
            opt_kwargs["hooks"] = merged
        else:
            opt_kwargs["hooks"] = {"PostToolUse": [artifact_hook]}

        options = ClaudeAgentOptions(**opt_kwargs)

        session_id: str | None = None

        try:
            async for message in query(prompt=request.prompt, options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, SdkToolUseBlock):
                            yield AgentEvent(block=ToolUseBlock(
                                name=block.name,
                                input_summary=_build_input_summary(block.name, block.input or {}),
                            ))
                        elif isinstance(block, SdkThinkingBlock):
                            # SDK field is 'thinking'; norn's ThinkingBlock field is 'text'
                            yield AgentEvent(block=ThinkingBlock(text=block.thinking))
                        elif isinstance(block, SdkTextBlock):
                            yield AgentEvent(text=block.text)
                        else:
                            # Log unrecognised blocks so SDK shape changes are visible
                            log.debug(
                                "[claude-code] Unrecognised AssistantMessage block type %s; skipping",
                                type(block).__name__,
                            )

                elif isinstance(message, UserMessage):
                    # Tool results arrive on UserMessage.content, not AssistantMessage
                    content = message.content
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, SdkToolResultBlock):
                                yield AgentEvent(block=ToolResultBlock(
                                    ok=not block.is_error,
                                    summary=_build_result_summary(block.content),
                                ))
                            else:
                                log.debug(
                                    "[claude-code] Unrecognised UserMessage block type %s; skipping",
                                    type(block).__name__,
                                )
                    # Plain-string UserMessage.content carries no structured blocks

                elif isinstance(message, ResultMessage):
                    session_id = message.session_id
                    yield AgentEvent(session_id=message.session_id)

                    # Emit structured output if present
                    if hasattr(message, "structured_output") and message.structured_output is not None:
                        yield AgentEvent(structured_output=message.structured_output)

                    # Emit usage
                    usage = AgentUsage(
                        provider="claude-code",
                        model=request.model,
                        session_id=message.session_id,
                        total_cost_usd=message.total_cost_usd or 0.0,
                        duration_ms=message.duration_ms,
                        duration_api_ms=message.duration_api_ms,
                        num_turns=message.num_turns,
                        is_error=message.is_error,
                    )
                    if message.usage:
                        usage.input_tokens = message.usage.get("input_tokens", 0)
                        usage.output_tokens = message.usage.get("output_tokens", 0)
                        usage.cache_read_input_tokens = message.usage.get("cache_read_input_tokens", 0)
                        usage.cache_creation_input_tokens = message.usage.get("cache_creation_input_tokens", 0)
                    yield AgentEvent(usage=usage)

                else:
                    # Capture session_id from init/system messages
                    if hasattr(message, "session_id") and message.session_id:
                        if not session_id:
                            session_id = message.session_id
                            log.debug(
                                "[claude-code] Captured session_id from %s: %s",
                                type(message).__name__,
                                message.session_id,
                            )
                            yield AgentEvent(session_id=message.session_id)

                    # Forward text content from non-standard messages
                    if hasattr(message, "content"):
                        for block in message.content:
                            if hasattr(block, "text"):
                                yield AgentEvent(text=block.text)
                    elif isinstance(message, str):
                        yield AgentEvent(text=message)
        except ImportError:
            raise
        except Exception as exc:
            raise ClaudeCodeError(exc, stderr_lines) from exc

        # Emit artifacts collected by the hook
        for artifact_path in artifacts:
            yield AgentEvent(artifact=artifact_path)

from __future__ import annotations

import functools
import logging
import os
import re
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any, AsyncIterator

from norn.agents import session_totals
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
from norn.agents.session_totals import SessionTotals
from norn.model_labels import resolve_label

log = logging.getLogger(__name__)

# The SDK emits an INFO-level "Using bundled Claude Code CLI: <path>" line on
# every transport spawn.  Pin the SDK logger to WARNING to keep real errors but
# drop the spawn chatter.
logging.getLogger("claude_agent_sdk").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Which `claude` binary to spawn
# ---------------------------------------------------------------------------

# The SDK's own search prefers the CLI bundled inside the installed
# claude-agent-sdk wheel and only falls back to PATH when that file is absent.
# The bundle is frozen at whatever version the wheel shipped with, so a norn
# upgrade cycle away from the CLI the user actually installed is invisible until
# a flag stops working mid-run: sdk 0.1.50 bundles CLI 2.1.81, which rejects
# `--effort xhigh` that the `deep` label sends and that CLI 2.1.263 accepts.
# norn therefore picks the *newer* of the two and says which one it chose.
CLI_ENV_VAR = "NORN_CLAUDE_CLI"

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")

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


def cli_version(path: str) -> tuple[int, int, int] | None:
    """Return the version *path* reports, or ``None`` when it cannot be read.

    Any failure — missing binary, non-zero exit, unparseable output, a hang —
    is a "don't know", not an error: the caller then prefers a candidate whose
    version it could read, and falls back to the SDK's own search if neither
    answers.
    """
    try:
        result = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    match = _VERSION_RE.search(result.stdout)
    if not match:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def _bundled_cli() -> str | None:
    """Return the CLI bundled in the installed SDK, mirroring the SDK's own lookup.

    Every failure yields ``None``: the SDK may be absent, may be a namespace
    package with no usable ``__file__``, or may be a test double standing in for
    it. None of those are worth failing an agent call over — the caller simply
    has one candidate fewer.
    """
    try:
        import claude_agent_sdk

        name = "claude.exe" if os.name == "nt" else "claude"
        path = Path(claude_agent_sdk.__file__).parent / "_bundled" / name
        return str(path) if path.is_file() else None
    except (ImportError, AttributeError, TypeError, OSError):
        return None


@functools.cache
def resolve_cli_path() -> str | None:
    """Return the CLI norn should spawn, or ``None`` to let the SDK choose.

    ``NORN_CLAUDE_CLI`` wins outright and is not version-checked — an operator
    naming a binary means that binary. Otherwise the CLI on PATH and the one
    bundled in the SDK are compared by version and the newer wins, because the
    two drift apart independently: the bundle moves when the SDK is upgraded,
    PATH moves when the user updates Claude Code.

    Cached: the two ``--version`` probes run once per process, and the chosen
    path is logged once rather than on every agent call.
    """
    override = os.environ.get(CLI_ENV_VAR)
    if override:
        log.info("[claude-code] CLI from %s: %s", CLI_ENV_VAR, override)
        return override

    candidates = [
        (label, path)
        for label, path in (("PATH", shutil.which("claude")), ("SDK bundle", _bundled_cli()))
        if path
    ]
    versioned = [
        (version, label, path)
        for label, path in candidates
        if (version := cli_version(path)) is not None
    ]
    if not versioned:
        # Nothing answered `--version`. Let the SDK run its own search so its
        # install instructions are what the user sees.
        return None

    version, label, path = max(versioned)
    log.info(
        "[claude-code] CLI: %s (%s, %s)",
        path,
        label,
        ".".join(str(part) for part in version),
    )
    return path


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
        reports_result_subtype=True,
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
        if (cli_path := resolve_cli_path()) is not None:
            opt_kwargs["cli_path"] = cli_path
        if request.setting_sources:
            opt_kwargs["setting_sources"] = request.setting_sources
        if request.add_dirs:
            opt_kwargs["add_dirs"] = request.add_dirs
        if request.model:
            r = resolve_label("claude-code", request.model)
            opt_kwargs["model"] = r.model
            opt_kwargs["effort"] = r.effort   # always present for claude-code
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
        model_id: str | None = None
        # Running totals of the session this call resumes or forks (zero for
        # a fresh session); a fork starts from its parent's totals.
        prior = session_totals.get(request.session_id)
        call_usage = AgentUsage(provider="claude-code", model=request.model)

        try:
            async for message in query(prompt=request.prompt, options=options):
                if isinstance(message, AssistantMessage):
                    if model_id is None and hasattr(message, "model") and message.model:
                        model_id = message.model
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

                    # One call can produce several ResultMessages: a background
                    # task or subagent finishing wakes the agent for another
                    # round, and each round ends with its own result.  Tokens,
                    # turns and wall time are per round, so they add up; cost
                    # and API time are session running totals, so the call's
                    # share is the latest total minus what the session had
                    # already spent before this call.
                    call_usage.session_id = message.session_id
                    call_usage.duration_ms += message.duration_ms
                    call_usage.num_turns += message.num_turns
                    call_usage.is_error = message.is_error
                    call_usage.model_id = model_id
                    call_usage.result_subtype = message.subtype if hasattr(message, "subtype") else None
                    call_usage.stop_reason = message.stop_reason if hasattr(message, "stop_reason") else None
                    if message.usage:
                        call_usage.input_tokens += message.usage.get("input_tokens", 0)
                        call_usage.output_tokens += message.usage.get("output_tokens", 0)
                        call_usage.cache_read_input_tokens += message.usage.get("cache_read_input_tokens", 0)
                        call_usage.cache_creation_input_tokens += message.usage.get("cache_creation_input_tokens", 0)
                    running = SessionTotals(
                        cost_usd=message.total_cost_usd or 0.0,
                        duration_api_ms=message.duration_api_ms,
                    )
                    session_totals.record(message.session_id, running)
                    call_usage.total_cost_usd = running.cost_usd - prior.cost_usd
                    call_usage.duration_api_ms = running.duration_api_ms - prior.duration_api_ms
                    yield AgentEvent(usage=replace(call_usage))

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

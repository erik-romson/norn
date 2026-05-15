from __future__ import annotations

import os
from typing import TYPE_CHECKING

from norn.agents.base import AgentError, AgentEvent, AgentProvider, AgentRequest, AgentUsage
from norn.agents.claude_code import ClaudeCodeProvider
from norn.agents.opencode import OpenCodeProvider
from norn.agents.registry import register

if TYPE_CHECKING:
    from norn.dsl import Pipeline

__all__ = [
    "AgentError",
    "AgentEvent",
    "AgentProvider",
    "AgentRequest",
    "AgentUsage",
    "ClaudeCodeProvider",
    "OpenCodeProvider",
    "resolve_agent_provider",
]

# Register providers on import.
register(ClaudeCodeProvider())
register(OpenCodeProvider())


def resolve_agent_provider(pipeline: "Pipeline", cli_provider: str | None = None) -> str:
    """Resolve the agent provider using the configured priority order.

    Priority (highest to lowest):

    1. ``cli_provider`` — explicit ``--agent-provider`` CLI flag.
    2. ``NORN_AGENT_PROVIDER`` environment variable.
    3. ``pipeline.agent_provider_name`` — set via ``Pipeline.agent_provider()``.
    4. ``"claude-code"`` — backward-compatible default.

    Args:
        pipeline: The pipeline definition being run.
        cli_provider: Provider name from the CLI flag, or ``None``.

    Returns:
        The resolved provider name string.
    """
    if cli_provider is not None:
        return cli_provider
    env_provider = os.environ.get("NORN_AGENT_PROVIDER")
    if env_provider:
        return env_provider
    if pipeline.agent_provider_name:
        return pipeline.agent_provider_name
    return "claude-code"

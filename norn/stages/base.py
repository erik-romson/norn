from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from norn.models import PipelineContext, StageResult


class BaseStage(ABC):
    """Abstract base for all pipeline stages.

    Subclass this to create a new stage type. Set ``needs_agent = True`` for
    stages that call the Claude Agent SDK (e.g. Generate). Non-agent stages
    (ReadFile, RunCommand) must not import claude-agent-sdk.

    The runner calls ``run(ctx, **kwargs)`` and stores the returned
    ``StageResult`` on the pipeline context keyed by stage name.

    Attributes:
        needs_agent: When ``True`` the runner passes extra kwargs
            (``session_id``, ``attempt``, ``fork_session``, ``mcp_servers``)
            so the stage can manage Claude SDK sessions.
        emits_events: When ``True`` the runner passes ``node_id`` and
            ``attempt`` so the stage can emit its own run-events (e.g.
            ``CommandOutput``) keyed to the same graph node as the
            ``StageStarted``/``StageFinished`` events the runner emits around
            it.  Agent stages get ``node_id`` via the agent kwargs instead, so
            this flag is only for non-agent stages.
        mcp_tools: List of ``SdkMcpTool`` instances (created with the
            ``@tool`` decorator) that should be available to the agent
            during this stage. The runner creates an MCP server from these
            tools and passes it as ``mcp_servers`` to the stage's ``run()``.
    """

    needs_agent: bool = False
    emits_events: bool = False
    mcp_tools: list[Any] = []

    @abstractmethod
    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        """Execute this stage. Return success/failure + structured output.

        Kwargs are used by the runner to pass session info to agent-backed stages.
        Non-agent stages can safely ignore them.
        """
        ...

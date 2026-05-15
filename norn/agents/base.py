from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator, Protocol, runtime_checkable

if TYPE_CHECKING:
    from norn.agents.permissions import AgentPermissions


class AgentError(Exception):
    """Provider-neutral wrapper for errors raised during an agent call.

    Provider adapters catch their own exceptions and re-raise as
    ``AgentError`` (or a subclass) so that ``Generate.run()`` can handle
    errors without importing provider-specific types.

    Attributes:
        original: The underlying exception from the provider SDK.
        stderr_lines: Lines captured from stderr before the error (may be empty).
    """

    def __init__(self, original: Exception, stderr_lines: list[str] | None = None) -> None:
        self.original = original
        self.stderr_lines = stderr_lines or []
        super().__init__(str(original))


@dataclass
class AgentRequest:
    """Provider-neutral description of a single agent invocation.

    Passed by the runner to whichever ``AgentProvider`` is selected for the
    pipeline. Fields mirror the knobs exposed by ``Generate`` and are mapped
    to provider-specific options inside each adapter.
    """

    prompt: str
    stage_name: str
    provider: str
    model: str | None = None
    session_id: str | None = None
    fork_session: bool = False
    allowed_tools: list[str] | None = None
    permission_mode: str | None = None
    max_turns: int | None = None
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    system_prompt: str | None = None
    output_format: dict[str, Any] | None = None
    thinking: dict[str, Any] | None = None
    attempt: int = 1
    add_dirs: list[str] | None = None
    setting_sources: list[str] | None = None
    hooks: dict[str, Any] | None = None
    mcp_servers: dict[str, Any] | None = None
    mcp_tools: list[Any] | None = None
    permissions: AgentPermissions | None = None


@dataclass
class AgentUsage:
    """Provider-neutral token and cost usage for a single agent call.

    Each provider adapter populates this from its own result type and
    returns it inside the terminal ``AgentEvent``.
    """

    provider: str
    model: str | None = None
    session_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    total_cost_usd: float = 0.0
    duration_ms: int = 0
    duration_api_ms: int = 0
    num_turns: int = 0
    is_error: bool = False


@dataclass
class AgentEvent:
    """A single event emitted by a provider during an agent call.

    Providers yield a stream of these. Text chunks arrive with ``text`` set;
    the final event carries ``usage`` (and optionally ``structured_output`` or
    ``artifact``).
    """

    text: str | None = None
    session_id: str | None = None
    usage: AgentUsage | None = None
    structured_output: Any = None
    artifact: str | None = None


@runtime_checkable
class AgentProvider(Protocol):
    """Contract that every provider adapter must satisfy.

    Adapters are registered by name in ``norn.agents.registry`` and looked
    up at pipeline start time based on the resolved provider selection.
    """

    name: str

    async def run(self, request: AgentRequest) -> AsyncIterator[AgentEvent]:
        ...

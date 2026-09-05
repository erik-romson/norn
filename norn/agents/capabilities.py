"""Structured capability descriptor for agent providers.

Each provider exposes a ``capabilities`` attribute that is an
``AgentCapabilities`` instance reflecting what the provider can actually do.
Consumers use this to gate features rather than comparing provider name strings.

This module has no dependencies on ``norn.contrib`` or any provider SDK.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from norn.agents.base import AgentRequest


class CostMode(Enum):
    """How an agent provider tracks monetary cost.

    ``TRACKED``
        The provider reports accurate per-run cost in USD (e.g. claude-code
        with an API key).

    ``ZERO_UNKNOWN``
        The provider may report zero cost even when tokens are consumed —
        either because the billing is unknown at the transport layer or
        because the underlying service uses a subscription model where per-call
        cost is not surfaced (e.g. opencode with GitHub Copilot).

    ``SUBSCRIPTION``
        The provider is known to be subscription-billed; cost is always 0 in
        the event stream.
    """

    TRACKED = auto()
    ZERO_UNKNOWN = auto()
    SUBSCRIPTION = auto()


@dataclass(frozen=True)
class AgentCapabilities:
    """Structured descriptor for the capabilities of an agent provider.

    Attach one of these to each ``AgentProvider`` as a ``capabilities``
    attribute.  Consumers gate features on the descriptor's fields rather than
    branching on provider name strings.

    Attributes:
        block_kinds:
            The set of ``AgentMessageBlock`` subtype names that this provider
            can emit.  Possible values: ``"text"``, ``"tool_use"``,
            ``"tool_result"``, ``"thinking"``.

        cost_mode:
            How cost tracking works for this provider (see ``CostMode``).

        supports_structured_output:
            ``True`` when the provider honours ``AgentRequest.output_format``
            and returns a populated ``AgentEvent.structured_output``.

        supports_fork:
            ``True`` when ``AgentRequest.fork_session=True`` is safe to pass
            to the provider.

        supports_hooks:
            ``True`` when ``AgentRequest.hooks`` is honoured by the provider.

        supports_mcp:
            ``True`` when ``AgentRequest.mcp_servers`` / ``mcp_tools`` are
            honoured by the provider.

        supports_thinking:
            ``True`` when ``AgentRequest.thinking`` budgets are honoured by
            the provider.

        file_edit_without_terminal:
            ``True`` when the provider can grant file-edit permissions without
            also granting terminal/shell permissions.

        session_resumable:
            ``True`` when the provider supports resuming a previous session via
            ``AgentRequest.session_id``.

        session_forkable:
            ``True`` when the provider supports forking (branching) a session.

        session_attachable:
            ``True`` when the provider supports interactive terminal attach
            (Ratatosk-era ``tmux attach`` style).  Always ``False`` in the
            current single-unit implementation.

        live_model_switch:
            ``True`` when the provider supports changing the model mid-run
            without restarting the session.

        model_alias_table:
            Key into ``norn.agents.models.MODEL_ALIASES`` that maps friendly
            shorthand names (e.g. ``"sonnet"``) to provider-specific model IDs.

        can_list_models:
            ``True`` when the provider exposes a way to enumerate available
            models at runtime.

        supports_setting_sources:
            ``True`` when the provider honours non-``"project"``
            ``setting_sources`` passed in ``AgentRequest.setting_sources``.
            ``"project"`` is always resolved by Norn before the request
            reaches the provider, so this flag only covers additional
            provider-specific sources (e.g. ``"user"``).
    """

    block_kinds: frozenset[str]
    cost_mode: CostMode
    supports_structured_output: bool
    supports_fork: bool
    supports_hooks: bool
    supports_mcp: bool
    supports_thinking: bool
    file_edit_without_terminal: bool
    session_resumable: bool
    session_forkable: bool
    session_attachable: bool
    live_model_switch: bool
    model_alias_table: str
    can_list_models: bool
    # Default False so existing capability declarations stay valid; real
    # providers that support non-portable setting_sources set this explicitly.
    supports_setting_sources: bool = False


def validate_capabilities(
    request: "AgentRequest",
    capabilities: AgentCapabilities,
    provider_name: str,
) -> None:
    """Validate that *request* only uses features supported by *capabilities*.

    Raises ``ValueError`` describing the first unsupported feature found, so
    the caller can wrap it in a provider-specific error type or surface it as a
    :class:`~norn.models.StageResult` failure.

    Args:
        request: The provider-neutral agent request to validate.
        capabilities: The capability descriptor for the target provider.
        provider_name: Human-readable provider name used in error messages.

    Raises:
        ValueError: When the request uses a feature not declared by
            *capabilities*.
    """
    if request.output_format is not None and not capabilities.supports_structured_output:
        raise ValueError(
            f"Structured output (output_format) is not supported by the {provider_name} provider. "
            "Remove output_format or switch to a provider that supports it."
        )
    if request.fork_session and not capabilities.supports_fork:
        raise ValueError(
            f"fork_session is not supported by the {provider_name} provider. "
            f"{provider_name} does not expose fork semantics."
        )
    if request.setting_sources and not capabilities.supports_setting_sources:
        raise ValueError(
            f"setting_sources {request.setting_sources!r} are not supported by the {provider_name} provider. "
            "Only 'project' is portable and must be resolved by Norn before reaching the provider."
        )
    if request.hooks and not capabilities.supports_hooks:
        raise ValueError(
            f"hooks are not supported by the {provider_name} provider. "
            "Hooks are a Claude Code SDK feature."
        )
    if request.mcp_servers and not capabilities.supports_mcp:
        raise ValueError(
            f"mcp_servers are not supported by the {provider_name} provider. "
            "MCP server configuration is provider-specific."
        )
    if request.mcp_tools and not capabilities.supports_mcp:
        raise ValueError(
            f"mcp_tools are not supported by the {provider_name} provider. "
            "MCP tool injection is a Claude Code SDK feature."
        )
    if request.thinking and not capabilities.supports_thinking:
        raise ValueError(
            f"thinking budget is not supported by the {provider_name} provider. "
            f"{provider_name} does not expose a thinking/reasoning budget parameter."
        )

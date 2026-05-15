from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from norn.agents.base import AgentProvider

_registry: dict[str, "AgentProvider"] = {}


def register(provider: "AgentProvider") -> None:
    """Register a provider instance by its ``name`` attribute.

    Args:
        provider: An object satisfying the ``AgentProvider`` protocol.
    """
    _registry[provider.name] = provider


def get_provider(name: str) -> "AgentProvider":
    """Return the registered provider for *name*.

    Args:
        name: Provider name, e.g. ``"claude-code"`` or ``"opencode"``.

    Returns:
        The registered ``AgentProvider`` instance.

    Raises:
        ValueError: When no provider with *name* has been registered.
    """
    if name not in _registry:
        known = ", ".join(sorted(_registry)) or "(none)"
        raise ValueError(
            f"Unknown agent provider {name!r}. "
            f"Registered providers: {known}"
        )
    return _registry[name]

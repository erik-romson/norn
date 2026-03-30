from __future__ import annotations

import logging
from importlib.metadata import entry_points

from norn.stages.base import BaseStage

log = logging.getLogger(__name__)

_registry: dict[str, type[BaseStage]] = {}
_discovered = False


def discover_stages() -> dict[str, type[BaseStage]]:
    """Scan installed packages for norn.stages entry points."""
    global _discovered
    if _discovered:
        return _registry
    for ep in entry_points(group="norn.stages"):
        try:
            cls = ep.load()
        except Exception:
            log.warning(
                "Failed to load stage plugin %r from %s",
                ep.name,
                ep.dist.name,
                exc_info=True,
            )
            continue
        if not (isinstance(cls, type) and issubclass(cls, BaseStage)):
            log.warning(
                "Stage plugin %r (%s) is not a BaseStage subclass, skipping",
                ep.name,
                cls,
            )
            continue
        if ep.name in _registry:
            log.warning(
                "Duplicate stage plugin name %r — %s overrides %s",
                ep.name,
                cls,
                _registry[ep.name],
            )
        _registry[ep.name] = cls
    _discovered = True
    return _registry


def get_stage_class(name: str) -> type[BaseStage]:
    """Lookup a stage class by registered name."""
    discover_stages()
    if name not in _registry:
        raise KeyError(
            f"Unknown stage type: {name!r}. "
            f"Available: {sorted(_registry.keys())}"
        )
    return _registry[name]


def reset_registry() -> None:
    """Clear registry (for testing)."""
    global _discovered
    _registry.clear()
    _discovered = False

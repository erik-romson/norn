"""Resolution of per-pipeline runtime state (history / checkpoint) paths.

A pipeline's ``.history`` and ``.checkpoint`` live beside a *state key* derived
from how the pipeline was referenced. External ``.py`` files outside the cwd
use a cwd-local key first with the original location as a read fallback;
everything else uses the reference as-is. Shared by ``norn/cli.py`` (run /
history / resume) and the TUI history browser so both resolve identically.
"""
from __future__ import annotations

from pathlib import Path

from norn.checkpoint import Checkpoint, load_checkpoint
from norn.history import load_history


def _state_key_candidates(config_arg: str, *, cwd: Path | None = None) -> list[str]:
    """Return preferred state-key paths for history/checkpoint files.

    External pipeline files that live outside the current working directory use
    a cwd-local state key first, with the legacy config-adjacent location as a
    fallback for reads.
    """
    cwd_path = (cwd or Path.cwd()).resolve()
    raw = Path(config_arg)
    if raw.suffix == ".py" and raw.exists():
        resolved = raw.resolve()
        if resolved.is_relative_to(cwd_path):
            return [str(resolved)]
        return [str((cwd_path / resolved.name).resolve()), str(resolved)]
    return [config_arg]


def _primary_state_key(config_arg: str, *, cwd: Path | None = None) -> str:
    """Return the preferred state key for new checkpoint/history writes."""
    return _state_key_candidates(config_arg, cwd=cwd)[0]


def _load_checkpoint_for_config(config_arg: str, *, cwd: Path | None = None) -> Checkpoint | None:
    """Load a checkpoint using primary state resolution with legacy fallback."""
    for candidate in _state_key_candidates(config_arg, cwd=cwd):
        checkpoint = load_checkpoint(candidate)
        if checkpoint is not None:
            return checkpoint
    return None


def _load_history_for_config(config_arg: str, *, cwd: Path | None = None) -> list:
    """Load history using primary state resolution with legacy fallback."""
    for candidate in _state_key_candidates(config_arg, cwd=cwd):
        records = load_history(candidate)
        if records:
            return records
    return load_history(_primary_state_key(config_arg, cwd=cwd))

"""Pipeline loading/setup helpers for the TUI launcher flow.

Textbook-free (no textual imports) so it can be unit-tested directly and
shared between the navigation app (:class:`norn.tui.app.NornUIApp`) and any
CLI direct-launch path. Imports core ``norn`` only.
"""
from __future__ import annotations

import importlib
import shlex
import sys
from pathlib import Path
from typing import Any

from norn.agents import resolve_agent_provider
from norn.catalog import _extract_metadata, get_pipeline_info, load_bundled_pipeline
from norn.graph import build_graph
from norn.loader import load_pipeline as _load_pipeline_from_file
from norn.state import (
    _load_checkpoint_for_config,
    _load_history_for_config,
    _primary_state_key,
)


def _bundled_dir() -> Path:
    """Return the directory holding bundled pipeline modules."""
    import norn.pipelines as _pkg

    return Path(_pkg.__file__).parent


def pipeline_args_meta(pipeline_ref: str) -> dict[str, str]:
    """Return a pipeline's declared ``args`` metadata (name → description).

    Works for a bundled name or an external file path. Empty when the
    pipeline declares no args or metadata can't be read — i.e. no prompt.
    """
    info = get_pipeline_info(pipeline_ref)
    if info is not None:
        return dict(info.args)
    try:
        _short, _long, _env, declared = _extract_metadata(Path(pipeline_ref).read_text())
        return dict(declared)
    except (OSError, SyntaxError, ValueError):
        return {}


def resolve_ref(info: Any) -> tuple[str, bool]:
    """Map a :class:`~norn.catalog.PipelineInfo` to ``(ref, is_bundled)``.

    ``ref`` is the bundled name (when the pipeline is a real bundled one) or
    the file path otherwise. The path check guards against a discovered
    pipeline that merely shares a name with a bundled one.
    """
    is_bundled = (
        get_pipeline_info(info.name) is not None and info.path.parent == _bundled_dir()
    )
    return (info.name if is_bundled else str(info.path)), is_bundled


def load_pipeline_with_args(pipeline_ref: str, *, is_bundled: bool, params: dict[str, str]):
    """Load the pipeline object, exposing positional args via ``sys.argv``.

    Some bundled pipelines (e.g. ``implement_features``) read ``sys.argv`` at
    import time to build their structure. Reconstruct ``argv`` from the
    positional ``args`` param around the import (and reload an already-imported
    module so a repeat selection with different args isn't served stale).
    """
    positional = (params or {}).get("args", "")
    saved = list(sys.argv)
    try:
        sys.argv = [saved[0]] + (shlex.split(positional) if positional else [])
        if is_bundled:
            mod = sys.modules.get(f"norn.pipelines.{pipeline_ref}")
            if mod is not None:
                importlib.reload(mod)
            return load_bundled_pipeline(pipeline_ref)
        return _load_pipeline_from_file(str(pipeline_ref))
    finally:
        sys.argv = saved


def build_run_setup(
    pipeline_obj: Any, params: dict[str, str], *, ref: str
) -> tuple[Any, Any, dict[str, Any]]:
    """Return ``(graph, budget, run_kwargs)`` for driving a run in the TUI.

    Sets ``config_path`` so every TUI run — whether launched from the launcher,
    history view, or direct mode — writes history and checkpoints to the same
    state key that ``norn run`` and ``norn history`` use for *ref*.  The resume
    branch in ``_start_run`` only needs to add ``resume_checkpoint`` on top.
    """
    graph = build_graph(pipeline_obj)
    budget = (pipeline_obj.budgets or [None])[0]
    run_kwargs: dict[str, Any] = {
        "agent_provider": resolve_agent_provider(pipeline_obj),
        "params": params,
        "config_path": history_config_key(ref),
    }
    return graph, budget, run_kwargs


# --- history / resume (same state-key resolution as `norn run`/`norn history`)


def load_run_history(pipeline_ref: str) -> list:
    """Return the run history records for a pipeline (bundled name or path)."""
    return _load_history_for_config(pipeline_ref)


def history_config_key(pipeline_ref: str) -> str:
    """Return the state key (history/checkpoint path) for a pipeline."""
    return _primary_state_key(pipeline_ref)


def load_run_checkpoint(pipeline_ref: str):
    """Return the saved checkpoint for a pipeline, or ``None``."""
    return _load_checkpoint_for_config(pipeline_ref)

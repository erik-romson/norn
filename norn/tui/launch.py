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
from norn.catalog import ArgSpec, _extract_metadata, get_pipeline_info, load_bundled_pipeline
from norn.dsl import apply_budget_overrides
from norn.graph import build_graph
from norn.loader import load_pipeline as _load_pipeline_from_file
from norn.state import (
    _load_checkpoint_for_config,
    _load_history_for_config,
    _primary_state_key,
    publish_state_key,
)


# Param keys the launcher's args screen uses for launch-time budget limits.
# Defined here (not in the textual-only args screen) because this module is the
# one that consumes them, and it must stay importable without textual.  The
# names are reserved (double-underscored) so they can never be confused with a
# pipeline's own declared arg of the same idea.
BUDGET_PARAM = "__budget__"
TOKEN_BUDGET_PARAM = "__token_budget__"


def _bundled_dir() -> Path:
    """Return the directory holding bundled pipeline modules."""
    import norn.pipelines as _pkg

    return Path(_pkg.__file__).parent


def pipeline_args_meta(pipeline_ref: str) -> dict[str, ArgSpec]:
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


def launch_argv(params: dict[str, str]) -> list[str]:
    """Return the ``sys.argv`` tail that ``norn run`` would have for *params*.

    The positional ``args`` param is shell-split; every other declared param
    becomes ``--arg KEY=VALUE``, the form pipelines such as
    ``check_and_fix_ci_v2`` parse at import time. Blank values are left out,
    like a flag never typed on the command line, so the pipeline's own default
    applies. The budget keys are launch-time limits, not pipeline args.
    """
    positional = params.get("args", "")
    tail = shlex.split(positional) if positional else []
    for key, value in params.items():
        if key in ("args", BUDGET_PARAM, TOKEN_BUDGET_PARAM) or not value.strip():
            continue
        tail += ["--arg", f"{key}={value}"]
    return tail


def load_pipeline_with_args(pipeline_ref: str, *, is_bundled: bool, params: dict[str, str]):
    """Load the pipeline object, exposing the launch params via ``sys.argv``.

    Some bundled pipelines (e.g. ``implement_features_v1``,
    ``check_and_fix_ci_v2``) read ``sys.argv`` at import time to build their
    structure. Reconstruct ``argv`` from the params around the import (see
    :func:`launch_argv`), and reload an already-imported module so a repeat
    selection with different args isn't served stale.

    This is also the TUI's single pipeline-import site, so it is where the state
    key is published. Pipelines that compute step ownership by diffing worktree
    snapshots read ``NORN_STATE_KEY`` at import time to subtract norn's own
    ``.runs``/``.checkpoint``/``.history`` from the diff; without it a TUI-
    launched ``implement_features_v2`` commits its own run journal as a
    step-owned change and then fails its own "owned paths are clean"
    postcondition on the very next journal write.
    """
    publish_state_key(pipeline_ref)
    saved = list(sys.argv)
    try:
        sys.argv = [saved[0]] + launch_argv(params or {})
        if is_bundled:
            mod = sys.modules.get(f"norn.pipelines.{pipeline_ref}")
            if mod is not None:
                importlib.reload(mod)
            return load_bundled_pipeline(pipeline_ref)
        return _load_pipeline_from_file(str(pipeline_ref))
    finally:
        sys.argv = saved


def apply_budget_params(pipeline_obj: Any, params: dict[str, str]) -> dict[str, str]:
    """Apply the args screen's budget overrides and return the remaining params.

    The launcher collects ``budget`` / ``token_budget`` (see
    :mod:`norn.tui.args_prompt`) alongside a pipeline's declared args.  They are
    launch-time limits, not pipeline parameters, so they are consumed here
    rather than travelling on into ``{param.*}`` substitution.
    """
    cost = params.get(BUDGET_PARAM)
    tokens = params.get(TOKEN_BUDGET_PARAM)
    if cost is None and tokens is None:
        return params
    apply_budget_overrides(
        pipeline_obj,
        max_cost_usd=float(cost) if cost else None,
        max_tokens=int(float(tokens)) if tokens else None,
    )
    return {k: v for k, v in params.items() if k not in (BUDGET_PARAM, TOKEN_BUDGET_PARAM)}


def build_run_setup(
    pipeline_obj: Any, params: dict[str, str], *, ref: str
) -> tuple[Any, Any, dict[str, Any]]:
    """Return ``(graph, budget, run_kwargs)`` for driving a run in the TUI.

    Sets ``config_path`` so every TUI run — whether launched from the launcher,
    history view, or direct mode — writes history and checkpoints to the same
    state key that ``norn run`` and ``norn history`` use for *ref*.  The resume
    branch in ``_start_run`` only needs to add ``resume_checkpoint`` on top.

    Budget overrides typed on the args screen are applied to *pipeline_obj*
    here, before the graph and the run screen's budget meter are built from it.
    """
    params = apply_budget_params(pipeline_obj, params)
    graph = build_graph(pipeline_obj)
    budget = (pipeline_obj.budgets or [None])[0]

    # Resolve pipeline_source: bundled pipelines use their module path; file
    # pipelines use the absolute path of the config file.
    info = get_pipeline_info(ref)
    if info is not None:
        pipeline_source = str(info.path)
    else:
        pipeline_source = str(Path(ref).resolve())

    run_kwargs: dict[str, Any] = {
        "agent_provider": resolve_agent_provider(pipeline_obj),
        "params": params,
        "config_path": history_config_key(ref),
        "pipeline_source": pipeline_source,
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

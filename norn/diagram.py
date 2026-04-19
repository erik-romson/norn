from __future__ import annotations

from pathlib import Path

from norn.catalog import _extract_metadata
from norn.dsl import ClearContext, Include, Loop, Parallel, Pipeline, Stage


def to_markdown(pipeline: Pipeline, config_path: str) -> str:
    """Generate a full Markdown document for a pipeline.

    Includes a title, short description from the module docstring,
    required inputs (env vars, args), and a Mermaid flowchart.
    """
    path = Path(config_path).resolve()
    short, _long, env_vars, args = _extract_metadata(path.read_text())

    lines: list[str] = []
    lines.append(f"# {pipeline.name}")
    if short:
        lines.append("")
        lines.append(short)

    if env_vars or args:
        lines.append("")
        lines.append("## Inputs")
        if args:
            for arg_name, arg_desc in args.items():
                lines.append(f"- **{arg_name}**: {arg_desc}")
        if env_vars:
            lines.append("")
            lines.append("Environment variables: " + ", ".join(f"`{v}`" for v in env_vars))

    lines.append("")
    lines.append("## Pipeline")
    lines.append("")
    lines.append("```mermaid")
    lines.append(to_mermaid(pipeline).rstrip())
    lines.append("```")
    lines.append("")

    return "\n".join(lines)


def to_mermaid(pipeline: Pipeline) -> str:
    """Convert a Pipeline to a Mermaid flowchart string."""
    lines: list[str] = []
    lines.append("flowchart TD")

    node_ids: list[str] = []
    counter = _Counter()

    for item in pipeline.items:
        node_id = _emit_item(item, lines, counter)
        node_ids.append(node_id)

    # Connect top-level items sequentially
    for i in range(len(node_ids) - 1):
        lines.append(f"    {node_ids[i]} --> {node_ids[i + 1]}")

    return "\n".join(lines) + "\n"


class _Counter:
    """Simple counter for generating unique node IDs."""

    def __init__(self) -> None:
        self._n = 0

    def next(self, prefix: str = "n") -> str:
        self._n += 1
        return f"{prefix}{self._n}"


def _sanitize_label(text: str) -> str:
    """Escape characters that break Mermaid labels."""
    return text.replace('"', "#quot;")


def _emit_item(item: Stage | Loop | ClearContext | Parallel | Include,
               lines: list[str], counter: _Counter) -> str:
    """Emit Mermaid lines for a single pipeline item. Returns the node/subgraph ID."""
    if isinstance(item, Stage):
        return _emit_stage(item, lines, counter)
    if isinstance(item, Loop):
        return _emit_loop(item, lines, counter)
    if isinstance(item, Parallel):
        return _emit_parallel(item, lines, counter)
    if isinstance(item, Include):
        return _emit_include(item, lines, counter)
    # ClearContext
    return _emit_clear_context(lines, counter)


def _emit_stage(stage: Stage, lines: list[str], counter: _Counter) -> str:
    nid = counter.next("s")
    label = _sanitize_label(stage.name)
    lines.append(f'    {nid}["{label}"]')
    return nid


def _emit_loop(loop: Loop, lines: list[str], counter: _Counter) -> str:
    gid = counter.next("loop")
    label = _sanitize_label(loop.name)
    lines.append(f'    subgraph {gid} ["{label} (loop, max {loop.max_retries})"]')

    stage_ids: list[str] = []
    for stage in loop.stages:
        sid = _emit_item(stage, lines, counter)
        stage_ids.append(sid)

    # Sequential edges inside the loop
    for i in range(len(stage_ids) - 1):
        lines.append(f"        {stage_ids[i]} --> {stage_ids[i + 1]}")

    # Retry edge from last to first
    if len(stage_ids) >= 2:
        lines.append(f"        {stage_ids[-1]} -. retry .-> {stage_ids[0]}")

    lines.append("    end")
    return gid


def _emit_parallel(par: Parallel, lines: list[str], counter: _Counter) -> str:
    gid = counter.next("par")
    label = _sanitize_label(par.name)
    lines.append(f'    subgraph {gid} ["{label} (parallel)"]')

    fork_id = counter.next("fork")
    join_id = counter.next("join")
    lines.append(f'        {fork_id}(("{label}"))')

    stage_ids: list[str] = []
    for stage in par.stages:
        sid = _emit_item(stage, lines, counter)
        stage_ids.append(sid)

    lines.append(f'        {join_id}(("{label} done"))')

    for sid in stage_ids:
        lines.append(f"        {fork_id} --> {sid} --> {join_id}")

    lines.append("    end")
    return gid


def _emit_include(include: Include, lines: list[str], counter: _Counter) -> str:
    nid = counter.next("inc")
    label = _sanitize_label(include.path)
    lines.append(f'    {nid}[["{label}"]]')
    return nid


def _emit_clear_context(lines: list[str], counter: _Counter) -> str:
    nid = counter.next("cc")
    lines.append(f'    {nid}(["clear context"])')
    return nid

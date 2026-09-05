from __future__ import annotations

from pathlib import Path

from norn.catalog import _extract_metadata
from norn.dsl import Pipeline
from norn.graph import PipelineNode, build_graph


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
    graph = build_graph(pipeline)
    lines: list[str] = []
    lines.append("flowchart TD")

    counter = _Counter()
    node_ids: list[str] = []

    for node in graph.root.children:
        node_id = _emit_node(node, lines, counter)
        node_ids.append(node_id)

    # Connect top-level items sequentially
    for i in range(len(node_ids) - 1):
        lines.append(f"    {node_ids[i]} --> {node_ids[i + 1]}")

    return "\n".join(lines) + "\n"


class _Counter:
    """Simple counter for generating unique Mermaid node IDs."""

    def __init__(self) -> None:
        self._n = 0

    def next(self, prefix: str = "n") -> str:
        self._n += 1
        return f"{prefix}{self._n}"


def _sanitize_label(text: str) -> str:
    """Escape characters that break Mermaid labels."""
    return text.replace('"', "#quot;")


def _emit_node(node: PipelineNode, lines: list[str], counter: _Counter) -> str:
    """Emit Mermaid lines for a single pipeline node. Returns the Mermaid node/subgraph ID."""
    if node.kind == "stage":
        return _emit_stage_node(node, lines, counter)
    if node.kind == "loop":
        return _emit_loop_node(node, lines, counter)
    if node.kind == "parallel":
        return _emit_parallel_node(node, lines, counter)
    if node.kind == "include":
        return _emit_include_node(node, lines, counter)
    # clear
    return _emit_clear_node(node, lines, counter)


def _emit_stage_node(node: PipelineNode, lines: list[str], counter: _Counter) -> str:
    nid = counter.next("s")
    label = _sanitize_label(node.name)
    lines.append(f'    {nid}["{label}"]')
    return nid


def _emit_loop_node(node: PipelineNode, lines: list[str], counter: _Counter) -> str:
    gid = counter.next("loop")
    label = _sanitize_label(node.name)
    max_retries = node.metadata.get("max_retries", 0)
    lines.append(f'    subgraph {gid} ["{label} (loop, max {max_retries})"]')

    stage_ids: list[str] = []
    for child in node.children:
        sid = _emit_node(child, lines, counter)
        stage_ids.append(sid)

    # Sequential edges inside the loop
    for i in range(len(stage_ids) - 1):
        lines.append(f"        {stage_ids[i]} --> {stage_ids[i + 1]}")

    # Retry edge from last to first
    if len(stage_ids) >= 2:
        lines.append(f"        {stage_ids[-1]} -. retry .-> {stage_ids[0]}")

    lines.append("    end")
    return gid


def _emit_parallel_node(node: PipelineNode, lines: list[str], counter: _Counter) -> str:
    gid = counter.next("par")
    label = _sanitize_label(node.name)
    lines.append(f'    subgraph {gid} ["{label} (parallel)"]')

    fork_id = counter.next("fork")
    join_id = counter.next("join")
    lines.append(f'        {fork_id}(("{label}"))')

    stage_ids: list[str] = []
    for child in node.children:
        sid = _emit_node(child, lines, counter)
        stage_ids.append(sid)

    lines.append(f'        {join_id}(("{label} done"))')

    for sid in stage_ids:
        lines.append(f"        {fork_id} --> {sid} --> {join_id}")

    lines.append("    end")
    return gid


def _emit_include_node(node: PipelineNode, lines: list[str], counter: _Counter) -> str:
    nid = counter.next("inc")
    label = _sanitize_label(node.name)
    lines.append(f'    {nid}[["{label}"]]')
    return nid


def _emit_clear_node(node: PipelineNode, lines: list[str], counter: _Counter) -> str:
    nid = counter.next("cc")
    lines.append(f'    {nid}(["clear context"])')
    return nid

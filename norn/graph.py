"""Pipeline structure model.

Builds a reusable, stable-ID graph from a :class:`~norn.dsl.Pipeline` so that
the diagram renderer, the TUI Graph widget, and dry-run can all share the same
structural representation.

The module is pure data — no Textual, no SDK imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from norn.dsl import ClearContext, Include, Loop, Parallel, Pipeline, Stage


@dataclass
class PipelineNode:
    """A single node in the pipeline structure graph.

    Attributes:
        node_id:  Stable, deterministic identifier.  For ``stage`` nodes this
                  is the same ID that run-events use as ``stage_id``.
        kind:     One of ``stage``, ``loop``, ``parallel``, ``include``,
                  ``clear``, or ``pipeline`` (root only).
        name:     Human-readable name (stage/loop/parallel name; file path for
                  includes; ``"clear context"`` for clear-context markers;
                  pipeline name for the root).
        parent:   ``node_id`` of the parent node, or ``None`` for the root.
        order:    Zero-based position within the parent's children list.
        children: Ordered list of child nodes (stages inside a loop/parallel).
        status:   Runtime status slot — one of ``pending``, ``running``,
                  ``passed``, ``failed``, ``skipped``, ``cached``,
                  ``retrying``.  Starts as ``"pending"``; mutated by the
                  runner / TUI view-model.
        attempts: Retry-attempt counter for loop nodes.
    """

    node_id: str
    kind: str  # stage|loop|parallel|include|clear|pipeline
    name: str
    parent: str | None
    order: int
    children: list[PipelineNode] = field(default_factory=list)
    status: str = "pending"
    attempts: int = 0
    metadata: dict = field(default_factory=dict)


@dataclass
class PipelineGraph:
    """The complete structural graph for a pipeline.

    Attributes:
        root:  The virtual root node whose ``kind`` is ``"pipeline"``.
               Its ``children`` are the top-level pipeline items in order.
        by_id: Flat lookup table mapping every ``node_id`` to its node,
               including the root.
    """

    root: PipelineNode
    by_id: dict[str, PipelineNode]


def build_graph(pipeline: Pipeline) -> PipelineGraph:
    """Build a :class:`PipelineGraph` from *pipeline*.

    Node IDs are **stable** and **deterministic**: running ``build_graph``
    twice on the same pipeline object always produces identical ``node_id``
    values.  ID scheme:

    * Pipeline root:  ``pipeline:<pipeline.name>``
    * Top-level stage:  ``stage:<name>``
    * Top-level loop:  ``loop:<name>``
    * Top-level parallel:  ``parallel:<name>``
    * Top-level include:  ``include:<path>``
    * Top-level ClearContext (Nth):  ``clear:<N>``
    * Stage inside loop named *L*:  ``loop:<L>/stage:<name>``
    * Stage inside parallel named *P*:  ``parallel:<P>/stage:<name>``
    * ClearContext (Nth) inside any container *C*:  ``<C>/clear:<N>``

    The prefix for nested nodes is always ``<parent_node_id>/`` so the full
    path is unique and human-readable.
    """
    by_id: dict[str, PipelineNode] = {}

    root_id = f"pipeline:{pipeline.name}"
    root = PipelineNode(
        node_id=root_id,
        kind="pipeline",
        name=pipeline.name,
        parent=None,
        order=0,
        children=[],
    )
    by_id[root_id] = root

    root.children = _build_children(
        items=pipeline.items,
        parent_id=root_id,
        id_prefix="",
        by_id=by_id,
        pipeline_name=pipeline.name,
    )

    return PipelineGraph(root=root, by_id=by_id)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_children(
    items: list,
    parent_id: str,
    id_prefix: str,
    by_id: dict[str, PipelineNode],
    pipeline_name: str,
) -> list[PipelineNode]:
    """Recursively build :class:`PipelineNode` objects for *items*.

    *id_prefix* is prepended to the local ``<kind>:<name>`` token so that
    nested nodes carry the full ancestry path, ensuring uniqueness.

    Raises:
        ValueError: If two sibling items produce the same ``node_id``.  Two
            stages (or a stage and a loop, etc.) with the same name inside the
            same parent share an id, which causes event-key collisions and TUI
            rendering bugs.  Stage, loop, and parallel names must be unique
            within their parent container.
    """
    children: list[PipelineNode] = []
    clear_count = 0  # position counter for nameless ClearContext markers

    for order, item in enumerate(items):
        if isinstance(item, ClearContext):
            # clear_count suffix makes each clear marker unique — no collision possible.
            node_id = f"{id_prefix}clear:{clear_count}"
            node = PipelineNode(
                node_id=node_id,
                kind="clear",
                name="clear context",
                parent=parent_id,
                order=order,
                children=[],
            )
            clear_count += 1
            by_id[node_id] = node
            children.append(node)

        elif isinstance(item, Stage):
            node_id = f"{id_prefix}stage:{item.name}"
            _assert_unique_node_id(node_id, item.name, pipeline_name, by_id)
            node = PipelineNode(
                node_id=node_id,
                kind="stage",
                name=item.name,
                parent=parent_id,
                order=order,
                children=[],
            )
            by_id[node_id] = node
            children.append(node)

        elif isinstance(item, Loop):
            node_id = f"{id_prefix}loop:{item.name}"
            _assert_unique_node_id(node_id, item.name, pipeline_name, by_id)
            node = PipelineNode(
                node_id=node_id,
                kind="loop",
                name=item.name,
                parent=parent_id,
                order=order,
                children=[],
                metadata={"max_retries": item.max_retries},
            )
            by_id[node_id] = node
            node.children = _build_children(
                items=item.stages,
                parent_id=node_id,
                id_prefix=f"{node_id}/",
                by_id=by_id,
                pipeline_name=pipeline_name,
            )
            children.append(node)

        elif isinstance(item, Parallel):
            node_id = f"{id_prefix}parallel:{item.name}"
            _assert_unique_node_id(node_id, item.name, pipeline_name, by_id)
            node = PipelineNode(
                node_id=node_id,
                kind="parallel",
                name=item.name,
                parent=parent_id,
                order=order,
                children=[],
            )
            by_id[node_id] = node
            node.children = _build_children(
                items=item.stages,
                parent_id=node_id,
                id_prefix=f"{node_id}/",
                by_id=by_id,
                pipeline_name=pipeline_name,
            )
            children.append(node)

        elif isinstance(item, Include):
            node_id = f"{id_prefix}include:{item.path}"
            _assert_unique_node_id(node_id, item.path, pipeline_name, by_id)
            node = PipelineNode(
                node_id=node_id,
                kind="include",
                name=item.path,
                parent=parent_id,
                order=order,
                children=[],
            )
            by_id[node_id] = node
            children.append(node)

        else:
            raise TypeError(f"Unknown pipeline item type: {type(item)!r}")

    return children


def _assert_unique_node_id(
    node_id: str,
    item_name: str,
    pipeline_name: str,
    by_id: dict[str, PipelineNode],
) -> None:
    """Raise :exc:`ValueError` if *node_id* is already registered in *by_id*.

    Args:
        node_id:       The fully-qualified id about to be registered.
        item_name:     Human-readable name of the item (stage/loop/parallel
                       name, or include path) — used in the error message.
        pipeline_name: Name of the enclosing pipeline — used in the message.
        by_id:         The running id→node registry to check against.

    Raises:
        ValueError: With a message that names the colliding id, the pipeline,
            and both conflicting item names, and tells the user how to fix it.
    """
    if node_id not in by_id:
        return
    raise ValueError(
        f"duplicate node id {node_id!r} in pipeline {pipeline_name!r}: "
        f"two sibling items share the name {item_name!r}; "
        "stage, loop, parallel, and include names must be unique within their parent"
    )

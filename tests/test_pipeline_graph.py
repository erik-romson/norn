"""Tests for norn.graph — PipelineGraph structure model."""

from __future__ import annotations

import pytest

from norn.dsl import ClearContext, Include, Loop, Parallel, Pipeline, Stage
from norn.graph import PipelineGraph, PipelineNode, build_graph
from norn.stages.base import BaseStage, StageResult


# ---------------------------------------------------------------------------
# Minimal stub stage so we can construct Stage objects without real impls
# ---------------------------------------------------------------------------


class _Noop(BaseStage):
    async def run(self, ctx):
        return StageResult(name="", success=True)


def _stage(name: str) -> Stage:
    return Stage(name=name, impl=_Noop())


# ---------------------------------------------------------------------------
# Helper pipeline used across most tests
# ---------------------------------------------------------------------------


def _make_pipeline() -> Pipeline:
    """Pipeline containing one loop, one parallel, one include, one clear, and
    two plain stages — enough to exercise every node kind."""
    return Pipeline(
        name="test_pipe",
        items=[
            _stage("setup"),
            Loop(
                name="build loop",
                stages=[_stage("compile"), _stage("lint")],
                max_retries=3,
            ),
            Parallel(
                name="deploy",
                stages=[_stage("deploy-a"), _stage("deploy-b")],
            ),
            Include(path="sub/pipeline.py", isolated=False),
            ClearContext(),
            _stage("finish"),
        ],
    )


# ---------------------------------------------------------------------------
# Basic shape tests
# ---------------------------------------------------------------------------


def test_build_graph_returns_pipeline_graph():
    pipeline = _make_pipeline()
    graph = build_graph(pipeline)
    assert isinstance(graph, PipelineGraph)
    assert isinstance(graph.root, PipelineNode)
    assert isinstance(graph.by_id, dict)


def test_root_node_kind_and_name():
    pipeline = _make_pipeline()
    graph = build_graph(pipeline)
    root = graph.root
    assert root.kind == "pipeline"
    assert root.name == "test_pipe"
    assert root.node_id == "pipeline:test_pipe"
    assert root.parent is None
    assert root.order == 0


def test_root_in_by_id():
    graph = build_graph(_make_pipeline())
    assert "pipeline:test_pipe" in graph.by_id
    assert graph.by_id["pipeline:test_pipe"] is graph.root


def test_top_level_children_count():
    graph = build_graph(_make_pipeline())
    # setup, loop, parallel, include, clear, finish
    assert len(graph.root.children) == 6


def test_top_level_order():
    graph = build_graph(_make_pipeline())
    orders = [n.order for n in graph.root.children]
    assert orders == list(range(6))


def test_top_level_kinds():
    graph = build_graph(_make_pipeline())
    kinds = [n.kind for n in graph.root.children]
    assert kinds == ["stage", "loop", "parallel", "include", "clear", "stage"]


# ---------------------------------------------------------------------------
# Stage nodes
# ---------------------------------------------------------------------------


def test_stage_node_id_and_name():
    graph = build_graph(_make_pipeline())
    setup = graph.by_id["stage:setup"]
    assert setup.kind == "stage"
    assert setup.name == "setup"
    assert setup.parent == "pipeline:test_pipe"
    assert setup.children == []


def test_stage_node_default_status_and_attempts():
    graph = build_graph(_make_pipeline())
    node = graph.by_id["stage:setup"]
    assert node.status == "pending"
    assert node.attempts == 0


# ---------------------------------------------------------------------------
# Loop node and children
# ---------------------------------------------------------------------------


def test_loop_node_id_and_kind():
    graph = build_graph(_make_pipeline())
    loop_node = graph.by_id["loop:build loop"]
    assert loop_node.kind == "loop"
    assert loop_node.name == "build loop"
    assert loop_node.parent == "pipeline:test_pipe"


def test_loop_has_two_children():
    graph = build_graph(_make_pipeline())
    loop_node = graph.by_id["loop:build loop"]
    assert len(loop_node.children) == 2


def test_loop_child_ids():
    graph = build_graph(_make_pipeline())
    compile_node = graph.by_id["loop:build loop/stage:compile"]
    lint_node = graph.by_id["loop:build loop/stage:lint"]
    assert compile_node.kind == "stage"
    assert compile_node.name == "compile"
    assert compile_node.parent == "loop:build loop"
    assert compile_node.order == 0
    assert lint_node.order == 1


def test_loop_children_order():
    graph = build_graph(_make_pipeline())
    loop_node = graph.by_id["loop:build loop"]
    child_names = [c.name for c in loop_node.children]
    assert child_names == ["compile", "lint"]


# ---------------------------------------------------------------------------
# Parallel node and children
# ---------------------------------------------------------------------------


def test_parallel_node_id_and_kind():
    graph = build_graph(_make_pipeline())
    par_node = graph.by_id["parallel:deploy"]
    assert par_node.kind == "parallel"
    assert par_node.name == "deploy"
    assert par_node.parent == "pipeline:test_pipe"


def test_parallel_children():
    graph = build_graph(_make_pipeline())
    a = graph.by_id["parallel:deploy/stage:deploy-a"]
    b = graph.by_id["parallel:deploy/stage:deploy-b"]
    assert a.kind == "stage"
    assert a.parent == "parallel:deploy"
    assert a.order == 0
    assert b.order == 1


# ---------------------------------------------------------------------------
# Include node
# ---------------------------------------------------------------------------


def test_include_node():
    graph = build_graph(_make_pipeline())
    inc = graph.by_id["include:sub/pipeline.py"]
    assert inc.kind == "include"
    assert inc.name == "sub/pipeline.py"
    assert inc.parent == "pipeline:test_pipe"
    assert inc.children == []


# ---------------------------------------------------------------------------
# ClearContext node
# ---------------------------------------------------------------------------


def test_clear_context_node():
    graph = build_graph(_make_pipeline())
    # ClearContext is the 5th top-level item (order=4); it is clear:0
    cc = graph.by_id["clear:0"]
    assert cc.kind == "clear"
    assert cc.name == "clear context"
    assert cc.parent == "pipeline:test_pipe"
    assert cc.order == 4


def test_multiple_clear_contexts_get_distinct_ids():
    pipeline = Pipeline(
        name="multi_clear",
        items=[
            ClearContext(),
            _stage("middle"),
            ClearContext(),
        ],
    )
    graph = build_graph(pipeline)
    assert "clear:0" in graph.by_id
    assert "clear:1" in graph.by_id
    cc0 = graph.by_id["clear:0"]
    cc1 = graph.by_id["clear:1"]
    assert cc0.order == 0
    assert cc1.order == 2


# ---------------------------------------------------------------------------
# by_id completeness
# ---------------------------------------------------------------------------


def test_by_id_contains_all_nodes():
    graph = build_graph(_make_pipeline())
    expected_ids = {
        "pipeline:test_pipe",
        "stage:setup",
        "loop:build loop",
        "loop:build loop/stage:compile",
        "loop:build loop/stage:lint",
        "parallel:deploy",
        "parallel:deploy/stage:deploy-a",
        "parallel:deploy/stage:deploy-b",
        "include:sub/pipeline.py",
        "clear:0",
        "stage:finish",
    }
    assert set(graph.by_id.keys()) == expected_ids


def test_by_id_nodes_are_same_objects_as_tree():
    graph = build_graph(_make_pipeline())
    # Each node in by_id should be reachable through the tree
    loop_node = graph.by_id["loop:build loop"]
    assert loop_node in graph.root.children
    compile_node = graph.by_id["loop:build loop/stage:compile"]
    assert compile_node in loop_node.children


# ---------------------------------------------------------------------------
# Determinism — same IDs on two independent builds
# ---------------------------------------------------------------------------


def test_build_graph_deterministic_ids():
    pipeline = _make_pipeline()
    graph1 = build_graph(pipeline)
    graph2 = build_graph(pipeline)
    assert set(graph1.by_id.keys()) == set(graph2.by_id.keys())


def test_build_graph_deterministic_node_ids_match():
    pipeline = _make_pipeline()
    graph1 = build_graph(pipeline)
    graph2 = build_graph(pipeline)
    for node_id in graph1.by_id:
        n1 = graph1.by_id[node_id]
        n2 = graph2.by_id[node_id]
        assert n1.node_id == n2.node_id
        assert n1.kind == n2.kind
        assert n1.name == n2.name
        assert n1.parent == n2.parent
        assert n1.order == n2.order


def test_build_graph_deterministic_across_separate_pipeline_instances():
    """Two separately-constructed identical pipelines must yield the same IDs."""
    ids_1 = set(build_graph(_make_pipeline()).by_id.keys())
    ids_2 = set(build_graph(_make_pipeline()).by_id.keys())
    assert ids_1 == ids_2


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_pipeline():
    pipeline = Pipeline(name="empty")
    graph = build_graph(pipeline)
    assert graph.root.children == []
    assert set(graph.by_id.keys()) == {"pipeline:empty"}


def test_pipeline_with_only_stages():
    pipeline = Pipeline(
        name="simple",
        items=[_stage("a"), _stage("b"), _stage("c")],
    )
    graph = build_graph(pipeline)
    assert len(graph.root.children) == 3
    assert graph.by_id["stage:a"].order == 0
    assert graph.by_id["stage:b"].order == 1
    assert graph.by_id["stage:c"].order == 2


def test_include_isolated_is_tracked():
    pipeline = Pipeline(
        name="inc_pipe",
        items=[
            Include(path="./sub.py", isolated=True, outputs=["result"]),
        ],
    )
    graph = build_graph(pipeline)
    inc = graph.by_id["include:./sub.py"]
    assert inc.kind == "include"
    # The isolated flag lives on the DSL object; graph records name only
    assert inc.name == "./sub.py"


def test_parent_child_consistency():
    """Every non-root node's parent_id should resolve to a real node."""
    graph = build_graph(_make_pipeline())
    for node_id, node in graph.by_id.items():
        if node.parent is not None:
            assert node.parent in graph.by_id, (
                f"Node {node_id!r} references parent {node.parent!r} not in by_id"
            )


# ---------------------------------------------------------------------------
# Uniqueness enforcement — duplicate node ids must raise ValueError
# ---------------------------------------------------------------------------


def test_duplicate_stage_names_raise():
    """Two top-level stages with the same name produce the same node id and must
    be rejected with a message that names the id, pipeline, and item name."""
    pipeline = Pipeline(
        name="dup_pipe",
        items=[_stage("commit"), _stage("commit")],
    )
    with pytest.raises(ValueError) as exc_info:
        build_graph(pipeline)
    msg = str(exc_info.value)
    assert "stage:commit" in msg, f"Expected node id in message: {msg!r}"
    assert "dup_pipe" in msg, f"Expected pipeline name in message: {msg!r}"
    assert "commit" in msg, f"Expected item name in message: {msg!r}"


def test_duplicate_loop_names_raise():
    """Two top-level loops with the same name must raise ValueError."""
    pipeline = Pipeline(
        name="dup_loop_pipe",
        items=[
            Loop(name="retry", stages=[_stage("work")], max_retries=2),
            Loop(name="retry", stages=[_stage("cleanup")], max_retries=1),
        ],
    )
    with pytest.raises(ValueError) as exc_info:
        build_graph(pipeline)
    msg = str(exc_info.value)
    assert "loop:retry" in msg
    assert "dup_loop_pipe" in msg


def test_duplicate_stage_names_inside_loop_raise():
    """Two stages sharing a name inside the same loop must raise ValueError."""
    pipeline = Pipeline(
        name="loop_dup",
        items=[
            Loop(name="build", stages=[_stage("compile"), _stage("compile")], max_retries=1),
        ],
    )
    with pytest.raises(ValueError) as exc_info:
        build_graph(pipeline)
    msg = str(exc_info.value)
    assert "loop:build/stage:compile" in msg
    assert "loop_dup" in msg


def test_same_stage_name_in_different_parents_is_legal():
    """The same stage name used under two *different* parent containers must not
    raise — the fully-qualified ids are distinct."""
    pipeline = Pipeline(
        name="shared_names",
        items=[
            Loop(name="a", stages=[_stage("work")], max_retries=2),
            Loop(name="b", stages=[_stage("work")], max_retries=2),
        ],
    )
    graph = build_graph(pipeline)  # must not raise
    assert "loop:a/stage:work" in graph.by_id
    assert "loop:b/stage:work" in graph.by_id


def test_stage_and_loop_sharing_a_name_do_not_collide():
    """A Stage and a Loop at the same level with the same name produce *different*
    node ids (``stage:x`` vs ``loop:x``) and must therefore NOT raise — they are
    distinct nodes."""
    # Per the id scheme, Stage → stage:<name>, Loop → loop:<name>.
    # They only collide if two items of the *same* kind share the name.
    pipeline = Pipeline(
        name="mixed_kinds",
        items=[
            _stage("run"),
            Loop(name="run", stages=[_stage("step")], max_retries=1),
        ],
    )
    graph = build_graph(pipeline)  # must not raise — ids are distinct
    assert "stage:run" in graph.by_id
    assert "loop:run" in graph.by_id


def test_error_message_includes_fix_guidance():
    """The ValueError message must tell the user how to fix the collision."""
    pipeline = Pipeline(
        name="fix_me",
        items=[_stage("init"), _stage("init")],
    )
    with pytest.raises(ValueError) as exc_info:
        build_graph(pipeline)
    msg = str(exc_info.value)
    # Must explain that names must be unique within their parent
    assert "unique" in msg.lower(), f"Expected 'unique' guidance in message: {msg!r}"

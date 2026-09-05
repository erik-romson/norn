"""Pilot tests for the Norn TUI header and pipeline Graph widgets.

Uses Textual's ``run_test()`` / ``Pilot`` API to mount the widgets for real
and assert that they reflect ViewModel state — behaviour that plain ViewModel
unit tests cannot cover.

All tests are offline: no SDK calls, no subprocesses, no network.
"""
from __future__ import annotations

import pytest
from textual.widgets import Tree

from norn.dsl import Parallel, Pipeline, Stage
from norn.events import EventKey, RunStarted, StageFinished, StageStarted
from norn.graph import build_graph
from norn.models import PipelineContext, StageResult
from norn.stages.base import BaseStage
from norn.tui.app import NornApp
from norn.tui.viewmodel import RunViewModel
from norn.tui.widgets import STATUS_GLYPHS, NornGraph, NornHeader


# ---------------------------------------------------------------------------
# Shared test fixtures / helpers
# ---------------------------------------------------------------------------


class _Stub(BaseStage):
    async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
        return StageResult(name="", success=True)


def _key(*, run_id: str = "test-run", stage_id: str | None = None, seq: int = 0) -> EventKey:
    return EventKey(run_id=run_id, unit_id="unit-0", stage_id=stage_id, seq=seq)


def _run_started(pipeline_name: str = "test-pipe") -> RunStarted:
    return RunStarted(key=_key(), pipeline_name=pipeline_name, provider="claude-code")


def _stage_started(stage_id: str) -> StageStarted:
    return StageStarted(key=_key(stage_id=stage_id), name=stage_id.split(":")[-1])


def _stage_finished(stage_id: str, *, status: str = "passed") -> StageFinished:
    return StageFinished(
        key=_key(stage_id=stage_id),
        name=stage_id.split(":")[-1],
        status=status,
        success=(status == "passed"),
    )


# ---------------------------------------------------------------------------
# Header widget tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_header_shows_pipeline_name():
    """After RunStarted the header contains the pipeline name."""
    pipeline = Pipeline("my-pipeline").stage("greet", _Stub())
    graph = build_graph(pipeline)
    vm = RunViewModel()
    app = NornApp(graph=graph, vm=vm)

    async with app.run_test() as pilot:
        app.apply_event(_run_started("my-pipeline"))
        await pilot.pause()

        header = app.query_one(NornHeader)
        assert "my-pipeline" in header.get_content()


@pytest.mark.asyncio
async def test_header_pending_status_before_run_started():
    """Before any events the header shows the pending glyph."""
    pipeline = Pipeline("idle").stage("s", _Stub())
    graph = build_graph(pipeline)
    vm = RunViewModel()
    app = NornApp(graph=graph, vm=vm)

    async with app.run_test() as pilot:
        await pilot.pause()
        header = app.query_one(NornHeader)
        assert STATUS_GLYPHS["pending"] in header.get_content()


@pytest.mark.asyncio
async def test_header_shows_provider():
    """Header includes the provider name from RunStarted."""
    pipeline = Pipeline("p").stage("s", _Stub())
    graph = build_graph(pipeline)
    vm = RunViewModel()
    app = NornApp(graph=graph, vm=vm)

    async with app.run_test() as pilot:
        app.apply_event(
            RunStarted(key=_key(), pipeline_name="p", provider="opencode")
        )
        await pilot.pause()

        assert "opencode" in app.query_one(NornHeader).get_content()


@pytest.mark.asyncio
async def test_header_shows_zero_cost_as_tokens():
    """When cost is 0 the header shows token count, not '$0.0000'."""
    pipeline = Pipeline("p").stage("s", _Stub())
    graph = build_graph(pipeline)
    vm = RunViewModel()
    app = NornApp(graph=graph, vm=vm)

    async with app.run_test() as pilot:
        app.apply_event(_run_started())
        await pilot.pause()
        content = app.query_one(NornHeader).get_content()
        assert "tokens" in content
        assert "$" not in content


# ---------------------------------------------------------------------------
# Graph widget tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graph_shows_all_stage_nodes():
    """Tree root children include a node for every pipeline stage."""
    pipeline = Pipeline("seq").stage("alpha", _Stub()).stage("beta", _Stub())
    graph = build_graph(pipeline)
    app = NornApp(graph=graph, vm=RunViewModel())

    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one(Tree)
        labels = [str(c.label) for c in tree.root.children]
        assert any("alpha" in lbl for lbl in labels)
        assert any("beta" in lbl for lbl in labels)


@pytest.mark.asyncio
async def test_graph_nodes_start_as_pending():
    """Before any events every graph node shows the pending glyph."""
    pipeline = Pipeline("seq").stage("alpha", _Stub()).stage("beta", _Stub())
    graph = build_graph(pipeline)
    app = NornApp(graph=graph, vm=RunViewModel())

    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one(Tree)
        for child in tree.root.children:
            assert STATUS_GLYPHS["pending"] in str(child.label)


@pytest.mark.asyncio
async def test_graph_shows_running_glyph_for_active_stage():
    """Running stage shows the running glyph in the tree."""
    pipeline = Pipeline("seq").stage("alpha", _Stub()).stage("beta", _Stub())
    graph = build_graph(pipeline)
    app = NornApp(graph=graph, vm=RunViewModel())

    async with app.run_test() as pilot:
        app.apply_event(_run_started())
        app.apply_event(_stage_started("stage:alpha"))
        await pilot.pause()

        tree = app.query_one(Tree)
        labels = [str(c.label) for c in tree.root.children]
        alpha_label = next(lbl for lbl in labels if "alpha" in lbl)
        assert STATUS_GLYPHS["running"] in alpha_label
        # beta stays pending
        beta_label = next(lbl for lbl in labels if "beta" in lbl)
        assert STATUS_GLYPHS["pending"] in beta_label


@pytest.mark.asyncio
async def test_graph_shows_passed_glyph_after_stage_finishes():
    """Finished stage shows the passed glyph after StageFinished is applied."""
    pipeline = Pipeline("seq").stage("alpha", _Stub()).stage("beta", _Stub())
    graph = build_graph(pipeline)
    app = NornApp(graph=graph, vm=RunViewModel())

    async with app.run_test() as pilot:
        app.apply_event(_run_started())
        app.apply_event(_stage_started("stage:alpha"))
        app.apply_event(_stage_finished("stage:alpha", status="passed"))
        await pilot.pause()

        tree = app.query_one(Tree)
        labels = [str(c.label) for c in tree.root.children]
        alpha_label = next(lbl for lbl in labels if "alpha" in lbl)
        assert STATUS_GLYPHS["passed"] in alpha_label


@pytest.mark.asyncio
async def test_graph_shows_failed_glyph():
    """Failed stage shows the failed glyph."""
    pipeline = Pipeline("seq").stage("alpha", _Stub())
    graph = build_graph(pipeline)
    app = NornApp(graph=graph, vm=RunViewModel())

    async with app.run_test() as pilot:
        app.apply_event(_run_started())
        app.apply_event(_stage_started("stage:alpha"))
        app.apply_event(_stage_finished("stage:alpha", status="failed"))
        await pilot.pause()

        tree = app.query_one(Tree)
        alpha_label = str(tree.root.children[0].label)
        assert STATUS_GLYPHS["failed"] in alpha_label


@pytest.mark.asyncio
async def test_graph_parallel_shows_nested_nodes():
    """Parallel block expands in the tree with child stage nodes."""
    pipeline = Pipeline("par-pipe")
    pipeline.items.append(
        Parallel(
            name="par",
            stages=[Stage("x", _Stub()), Stage("y", _Stub())],
        )
    )
    graph = build_graph(pipeline)
    app = NornApp(graph=graph, vm=RunViewModel())

    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one(Tree)
        # Root has one child: the parallel node
        assert len(tree.root.children) == 1
        par_node = tree.root.children[0]
        assert "par" in str(par_node.label)
        # Parallel node has two children: x and y
        child_labels = [str(c.label) for c in par_node.children]
        assert any("x" in lbl for lbl in child_labels)
        assert any("y" in lbl for lbl in child_labels)


@pytest.mark.asyncio
async def test_graph_parallel_multiple_running_nodes():
    """Both stages inside a Parallel show the running glyph simultaneously."""
    pipeline = Pipeline("par-pipe")
    pipeline.items.append(
        Parallel(
            name="par",
            stages=[Stage("x", _Stub()), Stage("y", _Stub())],
        )
    )
    graph = build_graph(pipeline)
    app = NornApp(graph=graph, vm=RunViewModel())

    async with app.run_test() as pilot:
        app.apply_event(_run_started("par-pipe"))
        # Both stages start running simultaneously
        app.apply_event(
            StageStarted(key=_key(stage_id="parallel:par/stage:x"), name="x")
        )
        app.apply_event(
            StageStarted(key=_key(stage_id="parallel:par/stage:y"), name="y")
        )
        await pilot.pause()

        tree = app.query_one(Tree)
        par_node = tree.root.children[0]
        child_labels = [str(c.label) for c in par_node.children]
        # Both children show the running glyph
        assert all(STATUS_GLYPHS["running"] in lbl for lbl in child_labels)


@pytest.mark.asyncio
async def test_graph_tree_root_label_contains_pipeline_name():
    """Tree root label includes the pipeline name."""
    pipeline = Pipeline("cool-pipe").stage("s", _Stub())
    graph = build_graph(pipeline)
    app = NornApp(graph=graph, vm=RunViewModel())

    async with app.run_test() as pilot:
        await pilot.pause()
        tree = app.query_one(Tree)
        assert "cool-pipe" in str(tree.root.label)


@pytest.mark.asyncio
async def test_apply_event_without_graph_does_not_raise():
    """apply_event works when no graph is provided (header-only mode)."""
    vm = RunViewModel()
    app = NornApp(vm=vm)

    async with app.run_test() as pilot:
        # Should not raise even though NornGraph is absent
        app.apply_event(_run_started("no-graph"))
        await pilot.pause()
        assert "no-graph" in app.query_one(NornHeader).get_content()

"""Textual widgets for the Norn TUI — Header, Graph, Transcript, StageDetail, BudgetMeter.

This module imports textual and must only be imported from within ``norn/tui/``.
It must **never** be imported at core (``norn.*``) import time.
"""
from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import RichLog, Static, Tree
from textual.widgets.tree import TreeNode

from norn.graph import PipelineGraph, PipelineNode
from norn.tui.viewmodel import RunViewModel


# ---------------------------------------------------------------------------
# Status glyphs
# ---------------------------------------------------------------------------

STATUS_GLYPHS: dict[str, str] = {
    "pending": "○",
    "running": "▶",
    "passed": "✓",
    "failed": "✗",
    "skipped": "⊘",
    "cached": "◇",
    "retrying": "↺",
    "paused": "⏸",
    "cancelled": "⊗",
}


def _glyph(status: str) -> str:
    return STATUS_GLYPHS.get(status, "?")


# ---------------------------------------------------------------------------
# Header widget
# ---------------------------------------------------------------------------


class NornHeader(Static):
    """Header widget showing live pipeline run summary.

    Reads from a :class:`~norn.tui.viewmodel.RunViewModel` and renders a
    single-line summary: pipeline name, run-id, provider, elapsed time,
    stage progress, and token/cost usage.

    Call :meth:`refresh_vm` after applying an event to the ViewModel to
    update the displayed content.
    """

    def __init__(self, vm: RunViewModel) -> None:
        # markup=False: renders plain external text (names, numbers, raw stage
        # error output) that can contain '[' / '{...}' Textual would otherwise
        # parse as console markup and crash on (MarkupError). As Transcript.
        super().__init__("", markup=False)
        self._vm = vm

    def on_mount(self) -> None:
        """Render initial content when first mounted."""
        self.refresh_vm()

    def get_content(self) -> str:
        """Return the current header text.

        Pure method — reads directly from the ViewModel without going through
        Textual's render pipeline.  Used in tests and by :meth:`refresh_vm`.
        """
        h = self._vm.header
        status_glyph = _glyph(h.status)
        total_tokens = h.total_input_tokens + h.total_output_tokens
        usage = f"${h.total_cost_usd:.4f}" if h.total_cost_usd else f"{total_tokens:,} tokens"
        return (
            f"{status_glyph} {h.pipeline_name or '(no pipeline)'}  "
            f"run: {h.run_id or '-'}  "
            f"provider: {h.provider or '-'}  "
            f"elapsed: {h.elapsed_s:.1f}s  "
            f"stages: {h.stages_done}/{h.stages_started}  "
            f"usage: {usage}"
        )

    def refresh_vm(self) -> None:
        """Re-render from current ViewModel state."""
        self.update(self.get_content())


# ---------------------------------------------------------------------------
# Graph widget
# ---------------------------------------------------------------------------


class NornGraph(Widget):
    """Pipeline structure tree widget with live status glyphs.

    Builds a :class:`textual.widgets.Tree` from a
    :class:`~norn.graph.PipelineGraph` at mount time, then updates node
    labels in place via :meth:`refresh_vm` when the ViewModel changes.

    Multiple simultaneously-running nodes (e.g. inside a Parallel block)
    are fully supported — each node reads its own entry in
    ``vm.node_status`` independently.
    """

    def __init__(self, graph: PipelineGraph, vm: RunViewModel) -> None:
        super().__init__()
        self._graph = graph
        self._vm = vm

    # ------------------------------------------------------------------
    # Compose — build the tree once at mount time
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        tree: Tree[str] = Tree(self._node_label(self._graph.root))
        tree.root.expand()
        self._populate(tree.root, self._graph.root)
        yield tree

    def _node_label(self, node: PipelineNode) -> str:
        """Render a single node label: ``<glyph> <name>``."""
        status = self._vm.node_status.get(node.node_id, "pending")
        return f"{_glyph(status)} {node.name}"

    def _populate(self, tree_node: TreeNode[str], pipeline_node: PipelineNode) -> None:
        """Recursively add *pipeline_node*'s children under *tree_node*."""
        for child in pipeline_node.children:
            label = self._node_label(child)
            if child.children:
                tn = tree_node.add(label, data=child.node_id)
                tn.expand()
                self._populate(tn, child)
            else:
                tree_node.add_leaf(label, data=child.node_id)

    # ------------------------------------------------------------------
    # Refresh — update labels in place without rebuilding the tree
    # ------------------------------------------------------------------

    def refresh_vm(self) -> None:
        """Update all node labels from the current ViewModel status map.

        Walks the existing :class:`~textual.widgets.Tree` and the
        :class:`~norn.graph.PipelineGraph` in lock-step, calling
        ``set_label`` on each ``TreeNode``.  The tree structure is *not*
        rebuilt — only the label text changes.
        """
        try:
            tree: Tree[str] = self.query_one(Tree)
        except Exception:
            return
        self._rebuild_labels(tree.root, self._graph.root)

    def _rebuild_labels(
        self,
        tree_node: TreeNode[str],
        pipeline_node: PipelineNode,
    ) -> None:
        tree_node.set_label(self._node_label(pipeline_node))
        for t_child, p_child in zip(tree_node.children, pipeline_node.children):
            self._rebuild_labels(t_child, p_child)


# ---------------------------------------------------------------------------
# Transcript widget
# ---------------------------------------------------------------------------


class Transcript(RichLog):
    """Live per-stage transcript rendered from :class:`~norn.agents.base.AgentMessageBlock` objects.

    Block-to-line mapping:

    * :class:`~norn.agents.base.TextBlock` — rendered as plain prose.
    * :class:`~norn.agents.base.ToolUseBlock` — ``tool <name> <input_summary>``.
    * :class:`~norn.agents.base.ToolResultBlock` — ``tool_result ok`` or ``tool_result err``.
    * :class:`~norn.agents.base.ThinkingBlock` — rendered dim.

    Content is read directly from the ViewModel and is therefore already
    redacted by the event sink.  Call :meth:`set_stage` to select a stage and
    :meth:`refresh_vm` to re-render after new blocks arrive.
    """

    def __init__(self, vm: RunViewModel) -> None:
        super().__init__(markup=False, highlight=False)
        self._vm = vm
        self._stage_id: str | None = None
        self._rendered = 0

    # ------------------------------------------------------------------
    # Stage selection and refresh
    # ------------------------------------------------------------------

    def set_stage(self, stage_id: str) -> None:
        """Select *stage_id* and render its transcript from the ViewModel."""
        if stage_id != self._stage_id:
            self._stage_id = stage_id
            self.clear()
            self._rendered = 0
        self.refresh_vm()

    def refresh_vm(self) -> None:
        """Render blocks for the current stage that are not on screen yet.

        Appends rather than redrawing.  The app calls this on **every** run
        event, and a streaming command stage produces thousands of blocks —
        clearing and rewriting the whole log each time is quadratic and makes
        the UI crawl exactly when output is heaviest.  A full redraw still
        happens when the selected stage changes, or when the spool shrinks
        (a re-run of the same stage).
        """
        blocks = self._vm.transcript.get(self._stage_id or "", [])
        if len(blocks) < self._rendered:
            self.clear()
            self._rendered = 0
        for block in blocks[self._rendered:]:
            self.write(self._render_block(block))
        self._rendered = len(blocks)

    # ------------------------------------------------------------------
    # Block rendering
    # ------------------------------------------------------------------

    @staticmethod
    def _block_to_str(block: Any) -> str:
        """Convert *block* to a plain-text string (used by :meth:`get_lines`)."""
        from norn.agents.base import TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock  # noqa: PLC0415

        if isinstance(block, TextBlock):
            return block.text
        if isinstance(block, ToolUseBlock):
            return f"tool {block.name} {block.input_summary}"
        if isinstance(block, ToolResultBlock):
            return f"tool_result {'ok' if block.ok else 'err'}"
        if isinstance(block, ThinkingBlock):
            return block.text
        return str(block)

    @staticmethod
    def _render_block(block: Any) -> Text:
        """Convert *block* to a Rich :class:`~rich.text.Text` renderable.

        ``ThinkingBlock`` is styled dim.  Other blocks use the default style
        so that existing terminal theme colours apply cleanly.
        """
        from norn.agents.base import ThinkingBlock  # noqa: PLC0415

        plain = Transcript._block_to_str(block)
        t = Text(plain)
        if isinstance(block, ThinkingBlock):
            t.stylize("dim")
        return t

    def get_lines(self) -> list[str]:
        """Return plain-text lines for the current stage.

        Pure accessor — reads directly from the ViewModel without going
        through Textual's render pipeline.  Used in tests to assert
        transcript content without fighting the render cycle.
        """
        return [
            self._block_to_str(b)
            for b in self._vm.transcript.get(self._stage_id or "", [])
        ]


# ---------------------------------------------------------------------------
# StageDetail widget
# ---------------------------------------------------------------------------


class StageDetail(Static):
    """Panel showing attempts, duration, usage, and artifacts for a stage.

    Data is read from :attr:`~norn.tui.viewmodel.RunViewModel.stage_details`
    which is populated when :class:`~norn.events.StageFinished` events arrive.
    Call :meth:`set_stage` to switch stages and :meth:`refresh_vm` after new
    events to update the display.
    """

    def __init__(self, vm: RunViewModel) -> None:
        # markup=False: renders plain external text (names, numbers, raw stage
        # error output) that can contain '[' / '{...}' Textual would otherwise
        # parse as console markup and crash on (MarkupError). As Transcript.
        super().__init__("", markup=False)
        self._vm = vm
        self._stage_id: str | None = None

    def set_stage(self, stage_id: str) -> None:
        """Select *stage_id* and re-render the detail panel."""
        self._stage_id = stage_id
        self.refresh_vm()

    def get_content(self) -> str:
        """Return the current panel text.

        Pure accessor used in tests and by :meth:`refresh_vm`.
        """
        if self._stage_id is None:
            return ""
        detail = self._vm.stage_details.get(self._stage_id)
        if detail is None:
            # No StageFinished record yet — show the live status so a running
            # stage reads "running" rather than a misleading "pending".
            status = self._vm.node_status.get(self._stage_id, "pending")
            return f"Stage: {self._stage_id}\nStatus: {status}"
        lines = [
            f"Stage: {detail.name}",
            f"Status: {detail.status}",
            f"Attempts: {detail.attempts}",
            f"Duration: {detail.duration_ms}ms",
            f"Input tokens: {detail.usage_input_tokens}",
            f"Output tokens: {detail.usage_output_tokens}",
        ]
        if detail.usage_cost_usd:
            lines.append(f"Cost: ${detail.usage_cost_usd:.4f}")
        if detail.artifacts:
            lines.append(f"Artifacts: {', '.join(detail.artifacts)}")
        if detail.error:
            lines.append(f"Error: {detail.error}")
        return "\n".join(lines)

    def refresh_vm(self) -> None:
        """Re-render from current ViewModel stage-detail state."""
        self.update(self.get_content())


# ---------------------------------------------------------------------------
# BudgetMeter widget
# ---------------------------------------------------------------------------


class BudgetMeter(Static):
    """Displays current cost/token usage against the configured budget.

    Always shows a meaningful value — when cost is ``0`` (token-only /
    subscription / opencode runs) the meter shows a token count rather
    than suppressing the line or displaying ``$0.0000``.  Before any usage
    has been reported (no stage finished yet) it shows ``pending`` instead
    of a misleading ``0 tokens``.

    Pass the :class:`~norn.dsl.Budget` DSL object as *budget* to display
    budget limits alongside the current usage.  ``None`` shows usage only.
    """

    def __init__(self, vm: RunViewModel, budget: Any = None) -> None:
        # markup=False: renders plain external text (names, numbers, raw stage
        # error output) that can contain '[' / '{...}' Textual would otherwise
        # parse as console markup and crash on (MarkupError). As Transcript.
        super().__init__("", markup=False)
        self._vm = vm
        self._budget = budget

    def on_mount(self) -> None:
        """Render initial content when first mounted."""
        self.refresh_vm()

    def get_content(self) -> str:
        """Return the current meter text.

        Pure accessor used in tests and by :meth:`refresh_vm`.
        """
        total_tokens = self._vm.total_input_tokens + self._vm.total_output_tokens
        cost = self._vm.total_cost_usd

        if cost > 0:
            usage_str = f"${cost:.4f}"
        elif total_tokens > 0:
            usage_str = f"{total_tokens:,} tokens"
        else:
            # No usage reported yet — the first stage hasn't finished.
            # Show "pending" instead of a misleading "0 tokens".
            usage_str = "pending"

        if self._budget is not None:
            max_cost = getattr(self._budget, "max_cost_usd", None)
            max_tokens = getattr(self._budget, "max_tokens", None)
            if cost > 0 and max_cost:
                return f"Budget: {usage_str} / ${max_cost:.4f}"
            if max_tokens:
                return f"Budget: {usage_str} / {max_tokens:,} tokens"

        return f"Usage: {usage_str}"

    def refresh_vm(self) -> None:
        """Re-render from current ViewModel usage state."""
        self.update(self.get_content())

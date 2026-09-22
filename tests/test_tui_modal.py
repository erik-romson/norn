"""Unit tests for the in-run input-decision modal (norn/tui/modals.py).

Offline Textual Pilot tests — assert the modal's choices and that a keypress
or Escape dismisses it with the right runner choice code.
"""
from __future__ import annotations

import pytest
from textual.app import App

from norn.tui.modals import InputDecisionModal, choices_for, node_display_name


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_choices_for_by_kind():
    assert [c for c, _ in choices_for("failure_recovery")] == ["r", "c", "a"]
    # A timed-out stage can also be retried with its timeout raised.
    assert [c for c, _ in choices_for("timeout_recovery")] == ["r", "t", "n", "c", "a"]
    # Budget offers three sizes of extension plus abort — continuing raises the
    # limit, so the prompt does not come back after every remaining stage.
    assert [c for c, _ in choices_for("budget")] == ["c", "d", "u", "a"]
    assert dict(choices_for("budget"))["c"] == "+50%"
    assert [c for c, _ in choices_for("step")] == ["r", "s", "a"]
    # Unknown kinds fall back to a safe continue/abort pair.
    assert [c for c, _ in choices_for("anything-else")] == ["c", "a"]


def test_node_display_name_strips_graph_prefixes():
    assert node_display_name("loop:test x/stage:test x") == "test x"
    assert node_display_name("stage:foo") == "foo"
    assert node_display_name("parallel:p/stage:y") == "y"
    assert node_display_name("") == ""
    assert node_display_name(None) == ""


# ---------------------------------------------------------------------------
# Modal behaviour
# ---------------------------------------------------------------------------


def _rendered_text(widget) -> str:
    """Return a widget's painted text — markup already applied or escaped.

    `Static.content` hands back the source string either way, so only the
    rendered strips show whether a `[c]` survived as literal text.
    """
    from textual.geometry import Region

    size = widget.size
    strips = widget.render_lines(Region(0, 0, size.width, size.height))
    return "\n".join(strip.text for strip in strips)


class _Host(App):
    """Minimal app that pushes a modal and records its dismiss result."""

    def __init__(self, modal: InputDecisionModal) -> None:
        super().__init__()
        self._modal = modal
        self.result: str | None = "UNSET"

    def on_mount(self) -> None:
        self.push_screen(self._modal, lambda r: setattr(self, "result", r))


@pytest.mark.asyncio
async def test_modal_keypress_dismisses_with_choice_code():
    app = _Host(InputDecisionModal("failure_recovery", stage_name="s1", excerpt="boom"))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, InputDecisionModal)
        await pilot.press("r")
        await pilot.pause()
    assert app.result == "r"


@pytest.mark.asyncio
async def test_modal_ignores_keys_not_in_choices():
    app = _Host(InputDecisionModal("budget"))  # only c / d / u / a are valid
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r")  # not a budget choice — modal stays open
        await pilot.pause()
        assert isinstance(app.screen, InputDecisionModal)
        await pilot.press("a")
        await pilot.pause()
    assert app.result == "a"


@pytest.mark.asyncio
async def test_modal_buttons_show_their_key_code():
    """Regression: Textual reads an unescaped `[c]` as markup and drops it."""
    from textual.widgets import Button

    app = _Host(InputDecisionModal("budget"))
    async with app.run_test() as pilot:
        await pilot.pause()
        labels = [str(b.label) for b in app.screen.query(Button)]
        await pilot.press("a")
        await pilot.pause()
    assert labels == ["+50% [c]", "+100% [d]", "No limit [u]", "Abort [a]"]


@pytest.mark.asyncio
async def test_modal_excerpt_keeps_bracketed_stage_output():
    """Regression: a stage error containing `[c]` must render literally.

    The excerpt is the stage's own output, not console markup — Textual read
    the `[c]` in implement_features_v2's no-op guidance as a style tag and
    swallowed it.
    """
    excerpt = "ERROR: changed nothing\n[c]ontinue accepts this. [r]etry re-checks."
    app = _Host(InputDecisionModal(
        "failure_recovery", stage_name="assert owned diff [step-20]", excerpt=excerpt,
    ))
    async with app.run_test() as pilot:
        await pilot.pause()
        rendered = _rendered_text(app.screen.query_one("#excerpt"))
        title = _rendered_text(app.screen.query_one("#title"))
        await pilot.press("a")
        await pilot.pause()
    assert "[c]ontinue" in rendered, f"`[c]` swallowed as markup: {rendered!r}"
    assert "[r]etry" in rendered, f"`[r]` swallowed as markup: {rendered!r}"
    assert "[step-20]" in title, f"stage name mangled: {title!r}"


@pytest.mark.asyncio
async def test_modal_survives_unbalanced_markup_in_excerpt():
    """An unbalanced closing tag in stage output must not crash the modal."""
    app = _Host(InputDecisionModal("failure_recovery", excerpt="assert x == [/y]"))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, InputDecisionModal)
        await pilot.press("a")
        await pilot.pause()
    assert app.result == "a"


@pytest.mark.asyncio
async def test_modal_shows_every_budget_choice_in_a_narrow_terminal():
    """All four choices must stay clickable at 64 columns, Abort included."""
    from textual.widgets import Button

    app = _Host(InputDecisionModal("budget", stage_name="closeout step-14", excerpt="$31 > $30"))
    async with app.run_test(size=(64, 24)) as pilot:
        await pilot.pause()
        buttons = app.screen.query(Button)
        count = len(buttons)
        widths = sum(b.outer_size.width for b in buttons)
        available = app.screen.query_one("#dialog").content_size.width
        await pilot.press("a")
        await pilot.pause()
    assert count == 4
    assert widths <= available


@pytest.mark.asyncio
async def test_modal_escape_aborts():
    app = _Host(InputDecisionModal("failure_recovery"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert app.result == "a"


@pytest.mark.asyncio
async def test_modal_button_click_dismisses():
    app = _Host(InputDecisionModal("step"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click("#choice-s")
        await pilot.pause()
    assert app.result == "s"

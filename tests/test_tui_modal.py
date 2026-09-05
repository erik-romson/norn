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
    assert [c for c, _ in choices_for("budget")] == ["c", "a"]
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
    app = _Host(InputDecisionModal("budget"))  # only c / a are valid
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r")  # not a budget choice — modal stays open
        await pilot.pause()
        assert isinstance(app.screen, InputDecisionModal)
        await pilot.press("a")
        await pilot.pause()
    assert app.result == "a"


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

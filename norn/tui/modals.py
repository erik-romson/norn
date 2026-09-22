"""In-run input-decision modal for the Norn TUI.

When the runner blocks on a :class:`~norn.events.WaitingInput` (a stage
failure, an exhausted loop, a budget overrun, or a step gate) it awaits an
answer through the :class:`~norn.responder.TUIResponder`.  This modal lets the
user pick that answer instead of the old behaviour of silently auto-answering
``"c"``.

The modal dismisses with the single-character code the runner's responder
understands:

* ``failure_recovery`` → ``r`` (retry), ``c`` (continue past), ``a`` (abort)
* ``timeout_recovery`` → the same, plus ``t``/``n`` (retry with the timeout
  raised — see ``norn.dsl.TIMEOUT_EXTENSIONS``)
* ``budget``           → ``c``/``d``/``u`` (continue with the limit raised by
  that much — see ``norn.dsl.BUDGET_EXTENSIONS``), ``a`` (abort)
* ``step``             → ``r`` (run), ``s`` (skip), ``a`` (abort)
* anything else        → ``c`` (continue), ``a`` (abort)

It is ``textual``-only and imported lazily by the run screen, never at core
import time.
"""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static

from norn.dsl import budget_choices, failure_choices

# kind -> ordered list of (choice code, button label)
_CHOICES: dict[str, list[tuple[str, str]]] = {
    "failure_recovery": failure_choices(),
    "timeout_recovery": failure_choices(timed_out=True),
    "budget": budget_choices(),
    "step": [("r", "Run"), ("s", "Skip"), ("a", "Abort")],
}
_DEFAULT_CHOICES: list[tuple[str, str]] = [("c", "Continue"), ("a", "Abort")]

_TITLES: dict[str, str] = {
    "failure_recovery": "Stage failed",
    "timeout_recovery": "Stage timed out",
    "budget": "Budget exceeded",
    "step": "Next stage",
    "agent": "Agent is waiting for input",
}

_BUTTON_VARIANTS: dict[str, str] = {
    "r": "primary",
    "t": "primary",
    "n": "default",
    "c": "warning",
    "d": "warning",
    "u": "default",
    "s": "default",
    "a": "error",
}

# Shown under a budget prompt so the choices read as "how much more room",
# not "wave this one stage through".  Kept short enough to fit the dialog at
# its narrowest, where a longer line is clipped rather than wrapped.
_HINTS: dict[str, str] = {
    "budget": "Raises the limit above what this run has spent.",
    "timeout_recovery": "Retry as before, or with the stage's timeout raised.",
}


def choices_for(kind: str) -> list[tuple[str, str]]:
    """Return the ordered ``(code, label)`` choices for a WaitingInput *kind*."""
    return _CHOICES.get(kind, _DEFAULT_CHOICES)


def node_display_name(stage_id: str | None) -> str:
    """Turn a graph node id (``loop:x/stage:y``) into a readable name (``y``)."""
    if not stage_id:
        return ""
    return stage_id.split("/")[-1].split(":", 1)[-1]


def _trim_excerpt(text: str, *, max_lines: int = 8) -> str:
    """Keep only the last *max_lines* non-empty lines so the modal stays small."""
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return "\n".join(lines[-max_lines:])


class InputDecisionModal(ModalScreen[str]):
    """Modal asking the user how to resolve a blocking ``WaitingInput``.

    Dismisses with the chosen single-character code; the run screen passes it
    to ``RunController.answer_input`` to unblock the runner.
    """

    DEFAULT_CSS = """
    InputDecisionModal {
        align: center middle;
    }
    InputDecisionModal #dialog {
        width: auto;
        min-width: 56;
        max-width: 90%;
        height: auto;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }
    InputDecisionModal #title {
        text-style: bold;
        width: 100%;
    }
    InputDecisionModal #excerpt {
        color: $text-muted;
        height: auto;
        max-height: 8;
        margin: 1 0 0 0;
    }
    InputDecisionModal #hint {
        color: $text-muted;
        margin: 1 0;
    }
    InputDecisionModal #buttons {
        height: auto;
        align-horizontal: center;
    }
    InputDecisionModal Button {
        margin: 0 1;
        /* Textual's default 16-cell minimum makes four choices need a 78-column
           terminal; the labels here are short, so let them size themselves. */
        min-width: 10;
    }
    """

    def __init__(self, kind: str, *, stage_name: str = "", excerpt: str = "") -> None:
        super().__init__()
        self._kind = kind
        self._stage_name = stage_name
        self._excerpt = _trim_excerpt(excerpt) if excerpt else ""
        self._choices = choices_for(kind)
        self._valid = {code for code, _ in self._choices}

    @property
    def choices(self) -> list[tuple[str, str]]:
        """The ordered ``(code, label)`` choices presented (exposed for tests)."""
        return self._choices

    def compose(self) -> ComposeResult:
        title = _TITLES.get(self._kind, "Input needed")
        if self._stage_name:
            title = f"{title}: {self._stage_name}"
        with Vertical(id="dialog"):
            # markup=False on the title and excerpt: both carry external text
            # (a stage name, a stage's own error output).  Textual would read a
            # bare `[c]` in them as a style tag and swallow it, and raise
            # MarkupError on an unbalanced `[/x]`.  The hint is ours and uses
            # `\[` escapes deliberately, so it keeps markup on.
            yield Label(title, id="title", markup=False)
            if self._excerpt:
                yield Static(self._excerpt, id="excerpt", markup=False)
            yield Label(self._hint(), id="hint")
            with Horizontal(id="buttons"):
                for code, label in self._choices:
                    yield Button(
                        # Escaped: Textual reads `[c]` in a label as markup and
                        # swallows it, which hid the key codes entirely.
                        f"{label} \\[{code}]",
                        id=f"choice-{code}",
                        variant=_BUTTON_VARIANTS.get(code, "default"),
                    )

    def _hint(self) -> str:
        keys = "   ".join(f"\\[{code}] {label}" for code, label in self._choices)
        note = _HINTS.get(self._kind)
        return f"{note}\n{keys}" if note else keys

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        code = str(event.button.id or "").removeprefix("choice-")
        if code:
            self.dismiss(code)

    def on_key(self, event: Any) -> None:
        """Resolve on a single keypress matching a choice code; Esc aborts."""
        key = event.key
        if key in self._valid:
            event.stop()
            self.dismiss(key)
        elif key == "escape":
            event.stop()
            # Prefer abort when offered; otherwise the last (safest) choice.
            self.dismiss("a" if "a" in self._valid else self._choices[-1][0])

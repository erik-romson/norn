"""Unit tests for the half-block logo renderer in ``norn.tui.widgets``."""
from __future__ import annotations

from norn.tui.widgets import LOGO_PALETTE, LOGO_PIXELS, logo_text


def test_logo_renders_two_pixel_rows_per_line() -> None:
    """Each terminal line packs two pixel rows, one cell per pixel column."""
    lines = logo_text().plain.split("\n")
    assert len(lines) == len(LOGO_PIXELS) // 2
    assert {len(line) for line in lines} == {len(LOGO_PIXELS[0])}


def test_logo_pixels_use_only_palette_keys() -> None:
    """Every pixel is a palette colour or the transparent ``.``."""
    used = {key for row in LOGO_PIXELS for key in row}
    assert used <= set(LOGO_PALETTE) | {"."}


def test_logo_picks_glyph_per_cell() -> None:
    """Transparent → blank, transparent top → lower block, solid → painted blank, mixed → upper block."""
    text = logo_text(["..kc", ".kkk"])
    assert text.plain == " ▄ ▀"
    solid_style = next(span.style for span in text.spans if span.start == 2)
    assert solid_style.bgcolor.name == LOGO_PALETTE["k"]

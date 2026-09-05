from __future__ import annotations

from norn.contrib.extractors.adf import adf_to_text, field_to_text


def _doc(*content) -> dict:
    return {"type": "doc", "version": 1, "content": list(content)}


def _paragraph(text: str) -> dict:
    return {"type": "paragraph", "content": [{"type": "text", "text": text}]}


# ---------------------------------------------------------------------------
# adf_to_text
# ---------------------------------------------------------------------------


def test_adf_plain_string_passthrough():
    assert adf_to_text("already plain") == "already plain"


def test_adf_none_returns_empty():
    assert adf_to_text(None) == ""


def test_adf_single_paragraph():
    assert adf_to_text(_doc(_paragraph("Hello world"))).strip() == "Hello world"


def test_adf_multiple_paragraphs_separated_by_newline():
    doc = _doc(_paragraph("First"), _paragraph("Second"))
    assert adf_to_text(doc).strip() == "First\nSecond"


def test_adf_hard_break():
    doc = _doc({
        "type": "paragraph",
        "content": [
            {"type": "text", "text": "line1"},
            {"type": "hardBreak"},
            {"type": "text", "text": "line2"},
        ],
    })
    assert adf_to_text(doc).strip() == "line1\nline2"


def test_adf_table_cells_tab_separated():
    table = {
        "type": "table",
        "content": [
            {
                "type": "tableRow",
                "content": [
                    {"type": "tableCell", "content": [_paragraph("a")]},
                    {"type": "tableCell", "content": [_paragraph("b")]},
                ],
            }
        ],
    }
    text = adf_to_text(_doc(table))
    assert "a" in text and "b" in text
    assert "\t" in text


def test_adf_unknown_node_still_traversed():
    # A node type the flattener doesn't special-case must not drop its text.
    doc = _doc({"type": "panel", "content": [_paragraph("inside panel")]})
    assert "inside panel" in adf_to_text(doc)


# ---------------------------------------------------------------------------
# field_to_text
# ---------------------------------------------------------------------------


def test_field_to_text_plain_string_unchanged():
    assert field_to_text("plain description") == "plain description"


def test_field_to_text_none_is_empty():
    assert field_to_text(None) == ""


def test_field_to_text_adf_is_flattened_and_stripped():
    doc = _doc(_paragraph("NPE in Foo.bar()"))
    assert field_to_text(doc) == "NPE in Foo.bar()"

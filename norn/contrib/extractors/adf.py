from __future__ import annotations

from typing import Any

# ADF block nodes are flattened with a trailing newline so paragraphs,
# headings, list items and table rows stay on separate lines.
_BLOCK_TYPES = {
    "paragraph",
    "heading",
    "blockquote",
    "codeBlock",
    "listItem",
    "tableRow",
    "rule",
}
# Table cells are separated by a tab within their row.
_CELL_TYPES = {"tableCell", "tableHeader"}


def adf_to_text(node: Any) -> str:
    """Recursively flatten an Atlassian Document Format (ADF) node to plain text.

    Accepts an ADF document dict (``{"type": "doc", "content": [...]}``), a list
    of nodes, a plain string (returned as-is), or ``None`` (returns ``""``).
    Unknown node types are still traversed for their ``content`` children, so
    text is never silently dropped.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_to_text(n) for n in node)
    if not isinstance(node, dict):
        return ""

    node_type = node.get("type")
    if node_type == "text":
        return node.get("text", "")
    if node_type == "hardBreak":
        return "\n"

    text = "".join(adf_to_text(child) for child in node.get("content", []))
    if node_type in _CELL_TYPES:
        return text + "\t"
    if node_type in _BLOCK_TYPES:
        return text + "\n"
    return text


def field_to_text(value: Any) -> str:
    """Normalize a Jira text field that may be plain text or ADF to a string.

    The REST API v3 returns ``description`` and comment bodies as ADF dicts,
    while older/rendered responses return plain strings. Returns plain strings
    unchanged and flattens ADF (stripped of trailing block whitespace).
    """
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return adf_to_text(value).strip()

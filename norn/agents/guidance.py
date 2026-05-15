"""Portable project guidance resolution for agent providers.

When ``setting_sources=["project"]`` is requested, this module reads
canonical guidance files from the project directory and returns their
content as a single string suitable for injection into the system prompt.

Supported sources (in order):

- ``AGENTS.md`` — canonical repo instruction file.
- ``CLAUDE.md`` — Claude Code compatibility guidance.
- ``opencode.json`` — OpenCode project config; only ``instructions``
  entries (file paths or globs) are read and appended.

Private/local config is explicitly excluded:

- ``.claude/settings.local.json``
- OpenCode global config, ``OPENCODE_CONFIG``, ``OPENCODE_CONFIG_CONTENT``,
  managed config, and MDM preferences.
"""

from __future__ import annotations

import glob as glob_mod
import json
import logging
import pathlib

log = logging.getLogger(__name__)


def resolve_project_guidance(cwd: str | None = None) -> str | None:
    """Read portable project guidance from the working directory.

    Args:
        cwd: Directory to search for guidance files. Defaults to the
            current working directory.

    Returns:
        Concatenated guidance text, or ``None`` if no guidance files
        were found.
    """
    root = pathlib.Path(cwd) if cwd else pathlib.Path.cwd()
    parts: list[str] = []

    # AGENTS.md — canonical guidance
    agents_md = root / "AGENTS.md"
    if agents_md.is_file():
        content = agents_md.read_text().strip()
        if content:
            parts.append(f"# Project guidance (AGENTS.md)\n\n{content}")
            log.debug("[guidance] Loaded AGENTS.md from %s", agents_md)

    # CLAUDE.md — compatibility guidance
    claude_md = root / "CLAUDE.md"
    if claude_md.is_file():
        content = claude_md.read_text().strip()
        if content:
            parts.append(f"# Project guidance (CLAUDE.md)\n\n{content}")
            log.debug("[guidance] Loaded CLAUDE.md from %s", claude_md)

    # opencode.json — only ``instructions`` entries
    opencode_json = root / "opencode.json"
    if opencode_json.is_file():
        _append_opencode_instructions(opencode_json, root, parts)

    return "\n\n".join(parts) if parts else None


def _append_opencode_instructions(
    config_path: pathlib.Path,
    root: pathlib.Path,
    parts: list[str],
) -> None:
    """Read ``instructions`` from an OpenCode config and append file contents."""
    try:
        data = json.loads(config_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("[guidance] Failed to read %s: %s", config_path, exc)
        return

    instructions = data.get("instructions")
    if not instructions:
        return

    if isinstance(instructions, str):
        instructions = [instructions]

    if not isinstance(instructions, list):
        log.warning("[guidance] opencode.json 'instructions' is not a string or list, skipping")
        return

    for entry in instructions:
        if not isinstance(entry, str):
            continue
        _resolve_instruction_entry(entry, root, parts)


def _resolve_instruction_entry(
    entry: str,
    root: pathlib.Path,
    parts: list[str],
) -> None:
    """Resolve a single OpenCode instruction entry (file path or glob)."""
    # Try as a literal file path first
    candidate = root / entry
    if candidate.is_file():
        content = candidate.read_text().strip()
        if content:
            parts.append(f"# Project guidance ({entry})\n\n{content}")
            log.debug("[guidance] Loaded opencode instruction file %s", candidate)
        return

    # Try as a glob pattern
    matched = sorted(glob_mod.glob(str(root / entry)))
    if not matched:
        log.debug("[guidance] opencode instruction entry matched no files: %s", entry)
        return

    for match_path_str in matched:
        match_path = pathlib.Path(match_path_str)
        if match_path.is_file():
            content = match_path.read_text().strip()
            if content:
                rel = match_path.relative_to(root)
                parts.append(f"# Project guidance ({rel})\n\n{content}")
                log.debug("[guidance] Loaded opencode glob match %s", match_path)

from __future__ import annotations

import logging
import pathlib
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class Skill:
    """An inline skill definition with a name and markdown content.

    Use when you want to define a skill directly in the pipeline config
    rather than loading from a file. The content is injected into the
    agent's ``system_prompt``.

    Attributes:
        name: Display name for the skill (shown in dry-run output).
        content: Markdown instructions injected into the system prompt.

    Example::

        strict = Skill(
            name="strict-review",
            content="Flag any function longer than 30 lines.",
        )
        Stage("review", Generate(prompt="Review src/", skills=[strict]))
    """

    name: str
    content: str


def _skill_candidates(name: str) -> list[pathlib.Path]:
    """Return candidate paths for a skill in resolution order.

    Supports plain names (``review-pr``) and qualified names (``pkg:skill``).
    Each base is tried in both layouts: the flat ``<name>.md`` file and the
    Claude Code directory layout ``<name>/SKILL.md``.
    """
    if ":" in name:
        package, skill = name.split(":", 1)
        bases: list[pathlib.Path] = [
            pathlib.Path("skills") / package,
            pathlib.Path(".claude") / "skills" / package,
            pathlib.Path.home() / ".claude" / "skills" / package,
        ]
    else:
        skill = name
        bases = [
            pathlib.Path("skills"),
            pathlib.Path(".claude") / "skills",
            pathlib.Path.home() / ".claude" / "skills",
        ]
    candidates: list[pathlib.Path] = []
    for base in bases:
        candidates.append(base / f"{skill}.md")
        candidates.append(base / skill / "SKILL.md")
    return candidates


def resolve_skill_content(skill: str | Skill) -> str:
    """Resolve a skill name or inline ``Skill`` to its markdown content.

    Resolution order: pipeline-local → project → user.

    Raises ``FileNotFoundError`` if a named skill cannot be found.
    """
    if isinstance(skill, Skill):
        return skill.content

    for candidate in _skill_candidates(skill):
        if candidate.exists():
            log.debug("[skills] Resolved %r → %s", skill, candidate)
            return candidate.read_text()

    searched = [str(p) for p in _skill_candidates(skill)]
    raise FileNotFoundError(f"Skill {skill!r} not found. Searched: {searched}")

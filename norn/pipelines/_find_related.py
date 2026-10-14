"""Find learnings related to a set of keywords (stdlib only).

Usage::

    python3 <abs path>/_find_related.py <store> <keyword> [<keyword> ...]

Run as a standalone script (like ``_step_snapshot.py``), so it must not import
anything from ``norn``; it is also importable for tests.

Recursively reads ``*.md`` under the store, the store root included. The
frontmatter is read line by line (no YAML library); the text of ``title``,
``tags``, ``module``, ``component``, ``problem_type`` and ``applies_when`` is
matched against each keyword, case-insensitively. A doc's score is the number
of distinct keywords it matched. Above 25 candidates the top 25 are kept
(score descending, then path); below 3, body-text matches are added after the
frontmatter matches (CE ``research.md``).
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

MAX_CANDIDATES = 25
MIN_CANDIDATES = 3
HEAD_LINES = 30
_FIELDS = frozenset({"title", "tags", "module", "component", "problem_type", "applies_when"})


@dataclass
class Candidate:
    path: Path
    matched: list[str]
    score: int
    head: list[str] = field(default_factory=list)


def _split(text: str) -> tuple[list[str], str]:
    """Return (frontmatter lines, body)."""
    lines = text.splitlines()
    if lines and lines[0] == "---":
        for i in range(1, len(lines)):
            if lines[i] == "---":
                return lines[1:i], "\n".join(lines[i + 1 :])
    return [], text


def _field_text(fm_lines: list[str]) -> str:
    collected: list[str] = []
    current: str | None = None
    for line in fm_lines:
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):(.*)$", line)
        if m:
            current = m.group(1)
            if current in _FIELDS:
                collected.append(m.group(2))
        elif current in _FIELDS and (line[:1].isspace() or line.startswith("-")):
            collected.append(line)
    return "\n".join(collected)


def _matches(haystack: str, keywords: list[str]) -> list[str]:
    low = haystack.lower()
    return [kw for kw in keywords if kw in low]


def find_related(store: Path | str, keywords: list[str]) -> list[Candidate]:
    """Return related learnings under *store*; a missing store gives an empty list."""
    store = Path(store)
    kws: list[str] = []
    for kw in keywords:
        kw = kw.strip().lower()
        if kw and kw not in kws:
            kws.append(kw)
    if not store.is_dir() or not kws:
        return []

    docs: list[tuple[Path, str, str, str]] = []
    for path in sorted(store.rglob("*.md")):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        fm_lines, body = _split(text)
        docs.append((path, text, _field_text(fm_lines), body))

    def candidate(path: Path, text: str, matched: list[str]) -> Candidate:
        return Candidate(path, matched, len(matched), text.splitlines()[:HEAD_LINES])

    found: list[Candidate] = []
    for path, text, fields, _body in docs:
        matched = _matches(fields, kws)
        if matched:
            found.append(candidate(path, text, matched))
    found.sort(key=lambda c: (-c.score, str(c.path)))
    found = found[:MAX_CANDIDATES]

    if len(found) < MIN_CANDIDATES:
        have = {c.path for c in found}
        extra: list[Candidate] = []
        for path, text, _fields, body in docs:
            if path in have:
                continue
            matched = _matches(body, kws)
            if matched:
                extra.append(candidate(path, text, matched))
        extra.sort(key=lambda c: (-c.score, str(c.path)))
        found = (found + extra)[:MAX_CANDIDATES]
    return found


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: _find_related.py <store> <keyword> [<keyword> ...]", file=sys.stderr)
        return 2
    for cand in find_related(argv[0], argv[1:]):
        print(f"=== {cand.path} (matched: {', '.join(cand.matched)})")
        for line in cand.head:
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

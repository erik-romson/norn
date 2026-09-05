"""Private helper for fix_jira_issue -- argv parsing, artifact paths, and prompts."""
from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

# Flags that consume the following token as their value (mirrors _preplan.py).
_VALUE_FLAGS = {"--arg", "--skip", "--org", "--agent-provider"}

# Regex that matches a Jira issue key, e.g. CBS-2249 or PROJ-1.
_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")

# Regex for /browse/<KEY> URL fragment.
_BROWSE_RE = re.compile(r"/browse/([A-Z][A-Z0-9]*-\d+)")

BRIEF_HEADINGS = [
    "## Summary",
    "## Background",
    "## Problem",
    "## Acceptance Criteria",
    "## Out of Scope",
    "## Attachments",
]


class ArtifactPaths(NamedTuple):
    dir: str
    issue_json: str
    issue_md: str
    attachments: str
    preplan: str


def resolve_issue_args(argv: list[str]) -> tuple[str, bool]:
    """Return ``(ISSUE_KEY, stop_flag)`` extracted from *argv*.

    Token rules:
    - A token matching ``^[A-Z][A-Z0-9]*-\\d+$`` is the key.
    - A ``/browse/<KEY>`` URL extracts the key from the path.
    - The literal ``stop`` sets the stop flag.
    - Flag tokens and their values (from ``_VALUE_FLAGS``) are skipped.
    - All other tokens are ignored.

    Raises ``ValueError`` (never ``SystemExit``) when zero or more than one
    key is found.
    """
    keys: list[str] = []
    stop_flag = False
    i = 0
    while i < len(argv):
        token = argv[i]
        if token.startswith("-"):
            # Drop flags; consume their value token when needed.
            if "=" not in token and token in _VALUE_FLAGS and i + 1 < len(argv):
                i += 2
            else:
                i += 1
            continue

        if token == "stop":
            stop_flag = True
            i += 1
            continue

        # URL with /browse/<KEY>
        m = _BROWSE_RE.search(token)
        if m:
            keys.append(m.group(1))
            i += 1
            continue

        # Bare issue key
        if _KEY_RE.match(token):
            keys.append(token)
            i += 1
            continue

        i += 1

    if len(keys) == 0:
        raise ValueError(
            "fix_jira_issue needs a Jira issue key as its positional argument, "
            "e.g. norn run fix_jira_issue CBS-2249"
        )
    if len(keys) > 1:
        found = ", ".join(keys)
        raise ValueError(
            f"fix_jira_issue found multiple issue keys and cannot choose: {found}"
        )

    return keys[0], stop_flag


def artifact_paths(project_dir: str, key: str) -> ArtifactPaths:
    """Return an :class:`ArtifactPaths` namedtuple for *key* under *project_dir*."""
    base = Path(project_dir) / "tmp" / "jira" / key
    return ArtifactPaths(
        dir=str(base) + "/",
        issue_json=str(base / "issue.json"),
        issue_md=str(base / "issue.md"),
        attachments=str(base / "attachments"),
        preplan=str(base / f"{key}-preplan.md"),
    )


def brief_prompt(issue_md: str, attachments: str, out: str, project_dir: str) -> str:
    """Return the haiku prompt for generating the issue brief.

    Mentions absolute paths and includes every heading from the brief skeleton.
    Rules: no invention, traceable, keep reporter terminology, <= ~1500 words.
    """
    headings_block = "\n".join(BRIEF_HEADINGS)
    return (
        f"You are a senior engineer writing a concise issue brief.\n\n"
        f"Source files:\n"
        f"- Issue description: {issue_md}\n"
        f"- Attachments directory: {attachments}\n\n"
        f"Write the brief to: {out}\n\n"
        f"The brief must contain exactly these sections (in order):\n"
        f"{headings_block}\n\n"
        f"Rules:\n"
        f"- Do not invent facts not present in the source material.\n"
        f"- Every claim must be traceable to the issue or an attachment.\n"
        f"- Keep the reporter's own terminology; do not paraphrase key terms.\n"
        f"- Total length: <= ~1500 words.\n"
        f"- Use the project directory for any relative path resolution: {project_dir}\n"
    )

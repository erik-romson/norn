"""Shared utilities for dogfooding pipelines.

Reusable functions for stage factories, markdown parsing, and git helpers.
All functions are pure with clear inputs — no global state, no classes.
"""

from __future__ import annotations

import re
import shlex
import subprocess

from norn.stages.generate import Generate
from norn.stages.run_command import RunCommand


# ---------------------------------------------------------------------------
# Stage factories
# ---------------------------------------------------------------------------


def generate(project_dir: str, prompt: str, *, read_only: bool = False) -> Generate:
    """Build a ``Generate`` with the standard working-dir preamble and tools."""
    full_prompt = (
        f"## Working directory\n{project_dir}\n\n"
        "IMPORTANT: When creating or editing files, always use absolute paths "
        f"based on {project_dir}.\n\n"
        f"{prompt}"
    )
    if read_only:
        allowed_tools = ["Read", "Glob", "Grep", "Bash"]
    else:
        allowed_tools = ["Read", "Write", "Edit", "Glob", "Grep", "Bash"]
    return Generate(
        prompt=full_prompt,
        allowed_tools=allowed_tools,
        permission_mode="acceptEdits",
        cwd=project_dir,
        setting_sources=["project"],
    )


def clean_worktree(project_dir: str) -> RunCommand:
    """Gate: fail if the working tree is dirty."""
    return RunCommand(cmd=(
        f'cd {shlex.quote(project_dir)} && '
        'if [ -n "$(git status --porcelain)" ]; then '
        'echo "ERROR: Working tree is not clean. Commit or .gitignore these files:" && '
        'git status --short && exit 1; fi'
    ))


def preflight(project_dir: str, *tools: str) -> RunCommand:
    """Gate: check that each tool is on PATH and print versions."""
    parts = [f'cd {shlex.quote(project_dir)}']
    for tool in tools:
        parts.append(
            f'command -v {shlex.quote(tool)} >/dev/null || '
            f'{{ echo "ERROR: {tool} not on PATH"; exit 1; }}'
        )
    for tool in tools:
        parts.append(f'{shlex.quote(tool)} --version')
    return RunCommand(cmd=" && ".join(parts))


def record_start(project_dir: str) -> RunCommand:
    """Capture the starting commit SHA."""
    return RunCommand(
        cmd=f"cd {shlex.quote(project_dir)} && git rev-parse HEAD"
    )


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def parse_front_matter(text: str) -> tuple[dict, str]:
    """Parse a tiny subset of YAML front-matter: ``key: value`` and ``key:``
    + indented ``- item`` lists.  Returns ``(dict, body)``."""
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    if not m:
        return {}, text
    block, body = m.group(1), m.group(2)
    data: dict = {}
    current_list_key: str | None = None
    for raw_line in block.splitlines():
        if not raw_line.strip():
            current_list_key = None
            continue
        if raw_line.lstrip().startswith("- ") and current_list_key:
            data[current_list_key].append(raw_line.lstrip()[2:].strip())
            continue
        if ":" in raw_line:
            k, v = raw_line.split(":", 1)
            k, v = k.strip(), v.strip()
            if v == "":
                data[k] = []
                current_list_key = k
            else:
                data[k] = v
                current_list_key = None
    return data, body


def first_h1(text: str) -> str | None:
    """Return the text of the first ``# H1`` heading, or ``None``."""
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def already_committed_steps(project_dir: str) -> set[str]:
    """Return step names whose ``refactor: <name>`` commit is already on HEAD.

    Used for resume support — those steps are skipped at build time.
    """
    try:
        out = subprocess.check_output(
            ["git", "-C", project_dir, "log", "--pretty=%s", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return set()
    done = set()
    for subject in out.splitlines():
        m = re.match(r"^refactor:\s+(\S+)", subject)
        if m:
            done.add(m.group(1))
    return done

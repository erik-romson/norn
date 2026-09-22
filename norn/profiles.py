from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class SessionProfile:
    """Reusable bundle of tool permissions and hooks for a pipeline phase."""

    name: str
    permission_mode: str
    allowed_tools: list[str] | None = None
    blocked_patterns: list[str] | None = None
    env: dict[str, str] | None = None
    max_turns: int | None = None


ANALYSIS = SessionProfile(
    name="analysis",
    permission_mode="plan",
    allowed_tools=["Read", "Grep", "Glob"],
)

CODING = SessionProfile(
    name="coding",
    permission_mode="bypassPermissions",
    allowed_tools=["Read", "Edit", "Write", "Bash", "Grep", "Glob"],
    blocked_patterns=[
        "rm -rf /",
        "rm -rf ~",
        "DROP TABLE",
        "DROP DATABASE",
        "git push --force",
        "git push -f",
        "curl.*| bash",
        "wget.*| bash",
    ],
)

SHIPPING = SessionProfile(
    name="shipping",
    permission_mode="default",
    allowed_tools=["Read", "Bash", "Grep", "Glob"],
    blocked_patterns=["git checkout", "git reset", "git rebase", "rm ", "mv ", "cp "],
)


def build_block_hooks(patterns: list[str]) -> dict:
    """Build claude-agent-sdk hook config to block dangerous Bash commands."""
    checks = []
    for p in patterns:
        escaped = p.replace("'", "'\\''")
        checks.append(
            f"echo \"$cmd\" | grep -qF '{escaped}' && echo 'BLOCKED: {escaped}' >&2 && exit 2"
        )
    script = (
        "#!/bin/bash\n"
        "cmd=$(cat - | python3 -c \"import sys,json; d=json.load(sys.stdin);"
        " print(d.get('tool_input',{}).get('command',''))\" 2>/dev/null || true)\n"
    )
    script += "\n".join(checks) + "\nexit 0"
    return {
        "PreToolUse": [
            {
                "matcher": {"tool_name": "Bash"},
                "hook": {
                    "type": "command",
                    "command": script,
                },
            }
        ]
    }


def build_write_allowlist_hooks(allowed_paths: list[str]) -> dict:
    """Build a PreToolUse hook that denies file writes outside *allowed_paths*.

    A prompt that says "do not edit any file except X" is guidance, not a
    constraint: under ``permission_mode="acceptEdits"`` an agent that decides a
    bug is worth fixing simply fixes it, and the violation only surfaces later
    at a postcondition gate — after the tokens are spent.  This turns the
    instruction into a permission decision the agent cannot talk its way past.

    Paths are compared as real paths, so a symlink or a ``../`` detour cannot
    smuggle a write past the allowlist.  Tools with no ``file_path`` in their
    input are left alone; the matcher already limits this to the edit tools.

    Residual gap: ``Bash`` can still write (``echo > file``).  Blocking that
    means pattern-matching shell text, which is why the postcondition gates
    remain the backstop rather than being replaced by this hook.
    """
    allowed = {os.path.realpath(p) for p in allowed_paths}

    async def _deny_write_outside(input_data: dict, tool_use_id: str, context: object) -> dict:
        file_path = (input_data.get("tool_input") or {}).get("file_path")
        if not file_path:
            return {}
        if os.path.realpath(file_path) in allowed:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"This stage may write only: {', '.join(sorted(allowed))}. "
                    f"Writing {file_path} is blocked. Report the change you wanted "
                    "to make as a finding instead of making it."
                ),
            }
        }

    return {
        "PreToolUse": [
            {
                "matcher": "Write|Edit|MultiEdit|NotebookEdit",
                "hooks": [_deny_write_outside],
            }
        ]
    }

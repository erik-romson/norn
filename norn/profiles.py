from __future__ import annotations

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

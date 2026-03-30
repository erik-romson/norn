from __future__ import annotations

import json
import shlex
import subprocess

from claude_agent_sdk import tool

_SCHEMA_CHECK_CI = {"repo": str, "branch": str}

_SCHEMA_CREATE_PR = {
    "type": "object",
    "properties": {
        "repo": {"type": "string"},
        "branch": {"type": "string"},
        "title": {"type": "string"},
        "body": {"type": "string"},
        "draft": {"type": "boolean"},
    },
    "required": ["repo", "branch", "title", "body"],
}

_SCHEMA_NOTIFY = {"channel": str, "message": str}


@tool(
    "check_ci_status",
    "Check GitHub Actions workflow status for a branch.",
    _SCHEMA_CHECK_CI,
)
async def check_ci_status(args: dict) -> dict:
    """Poll CI workflow status via gh CLI."""
    repo: str = args["repo"]
    branch: str = args["branch"]

    cmd = (
        f"gh run list -R {shlex.quote(repo)} -b {shlex.quote(branch)}"
        " --json status,conclusion,name,databaseId -L 5"
    )
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return {
            "content": [{"type": "text", "text": f"Error: {result.stderr.strip()}"}],
            "is_error": True,
        }
    return {"content": [{"type": "text", "text": result.stdout}]}


@tool(
    "create_pr",
    "Create a GitHub pull request.",
    _SCHEMA_CREATE_PR,
)
async def create_pr(args: dict) -> dict:
    """Create a pull request using the gh CLI."""
    repo: str = args["repo"]
    branch: str = args["branch"]
    title: str = args["title"]
    body: str = args["body"]
    draft: bool = args.get("draft", False)

    cmd = (
        f"gh pr create -R {shlex.quote(repo)}"
        f" --head {shlex.quote(branch)}"
        f" --title {shlex.quote(title)}"
        f" --body {shlex.quote(body)}"
    )
    if draft:
        cmd += " --draft"

    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
    output = {"url": result.stdout.strip(), "success": result.returncode == 0}
    if result.returncode != 0:
        output["error"] = result.stderr.strip()
        return {"content": [{"type": "text", "text": json.dumps(output)}], "is_error": True}
    return {"content": [{"type": "text", "text": json.dumps(output)}]}


@tool(
    "notify",
    "Send a notification via a Slack webhook. channel is a Slack webhook URL or '#channel' identifier.",
    _SCHEMA_NOTIFY,
)
async def notify(args: dict) -> dict:
    """Send a notification message via httpx to a Slack-compatible webhook."""
    channel: str = args["channel"]
    message: str = args["message"]

    try:
        import httpx
    except ImportError:
        return {
            "content": [{"type": "text", "text": "Error: httpx is not installed — install with: pip install httpx"}],
            "is_error": True,
        }

    if not channel.startswith("http"):
        return {
            "content": [{"type": "text", "text": f"Error: channel must be a webhook URL, got: {channel!r}"}],
            "is_error": True,
        }

    async with httpx.AsyncClient() as client:
        resp = await client.post(channel, json={"text": message}, timeout=10)
        resp.raise_for_status()

    return {"content": [{"type": "text", "text": f"Notification sent (status {resp.status_code})"}]}

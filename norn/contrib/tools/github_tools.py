from __future__ import annotations

import json
import shlex
import subprocess

from claude_agent_sdk import tool

_SCHEMA_SEARCH_CODE = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "org": {"type": "string"},
        "language": {"type": "string"},
    },
    "required": ["query", "org"],
}

_SCHEMA_LIST_REPOS = {
    "type": "object",
    "properties": {
        "org": {"type": "string"},
        "language": {"type": "string"},
    },
    "required": ["org"],
}


@tool(
    "github_search_code",
    "Search for code across all repos in a GitHub organization.",
    _SCHEMA_SEARCH_CODE,
)
async def github_search_code(args: dict) -> dict:
    """Search for code across org repos using the gh CLI."""
    query: str = args["query"]
    org: str = args["org"]
    language: str | None = args.get("language")

    cmd = f"gh search code {shlex.quote(query)} --owner {shlex.quote(org)}"
    if language:
        cmd += f" --language {shlex.quote(language)}"
    cmd += " --json repository,path --limit 20"

    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return {
            "content": [{"type": "text", "text": f"Error: {result.stderr.strip()}"}],
            "is_error": True,
        }
    return {"content": [{"type": "text", "text": result.stdout}]}


@tool(
    "github_list_repos",
    "List all repositories in a GitHub organization.",
    _SCHEMA_LIST_REPOS,
)
async def github_list_repos(args: dict) -> dict:
    """List repos in a GitHub org using the gh CLI."""
    org: str = args["org"]
    language: str | None = args.get("language")

    cmd = f"gh repo list {shlex.quote(org)} -L 200 --json name,url,language,description"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return {
            "content": [{"type": "text", "text": f"Error: {result.stderr.strip()}"}],
            "is_error": True,
        }

    repos: list[dict] = json.loads(result.stdout)
    if language:
        repos = [r for r in repos if (r.get("language") or "").lower() == language.lower()]
    return {"content": [{"type": "text", "text": json.dumps(repos, indent=2)}]}

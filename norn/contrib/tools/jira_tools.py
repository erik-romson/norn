from __future__ import annotations

import json
import os

from claude_agent_sdk import tool

_SCHEMA_GET_ISSUE = {"issue_key": str}

_SCHEMA_GET_ATTACHMENTS = {
    "type": "object",
    "properties": {
        "issue_key": {"type": "string"},
        "download_dir": {"type": "string"},
    },
    "required": ["issue_key"],
}


@tool(
    "jira_get_issue",
    "Fetch a Jira issue including summary, description, comments, attachments, labels, components, and custom fields.",
    _SCHEMA_GET_ISSUE,
)
async def jira_get_issue(args: dict) -> dict:
    """Fetch a Jira issue with all fields, comments, and attachments."""
    try:
        from jira import JIRA
    except ImportError:
        return {"content": [{"type": "text", "text": "Error: jira package is not installed"}], "is_error": True}

    issue_key: str = args["issue_key"]
    url = os.environ.get("JIRA_URL", "")
    email = os.environ.get("JIRA_EMAIL", "")
    token = os.environ.get("JIRA_TOKEN", "")

    if not url or not email or not token:
        return {
            "content": [{"type": "text", "text": "Error: JIRA_URL, JIRA_EMAIL, JIRA_TOKEN must be set"}],
            "is_error": True,
        }

    from norn.contrib.extractors.adf import field_to_text

    jira = JIRA(url, basic_auth=(email, token))
    issue = jira.issue(issue_key, expand="renderedFields")
    comments = [field_to_text(c.body) for c in jira.comments(issue_key)]
    attachments = [
        {"filename": a.filename, "size": a.size}
        for a in issue.fields.attachment
    ]
    result = {
        "key": issue.key,
        "summary": issue.fields.summary,
        "description": field_to_text(issue.fields.description),
        "labels": issue.fields.labels,
        "components": [c.name for c in issue.fields.components],
        "comments": comments,
        "attachments": attachments,
    }
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}


@tool(
    "jira_get_attachments",
    "Download all attachments from a Jira issue and return their paths.",
    _SCHEMA_GET_ATTACHMENTS,
)
async def jira_get_attachments(args: dict) -> dict:
    """Download and return attachment contents from a Jira issue."""
    try:
        from jira import JIRA
    except ImportError:
        return {"content": [{"type": "text", "text": "Error: jira package is not installed"}], "is_error": True}

    issue_key: str = args["issue_key"]
    download_dir: str = args.get("download_dir", "/tmp")
    url = os.environ.get("JIRA_URL", "")
    email = os.environ.get("JIRA_EMAIL", "")
    token = os.environ.get("JIRA_TOKEN", "")

    if not url or not email or not token:
        return {
            "content": [{"type": "text", "text": "Error: JIRA_URL, JIRA_EMAIL, JIRA_TOKEN must be set"}],
            "is_error": True,
        }

    import pathlib

    jira = JIRA(url, basic_auth=(email, token))
    issue = jira.issue(issue_key)
    saved: list[dict] = []
    out_dir = pathlib.Path(download_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    for attachment in issue.fields.attachment:
        data = attachment.get()
        # Prefix with the attachment id to avoid collisions when the same
        # filename appears more than once on an issue.
        dest = out_dir / f"{attachment.id}_{attachment.filename}"
        dest.write_bytes(data)
        saved.append({"filename": attachment.filename, "path": str(dest), "size": attachment.size})

    return {"content": [{"type": "text", "text": json.dumps(saved, indent=2)}]}

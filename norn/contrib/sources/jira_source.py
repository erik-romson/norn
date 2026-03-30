from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from norn.contrib.extractors.stacktrace import extract_stacktraces
from norn.contrib.models.issue_context import IssueContext
from norn.contrib.sources.base import IssueSource

if TYPE_CHECKING:
    from norn.models import PipelineContext

log = logging.getLogger(__name__)


class JiraSource(IssueSource):
    def __init__(
        self,
        url: str,
        projects: list[str] | None = None,
        auth_method: str = "api_token",
        include_comments: bool = True,
        include_attachments: bool = True,
        extract_stacktraces_flag: bool = True,
        attachment_dir: str = "/tmp/issueprocessing/attachments",
    ) -> None:
        self.url = url
        self.projects = projects or []
        self.auth_method = auth_method
        self.include_comments = include_comments
        self.include_attachments = include_attachments
        self.extract_stacktraces_flag = extract_stacktraces_flag
        self.attachment_dir = Path(attachment_dir)

    def _make_client(self, email: str, token: str):
        from jira import JIRA
        return JIRA(self.url, basic_auth=(email, token))

    async def fetch(self, issue_key: str, ctx: PipelineContext | None = None) -> IssueContext:
        loop = asyncio.get_event_loop()

        # Resolve credentials
        email = ""
        token = ""
        if ctx:
            email = ctx.secrets.get("JIRA_EMAIL", "")
            token = ctx.secrets.get("JIRA_TOKEN", "")
        if not email:
            import os
            email = os.environ.get("JIRA_EMAIL", "")
        if not token:
            import os
            token = os.environ.get("JIRA_TOKEN", "")

        def _fetch_sync():
            client = self._make_client(email, token)
            issue = client.issue(issue_key, expand="renderedFields")

            description = issue.fields.description or ""
            labels = list(issue.fields.labels or [])
            components = [c.name for c in (issue.fields.components or [])]
            linked = [
                li.outwardIssue.key if hasattr(li, "outwardIssue") else li.inwardIssue.key
                for li in (issue.fields.issuelinks or [])
                if hasattr(li, "outwardIssue") or hasattr(li, "inwardIssue")
            ]

            comments = []
            if self.include_comments:
                for c in client.comments(issue_key):
                    comments.append(c.body)

            attachments = []
            if self.include_attachments:
                self.attachment_dir.mkdir(parents=True, exist_ok=True)
                for att in (issue.fields.attachment or []):
                    dest = self.attachment_dir / att.filename
                    with open(dest, "wb") as f:
                        f.write(att.get())
                    attachments.append(dest)

            # Extract stacktraces
            stacktraces = []
            if self.extract_stacktraces_flag:
                all_text = description + "\n" + "\n".join(comments)
                stacktraces = extract_stacktraces(all_text)

            return IssueContext(
                key=issue.key,
                summary=issue.fields.summary,
                description=description,
                stacktraces=stacktraces,
                labels=labels,
                components=components,
                comments=comments,
                linked_issues=linked,
                attachments=attachments,
            )

        return await loop.run_in_executor(None, _fetch_sync)

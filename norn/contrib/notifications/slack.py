from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from norn.contrib.notifications.base import NotifyChannel

if TYPE_CHECKING:
    from norn.contrib.models.issue_context import IssueContext

log = logging.getLogger(__name__)


class Slack(NotifyChannel):
    """Send PR notification to a Slack webhook URL.

    Requires ``slack-sdk`` (optional dependency)::

        uv add --optional slack slack-sdk
    """

    def __init__(self, webhook_url: str) -> None:
        self.webhook_url = webhook_url

    async def send(self, issue: IssueContext, pr_url: str) -> None:
        try:
            from slack_sdk.webhook.async_client import AsyncWebhookClient
        except ImportError as e:
            raise ImportError("slack-sdk is required: uv add slack-sdk") from e

        client = AsyncWebhookClient(self.webhook_url)
        text = f"PR created for {issue.key}: {pr_url}\n{issue.summary}"
        await client.send(text=text)
        log.debug("Slack notification sent to %s", self.webhook_url)

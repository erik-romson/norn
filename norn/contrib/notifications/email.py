from __future__ import annotations

import logging
from email.mime.text import MIMEText
from typing import TYPE_CHECKING

from norn.contrib.notifications.base import NotifyChannel

if TYPE_CHECKING:
    from norn.contrib.models.issue_context import IssueContext

log = logging.getLogger(__name__)


class Email(NotifyChannel):
    """Send PR notification via email.

    Requires ``aiosmtplib`` (optional dependency)::

        uv add --optional email aiosmtplib
    """

    def __init__(
        self,
        to: str,
        *,
        smtp_host: str = "localhost",
        smtp_port: int = 587,
        username: str | None = None,
        password: str | None = None,
        from_addr: str = "issueprocessing@localhost",
    ) -> None:
        self.to = to
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.username = username
        self.password = password
        self.from_addr = from_addr

    async def send(self, issue: IssueContext, pr_url: str) -> None:
        try:
            import aiosmtplib
        except ImportError as e:
            raise ImportError("aiosmtplib is required: uv add aiosmtplib") from e

        msg = MIMEText(f"PR created for {issue.key}: {pr_url}\n\n{issue.summary}")
        msg["Subject"] = f"[{issue.key}] PR ready: {issue.summary}"
        msg["From"] = self.from_addr
        msg["To"] = self.to

        await aiosmtplib.send(
            msg,
            hostname=self.smtp_host,
            port=self.smtp_port,
            username=self.username,
            password=self.password,
        )
        log.debug("Email notification sent to %s", self.to)

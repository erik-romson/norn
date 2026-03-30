from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from norn.contrib.models.issue_context import IssueContext


class NotifyChannel(ABC):
    """Abstract notification channel for pipeline events."""

    @abstractmethod
    async def send(self, issue: IssueContext, pr_url: str) -> None: ...

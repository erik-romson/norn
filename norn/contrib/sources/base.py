from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from norn.contrib.models.issue_context import IssueContext

if TYPE_CHECKING:
    from norn.models import PipelineContext


class IssueSource(ABC):
    @abstractmethod
    async def fetch(self, issue_key: str, ctx: PipelineContext | None = None) -> IssueContext: ...

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


@dataclass
class PipelineResult:
    jira_key: str
    pr_url: str | None = None
    status: str = "success"
    summary: str = ""
    duration_ms: int = 0
    total_cost_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PipelineResult:
        return cls(**d)

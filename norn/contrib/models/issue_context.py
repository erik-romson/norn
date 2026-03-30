from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


@dataclass
class IssueContext:
    key: str
    summary: str
    description: str
    stacktraces: list[str] = field(default_factory=list)
    repro_steps: str | None = None
    attachments: list[Path] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    components: list[str] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)
    linked_issues: list[str] = field(default_factory=list)
    repo: str | None = None
    local_path: Path | None = None
    branch: str | None = None
    match_confidence: float = 0.0
    match_method: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["attachments"] = [str(p) for p in self.attachments]
        d["local_path"] = str(self.local_path) if self.local_path else None
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> IssueContext:
        d = dict(d)
        d["attachments"] = [Path(p) for p in d.get("attachments", [])]
        if d.get("local_path"):
            d["local_path"] = Path(d["local_path"])
        return cls(**d)

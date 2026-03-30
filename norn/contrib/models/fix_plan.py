from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class FileChange:
    path: str
    description: str
    reason: str


@dataclass
class FixPlan:
    analysis: str
    files_to_change: list[FileChange] = field(default_factory=list)
    test_strategy: str = ""
    test_files: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    confidence: float = 0.0
    approved: bool = False
    approval_feedback: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FixPlan:
        d = dict(d)
        d["files_to_change"] = [FileChange(**fc) for fc in d.get("files_to_change", [])]
        return cls(**d)

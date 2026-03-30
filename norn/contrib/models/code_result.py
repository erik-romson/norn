from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


@dataclass
class TestResult:
    passed: int
    failed: int
    skipped: int
    output: str
    success: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TestResult:
        return cls(**d)


@dataclass
class CoverageReport:
    overall_pct: float
    changed_files_pct: float
    uncovered_lines: dict[str, list[int]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CoverageReport:
        return cls(**d)


@dataclass
class CodeResult:
    local_path: Path
    branch: str
    commits: list[str] = field(default_factory=list)
    test_results: TestResult | None = None
    coverage: CoverageReport | None = None
    files_changed: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["local_path"] = str(self.local_path)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CodeResult:
        d = dict(d)
        d["local_path"] = Path(d["local_path"])
        if d.get("test_results"):
            d["test_results"] = TestResult.from_dict(d["test_results"])
        if d.get("coverage"):
            d["coverage"] = CoverageReport.from_dict(d["coverage"])
        return cls(**d)

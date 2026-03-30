from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from norn.models import StageResult
from norn.stages.base import BaseStage
from norn.contrib.models.code_result import CoverageReport

if TYPE_CHECKING:
    from norn.models import PipelineContext

log = logging.getLogger(__name__)


class Coverage(BaseStage):
    needs_agent = False

    def __init__(self, *, min_pct: float = 80) -> None:
        self.min_pct = min_pct

    async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
        r = ctx.results.get("read_issue")
        if r is None:
            return StageResult(name="", success=False, error="No read_issue result")
        issue_ctx = r.output

        report = await run_coverage(issue_ctx.local_path)

        if report.changed_files_pct < self.min_pct:
            return StageResult(
                name="", success=False,
                error=f"Coverage {report.changed_files_pct:.0f}% < {self.min_pct}%",
                output=report,
            )
        return StageResult(name="", success=True, output=report)


async def run_coverage(repo_path: Path) -> CoverageReport:
    """Run coverage and parse results."""
    if (repo_path / "pom.xml").exists():
        return await _java_coverage(repo_path)
    if (repo_path / "pyproject.toml").exists():
        return await _python_coverage(repo_path)
    raise ValueError(f"Cannot detect coverage tool in {repo_path}")


async def _python_coverage(repo_path: Path) -> CoverageReport:
    proc = await asyncio.create_subprocess_shell(
        "python -m pytest --cov --cov-report=json",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(repo_path),
    )
    await proc.communicate()

    cov_file = repo_path / "coverage.json"
    if not cov_file.exists():
        raise ValueError("coverage.json not generated")

    data = json.loads(cov_file.read_text())
    overall = data.get("totals", {}).get("percent_covered", 0.0)
    uncovered: dict[str, list[int]] = {}
    for fname, fdata in data.get("files", {}).items():
        missing = fdata.get("missing_lines", [])
        if missing:
            uncovered[fname] = missing

    return CoverageReport(
        overall_pct=overall,
        changed_files_pct=overall,
        uncovered_lines=uncovered,
    )


async def _java_coverage(repo_path: Path) -> CoverageReport:
    proc = await asyncio.create_subprocess_shell(
        "mvn verify -B jacoco:report",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(repo_path),
    )
    await proc.communicate()

    # Parse JaCoCo XML report
    import xml.etree.ElementTree as ET

    report_path = repo_path / "target" / "site" / "jacoco" / "jacoco.xml"
    if not report_path.exists():
        raise ValueError(f"JaCoCo report not found: {report_path}")

    tree = ET.parse(report_path)
    root = tree.getroot()

    # Extract line coverage from counter elements
    line_missed = 0
    line_covered = 0
    for counter in root.findall(".//counter[@type='LINE']"):
        line_missed += int(counter.get("missed", 0))
        line_covered += int(counter.get("covered", 0))

    total = line_missed + line_covered
    overall_pct = (line_covered / total * 100) if total > 0 else 0.0

    return CoverageReport(
        overall_pct=overall_pct,
        changed_files_pct=overall_pct,
    )

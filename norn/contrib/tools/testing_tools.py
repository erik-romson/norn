from __future__ import annotations

import json
import subprocess
from pathlib import Path

from claude_agent_sdk import tool

_SCHEMA_RUN_TESTS = {
    "type": "object",
    "properties": {
        "test_files": {
            "type": "array",
            "items": {"type": "string"},
        },
        "full": {"type": "boolean"},
    },
    "required": [],
}

_SCHEMA_RUN_COVERAGE = {
    "type": "object",
    "properties": {
        "changed_files": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": [],
}


def _detect_build_command(repo_path: Path) -> str:
    """Auto-detect the build/test command for the project."""
    if (repo_path / "pyproject.toml").exists() or (repo_path / "setup.py").exists():
        return "python -m pytest"
    if (repo_path / "pom.xml").exists():
        return "mvn test -q"
    if (repo_path / "build.gradle").exists() or (repo_path / "build.gradle.kts").exists():
        return "./gradlew test"
    return "python -m pytest"


def _detect_test_command(repo_path: Path, test_files: list[str]) -> str:
    """Build the test command for specific test files."""
    base = _detect_build_command(repo_path)
    if base.startswith("python -m pytest"):
        return f"{base} {' '.join(test_files)}"
    return base


@tool(
    "run_tests",
    "Run tests in the current project. Specify test_files for targeted runs or full=true for the full suite.",
    _SCHEMA_RUN_TESTS,
)
async def run_tests(args: dict) -> dict:
    """Execute the project test suite."""
    test_files: list[str] | None = args.get("test_files")
    full: bool = args.get("full", False)

    repo_path = Path.cwd()
    if full or not test_files:
        cmd = _detect_build_command(repo_path)
    else:
        cmd = _detect_test_command(repo_path, test_files)

    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True, cwd=repo_path, timeout=300
    )
    output = {
        "success": result.returncode == 0,
        "stdout": result.stdout[-5000:],
        "stderr": result.stderr[-2000:],
        "returncode": result.returncode,
    }
    return {"content": [{"type": "text", "text": json.dumps(output, indent=2)}]}


@tool(
    "run_coverage",
    "Run coverage analysis on the project and return overall and per-file coverage percentages.",
    _SCHEMA_RUN_COVERAGE,
)
async def run_coverage(args: dict) -> dict:
    """Run coverage and return the report."""
    changed_files: list[str] | None = args.get("changed_files")
    repo_path = Path.cwd()

    subprocess.run(
        "python -m pytest --cov --cov-report=json",
        shell=True,
        cwd=repo_path,
        capture_output=True,
        timeout=300,
    )

    coverage_json = repo_path / "coverage.json"
    if not coverage_json.exists():
        return {
            "content": [{"type": "text", "text": "Error: coverage.json not found — ensure pytest-cov is installed"}],
            "is_error": True,
        }

    data = json.loads(coverage_json.read_text())
    result: dict = {"overall_pct": data["totals"]["percent_covered"]}

    if changed_files:
        changed_coverage = {}
        for f in changed_files:
            if f in data["files"]:
                changed_coverage[f] = data["files"][f]["summary"]["percent_covered"]
        result["changed_files"] = changed_coverage

    return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}

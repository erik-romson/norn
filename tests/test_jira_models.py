from __future__ import annotations

from pathlib import Path

from norn.contrib.models.issue_context import IssueContext
from norn.contrib.models.fix_plan import FileChange, FixPlan
from norn.contrib.models.code_result import CodeResult, CoverageReport, TestResult
from norn.contrib.models.pipeline_result import PipelineResult


def test_issue_context_roundtrip():
    ctx = IssueContext(
        key="PROJ-123",
        summary="Fix null pointer",
        description="NPE in auth",
        stacktraces=["at com.Foo.bar(Foo.java:42)"],
        local_path=Path("/tmp/clone"),
        attachments=[Path("/tmp/att.txt")],
        repo="org/repo",
        branch="PROJ-123-fix",
    )
    d = ctx.to_dict()
    assert d["local_path"] == "/tmp/clone"
    assert d["attachments"] == ["/tmp/att.txt"]

    restored = IssueContext.from_dict(d)
    assert restored.key == "PROJ-123"
    assert restored.local_path == Path("/tmp/clone")
    assert restored.attachments == [Path("/tmp/att.txt")]


def test_fix_plan_roundtrip():
    plan = FixPlan(
        analysis="Root cause is X",
        files_to_change=[FileChange(path="a.py", description="fix", reason="bug")],
        test_strategy="Add unit test",
        test_files=["tests/test_a.py"],
        risks=["Side effect"],
        confidence=0.8,
        approved=True,
    )
    d = plan.to_dict()
    restored = FixPlan.from_dict(d)
    assert restored.analysis == "Root cause is X"
    assert len(restored.files_to_change) == 1
    assert restored.files_to_change[0].path == "a.py"
    assert restored.approved is True


def test_code_result_roundtrip():
    result = CodeResult(
        local_path=Path("/tmp/repo"),
        branch="fix-branch",
        commits=["abc123"],
        test_results=TestResult(passed=5, failed=0, skipped=1, output="ok", success=True),
        coverage=CoverageReport(overall_pct=85.0, changed_files_pct=90.0),
        files_changed=["src/main.py"],
    )
    d = result.to_dict()
    assert d["local_path"] == "/tmp/repo"

    restored = CodeResult.from_dict(d)
    assert restored.local_path == Path("/tmp/repo")
    assert restored.test_results.passed == 5
    assert restored.coverage.overall_pct == 85.0


def test_pipeline_result_roundtrip():
    result = PipelineResult(
        jira_key="PROJ-42",
        pr_url="https://github.com/org/repo/pull/1",
        status="success",
        summary="PR created",
        duration_ms=12345,
        total_cost_usd=0.50,
    )
    d = result.to_dict()
    restored = PipelineResult.from_dict(d)
    assert restored.jira_key == "PROJ-42"
    assert restored.pr_url == "https://github.com/org/repo/pull/1"


def test_code_result_without_optional_fields():
    result = CodeResult(local_path=Path("/tmp/x"), branch="b")
    d = result.to_dict()
    restored = CodeResult.from_dict(d)
    assert restored.test_results is None
    assert restored.coverage is None
    assert restored.commits == []

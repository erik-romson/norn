from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from norn.models import PipelineContext, StageResult, UsageTracker
from norn.contrib.models.issue_context import IssueContext
from norn.contrib.models.fix_plan import FileChange, FixPlan


def _make_ctx(
    issue: IssueContext | None = None,
    plan: FixPlan | None = None,
) -> PipelineContext:
    ctx = PipelineContext(results={}, usage_tracker=UsageTracker(), params={})
    if issue is not None:
        ctx.results["read_issue"] = StageResult(name="read_issue", success=True, output=issue)
    if plan is not None:
        ctx.results["plan"] = StageResult(name="plan", success=True, output=plan)
    return ctx


def _make_issue(**overrides) -> IssueContext:
    defaults = dict(
        key="PROJ-1",
        summary="Fix bug",
        description="Bug description",
        repo="org/repo",
        local_path=Path("/tmp/test-repo"),
        branch="PROJ-1-fix-bug",
    )
    defaults.update(overrides)
    return IssueContext(**defaults)


def _make_plan(**overrides) -> FixPlan:
    defaults = dict(
        analysis="Root cause is X",
        files_to_change=[FileChange(path="a.py", description="fix", reason="bug")],
        test_strategy="Add test",
        test_files=["tests/test_a.py"],
    )
    defaults.update(overrides)
    return FixPlan(**defaults)


# ---------------------------------------------------------------------------
# WriteTest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_test_missing_context():
    from norn.contrib.stages.write_test import WriteTest

    stage = WriteTest()
    ctx = _make_ctx()
    result = await stage.run(ctx)
    assert not result.success
    assert "Missing" in result.error


@pytest.mark.asyncio
async def test_write_test_delegates_to_generate():
    from norn.contrib.stages.write_test import WriteTest

    issue = _make_issue()
    plan = _make_plan()
    ctx = _make_ctx(issue=issue, plan=plan)

    generate_result = StageResult(name="write_test", success=True, output="test written")

    with patch("norn.contrib.stages.write_test.Generate") as MockGenerate:
        mock_gen = AsyncMock()
        mock_gen.run = AsyncMock(return_value=generate_result)
        MockGenerate.return_value = mock_gen

        stage = WriteTest()
        result = await stage.run(ctx)

    assert result.success
    call_kwargs = MockGenerate.call_args.kwargs
    assert issue.key in call_kwargs["prompt"]
    assert call_kwargs["permission_mode"] == "bypassPermissions"
    assert "Write" in call_kwargs["allowed_tools"]


# ---------------------------------------------------------------------------
# VerifyTest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_test_missing_context():
    from norn.contrib.stages.verify_test import VerifyTest

    stage = VerifyTest()
    ctx = _make_ctx()
    result = await stage.run(ctx)
    assert not result.success
    assert "Missing" in result.error


# ---------------------------------------------------------------------------
# FullBuild
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_build_missing_context():
    from norn.contrib.stages.full_build import FullBuild

    stage = FullBuild()
    ctx = _make_ctx()
    result = await stage.run(ctx)
    assert not result.success
    assert "No read_issue" in result.error


@pytest.mark.asyncio
async def test_full_build_no_auto_detect():
    from norn.contrib.stages.full_build import FullBuild

    stage = FullBuild(auto_detect=False)
    ctx = _make_ctx(issue=_make_issue())
    result = await stage.run(ctx)
    assert not result.success
    assert "No build command" in result.error


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coverage_missing_context():
    from norn.contrib.stages.coverage import Coverage

    stage = Coverage(min_pct=80)
    ctx = _make_ctx()
    result = await stage.run(ctx)
    assert not result.success
    assert "No read_issue" in result.error


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_missing_context():
    from norn.contrib.stages.push import Push

    stage = Push()
    ctx = _make_ctx()
    result = await stage.run(ctx)
    assert not result.success
    assert "No read_issue" in result.error


@pytest.mark.asyncio
async def test_push_missing_branch():
    from norn.contrib.stages.push import Push

    stage = Push()
    issue = _make_issue(branch=None, local_path=None)
    ctx = _make_ctx(issue=issue)
    result = await stage.run(ctx)
    assert not result.success
    assert "local_path or branch" in result.error


# ---------------------------------------------------------------------------
# CI
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ci_missing_context():
    from norn.contrib.stages.ci import CI

    stage = CI()
    ctx = _make_ctx()
    result = await stage.run(ctx)
    assert not result.success
    assert "No read_issue" in result.error


@pytest.mark.asyncio
async def test_ci_missing_repo():
    from norn.contrib.stages.ci import CI

    stage = CI()
    issue = _make_issue(repo=None, branch=None)
    ctx = _make_ctx(issue=issue)
    result = await stage.run(ctx)
    assert not result.success
    assert "No repo or branch" in result.error


# ---------------------------------------------------------------------------
# Ship
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ship_missing_context():
    from norn.contrib.stages.ship import Ship

    stage = Ship()
    ctx = _make_ctx()
    result = await stage.run(ctx)
    assert not result.success
    assert "No read_issue" in result.error


# ---------------------------------------------------------------------------
# SearchLogs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_logs_missing_context():
    from norn.contrib.stages.search_logs import SearchLogs

    stage = SearchLogs(url="http://localhost:9200", index="logs-*")
    ctx = _make_ctx()
    result = await stage.run(ctx)
    assert not result.success
    assert "No read_issue" in result.error


def test_search_logs_build_query():
    from norn.contrib.stages.search_logs import SearchLogs

    stage = SearchLogs(url="http://localhost:9200", index="logs-*")
    issue = _make_issue(stacktraces=["NullPointerException at com.Foo.bar()"])
    query = stage._build_query(issue)
    assert query["bool"]["should"][0]["match"]["message"] == "PROJ-1"
    assert len(query["bool"]["should"]) == 2


# ---------------------------------------------------------------------------
# Fix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fix_missing_context():
    from norn.contrib.stages.fix import Fix

    stage = Fix()
    ctx = _make_ctx()
    result = await stage.run(ctx)
    assert not result.success
    assert "Missing" in result.error


@pytest.mark.asyncio
async def test_fix_delegates_to_generate():
    from norn.contrib.stages.fix import Fix

    issue = _make_issue()
    plan = _make_plan()
    ctx = _make_ctx(issue=issue, plan=plan)

    generate_result = StageResult(name="fix", success=True, output="fixed")

    with patch("norn.contrib.stages.fix.Generate") as MockGenerate:
        mock_gen = AsyncMock()
        mock_gen.run = AsyncMock(return_value=generate_result)
        MockGenerate.return_value = mock_gen

        stage = Fix()
        result = await stage.run(ctx)

    assert result.success
    call_kwargs = MockGenerate.call_args.kwargs
    assert issue.key in call_kwargs["prompt"]
    assert call_kwargs["permission_mode"] == "bypassPermissions"


@pytest.mark.asyncio
async def test_fix_blocked_patterns_passed_as_hooks():
    from norn.contrib.stages.fix import Fix

    issue = _make_issue()
    plan = _make_plan()
    ctx = _make_ctx(issue=issue, plan=plan)

    generate_result = StageResult(name="fix", success=True, output="fixed")

    with patch("norn.contrib.stages.fix.Generate") as MockGenerate:
        mock_gen = AsyncMock()
        mock_gen.run = AsyncMock(return_value=generate_result)
        MockGenerate.return_value = mock_gen

        with patch("norn.profiles.build_block_hooks", return_value={"pre_tool_use": []}) as mock_hooks:
            stage = Fix(blocked_patterns=["rm -rf /"])
            await stage.run(ctx)

    mock_hooks.assert_called_once_with(["rm -rf /"])
    assert MockGenerate.call_args.kwargs["hooks"] is not None


# ---------------------------------------------------------------------------
# VerifyTest — functional
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_test_runs_detected_command(tmp_path):
    from norn.contrib.stages.verify_test import VerifyTest

    (tmp_path / "pyproject.toml").write_text("[project]\nname='test'")
    issue = _make_issue(local_path=tmp_path)
    plan = _make_plan(test_files=["tests/test_a.py"])
    ctx = _make_ctx(issue=issue, plan=plan)

    async def _fake_proc(*args, **kwargs):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"1 passed", b""))
        return proc

    with patch("asyncio.create_subprocess_shell", side_effect=_fake_proc) as mock_proc:
        stage = VerifyTest()
        result = await stage.run(ctx)

    assert result.success
    cmd_called = mock_proc.call_args[0][0]
    assert "pytest" in cmd_called
    assert "tests/test_a.py" in cmd_called


# ---------------------------------------------------------------------------
# Coverage — functional
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coverage_python_parses_report(tmp_path):
    import json

    from norn.contrib.stages.coverage import _python_coverage

    cov_data = {
        "totals": {"percent_covered": 85.0},
        "files": {
            "src/foo.py": {"missing_lines": [10, 20]},
            "src/bar.py": {"missing_lines": []},
        },
    }
    (tmp_path / "coverage.json").write_text(json.dumps(cov_data))

    async def _fake_proc(*args, **kwargs):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    with patch("asyncio.create_subprocess_shell", side_effect=_fake_proc):
        report = await _python_coverage(tmp_path)

    assert report.overall_pct == 85.0
    assert "src/foo.py" in report.uncovered_lines
    assert report.uncovered_lines["src/foo.py"] == [10, 20]
    assert "src/bar.py" not in report.uncovered_lines


@pytest.mark.asyncio
async def test_coverage_java_parses_jacoco(tmp_path):
    from norn.contrib.stages.coverage import _java_coverage

    jacoco_dir = tmp_path / "target" / "site" / "jacoco"
    jacoco_dir.mkdir(parents=True)
    jacoco_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<report name="test">'
        '<counter type="LINE" missed="20" covered="80"/>'
        "</report>"
    )
    (jacoco_dir / "jacoco.xml").write_text(jacoco_xml)

    async def _fake_proc(*args, **kwargs):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    with patch("asyncio.create_subprocess_shell", side_effect=_fake_proc):
        report = await _java_coverage(tmp_path)

    assert report.overall_pct == 80.0


@pytest.mark.asyncio
async def test_coverage_below_threshold(tmp_path):
    from unittest.mock import AsyncMock as _AsyncMock

    from norn.contrib.models.code_result import CoverageReport
    from norn.contrib.stages.coverage import Coverage

    issue = _make_issue(local_path=tmp_path)
    ctx = _make_ctx(issue=issue)

    low_report = CoverageReport(overall_pct=60.0, changed_files_pct=60.0)

    with patch("norn.contrib.stages.coverage.run_coverage", new=_AsyncMock(return_value=low_report)):
        stage = Coverage(min_pct=80)
        result = await stage.run(ctx)

    assert not result.success
    assert "60%" in result.error
    assert result.output is low_report


@pytest.mark.asyncio
async def test_coverage_above_threshold(tmp_path):
    from unittest.mock import AsyncMock as _AsyncMock

    from norn.contrib.models.code_result import CoverageReport
    from norn.contrib.stages.coverage import Coverage

    issue = _make_issue(local_path=tmp_path)
    ctx = _make_ctx(issue=issue)

    good_report = CoverageReport(overall_pct=90.0, changed_files_pct=90.0)

    with patch("norn.contrib.stages.coverage.run_coverage", new=_AsyncMock(return_value=good_report)):
        stage = Coverage(min_pct=80)
        result = await stage.run(ctx)

    assert result.success
    assert result.output is good_report


# ---------------------------------------------------------------------------
# FullBuild — functional
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_build_auto_detect_python(tmp_path):
    from norn.contrib.stages.full_build import FullBuild

    (tmp_path / "pyproject.toml").write_text("[project]\nname='test'")
    issue = _make_issue(local_path=tmp_path)
    ctx = _make_ctx(issue=issue)

    stage = FullBuild(auto_detect=True)
    result = await stage.run(ctx)
    # Command runs (may fail due to no actual tests), but detects pytest
    assert result.output is not None
    assert "returncode" in result.output


@pytest.mark.asyncio
async def test_full_build_override(tmp_path):
    from norn.contrib.build.configs import BuildConfig
    from norn.contrib.stages.full_build import FullBuild

    issue = _make_issue(repo="myorg/myrepo", local_path=tmp_path)
    ctx = _make_ctx(issue=issue)

    stage = FullBuild(overrides={"myorg/myrepo": BuildConfig(cmd="echo build-ok")})
    result = await stage.run(ctx)
    assert result.success
    assert "build-ok" in result.output["stdout"]


# ---------------------------------------------------------------------------
# Push — functional
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_success(tmp_path):
    from norn.contrib.stages.push import Push

    issue = _make_issue(local_path=tmp_path, branch="PROJ-1-fix-bug")
    ctx = _make_ctx(issue=issue)

    async def _fake_proc(*args, **kwargs):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    stage = Push()
    with patch("asyncio.create_subprocess_shell", side_effect=_fake_proc):
        result = await stage.run(ctx)

    assert result.success
    assert result.output["branch"] == "PROJ-1-fix-bug"


@pytest.mark.asyncio
async def test_push_git_failure(tmp_path):
    from norn.contrib.stages.push import Push

    issue = _make_issue(local_path=tmp_path, branch="PROJ-1-fix-bug")
    ctx = _make_ctx(issue=issue)

    async def _fail_proc(*args, **kwargs):
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"error: not a git repo"))
        return proc

    stage = Push()
    with patch("asyncio.create_subprocess_shell", side_effect=_fail_proc):
        result = await stage.run(ctx)

    assert not result.success
    assert "Push failed" in result.error


# ---------------------------------------------------------------------------
# CI — functional
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ci_success():
    from norn.contrib.stages.ci import CI

    issue = _make_issue(repo="org/repo", branch="PROJ-1-fix-bug")
    ctx = _make_ctx(issue=issue)

    import json

    runs = json.dumps([{"status": "completed", "conclusion": "success",
                        "name": "CI", "databaseId": 42}]).encode()

    async def _gh_proc(*args, **kwargs):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(runs, b""))
        return proc

    stage = CI(poll_interval=0)
    with patch("asyncio.create_subprocess_shell", side_effect=_gh_proc):
        result = await stage.run(ctx)

    assert result.success
    assert result.output["conclusion"] == "success"


@pytest.mark.asyncio
async def test_ci_failure():
    from norn.contrib.stages.ci import CI

    issue = _make_issue(repo="org/repo", branch="PROJ-1-fix-bug")
    ctx = _make_ctx(issue=issue)

    import json

    runs = json.dumps([{"status": "completed", "conclusion": "failure",
                        "name": "CI", "databaseId": 42}]).encode()

    async def _gh_proc(*args, **kwargs):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(runs, b""))
        return proc

    stage = CI(poll_interval=0)
    with patch("asyncio.create_subprocess_shell", side_effect=_gh_proc):
        result = await stage.run(ctx)

    assert not result.success
    assert "failure" in result.error


@pytest.mark.asyncio
async def test_ci_timeout():
    from norn.contrib.stages.ci import CI

    issue = _make_issue(repo="org/repo", branch="PROJ-1-fix-bug")
    ctx = _make_ctx(issue=issue)

    import json

    runs = json.dumps([{"status": "in_progress", "conclusion": None,
                        "name": "CI", "databaseId": 42}]).encode()

    async def _gh_proc(*args, **kwargs):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(runs, b""))
        return proc

    stage = CI(poll_interval=0, timeout_minutes=0)
    with patch("asyncio.create_subprocess_shell", side_effect=_gh_proc):
        result = await stage.run(ctx)

    assert not result.success
    assert "timed out" in result.error


# ---------------------------------------------------------------------------
# Ship — functional
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ship_creates_pr():
    from norn.contrib.stages.ship import Ship, _create_pr

    issue = _make_issue(repo="org/repo", branch="PROJ-1-fix-bug")
    ctx = _make_ctx(issue=issue)

    async def _fake_pr(*args, **kwargs):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(
            return_value=(b"https://github.com/org/repo/pull/1", b""))
        return proc

    stage = Ship()
    with patch("asyncio.create_subprocess_shell", side_effect=_fake_pr):
        result = await stage.run(ctx)

    assert result.success
    assert result.output.pr_url == "https://github.com/org/repo/pull/1"
    assert result.output.jira_key == "PROJ-1"


@pytest.mark.asyncio
async def test_ship_sends_notifications():
    from norn.contrib.stages.ship import Ship

    issue = _make_issue(repo="org/repo", branch="PROJ-1-fix-bug")
    ctx = _make_ctx(issue=issue)

    sent: list[tuple] = []

    from norn.contrib.notifications.base import NotifyChannel

    class FakeChannel(NotifyChannel):
        async def send(self, iss, pr_url):
            sent.append((iss.key, pr_url))

    async def _fake_pr(*args, **kwargs):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(
            return_value=(b"https://github.com/org/repo/pull/2", b""))
        return proc

    stage = Ship(notify=[FakeChannel()])
    with patch("asyncio.create_subprocess_shell", side_effect=_fake_pr):
        result = await stage.run(ctx)

    assert result.success
    assert len(sent) == 1
    assert sent[0] == ("PROJ-1", "https://github.com/org/repo/pull/2")


@pytest.mark.asyncio
async def test_ship_draft():
    from norn.contrib.stages.ship import Ship

    issue = _make_issue(repo="org/repo", branch="PROJ-1-fix-bug")
    ctx = _make_ctx(issue=issue)

    captured_cmd: list[str] = []

    async def _fake_pr(cmd, **kwargs):
        captured_cmd.append(cmd)
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"https://github.com/org/repo/pull/3", b""))
        return proc

    stage = Ship(draft=True)
    with patch("asyncio.create_subprocess_shell", side_effect=_fake_pr):
        result = await stage.run(ctx)

    assert result.success
    assert "--draft" in captured_cmd[0]


@pytest.mark.asyncio
async def test_ship_pr_body_includes_analysis():
    from norn.contrib.stages.ship import Ship

    issue = _make_issue(repo="org/repo", branch="PROJ-1-fix-bug")
    plan = _make_plan(analysis="Root cause is X")
    ctx = _make_ctx(issue=issue, plan=plan)

    captured_cmd: list[str] = []

    async def _fake_pr(cmd, **kwargs):
        captured_cmd.append(cmd)
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"https://github.com/pr/1", b""))
        return proc

    stage = Ship(pr_body_includes=["jira_link", "analysis"])
    with patch("asyncio.create_subprocess_shell", side_effect=_fake_pr):
        result = await stage.run(ctx)

    assert result.success
    assert "Root cause is X" in captured_cmd[0]


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


def test_notify_channel_is_abstract():
    from norn.contrib.notifications.base import NotifyChannel

    with pytest.raises(TypeError):
        NotifyChannel()  # type: ignore[abstract]


def test_slack_missing_dependency():
    import sys
    from norn.contrib.notifications.slack import Slack
    from norn.contrib.models.issue_context import IssueContext

    issue = IssueContext(key="X-1", summary="s", description="d")
    slack = Slack(webhook_url="https://hooks.slack.com/fake")

    # Temporarily hide slack_sdk
    saved = sys.modules.get("slack_sdk")
    sys.modules["slack_sdk"] = None  # type: ignore[assignment]
    try:
        import pytest as _pytest
        with _pytest.raises(ImportError, match="slack-sdk"):
            import asyncio
            asyncio.run(slack.send(issue, "http://pr"))
    finally:
        if saved is None:
            sys.modules.pop("slack_sdk", None)
        else:
            sys.modules["slack_sdk"] = saved


def test_email_missing_dependency():
    import sys
    from norn.contrib.notifications.email import Email
    from norn.contrib.models.issue_context import IssueContext

    issue = IssueContext(key="X-1", summary="s", description="d")
    email = Email(to="dev@example.com")

    saved = sys.modules.get("aiosmtplib")
    sys.modules["aiosmtplib"] = None  # type: ignore[assignment]
    try:
        import pytest as _pytest
        with _pytest.raises(ImportError, match="aiosmtplib"):
            import asyncio
            asyncio.run(email.send(issue, "http://pr"))
    finally:
        if saved is None:
            sys.modules.pop("aiosmtplib", None)
        else:
            sys.modules["aiosmtplib"] = saved


# ---------------------------------------------------------------------------
# Analyze
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analyze_missing_context():
    from norn.contrib.stages.analyze import Analyze

    stage = Analyze()
    ctx = _make_ctx()
    result = await stage.run(ctx)
    assert not result.success
    assert "No read_issue" in result.error


@pytest.mark.asyncio
async def test_analyze_delegates_to_generate():
    from norn.contrib.stages.analyze import Analyze

    issue = _make_issue()
    ctx = _make_ctx(issue=issue)

    generate_result = StageResult(name="analyze", success=True, output='{"root_cause": "x"}')

    with patch("norn.contrib.stages.analyze.Generate") as MockGenerate:
        mock_gen = AsyncMock()
        mock_gen.run = AsyncMock(return_value=generate_result)
        MockGenerate.return_value = mock_gen

        stage = Analyze()
        result = await stage.run(ctx)

    assert result.success
    call_kwargs = MockGenerate.call_args.kwargs
    assert "Read" in call_kwargs["allowed_tools"]
    assert call_kwargs["permission_mode"] == "plan"
    assert issue.key in call_kwargs["prompt"]


@pytest.mark.asyncio
async def test_analyze_includes_prior_rejection_feedback():
    from norn.contrib.stages.analyze import Analyze

    issue = _make_issue()
    prior_plan = _make_plan(approval_feedback="Focus on the null check in Foo.bar()")
    ctx = _make_ctx(issue=issue)
    ctx.results["plan"] = StageResult(name="plan", success=False, output=prior_plan)

    captured_prompt: list[str] = []

    async def _fake_gen_run(ctx, **kwargs):
        return StageResult(name="analyze", success=True, output="{}")

    with patch("norn.contrib.stages.analyze.Generate") as MockGenerate:
        mock_gen = AsyncMock()
        mock_gen.run = AsyncMock(side_effect=_fake_gen_run)
        MockGenerate.return_value = mock_gen

        stage = Analyze()
        await stage.run(ctx)

    call_kwargs = MockGenerate.call_args.kwargs
    assert "Focus on the null check" in call_kwargs["prompt"]
    assert "Previous Plan Was Rejected" in call_kwargs["prompt"]


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_missing_context():
    from norn.contrib.stages.plan import Plan

    stage = Plan()
    ctx = _make_ctx()
    result = await stage.run(ctx)
    assert not result.success
    assert "No read_issue" in result.error


@pytest.mark.asyncio
async def test_plan_approved_returns_fix_plan():
    from norn.contrib.stages.plan import Plan

    issue = _make_issue()
    ctx = _make_ctx(issue=issue)

    raw_json = '{"analysis": "Bug in Foo", "files_to_change": [], "test_strategy": "unit", "test_files": [], "risks": [], "confidence": 0.9}'
    generate_result = StageResult(name="plan", success=True, output=raw_json)

    with patch("norn.contrib.stages.plan.Generate") as MockGenerate:
        mock_gen = AsyncMock()
        mock_gen.run = AsyncMock(return_value=generate_result)
        MockGenerate.return_value = mock_gen

        stage = Plan(require_approval=False)
        result = await stage.run(ctx)

    assert result.success
    from norn.contrib.models.fix_plan import FixPlan
    assert isinstance(result.output, FixPlan)
    assert result.output.analysis == "Bug in Foo"


@pytest.mark.asyncio
async def test_plan_user_approves():
    from norn.contrib.stages.plan import Plan

    issue = _make_issue()
    ctx = _make_ctx(issue=issue)

    raw_json = '{"analysis": "Bug", "files_to_change": [], "test_strategy": "", "test_files": [], "risks": [], "confidence": 0.5}'
    generate_result = StageResult(name="plan", success=True, output=raw_json)

    with patch("norn.contrib.stages.plan.Generate") as MockGenerate:
        mock_gen = AsyncMock()
        mock_gen.run = AsyncMock(return_value=generate_result)
        MockGenerate.return_value = mock_gen

        with patch("norn.contrib.stages.plan._present_plan"):
            with patch("norn.ui.ask_yes_no", return_value=True):
                stage = Plan(require_approval=True)
                result = await stage.run(ctx)

    assert result.success
    assert result.output.approved is True


@pytest.mark.asyncio
async def test_plan_user_rejects_with_feedback():
    from norn.contrib.stages.plan import Plan

    issue = _make_issue()
    ctx = _make_ctx(issue=issue)

    raw_json = '{"analysis": "Bug", "files_to_change": [], "test_strategy": "", "test_files": [], "risks": [], "confidence": 0.3}'
    generate_result = StageResult(name="plan", success=True, output=raw_json)

    with patch("norn.contrib.stages.plan.Generate") as MockGenerate:
        mock_gen = AsyncMock()
        mock_gen.run = AsyncMock(return_value=generate_result)
        MockGenerate.return_value = mock_gen

        mock_console = MagicMock()
        mock_console.input.return_value = "Check the auth layer instead"

        with patch("norn.contrib.stages.plan._present_plan"):
            with patch("norn.ui.ask_yes_no", return_value=False):
                with patch("norn.ui.console", mock_console):
                    stage = Plan(require_approval=True)
                    result = await stage.run(ctx)

    assert not result.success
    assert result.error == "Plan rejected by user"
    from norn.contrib.models.fix_plan import FixPlan
    assert isinstance(result.output, FixPlan)
    assert result.output.approval_feedback == "Check the auth layer instead"

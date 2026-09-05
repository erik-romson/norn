from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# jira_tools
# ---------------------------------------------------------------------------


def _mock_jira_module(mock_jira_instance):
    """Return a mock 'jira' module with JIRA set to return mock_jira_instance."""
    mock_jira_cls = MagicMock(return_value=mock_jira_instance)
    mock_module = MagicMock()
    mock_module.JIRA = mock_jira_cls
    return mock_module, mock_jira_cls


@pytest.mark.asyncio
async def test_jira_get_issue_missing_env():
    from norn.contrib.tools.jira_tools import jira_get_issue

    mock_module, _ = _mock_jira_module(MagicMock())
    with patch.dict("sys.modules", {"jira": mock_module}):
        with patch.dict("os.environ", {}, clear=True):
            result = await jira_get_issue.handler({"issue_key": "PROJ-1"})

    assert result["is_error"] is True
    assert "JIRA_URL" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_jira_get_issue_returns_fields():
    from norn.contrib.tools.jira_tools import jira_get_issue

    env = {"JIRA_URL": "https://jira.example.com", "JIRA_EMAIL": "user@example.com", "JIRA_TOKEN": "tok"}

    mock_issue = MagicMock()
    mock_issue.key = "PROJ-1"
    mock_issue.fields.summary = "Fix the bug"
    mock_issue.fields.description = "It crashes"
    mock_issue.fields.labels = ["backend"]
    component = MagicMock()
    component.name = "api"
    mock_issue.fields.components = [component]
    mock_issue.fields.attachment = []

    mock_jira_inst = MagicMock()
    mock_jira_inst.issue.return_value = mock_issue
    mock_jira_inst.comments.return_value = []

    mock_module, _ = _mock_jira_module(mock_jira_inst)
    with patch.dict("os.environ", env):
        with patch.dict("sys.modules", {"jira": mock_module}):
            result = await jira_get_issue.handler({"issue_key": "PROJ-1"})

    assert "is_error" not in result
    data = json.loads(result["content"][0]["text"])
    assert data["key"] == "PROJ-1"
    assert data["summary"] == "Fix the bug"
    assert data["labels"] == ["backend"]


@pytest.mark.asyncio
async def test_jira_get_attachments_missing_env():
    from norn.contrib.tools.jira_tools import jira_get_attachments

    mock_module, _ = _mock_jira_module(MagicMock())
    with patch.dict("sys.modules", {"jira": mock_module}):
        with patch.dict("os.environ", {}, clear=True):
            result = await jira_get_attachments.handler({"issue_key": "PROJ-1"})

    assert result["is_error"] is True


@pytest.mark.asyncio
async def test_jira_get_attachments_downloads_files(tmp_path):
    from norn.contrib.tools.jira_tools import jira_get_attachments

    env = {"JIRA_URL": "https://jira.example.com", "JIRA_EMAIL": "user@example.com", "JIRA_TOKEN": "tok"}

    mock_attachment = MagicMock()
    mock_attachment.id = "10001"
    mock_attachment.filename = "spec.txt"
    mock_attachment.size = 12
    mock_attachment.get.return_value = b"hello world\n"

    mock_issue = MagicMock()
    mock_issue.fields.attachment = [mock_attachment]

    mock_jira_inst = MagicMock()
    mock_jira_inst.issue.return_value = mock_issue

    mock_module, _ = _mock_jira_module(mock_jira_inst)
    with patch.dict("os.environ", env):
        with patch.dict("sys.modules", {"jira": mock_module}):
            result = await jira_get_attachments.handler({"issue_key": "PROJ-1", "download_dir": str(tmp_path)})

    assert "is_error" not in result
    saved = json.loads(result["content"][0]["text"])
    assert len(saved) == 1
    # Original filename is preserved in metadata; the on-disk file is id-prefixed.
    assert saved[0]["filename"] == "spec.txt"
    assert saved[0]["path"].endswith("10001_spec.txt")
    assert (tmp_path / "10001_spec.txt").read_bytes() == b"hello world\n"


# ---------------------------------------------------------------------------
# github_tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_github_search_code_success():
    from norn.contrib.tools.github_tools import github_search_code

    fake_output = json.dumps([{"repository": {"name": "repo"}, "path": "src/Foo.java"}])
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=fake_output, stderr="")
        result = await github_search_code.handler({"query": "class Foo", "org": "myorg"})

    assert "is_error" not in result
    assert "Foo.java" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_github_search_code_gh_error():
    from norn.contrib.tools.github_tools import github_search_code

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="no auth")
        result = await github_search_code.handler({"query": "Foo", "org": "myorg"})

    assert result["is_error"] is True
    assert "no auth" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_github_list_repos_filters_by_language():
    from norn.contrib.tools.github_tools import github_list_repos

    repos = [
        {"name": "java-repo", "language": "Java", "url": "https://github.com/org/java-repo", "description": ""},
        {"name": "py-repo", "language": "Python", "url": "https://github.com/org/py-repo", "description": ""},
    ]
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(repos), stderr="")
        result = await github_list_repos.handler({"org": "myorg", "language": "Java"})

    assert "is_error" not in result
    data = json.loads(result["content"][0]["text"])
    assert len(data) == 1
    assert data[0]["name"] == "java-repo"


# ---------------------------------------------------------------------------
# testing_tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_tests_success():
    from norn.contrib.tools.testing_tools import run_tests

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="3 passed", stderr="")
        result = await run_tests.handler({})

    assert "is_error" not in result
    data = json.loads(result["content"][0]["text"])
    assert data["success"] is True
    assert data["returncode"] == 0


@pytest.mark.asyncio
async def test_run_tests_failure():
    from norn.contrib.tools.testing_tools import run_tests

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="1 failed", stderr="AssertionError")
        result = await run_tests.handler({"test_files": ["tests/test_foo.py"]})

    assert "is_error" not in result
    data = json.loads(result["content"][0]["text"])
    assert data["success"] is False
    assert "1 failed" in data["stdout"]


@pytest.mark.asyncio
async def test_run_coverage_no_json(tmp_path):
    from norn.contrib.tools.testing_tools import run_coverage

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        with patch("norn.contrib.tools.testing_tools.Path.cwd", return_value=tmp_path):
            result = await run_coverage.handler({})

    assert result["is_error"] is True
    assert "coverage.json" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_run_coverage_parses_report(tmp_path):
    import json as _json

    from norn.contrib.tools.testing_tools import run_coverage

    cov = {
        "totals": {"percent_covered": 87.5},
        "files": {
            "src/foo.py": {"summary": {"percent_covered": 90.0}},
            "src/bar.py": {"summary": {"percent_covered": 75.0}},
        },
    }
    (tmp_path / "coverage.json").write_text(_json.dumps(cov))

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        with patch("norn.contrib.tools.testing_tools.Path.cwd", return_value=tmp_path):
            result = await run_coverage.handler({"changed_files": ["src/foo.py"]})

    assert "is_error" not in result
    data = _json.loads(result["content"][0]["text"])
    assert data["overall_pct"] == 87.5
    assert data["changed_files"]["src/foo.py"] == 90.0


# ---------------------------------------------------------------------------
# shipping_tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_ci_status_success():
    from norn.contrib.tools.shipping_tools import check_ci_status

    runs = json.dumps([{"status": "completed", "conclusion": "success", "name": "CI", "databaseId": 1}])
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=runs, stderr="")
        result = await check_ci_status.handler({"repo": "org/repo", "branch": "main"})

    assert "is_error" not in result
    data = json.loads(result["content"][0]["text"])
    assert data[0]["conclusion"] == "success"


@pytest.mark.asyncio
async def test_create_pr_success():
    from norn.contrib.tools.shipping_tools import create_pr

    pr_url = "https://github.com/org/repo/pull/42"
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=pr_url, stderr="")
        result = await create_pr.handler({
            "repo": "org/repo", "branch": "fix-branch",
            "title": "Fix bug", "body": "Fixes PROJ-1",
        })

    assert "is_error" not in result
    data = json.loads(result["content"][0]["text"])
    assert data["url"] == pr_url
    assert data["success"] is True


@pytest.mark.asyncio
async def test_create_pr_draft_flag():
    from norn.contrib.tools.shipping_tools import create_pr

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="https://github.com/pr/1", stderr="")
        await create_pr.handler({
            "repo": "org/repo", "branch": "fix-branch",
            "title": "Fix", "body": "body", "draft": True,
        })
        cmd = mock_run.call_args[0][0]
        assert "--draft" in cmd


@pytest.mark.asyncio
async def test_notify_missing_httpx():
    import sys
    from norn.contrib.tools.shipping_tools import notify

    saved = sys.modules.get("httpx")
    sys.modules["httpx"] = None  # type: ignore[assignment]
    try:
        result = await notify.handler({"channel": "https://hooks.slack.com/fake", "message": "hi"})
    finally:
        if saved is None:
            sys.modules.pop("httpx", None)
        else:
            sys.modules["httpx"] = saved

    assert result["is_error"] is True
    assert "httpx" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_notify_non_url_channel():
    from norn.contrib.tools.shipping_tools import notify

    result = await notify.handler({"channel": "#dev-alerts", "message": "hi"})
    assert result["is_error"] is True
    assert "webhook URL" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# servers
# ---------------------------------------------------------------------------


def test_create_triage_server():
    from norn.contrib.tools.servers import create_triage_server

    server = create_triage_server()
    assert server["type"] == "sdk"
    assert server["name"] == "triage"


def test_create_coding_server():
    from norn.contrib.tools.servers import create_coding_server

    server = create_coding_server()
    assert server["type"] == "sdk"
    assert server["name"] == "coding"


def test_create_shipping_server():
    from norn.contrib.tools.servers import create_shipping_server

    server = create_shipping_server()
    assert server["type"] == "sdk"
    assert server["name"] == "shipping"

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from norn.models import PipelineContext, StageResult, UsageTracker
from norn.contrib.models.issue_context import IssueContext


def _make_ctx(issue: IssueContext | None = None, token: str | None = None) -> PipelineContext:
    ctx = PipelineContext(results={}, usage_tracker=UsageTracker(), params={})
    if issue is not None:
        ctx.results["read_issue"] = StageResult(name="read_issue", success=True, output=issue)
    if token is not None:
        ctx.secrets["GITHUB_TOKEN"] = token
    return ctx


def _make_issue(**overrides) -> IssueContext:
    defaults = dict(key="PROJ-42", summary="Fix login bug", description="desc", repo="org/myrepo")
    defaults.update(overrides)
    return IssueContext(**defaults)


def _make_git_repo(existing_branches: list[str] | None = None) -> MagicMock:
    mock = MagicMock()
    mock.branches = [SimpleNamespace(name=b) for b in (existing_branches or [])]
    return mock


@pytest.mark.asyncio
async def test_clone_missing_context():
    from norn.contrib.stages.clone import Clone

    stage = Clone()
    ctx = PipelineContext(results={}, usage_tracker=UsageTracker(), params={})
    result = await stage.run(ctx)
    assert not result.success
    assert "No issue context" in result.error


@pytest.mark.asyncio
async def test_clone_missing_repo():
    from norn.contrib.stages.clone import Clone

    stage = Clone()
    ctx = _make_ctx(issue=_make_issue(repo=None))
    result = await stage.run(ctx)
    assert not result.success
    assert "No repo" in result.error


@pytest.mark.asyncio
async def test_clone_fresh(tmp_path):
    from norn.contrib.stages.clone import Clone

    stage = Clone(clone_dir=str(tmp_path))
    ctx = _make_ctx(issue=_make_issue())
    mock_repo = _make_git_repo()

    with patch("git.Repo.clone_from", return_value=mock_repo):
        result = await stage.run(ctx)

    assert result.success
    assert result.output.branch == "PROJ-42-fix-login-bug"
    assert result.output.local_path == tmp_path / "myrepo-PROJ-42"


@pytest.mark.asyncio
async def test_clone_reuse_existing(tmp_path):
    from norn.contrib.stages.clone import Clone

    stage = Clone(clone_dir=str(tmp_path))
    ctx = _make_ctx(issue=_make_issue())
    (tmp_path / "myrepo-PROJ-42").mkdir()
    mock_repo = _make_git_repo()

    with patch("git.Repo", return_value=mock_repo):
        result = await stage.run(ctx)

    assert result.success
    mock_repo.remotes.origin.fetch.assert_called_once()


@pytest.mark.asyncio
async def test_clone_branch_already_exists(tmp_path):
    from norn.contrib.stages.clone import Clone

    stage = Clone(clone_dir=str(tmp_path))
    ctx = _make_ctx(issue=_make_issue())
    mock_repo = _make_git_repo(existing_branches=["PROJ-42-fix-login-bug"])

    with patch("git.Repo.clone_from", return_value=mock_repo):
        result = await stage.run(ctx)

    assert result.success
    assert call("PROJ-42-fix-login-bug") in mock_repo.git.checkout.call_args_list
    assert call("-b", "PROJ-42-fix-login-bug") not in mock_repo.git.checkout.call_args_list


@pytest.mark.asyncio
async def test_clone_uses_token(tmp_path):
    from norn.contrib.stages.clone import Clone

    stage = Clone(clone_dir=str(tmp_path))
    ctx = _make_ctx(issue=_make_issue(), token="mytoken")
    mock_repo = _make_git_repo()
    captured: list[str] = []

    def _capture_clone(url, *args, **kwargs):
        captured.append(url)
        return mock_repo

    with patch("git.Repo.clone_from", side_effect=_capture_clone):
        result = await stage.run(ctx)

    assert result.success
    assert "mytoken@github.com" in captured[0]


@pytest.mark.asyncio
async def test_clone_shallow(tmp_path):
    from norn.contrib.stages.clone import Clone

    stage = Clone(clone_dir=str(tmp_path), depth=1)
    ctx = _make_ctx(issue=_make_issue())
    mock_repo = _make_git_repo()

    with patch("git.Repo.clone_from", return_value=mock_repo) as mock_clone:
        result = await stage.run(ctx)

    assert result.success
    assert mock_clone.call_args.kwargs.get("depth") == 1


@pytest.mark.asyncio
async def test_clone_failure(tmp_path):
    from norn.contrib.stages.clone import Clone

    stage = Clone(clone_dir=str(tmp_path))
    ctx = _make_ctx(issue=_make_issue())

    with patch("git.Repo.clone_from", side_effect=Exception("network error")):
        result = await stage.run(ctx)

    assert not result.success
    assert "Clone failed" in result.error
    assert "network error" in result.error

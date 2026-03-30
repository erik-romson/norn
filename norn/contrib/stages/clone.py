from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from norn.models import StageResult
from norn.stages.base import BaseStage
from norn.contrib.utils.slugify import slugify

if TYPE_CHECKING:
    from norn.models import PipelineContext

log = logging.getLogger(__name__)


class Clone(BaseStage):
    needs_agent = False

    def __init__(
        self,
        *,
        clone_dir: str = "/tmp/issueprocessing/clones",
        branch_format: str = "{issue_key}-{slugified_title}",
        dir_format: str = "{repo_name}-{issue_key}",
        default_branch: str = "main",
        depth: int | None = None,
    ) -> None:
        self.clone_dir = clone_dir
        self.branch_format = branch_format
        self.dir_format = dir_format
        self.default_branch = default_branch
        self.depth = depth

    async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
        try:
            from git import Repo
        except ImportError:
            return StageResult(name="", success=False, error="GitPython is required: uv add GitPython")

        r = ctx.results.get("read_issue") or ctx.results.get("match_repo")
        issue_ctx = r.output if r is not None else None
        if issue_ctx is None:
            return StageResult(name="", success=False, error="No issue context found")

        repo = issue_ctx.repo
        if not repo:
            return StageResult(name="", success=False, error="No repo set on IssueContext")

        key = issue_ctx.key
        repo_name = repo.split("/")[-1]
        slugified = slugify(issue_ctx.summary)
        target_dir = Path(self.clone_dir) / self.dir_format.format(
            repo_name=repo_name, issue_key=key)
        branch = self.branch_format.format(
            issue_key=key, slugified_title=slugified)

        loop = asyncio.get_event_loop()

        token = ctx.secrets.get("GITHUB_TOKEN")
        clone_url = f"https://github.com/{repo}.git"
        if token:
            clone_url = f"https://{token}@github.com/{repo}.git"

        def _do_clone():
            if target_dir.exists():
                git_repo = Repo(target_dir)
                git_repo.remotes.origin.fetch()
            else:
                clone_kwargs: dict = {}
                if self.depth:
                    clone_kwargs["depth"] = self.depth
                git_repo = Repo.clone_from(clone_url, target_dir, **clone_kwargs)

            git_repo.git.checkout(self.default_branch)
            git_repo.remotes.origin.pull()
            # Check if branch already exists
            existing_branches = [b.name for b in git_repo.branches]
            if branch in existing_branches:
                git_repo.git.checkout(branch)
            else:
                git_repo.git.checkout("-b", branch)
            return git_repo

        try:
            await loop.run_in_executor(None, _do_clone)
        except Exception as e:
            return StageResult(name="", success=False, error=f"Clone failed: {e}")

        issue_ctx.local_path = target_dir
        issue_ctx.branch = branch
        return StageResult(name="", success=True, output=issue_ctx)

from __future__ import annotations

from typing import TYPE_CHECKING

from norn.models import StageResult
from norn.stages.base import BaseStage
from norn.stages.run_command import RunCommand
from norn.contrib.build.configs import BuildConfig
from norn.contrib.build.detect import detect_build_command

if TYPE_CHECKING:
    from norn.models import PipelineContext


class FullBuild(BaseStage):
    needs_agent = False

    def __init__(
        self,
        *,
        auto_detect: bool = True,
        overrides: dict[str, BuildConfig] | None = None,
    ) -> None:
        self.auto_detect = auto_detect
        self.overrides = overrides or {}

    async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
        r = ctx.results.get("read_issue")
        if r is None:
            return StageResult(name="", success=False, error="No read_issue result")
        issue_ctx = r.output

        repo = issue_ctx.repo or ""
        if repo in self.overrides:
            cmd = self.overrides[repo].cmd
        elif self.auto_detect:
            cmd = detect_build_command(issue_ctx.local_path)
        else:
            return StageResult(name="", success=False, error="No build command configured")

        if issue_ctx.local_path:
            cmd = f"cd {issue_ctx.local_path} && {cmd}"

        run = RunCommand(cmd=cmd, env={"CI": "true"})
        return await run.run(ctx, **kwargs)

from __future__ import annotations

from typing import TYPE_CHECKING

from norn.models import StageResult
from norn.stages.base import BaseStage
from norn.stages.run_command import RunCommand
from norn.contrib.build.detect import detect_test_command

if TYPE_CHECKING:
    from norn.models import PipelineContext


class VerifyTest(BaseStage):
    needs_agent = False
    # Delegates to RunCommand and forwards **kwargs, so opting in here is
    # what makes the build/test output stream to the transcript.
    emits_events = True

    async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
        r = ctx.results.get("read_issue")
        p = ctx.results.get("plan")
        if r is None or p is None:
            return StageResult(name="", success=False, error="Missing read_issue or plan context")
        issue_ctx = r.output
        fix_plan = p.output

        test_cmd = detect_test_command(issue_ctx.local_path, fix_plan.test_files)
        repo_path = str(issue_ctx.local_path)
        cmd = RunCommand(cmd=f"cd {repo_path!r} && {test_cmd}")
        return await cmd.run(ctx, **kwargs)

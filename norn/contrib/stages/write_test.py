from __future__ import annotations

from typing import TYPE_CHECKING

from norn.models import StageResult
from norn.stages.base import BaseStage
from norn.stages.generate import Generate

if TYPE_CHECKING:
    from norn.models import PipelineContext


class WriteTest(BaseStage):
    needs_agent = True

    def __init__(self, *, model: str | None = None) -> None:
        self.model = model

    async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
        r = ctx.results.get("read_issue")
        p = ctx.results.get("plan")
        if r is None or p is None:
            return StageResult(name="", success=False, error="Missing read_issue or plan context")
        issue_ctx = r.output
        fix_plan = p.output

        prompt = f"""Write a test that reproduces this bug.

## Issue: {issue_ctx.key} — {issue_ctx.summary}

## Test Strategy
{fix_plan.test_strategy}

## Test Files
Place tests in: {fix_plan.test_files}

## Instructions
1. Write the test file(s)
2. Run the test — it MUST FAIL (the bug hasn't been fixed yet)
3. If the test passes, the bug is not reproduced — adjust the test
4. Return the test file path(s) and the failure output"""

        gen = Generate(
            prompt=prompt,
            model=self.model,
            allowed_tools=["Read", "Write", "Edit", "Bash", "Grep", "Glob"],
            cwd=str(issue_ctx.local_path) if issue_ctx.local_path else None,
            permission_mode="bypassPermissions",
        )
        return await gen.run(ctx, **kwargs)

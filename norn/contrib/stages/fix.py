from __future__ import annotations

from typing import TYPE_CHECKING

from norn.models import StageResult
from norn.stages.base import BaseStage
from norn.stages.generate import Generate

if TYPE_CHECKING:
    from norn.models import PipelineContext


class Fix(BaseStage):
    needs_agent = True

    def __init__(
        self,
        *,
        tools: list[str] | None = None,
        blocked_patterns: list[str] | None = None,
        model: str | None = None,
    ) -> None:
        self.tools = tools or ["Read", "Edit", "Write", "Bash", "Grep", "Glob"]
        self.blocked_patterns = blocked_patterns or []
        self.model = model

    async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
        r = ctx.results.get("read_issue")
        p = ctx.results.get("plan")
        if r is None or p is None:
            return StageResult(name="", success=False, error="Missing read_issue or plan context")
        issue_ctx = r.output
        fix_plan = p.output

        prompt = f"""Implement the fix according to this plan.

## Issue: {issue_ctx.key} — {issue_ctx.summary}

## Plan
{fix_plan.analysis}

## Files to Change
{fix_plan.files_to_change}

## Instructions
- Make minimal, focused changes
- Follow existing code style
- Do NOT change unrelated code
- Commit your changes with message: [{issue_ctx.key}] {issue_ctx.summary}"""

        hooks = None
        if self.blocked_patterns:
            from norn.profiles import build_block_hooks
            hooks = build_block_hooks(self.blocked_patterns)

        gen = Generate(
            prompt=prompt,
            model=self.model,
            allowed_tools=self.tools,
            cwd=str(issue_ctx.local_path) if issue_ctx.local_path else None,
            permission_mode="bypassPermissions",
            hooks=hooks,
        )
        return await gen.run(ctx, **kwargs)

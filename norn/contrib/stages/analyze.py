from __future__ import annotations

from typing import TYPE_CHECKING

from norn.models import StageResult
from norn.stages.base import BaseStage
from norn.stages.generate import Generate

if TYPE_CHECKING:
    from norn.models import PipelineContext


class Analyze(BaseStage):
    needs_agent = True

    def __init__(
        self,
        *,
        tools: list[str] | None = None,
        model: str | None = None,
        max_turns: int = 30,
    ) -> None:
        self.tools = tools or ["Read", "Grep", "Glob"]
        self.model = model
        self.max_turns = max_turns

    async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
        r = ctx.results.get("read_issue")
        if r is None:
            return StageResult(name="", success=False, error="No read_issue result")
        issue_ctx = r.output

        prompt = f"""Analyze this issue in the context of the repository.

## Issue
Key: {issue_ctx.key}
Summary: {issue_ctx.summary}
Description: {issue_ctx.description}
Stacktraces: {issue_ctx.stacktraces}
Repro steps: {issue_ctx.repro_steps}

## Instructions
- Explore the repository structure
- Find files related to the issue
- Understand the code flow that leads to the bug
- Identify root cause

Return a structured analysis as JSON:
{{
    "root_cause": "...",
    "relevant_files": ["path/to/file", ...],
    "code_flow": "...",
    "additional_context_needed": null
}}"""

        prior_plan_result = ctx.results.get("plan")
        if prior_plan_result is not None:
            prior_plan = prior_plan_result.output
            if hasattr(prior_plan, "approval_feedback") and prior_plan.approval_feedback:
                prompt += f"\n\n## Previous Plan Was Rejected\nUser feedback: {prior_plan.approval_feedback}\nPlease take this feedback into account."

        gen = Generate(
            prompt=prompt,
            model=self.model,
            allowed_tools=self.tools,
            max_turns=self.max_turns,
            cwd=str(issue_ctx.local_path) if issue_ctx.local_path else None,
            permission_mode="plan",
        )
        return await gen.run(ctx, **kwargs)

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from norn.models import StageResult
from norn.stages.base import BaseStage
from norn.stages.generate import Generate
from norn.contrib.models.fix_plan import FixPlan
from norn.contrib.parsers.fix_plan import parse_fix_plan

if TYPE_CHECKING:
    from norn.models import PipelineContext

log = logging.getLogger(__name__)


class Plan(BaseStage):
    needs_agent = True

    def __init__(
        self,
        *,
        require_approval: bool = True,
        include_risk_assessment: bool = True,
        model: str | None = None,
    ) -> None:
        self.require_approval = require_approval
        self.include_risk = include_risk_assessment
        self.model = model

    async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
        r = ctx.results.get("read_issue")
        if r is None:
            return StageResult(name="", success=False, error="No read_issue result")
        issue_ctx = r.output
        analysis_result = ctx.results.get("analyze")
        analysis = analysis_result.output if analysis_result is not None else None

        risk_instruction = "- Include risks and potential side effects" if self.include_risk else ""
        prompt = f"""Based on this analysis, create a fix plan.

## Issue
Key: {issue_ctx.key}
Summary: {issue_ctx.summary}

## Analysis
{analysis}

## Instructions
Create a structured plan as JSON:
{{
    "analysis": "What's wrong and why",
    "files_to_change": [
        {{"path": "...", "description": "what to change", "reason": "why"}}
    ],
    "test_strategy": "What test to write",
    "test_files": ["path/to/new/test"],
    "risks": ["potential side effects"],
    "confidence": 0.0
}}
{risk_instruction}"""

        gen = Generate(
            prompt=prompt,
            model=self.model,
            allowed_tools=["Read", "Grep", "Glob"],
            cwd=str(issue_ctx.local_path) if issue_ctx.local_path else None,
            permission_mode="plan",
        )
        result = await gen.run(ctx, **kwargs)

        if not result.success:
            return result

        fix_plan = parse_fix_plan(str(result.output))

        if self.require_approval:
            approved = await self._get_approval(fix_plan)
            if not approved:
                return StageResult(
                    name="", success=False,
                    error="Plan rejected by user",
                    output=fix_plan,
                )
            fix_plan.approved = True

        return StageResult(name="", success=True, output=fix_plan)

    async def _get_approval(self, plan: FixPlan) -> bool:
        from norn import ui
        _present_plan(plan)
        approved = ui.ask_yes_no("Approve this plan?")
        if not approved:
            feedback = ui.console.input("[dim]Reason for rejection (optional): [/dim]").strip()
            if feedback:
                plan.approval_feedback = feedback
        return approved


def _present_plan(plan: FixPlan) -> None:
    from norn import ui
    from rich.panel import Panel
    from rich.table import Table
    ui.console.print(Panel(plan.analysis, title="[bold]Analysis[/bold]"))
    if plan.files_to_change:
        table = Table(title="Files to Change")
        table.add_column("Path")
        table.add_column("Change")
        table.add_column("Reason")
        for fc in plan.files_to_change:
            table.add_row(fc.path, fc.description, fc.reason)
        ui.console.print(table)
    if plan.test_strategy:
        ui.console.print(f"Test strategy: {plan.test_strategy}")
    if plan.risks:
        ui.console.print(f"Risks: {', '.join(plan.risks)}")
    ui.console.print(f"Confidence: {plan.confidence:.0%}")

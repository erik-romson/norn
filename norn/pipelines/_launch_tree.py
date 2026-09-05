"""Private helper: AssertLaunchTree stage — not a pipeline."""
from __future__ import annotations

from typing import Any

from norn.models import PipelineContext, StageResult
from norn.stages.base import BaseStage


class AssertLaunchTree(BaseStage):
    """Fail fast when ctx.working_dir diverges from the launch repo.

    Pipelines that pin git and test commands to the launch directory at
    import time will silently target the wrong tree when run under the TUI
    worktree toggle.  This stage catches that mismatch before any work is done.
    """

    needs_agent = False

    def __init__(self, *, project_dir: str) -> None:
        self._project_dir = project_dir

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        if ctx.working_dir and ctx.working_dir != self._project_dir:
            return StageResult(
                name="",
                success=False,
                error=(
                    f"Worktree isolation is not supported: "
                    f"working_dir={ctx.working_dir} != {self._project_dir}"
                ),
            )
        return StageResult(name="", success=True)

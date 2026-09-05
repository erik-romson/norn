from __future__ import annotations

from typing import Any

from norn.models import PipelineContext, StageResult
from norn.runner import resolve_run_path
from norn.stages.base import BaseStage


class ReadFile(BaseStage):
    """Read a file from disk and return its contents as stage output.

    Pure Python — no agent session, no SDK dependency.

    Args:
        path: Path to the file to read (relative to CWD or absolute).

    Output:
        ``StageResult.output`` is the file contents as a ``str``.

    Example::

        Stage("read_spec", ReadFile(path="examples/spec.txt"))
        # downstream: ctx.get("read_spec") → "file contents..."
    """

    def __init__(self, *, path: str) -> None:
        self.path = path

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        try:
            content = resolve_run_path(ctx, self.path).read_text()
            return StageResult(name="", success=True, output=content)
        except OSError as e:
            return StageResult(name="", success=False, error=str(e))

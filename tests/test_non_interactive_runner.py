import pytest

from norn.dsl import OnFailure, Pipeline
from norn.models import PipelineContext, StageResult
from norn.responder import NonInteractiveResponder
from norn.runner import PipelineError, run_pipeline
from norn.stages.base import BaseStage


class FailStage(BaseStage):
    """Always fails."""

    async def run(self, ctx: PipelineContext) -> StageResult:
        return StageResult(name="", success=False, error="boom")


class TrackStage(BaseStage):
    """Records that it ran."""

    def __init__(self, ran: list) -> None:
        self._ran = ran

    async def run(self, ctx: PipelineContext) -> StageResult:
        self._ran.append("ran")
        return StageResult(name="", success=True)


@pytest.mark.asyncio
async def test_non_interactive_aborts_on_failure_and_skips_subsequent_stage():
    """A failing on_failure=ask_user stage under NonInteractiveResponder raises
    PipelineError and the subsequent stage never runs."""
    ran: list[str] = []

    p = (
        Pipeline("test")
        .stage("fail_stage", FailStage(), on_failure=OnFailure.ASK_USER)
        .stage("track_stage", TrackStage(ran))
    )

    with pytest.raises(PipelineError):
        await run_pipeline(p, input_responder=NonInteractiveResponder())

    assert ran == [], "subsequent stage must not run after abort"

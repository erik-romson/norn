import pytest

from norn.responder import NonInteractiveResponder


@pytest.mark.asyncio
async def test_ask_failure_returns_abort():
    responder = NonInteractiveResponder()
    result = await responder.ask_failure("my_stage", "something went wrong")
    assert result == "a"


@pytest.mark.asyncio
async def test_ask_budget_returns_abort():
    responder = NonInteractiveResponder()
    result = await responder.ask_budget(tracker=None, budget=None)
    assert result == "a"


@pytest.mark.asyncio
async def test_ask_step_raises_runtime_error():
    responder = NonInteractiveResponder()
    with pytest.raises(RuntimeError, match="incompatible with --non-interactive"):
        await responder.ask_step(stage=None, ctx=None)

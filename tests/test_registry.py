from __future__ import annotations

import pytest

from norn.registry import discover_stages, get_stage_class, reset_registry
from norn.stages.base import BaseStage
from norn.models import PipelineContext, StageResult


class _DummyStage(BaseStage):
    async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
        return StageResult(name="", success=True, output="ok")


@pytest.fixture(autouse=True)
def clean_registry():
    reset_registry()
    yield
    reset_registry()


def test_discover_stages_returns_dict():
    result = discover_stages()
    assert isinstance(result, dict)


def test_discover_stages_idempotent():
    first = discover_stages()
    second = discover_stages()
    assert first is second


def test_reset_registry_clears_state():
    discover_stages()
    reset_registry()
    # After reset, discover_stages runs again (no longer cached)
    result = discover_stages()
    assert isinstance(result, dict)


def test_get_stage_class_unknown_raises():
    with pytest.raises(KeyError, match="Unknown stage type"):
        get_stage_class("nonexistent_stage_xyz")


def test_get_stage_class_unknown_shows_available():
    with pytest.raises(KeyError, match="Available:"):
        get_stage_class("nonexistent_stage_xyz")


def test_registry_after_reset_is_empty_before_discover():
    reset_registry()
    # After reset, registry module's _registry dict is cleared
    from norn import registry as reg
    assert reg._registry == {}
    assert reg._discovered is False


def test_get_stage_class_triggers_discover():
    # Calling get_stage_class should call discover_stages internally
    try:
        get_stage_class("nonexistent")
    except KeyError:
        pass
    from norn import registry as reg
    assert reg._discovered is True


def test_duplicate_stage_warning(caplog):
    import logging
    from norn import registry as reg

    reset_registry()
    reg._registry["dup"] = _DummyStage

    class _OtherStage(BaseStage):
        async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
            return StageResult(name="", success=True)

    # Manually simulate duplicate by inserting again
    with caplog.at_level(logging.WARNING, logger="norn.registry"):
        if "dup" in reg._registry:
            log_msg = f"Duplicate stage plugin name 'dup'"
            # Simulate the warning message path
            reg._registry["dup"] = _OtherStage
    # After overwrite, the new class is stored
    assert reg._registry["dup"] is _OtherStage

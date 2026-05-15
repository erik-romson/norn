from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from norn.models import PipelineContext, StageResult
from norn.stages.extract_failing_step import (
    ExtractFailingStep,
    _parse_failed_step_names,
    _slice_failing_step,
)


def _ctx_with(name: str, result: StageResult, *, agent_provider: str = "claude-code") -> PipelineContext:
    ctx = PipelineContext(agent_provider=agent_provider)
    ctx.results[name] = result
    return ctx


# --- pure-Python slicer ---------------------------------------------------


def test_parse_failed_step_names_single():
    text = "## Failed job: build\nFailed steps: Run e2e tests\n\nbody"
    assert _parse_failed_step_names(text) == {"Run e2e tests"}


def test_parse_failed_step_names_multiple():
    text = (
        "## Failed job: build\n"
        "Failed steps: Run e2e tests, Upload artifacts\n"
        "\n"
        "## Failed job: deploy\n"
        "Failed steps: Push image\n"
    )
    assert _parse_failed_step_names(text) == {
        "Run e2e tests", "Upload artifacts", "Push image",
    }


def test_parse_failed_step_names_none():
    assert _parse_failed_step_names("just a raw log\nwith no header\n") == set()


def test_slice_keeps_only_failing_step():
    log = (
        "## Failed job: build\n"
        "Failed steps: Run tests\n"
        "\n"
        "##[group]Set up job\n"
        "setup line 1\n"
        "setup line 2\n"
        "##[endgroup]\n"
        "##[group]Install deps\n"
        "npm install...\n"
        "##[endgroup]\n"
        "##[group]Run tests\n"
        "test FAILED\n"
        "stack trace line\n"
        "##[endgroup]\n"
        "##[group]Post Run\n"
        "cleanup\n"
        "##[endgroup]\n"
    )
    sliced = _slice_failing_step(log)
    assert "test FAILED" in sliced
    assert "stack trace line" in sliced
    assert "setup line" not in sliced
    assert "npm install" not in sliced
    assert "cleanup" not in sliced


def test_slice_handles_nested_groups():
    """Nested ##[group] inside a step (e.g. download progress) must not be
    treated as a step boundary."""
    log = (
        "Failed steps: Run tests\n"
        "##[group]Run tests\n"
        "starting tests\n"
        "##[group]download progress\n"
        "5%...50%...100%\n"
        "##[endgroup]\n"
        "test FAILED\n"
        "##[endgroup]\n"
    )
    sliced = _slice_failing_step(log)
    assert "starting tests" in sliced
    assert "5%...50%...100%" in sliced
    assert "test FAILED" in sliced


def test_slice_word_boundary_step_name_match():
    """Step names sometimes differ slightly between header and group label.
    Word-boundary matching keeps it robust without trapping substrings."""
    log = (
        "Failed steps: Run e2e tests\n"
        "##[group]Run e2e tests (chromium)\n"
        "spec failed\n"
        "##[endgroup]\n"
    )
    sliced = _slice_failing_step(log)
    assert "spec failed" in sliced


def test_slice_does_not_substring_false_positive():
    """A short step name like ``test`` must not match group labels where
    it only appears as part of another word (e.g. ``latest``)."""
    log = (
        "Failed steps: test\n"
        "##[group]Run docker pull ghcr.io/foo:latest\n"
        "pull noise\n"
        "##[endgroup]\n"
        "##[group]Run npm test\n"
        "the real test failure\n"
        "##[endgroup]\n"
    )
    sliced = _slice_failing_step(log)
    assert "the real test failure" in sliced
    assert "pull noise" not in sliced


def test_slice_no_header_returns_empty():
    log = "##[group]Run tests\nfoo\n##[endgroup]\n"
    assert _slice_failing_step(log) == ""


def test_slice_no_groups_returns_empty():
    log = "Failed steps: Run tests\njust raw output\nFAIL!\n"
    assert _slice_failing_step(log) == ""


def test_slice_no_matching_group_returns_empty():
    """Header says step X failed, but no group is labeled X."""
    log = (
        "Failed steps: Step X\n"
        "##[group]Step Y\nfoo\n##[endgroup]\n"
        "##[group]Step Z\nbar\n##[endgroup]\n"
    )
    assert _slice_failing_step(log) == ""


def test_slice_unterminated_group_is_flushed():
    """CI killed mid-step → no endgroup; we still emit what we captured."""
    log = (
        "Failed steps: Run tests\n"
        "##[group]Run tests\n"
        "test FAILED\n"
        "killed by timeout\n"
    )
    sliced = _slice_failing_step(log)
    assert "test FAILED" in sliced
    assert "killed by timeout" in sliced


# --- stage --------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_source_stage_returns_empty():
    stage = ExtractFailingStep(source_stage="check ci")
    result = await stage.run(PipelineContext())
    assert result.success
    assert result.output == ""
    assert "has no result yet" in (result.error or "")


@pytest.mark.asyncio
async def test_skips_when_upstream_successful():
    ctx = _ctx_with(
        "check ci",
        StageResult(name="check ci", success=True, output="green log"),
    )
    stage = ExtractFailingStep(source_stage="check ci")
    result = await stage.run(ctx)
    assert result.success
    assert result.output == ""


@pytest.mark.asyncio
async def test_python_slice_then_haiku():
    """Happy path: Python slices the step, Haiku compresses the slice."""
    log = (
        "Failed steps: Run tests\n"
        "##[group]Run tests\n"
        + ("noise\n" * 2000)
        + "test FAILED\n"
        "##[endgroup]\n"
    )
    ctx = _ctx_with(
        "check ci",
        StageResult(name="check ci", success=False, output=log),
    )

    captured: dict[str, str] = {}

    async def _capture(text: str, *, model: str, provider: str = "claude-code") -> str:
        captured["sent_to_haiku"] = text
        return "compressed output"

    with patch(
        "norn.stages.extract_failing_step._haiku_compress",
        new=_capture,
    ):
        result = await ExtractFailingStep(source_stage="check ci").run(ctx)

    assert result.success
    assert result.output == "compressed output"
    # Haiku must have received the python-sliced text, not the raw header.
    assert "## Failed job" not in captured["sent_to_haiku"]
    assert "test FAILED" in captured["sent_to_haiku"]


@pytest.mark.asyncio
async def test_haiku_disabled_returns_python_slice():
    log = (
        "Failed steps: Run tests\n"
        "##[group]Run tests\n"
        "test FAILED\n"
        "##[endgroup]\n"
    )
    ctx = _ctx_with(
        "check ci",
        StageResult(name="check ci", success=False, output=log),
    )
    stage = ExtractFailingStep(source_stage="check ci", summarize_with_haiku=False)

    with patch(
        "norn.stages.extract_failing_step._haiku_compress",
        new=AsyncMock(),
    ) as haiku:
        result = await stage.run(ctx)

    assert result.success
    assert "test FAILED" in result.output
    haiku.assert_not_called()


@pytest.mark.asyncio
async def test_haiku_skipped_when_slice_below_threshold():
    """Small slices aren't worth compressing further."""
    log = (
        "Failed steps: Run tests\n"
        "##[group]Run tests\n"
        "tiny failure\n"
        "##[endgroup]\n"
    )
    ctx = _ctx_with(
        "check ci",
        StageResult(name="check ci", success=False, output=log),
    )
    stage = ExtractFailingStep(source_stage="check ci", min_chars_for_haiku=10_000)

    with patch(
        "norn.stages.extract_failing_step._haiku_compress",
        new=AsyncMock(),
    ) as haiku:
        result = await stage.run(ctx)

    assert result.success
    assert "tiny failure" in result.output
    haiku.assert_not_called()


@pytest.mark.asyncio
async def test_python_slice_fails_falls_through_to_haiku_on_raw():
    """No markers/header → Python slice empty, raw log goes to Haiku."""
    raw = "no markers here\n" * 500 + "boom\n"
    ctx = _ctx_with(
        "check ci",
        StageResult(name="check ci", success=False, output=raw),
    )

    captured: dict[str, str] = {}

    async def _capture(text: str, *, model: str, provider: str = "claude-code") -> str:
        captured["sent_to_haiku"] = text
        return "haiku output"

    with patch(
        "norn.stages.extract_failing_step._haiku_compress",
        new=_capture,
    ):
        result = await ExtractFailingStep(source_stage="check ci").run(ctx)

    assert result.success
    assert result.output == "haiku output"
    assert "no markers here" in captured["sent_to_haiku"]


@pytest.mark.asyncio
async def test_haiku_failure_returns_python_slice():
    log = (
        "Failed steps: Run tests\n"
        "##[group]Run tests\n"
        + ("x\n" * 3000)
        + "FAIL\n"
        "##[endgroup]\n"
    )
    ctx = _ctx_with(
        "check ci",
        StageResult(name="check ci", success=False, output=log),
    )

    with patch(
        "norn.stages.extract_failing_step._haiku_compress",
        new=AsyncMock(return_value=None),
    ):
        result = await ExtractFailingStep(source_stage="check ci").run(ctx)

    assert result.success
    # Haiku failed; we still get the python slice.
    assert "FAIL" in result.output


@pytest.mark.asyncio
async def test_haiku_input_truncated_when_oversized():
    """Slices bigger than max_haiku_input_chars are head+tail truncated."""
    big_step = "HEAD\n" + ("x" * 50_000) + "\nTAIL\n"
    log = (
        "Failed steps: Run tests\n"
        "##[group]Run tests\n"
        f"{big_step}"
        "##[endgroup]\n"
    )
    ctx = _ctx_with(
        "check ci",
        StageResult(name="check ci", success=False, output=log),
    )

    captured: dict[str, str] = {}

    async def _capture(text: str, *, model: str, provider: str = "claude-code") -> str:
        captured["sent_to_haiku"] = text
        return "compressed"

    stage = ExtractFailingStep(
        source_stage="check ci",
        min_chars_for_haiku=100,
        max_haiku_input_chars=5_000,
    )

    with patch(
        "norn.stages.extract_failing_step._haiku_compress",
        new=_capture,
    ):
        result = await stage.run(ctx)

    assert result.success
    sent = captured["sent_to_haiku"]
    assert len(sent) < 50_000
    assert "chars omitted from middle" in sent
    assert "HEAD" in sent
    assert "TAIL" in sent


@pytest.mark.asyncio
async def test_haiku_compress_receives_ctx_agent_provider():
    """When ctx.agent_provider is non-default, _haiku_compress receives it."""
    log_text = (
        "Failed steps: Run tests\n"
        "##[group]Run tests\n"
        + ("noise\n" * 2000)
        + "test FAILED\n"
        "##[endgroup]\n"
    )
    ctx = _ctx_with(
        "check ci",
        StageResult(name="check ci", success=False, output=log_text),
        agent_provider="opencode",
    )

    captured: dict[str, str] = {}

    async def _capture(text: str, *, model: str, provider: str = "claude-code") -> str:
        captured["provider"] = provider
        return "compressed"

    with patch(
        "norn.stages.extract_failing_step._haiku_compress",
        new=_capture,
    ):
        result = await ExtractFailingStep(source_stage="check ci").run(ctx)

    assert result.success
    assert captured["provider"] == "opencode"


@pytest.mark.asyncio
async def test_haiku_compress_delegates_to_complete_text():
    """_haiku_compress delegates to complete_text with correct args."""
    from norn.stages.extract_failing_step import _haiku_compress

    captured: dict[str, Any] = {}

    async def fake_complete(prompt, *, provider, model, system_prompt=None, cwd=None, env=None):
        captured["provider"] = provider
        captured["model"] = model
        captured["system_prompt"] = system_prompt
        captured["prompt"] = prompt
        return "result"

    with patch("norn.agents.complete.complete_text", new=fake_complete):
        result = await _haiku_compress("some log", model="haiku", provider="opencode")

    assert result == "result"
    assert captured["provider"] == "opencode"
    assert captured["model"] == "haiku"
    assert captured["system_prompt"] is not None
    assert "some log" in captured["prompt"]

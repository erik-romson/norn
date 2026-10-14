"""Tests for norn/evaluate.py — agent evaluation (judge + feedback)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator
from unittest.mock import patch

import pytest

from norn.agents.base import AgentEvent, AgentRequest, AgentUsage
from norn.agents.capabilities import AgentCapabilities, CostMode
from norn.evaluate import (
    EvaluationError,
    EvaluationResult,
    build_feedback_prompt,
    build_judge_prompt,
    evaluate_run,
    judge_stage,
    validate_feedback_header,
)


# ---------------------------------------------------------------------------
# Fake providers
# ---------------------------------------------------------------------------


class FakeProvider:
    """A fake provider that yields canned text, structured_output, and usage."""

    name = "fake"

    capabilities = AgentCapabilities(
        block_kinds=frozenset({"text"}),
        cost_mode=CostMode.TRACKED,
        supports_structured_output=True,
        supports_fork=False,
        supports_hooks=False,
        supports_mcp=False,
        supports_thinking=False,
        file_edit_without_terminal=False,
        session_resumable=False,
        session_forkable=False,
        session_attachable=False,
        live_model_switch=False,
        model_alias_table="claude-code",
        can_list_models=False,
    )

    def __init__(
        self,
        text: str = "",
        structured_output: Any = None,
        cost: float = 0.01,
        input_tokens: int = 100,
        output_tokens: int = 50,
    ):
        self._text = text
        self._structured_output = structured_output
        self._cost = cost
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens

    async def run(self, request: AgentRequest) -> AsyncIterator[AgentEvent]:
        if self._text:
            yield AgentEvent(text=self._text)
        if self._structured_output is not None:
            yield AgentEvent(structured_output=self._structured_output)
        yield AgentEvent(
            usage=AgentUsage(
                provider=self.name,
                total_cost_usd=self._cost,
                input_tokens=self._input_tokens,
                output_tokens=self._output_tokens,
            )
        )


class NoStructuredProvider:
    """A fake provider without structured output support."""

    name = "no-structured"

    capabilities = AgentCapabilities(
        block_kinds=frozenset({"text"}),
        cost_mode=CostMode.ZERO_UNKNOWN,
        supports_structured_output=False,
        supports_fork=False,
        supports_hooks=False,
        supports_mcp=False,
        supports_thinking=False,
        file_edit_without_terminal=False,
        session_resumable=False,
        session_forkable=False,
        session_attachable=False,
        live_model_switch=False,
        model_alias_table="opencode",
        can_list_models=False,
    )

    async def run(self, request: AgentRequest) -> AsyncIterator[AgentEvent]:
        yield AgentEvent(text="nope")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run_dir(tmp_path: Path, run_id: int = 1) -> Path:
    """Create a minimal run directory with run.json, stages, metrics, findings."""
    rd = tmp_path / "test.runs" / str(run_id)
    rd.mkdir(parents=True)
    (rd / "stages").mkdir()

    run_json = {
        "run_id": run_id,
        "pipeline": "test-pipeline",
        "pipeline_source": "/path/to/test.py",
        "provider": "fake",
        "cost_mode": "tracked",
        "success": True,
        "failed_stage": None,
        "duration_ms": 5000,
    }
    (rd / "run.json").write_text(json.dumps(run_json, indent=2))

    return rd


def _make_stage_record(
    rd: Path,
    node_id: str = "implement",
    attempt: int = 1,
    turns: int = 42,
    cost: float = 2.0,
    seq: int = 1,
) -> None:
    """Write a stage record JSON file."""
    record = {
        "node_id": node_id,
        "attempt": attempt,
        "status": "passed",
        "started_at": "2026-09-05T10:00:00Z",
        "finished_at": "2026-09-05T10:05:00Z",
        "duration_ms": 5000,
        "output": "Stage completed successfully.",
        "error": None,
        "success": True,
        "artifacts": [],
        "details": {
            "prompt": "Implement the feature as described.",
            "system_prompt": "You are a coding assistant.",
        },
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 500,
            "cache_read_input_tokens": 200,
            "cache_creation_input_tokens": 0,
            "total_cost_usd": cost,
            "duration_ms": 4000,
            "num_turns": turns,
            "model_id": "claude-sonnet-4-6",
            "model": "sonnet",
            "result_subtype": "success",
            "stop_reason": None,
            "tool_uses": 10,
            "tool_errors": 2,
            "session_id": "sess-1",
        },
    }
    safe_id = node_id.replace("/", "_").replace(":", "_")
    path = rd / "stages" / f"{seq:04d}-{safe_id}-a{attempt}.json"
    path.write_text(json.dumps(record, indent=2))


def _make_metrics(rd: Path) -> None:
    """Write minimal metrics.json and findings.json."""
    from dataclasses import asdict
    from norn.metrics import Finding, RunMetrics, StageMetrics

    sm = StageMetrics(
        name="implement", node_id="implement", attempt=1, kind="agent",
        status="passed", success=True, cost_usd=2.0, cost_share=100.0,
        billed_input_tokens=1200, output_tokens=500, cache_hit_ratio=0.17,
        turns=42, tokens_per_turn=35.7, tool_uses=10, tool_errors=2,
        prompt_chars=100, system_prompt_chars=30, unresolved_placeholders=[],
        duration_ms=5000, model="sonnet", model_id="claude-sonnet-4-6",
        result_subtype="success", stop_reason=None, nonfatal_error=False,
    )
    metrics = RunMetrics(
        run_id="1", pipeline="test-pipeline", provider="fake",
        cost_mode="tracked", success=True, failed_stage=None,
        cost_usd=2.0, billed_input_tokens=1200, output_tokens=500,
        cache_read_tokens=200, cache_hit_ratio=0.17, turns=42,
        wall_ms=5000, api_ms=4000, tool_time_ms=1000,
        agent_stage_count=1, command_stage_count=0,
        failed_attempts=0, failed_attempt_cost_usd=0.0, timed_out_attempts=0, retries=0,
        budget_max_cost_usd=None, budget_used_pct=None,
        waiting_input={}, top_cost_stages=["implement"],
        quota=None, codex_cost_usd_estimate=None, stages=[sm],
    )
    findings = [Finding(
        rule="tool-error-rate",
        severity="warn",
        stage="implement",
        evidence="2/10 tool calls failed",
        suggestion="Fix tool permissions.",
    )]
    (rd / "metrics.json").write_text(json.dumps(asdict(metrics), indent=2))
    (rd / "findings.json").write_text(json.dumps([asdict(f) for f in findings], indent=2))


# ---------------------------------------------------------------------------
# (a) Judge prompt contains stage data
# ---------------------------------------------------------------------------


def test_judge_prompt_contains_stage_data():
    """Judge prompt contains the stage's prompt head, tool digest with x, and numbers."""
    record = {
        "node_id": "implement",
        "details": {
            "prompt": "Implement the feature.",
            "system_prompt": "You are a helper.",
        },
        "output": "Done.",
        "usage": {
            "num_turns": 42,
            "total_cost_usd": 2.0,
            "tool_uses": 10,
            "tool_errors": 2,
        },
    }
    digest = "tool: Read file.py\ntool: Write file.py x"

    prompt = build_judge_prompt(record, digest)

    assert "42 turns" in prompt
    assert "$2.00" in prompt
    assert "Implement the feature." in prompt
    assert "tool: Write file.py x" in prompt
    assert "You are a helper." in prompt


# ---------------------------------------------------------------------------
# (b) .eval.json written from canned structured_output
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eval_json_written_from_structured_output(tmp_path):
    """judge_stage writes structured output that can be saved as .eval.json."""
    canned = {
        "score": 4,
        "did_what_was_asked": True,
        "waste": ["unnecessary retry"],
        "prompt_issues": [],
        "summary": "Good job overall.",
    }
    provider = FakeProvider(structured_output=canned)
    record = {
        "node_id": "implement",
        "details": {"prompt": "Do it.", "system_prompt": ""},
        "output": "ok",
        "usage": {"num_turns": 5, "total_cost_usd": 0.10, "tool_uses": 3, "tool_errors": 0},
    }

    result, usage = await judge_stage(provider, record, "digest", model="light")

    assert result == canned
    assert result["score"] == 4

    # Simulate writing
    eval_path = tmp_path / "eval.json"
    eval_path.write_text(json.dumps(result, indent=2))
    loaded = json.loads(eval_path.read_text())
    assert loaded["score"] == 4
    assert loaded["did_what_was_asked"] is True


# ---------------------------------------------------------------------------
# (c) Provider without structured output raises with --no-judge hint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_structured_output_raises():
    """Provider with supports_structured_output=False raises with --no-judge hint."""
    provider = NoStructuredProvider()
    record = {
        "node_id": "test",
        "details": {"prompt": "x", "system_prompt": ""},
        "output": "",
        "usage": {"num_turns": 1, "total_cost_usd": 0, "tool_uses": 0, "tool_errors": 0},
    }

    with pytest.raises(EvaluationError, match="--no-judge"):
        await judge_stage(provider, record, "", model="light")


# ---------------------------------------------------------------------------
# (d) Feedback prompt contains metrics, findings, pipeline source, prior, rating
# ---------------------------------------------------------------------------


def test_feedback_prompt_contents(tmp_path):
    """Feedback prompt contains metrics.json, findings.json, pipeline source, prior, rating."""
    rd = _make_run_dir(tmp_path)
    _make_stage_record(rd)
    _make_metrics(rd)

    from norn.metrics import RunMetrics, StageMetrics

    # Create a prior run for the prompt
    prior_sm = StageMetrics(
        name="gen", node_id="gen", attempt=1, kind="agent", status="passed",
        success=True, cost_usd=1.0, cost_share=100.0,
        billed_input_tokens=500, output_tokens=200, cache_hit_ratio=0.0,
        turns=10, tokens_per_turn=70.0, tool_uses=5, tool_errors=0,
        prompt_chars=50, system_prompt_chars=10, unresolved_placeholders=[],
        duration_ms=3000, model="sonnet", model_id=None,
        result_subtype="success", stop_reason=None, nonfatal_error=False,
    )
    prior = [RunMetrics(
        run_id="0", pipeline="test-pipeline", provider="fake",
        cost_mode="tracked", success=True, failed_stage=None,
        cost_usd=1.0, billed_input_tokens=500, output_tokens=200,
        cache_read_tokens=0, cache_hit_ratio=0.0, turns=10,
        wall_ms=3000, api_ms=2000, tool_time_ms=1000,
        agent_stage_count=1, command_stage_count=0,
        failed_attempts=0, failed_attempt_cost_usd=0.0, timed_out_attempts=0, retries=0,
        budget_max_cost_usd=None, budget_used_pct=None,
        waiting_input={}, top_cost_stages=["gen"],
        quota=None, codex_cost_usd_estimate=None, stages=[prior_sm],
    )]

    rating = {"score": 4, "note": "decent"}

    prompt = build_feedback_prompt(
        rd, None, [], {}, prior, rating, "/path/to/test.py"
    )

    assert "metrics.json" in prompt
    assert "findings.json" in prompt
    assert "/path/to/test.py" in prompt
    assert "Prior runs" in prompt
    assert "cost=$1.00" in prompt
    assert "Human rating: 4/5" in prompt
    assert "decent" in prompt


# ---------------------------------------------------------------------------
# (e) Valid feedback header writes feedback.md and evaluation.json
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feedback_writes_files(tmp_path):
    """Canned reply with valid header writes feedback.md and evaluation.json."""
    rd = _make_run_dir(tmp_path)
    _make_stage_record(rd)
    _make_metrics(rd)

    feedback_text = "RUN: 1\nSCORE: 7\nPIPELINE: /path/to/test.py\n\nGreat run overall."
    canned_judge = {
        "score": 4,
        "did_what_was_asked": True,
        "waste": [],
        "prompt_issues": [],
        "summary": "Fine.",
    }

    provider = FakeProvider(
        text=feedback_text,
        structured_output=canned_judge,
        cost=0.01,
        input_tokens=100,
        output_tokens=50,
    )

    # Register the fake provider
    from norn.agents.registry import _registry
    _registry["fake"] = provider

    try:
        state_key = str(tmp_path / "test")
        result = await evaluate_run(
            state_key, 1,
            provider_name="fake",
            judge_model="light",
            feedback_model="standard",
        )

        assert result.score == 7
        assert (rd / "feedback.md").exists()
        assert (rd / "evaluation.json").exists()

        eval_data = json.loads((rd / "evaluation.json").read_text())
        assert eval_data["score"] == 7

        # Cost should be summed from judge + feedback
        assert result.cost_usd >= 0
    finally:
        _registry.pop("fake", None)


# ---------------------------------------------------------------------------
# (f) Invalid header raises and leaves feedback.raw.md
# ---------------------------------------------------------------------------


def test_invalid_score_raises():
    """SCORE: 11 raises EvaluationError."""
    text = "RUN: 1\nSCORE: 11\nPIPELINE: test.py\n\nBad."
    with pytest.raises(EvaluationError):
        validate_feedback_header(text, 1, "test.py")


def test_wrong_run_id_raises():
    """Wrong RUN: raises EvaluationError."""
    text = "RUN: 99\nSCORE: 5\nPIPELINE: test.py\n\nMismatch."
    with pytest.raises(EvaluationError, match="does not match"):
        validate_feedback_header(text, 1, "test.py")


@pytest.mark.asyncio
async def test_invalid_header_saves_raw(tmp_path):
    """Invalid feedback header saves feedback.raw.md and raises."""
    rd = _make_run_dir(tmp_path)
    _make_stage_record(rd)
    _make_metrics(rd)

    bad_feedback = "RUN: 99\nSCORE: 5\nPIPELINE: test.py\n\nWrong run."
    canned_judge = {
        "score": 3,
        "did_what_was_asked": True,
        "waste": [],
        "prompt_issues": [],
        "summary": "ok",
    }
    provider = FakeProvider(
        text=bad_feedback,
        structured_output=canned_judge,
        cost=0.01,
    )

    from norn.agents.registry import _registry
    _registry["fake"] = provider

    try:
        state_key = str(tmp_path / "test")
        with pytest.raises(EvaluationError, match="does not match"):
            await evaluate_run(state_key, 1, provider_name="fake")

        assert (rd / "feedback.raw.md").exists()
        assert not (rd / "feedback.md").exists()
    finally:
        _registry.pop("fake", None)


# ---------------------------------------------------------------------------
# (g) --stages glob filters judge stages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage_glob_filters(tmp_path):
    """--stages 'implement*' judges only matching stages."""
    rd = _make_run_dir(tmp_path)
    _make_stage_record(rd, node_id="implement", seq=1)
    _make_stage_record(rd, node_id="test_suite", seq=2)
    _make_metrics(rd)

    feedback_text = "RUN: 1\nSCORE: 8\nPIPELINE: /path/to/test.py\n\nOk."
    canned_judge = {
        "score": 5,
        "did_what_was_asked": True,
        "waste": [],
        "prompt_issues": [],
        "summary": "Great.",
    }
    provider = FakeProvider(
        text=feedback_text,
        structured_output=canned_judge,
        cost=0.01,
    )

    from norn.agents.registry import _registry
    _registry["fake"] = provider

    try:
        state_key = str(tmp_path / "test")
        result = await evaluate_run(
            state_key, 1,
            provider_name="fake",
            stage_glob="implement*",
        )

        # Only "implement" should be judged, not "test_suite"
        assert "implement" in result.stage_scores
        assert "test_suite" not in result.stage_scores
    finally:
        _registry.pop("fake", None)


# ---------------------------------------------------------------------------
# (h) max_cost_usd exceeded raises after first call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_cost_exceeded(tmp_path):
    """max_cost_usd=0.01 with provider reporting $0.02 raises after first call."""
    rd = _make_run_dir(tmp_path)
    _make_stage_record(rd)
    _make_metrics(rd)

    canned_judge = {
        "score": 3,
        "did_what_was_asked": True,
        "waste": [],
        "prompt_issues": [],
        "summary": "ok",
    }
    provider = FakeProvider(
        structured_output=canned_judge,
        cost=0.02,
    )

    from norn.agents.registry import _registry
    _registry["fake"] = provider

    try:
        state_key = str(tmp_path / "test")
        with pytest.raises(EvaluationError, match="exceeds limit"):
            await evaluate_run(
                state_key, 1,
                provider_name="fake",
                max_cost_usd=0.01,
            )
    finally:
        _registry.pop("fake", None)


# ---------------------------------------------------------------------------
# (i) CLI evaluate and run --evaluate dispatch
# ---------------------------------------------------------------------------


def test_cli_evaluate_subparser(capsys):
    """CLI evaluate subparser is registered and parses args."""
    from norn.cli import main

    # Just verify the parser recognizes the command (it will fail on missing runs)
    with patch("sys.argv", ["norn", "evaluate", "nonexistent_pipeline"]):
        with pytest.raises(SystemExit):
            main()


def test_cli_run_dry_run_evaluate_flag(capsys):
    """run --dry-run --evaluate is accepted (evaluate ignored during dry-run)."""
    from norn.cli import main

    with patch("sys.argv", ["norn", "run", "hello_v1", "--dry-run", "--evaluate"]):
        main()

    captured = capsys.readouterr()
    assert "hello_v1" in captured.out


# ---------------------------------------------------------------------------
# (j) Feedback prompt contains quota line conditionally
# ---------------------------------------------------------------------------


def test_feedback_prompt_with_quota(tmp_path):
    """Feedback prompt contains quota line when metrics.quota is present."""
    rd = _make_run_dir(tmp_path)
    _make_metrics(rd)

    from norn.metrics import QuotaMetrics, RunMetrics, StageMetrics

    sm = StageMetrics(
        name="gen", node_id="gen", attempt=1, kind="agent", status="passed",
        success=True, cost_usd=1.0, cost_share=100.0,
        billed_input_tokens=500, output_tokens=200, cache_hit_ratio=0.0,
        turns=10, tokens_per_turn=70.0, tool_uses=5, tool_errors=0,
        prompt_chars=50, system_prompt_chars=10, unresolved_placeholders=[],
        duration_ms=3000, model="sonnet", model_id=None,
        result_subtype="success", stop_reason=None, nonfatal_error=False,
    )
    quota = QuotaMetrics(before_pct=30.0, after_pct=50.0)
    metrics_with_quota = RunMetrics(
        run_id="1", pipeline="test", provider="fake",
        cost_mode="tracked", success=True, failed_stage=None,
        cost_usd=1.0, billed_input_tokens=500, output_tokens=200,
        cache_read_tokens=0, cache_hit_ratio=0.0, turns=10,
        wall_ms=3000, api_ms=2000, tool_time_ms=1000,
        agent_stage_count=1, command_stage_count=0,
        failed_attempts=0, failed_attempt_cost_usd=0.0, timed_out_attempts=0, retries=0,
        budget_max_cost_usd=None, budget_used_pct=None,
        waiting_input={}, top_cost_stages=[],
        quota=quota, codex_cost_usd_estimate=None, stages=[sm],
    )

    prompt_with = build_feedback_prompt(rd, metrics_with_quota, [], {}, [], None, "test.py")
    assert "Quota:" in prompt_with
    assert "before=30.0%" in prompt_with

    # Without quota
    metrics_no_quota = RunMetrics(
        run_id="1", pipeline="test", provider="fake",
        cost_mode="tracked", success=True, failed_stage=None,
        cost_usd=1.0, billed_input_tokens=500, output_tokens=200,
        cache_read_tokens=0, cache_hit_ratio=0.0, turns=10,
        wall_ms=3000, api_ms=2000, tool_time_ms=1000,
        agent_stage_count=1, command_stage_count=0,
        failed_attempts=0, failed_attempt_cost_usd=0.0, timed_out_attempts=0, retries=0,
        budget_max_cost_usd=None, budget_used_pct=None,
        waiting_input={}, top_cost_stages=[],
        quota=None, codex_cost_usd_estimate=None, stages=[sm],
    )

    prompt_without = build_feedback_prompt(rd, metrics_no_quota, [], {}, [], None, "test.py")
    assert "Quota:" not in prompt_without


# ---------------------------------------------------------------------------
# Validate feedback header — valid cases
# ---------------------------------------------------------------------------


def test_valid_header_returns_score():
    """Valid header returns the score integer."""
    text = "RUN: 42\nSCORE: 7\nPIPELINE: /path/to/test.py\n\nFeedback here."
    score = validate_feedback_header(text, 42, "/path/to/test.py")
    assert score == 7


def test_valid_header_score_10():
    """Score 10 is valid."""
    text = "RUN: 1\nSCORE: 10\nPIPELINE: test.py\n\nPerfect."
    score = validate_feedback_header(text, 1, "test.py")
    assert score == 10


# ---------------------------------------------------------------------------
# complete_text raises ModelLabelError for stale / raw model IDs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_text_raises_for_stale_model_id():
    """complete_text raises ModelLabelError when given a raw model ID instead of an intent label."""
    from norn.agents.complete import complete_text
    from norn.model_labels import ModelLabelError

    with pytest.raises(ModelLabelError):
        await complete_text("hello", provider="claude-code", model="claude-haiku-4-5-20251001")


@pytest.mark.asyncio
async def test_complete_text_raises_for_unknown_label():
    """complete_text raises ModelLabelError for an unrecognised label."""
    from norn.agents.complete import complete_text
    from norn.model_labels import ModelLabelError

    with pytest.raises(ModelLabelError):
        await complete_text("hello", provider="claude-code", model="turbo")


def test_evaluate_run_defaults_are_light_and_standard():
    """evaluate_run signature defaults: judge_model='light', feedback_model='standard'."""
    import inspect
    from norn.evaluate import evaluate_run

    sig = inspect.signature(evaluate_run)
    assert sig.parameters["judge_model"].default == "light"
    assert sig.parameters["feedback_model"].default == "standard"

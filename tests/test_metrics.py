"""Tests for norn.metrics — deterministic metrics and findings."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from norn.metrics import (
    BUDGET_HEADROOM_PCT,
    CONTEXT_BLOAT_TOKENS,
    COST_SHARE_SINGLE_PCT,
    COST_SHARE_TOP3_PCT,
    LOW_CACHE_MIN_TURNS,
    LOW_CACHE_RATIO,
    MODEL_MIX_SURPRISE_OUTPUT_PCT,
    OVERSIZED_PROMPT_CHARS,
    PRIOR_RUNS_LIMIT,
    REGRESSION_FACTOR,
    RETRY_WASTE_COST_PCT,
    SLOW_COMMAND_MS,
    TOOL_ERROR_MIN_USES,
    TOOL_ERROR_RATE_THRESHOLD,
    Finding,
    QuotaMetrics,
    RunMetrics,
    StageMetrics,
    compute_metrics,
    events_digest,
    find_issues,
    load_findings,
    load_metrics,
    load_prior_metrics,
    stage_name_from_node_id,
    unresolved_placeholders,
    write_metrics,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stage_record(
    *,
    node_id: str = "stage-1",
    attempt: int = 1,
    status: str = "passed",
    success: bool = True,
    error: str | None = None,
    usage: dict[str, Any] | None = None,
    details: dict[str, Any] | None = None,
    duration_ms: int = 1000,
) -> dict:
    rec: dict[str, Any] = {
        "node_id": node_id,
        "attempt": attempt,
        "status": status,
        "success": success,
        "duration_ms": duration_ms,
    }
    if error is not None:
        rec["error"] = error
    if usage is not None:
        rec["usage"] = usage
    if details is not None:
        rec["details"] = details
    return rec


def make_run_dir(
    tmp_path: Path,
    *,
    stages: list[dict] | None = None,
    run_json: dict[str, Any] | None = None,
    events: list[dict] | None = None,
    run_id: int = 1,
) -> Path:
    """Create a synthetic run directory with stage records and run.json."""
    rd = tmp_path / "test.runs" / str(run_id)
    rd.mkdir(parents=True)
    stages_dir = rd / "stages"
    stages_dir.mkdir()

    if run_json is None:
        run_json = {
            "pipeline": "test",
            "run_id": run_id,
            "provider": "claude-code",
            "success": True,
            "duration_ms": 5000,
            "totals": {"retries": 0},
        }
    (rd / "run.json").write_text(json.dumps(run_json, indent=2))

    for i, stage in enumerate(stages or []):
        path = stages_dir / f"{i + 1:04d}-{stage.get('node_id', 'stage')}-a{stage.get('attempt', 1)}.json"
        path.write_text(json.dumps(stage, indent=2))

    if events:
        lines = [json.dumps(e) for e in events]
        (rd / "events.jsonl").write_text("\n".join(lines) + "\n")

    return rd


def _agent_usage(**overrides: Any) -> dict:
    base = {
        "input_tokens": 1000,
        "output_tokens": 500,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "total_cost_usd": 0.05,
        "duration_ms": 800,
        "num_turns": 3,
        "model_id": "claude-sonnet-4-6",
        "model": "sonnet",
        "tool_uses": 2,
        "tool_errors": 0,
    }
    base.update(overrides)
    return base


def _make_run_metrics(**overrides: Any) -> RunMetrics:
    """Build a RunMetrics with sensible defaults for testing."""
    defaults = dict(
        run_id="1",
        pipeline="test",
        provider="claude-code",
        cost_mode="tracked",
        success=True,
        failed_stage=None,
        cost_usd=0.10,
        billed_input_tokens=2000,
        output_tokens=500,
        cache_read_tokens=0,
        cache_hit_ratio=0.0,
        turns=3,
        wall_ms=5000,
        api_ms=3000,
        tool_time_ms=2000,
        agent_stage_count=1,
        command_stage_count=0,
        failed_attempts=0,
        failed_attempt_cost_usd=0.0,
        retries=0,
        budget_max_cost_usd=None,
        budget_used_pct=None,
        waiting_input={},
        top_cost_stages=["stage-1"],
        quota=None,
        codex_cost_usd_estimate=None,
        stages=[],
    )
    defaults.update(overrides)
    return RunMetrics(**defaults)


# ---------------------------------------------------------------------------
# Rule tests — one positive and one negative each
# ---------------------------------------------------------------------------


class TestMaxTurnsHit:
    def test_fires_on_error_max_turns(self):
        m = _make_run_metrics(stages=[
            StageMetrics(
                name="gen", node_id="gen", attempt=1, kind="agent", status="failed",
                success=False, cost_usd=0.05, cost_share=100.0, billed_input_tokens=1000,
                output_tokens=500, cache_hit_ratio=0.0, turns=10, tokens_per_turn=150.0,
                tool_uses=2, tool_errors=0, prompt_chars=100, system_prompt_chars=0,
                unresolved_placeholders=[], duration_ms=1000, model="sonnet",
                model_id="claude-sonnet-4-6", result_subtype="error_max_turns",
                stop_reason=None, nonfatal_error=False,
            )
        ])
        findings = find_issues(m)
        assert any(f.rule == "max-turns-hit" for f in findings)

    def test_does_not_fire_on_success(self):
        m = _make_run_metrics(stages=[
            StageMetrics(
                name="gen", node_id="gen", attempt=1, kind="agent", status="passed",
                success=True, cost_usd=0.05, cost_share=100.0, billed_input_tokens=1000,
                output_tokens=500, cache_hit_ratio=0.0, turns=3, tokens_per_turn=500.0,
                tool_uses=2, tool_errors=0, prompt_chars=100, system_prompt_chars=0,
                unresolved_placeholders=[], duration_ms=1000, model="sonnet",
                model_id="claude-sonnet-4-6", result_subtype="success",
                stop_reason=None, nonfatal_error=False,
            )
        ])
        findings = find_issues(m)
        assert not any(f.rule == "max-turns-hit" for f in findings)


class TestAgentError:
    def test_fires_on_bad_subtype(self):
        m = _make_run_metrics(stages=[
            StageMetrics(
                name="gen", node_id="gen", attempt=1, kind="agent", status="failed",
                success=False, cost_usd=0.05, cost_share=100.0, billed_input_tokens=1000,
                output_tokens=500, cache_hit_ratio=0.0, turns=3, tokens_per_turn=500.0,
                tool_uses=2, tool_errors=0, prompt_chars=100, system_prompt_chars=0,
                unresolved_placeholders=[], duration_ms=1000, model="sonnet",
                model_id="claude-sonnet-4-6", result_subtype="error_unknown",
                stop_reason=None, nonfatal_error=False,
            )
        ])
        findings = find_issues(m)
        assert any(f.rule == "agent-error" for f in findings)

    def test_fires_on_failed_without_subtype(self):
        m = _make_run_metrics(stages=[
            StageMetrics(
                name="gen", node_id="gen", attempt=1, kind="agent", status="failed",
                success=False, cost_usd=0.05, cost_share=100.0, billed_input_tokens=1000,
                output_tokens=500, cache_hit_ratio=0.0, turns=3, tokens_per_turn=500.0,
                tool_uses=2, tool_errors=0, prompt_chars=100, system_prompt_chars=0,
                unresolved_placeholders=[], duration_ms=1000, model="sonnet",
                model_id="claude-sonnet-4-6", result_subtype=None,
                stop_reason=None, nonfatal_error=False,
            )
        ])
        findings = find_issues(m)
        assert any(f.rule == "agent-error" for f in findings)

    def test_does_not_fire_on_success(self):
        m = _make_run_metrics(stages=[
            StageMetrics(
                name="gen", node_id="gen", attempt=1, kind="agent", status="passed",
                success=True, cost_usd=0.05, cost_share=100.0, billed_input_tokens=1000,
                output_tokens=500, cache_hit_ratio=0.0, turns=3, tokens_per_turn=500.0,
                tool_uses=2, tool_errors=0, prompt_chars=100, system_prompt_chars=0,
                unresolved_placeholders=[], duration_ms=1000, model="sonnet",
                model_id="claude-sonnet-4-6", result_subtype="success",
                stop_reason=None, nonfatal_error=False,
            )
        ])
        findings = find_issues(m)
        assert not any(f.rule == "agent-error" for f in findings)


class TestUnresolvedPlaceholder:
    def test_fires_on_unresolved(self):
        m = _make_run_metrics(stages=[
            StageMetrics(
                name="gen", node_id="gen", attempt=1, kind="agent", status="passed",
                success=True, cost_usd=0.05, cost_share=100.0, billed_input_tokens=1000,
                output_tokens=500, cache_hit_ratio=0.0, turns=3, tokens_per_turn=500.0,
                tool_uses=0, tool_errors=0, prompt_chars=100, system_prompt_chars=0,
                unresolved_placeholders=["{param.args}"], duration_ms=1000,
                model="sonnet", model_id="claude-sonnet-4-6",
                result_subtype="success", stop_reason=None, nonfatal_error=False,
            )
        ])
        findings = find_issues(m)
        assert any(f.rule == "unresolved-placeholder" for f in findings)

    def test_does_not_fire_when_resolved(self):
        m = _make_run_metrics(stages=[
            StageMetrics(
                name="gen", node_id="gen", attempt=1, kind="agent", status="passed",
                success=True, cost_usd=0.05, cost_share=100.0, billed_input_tokens=1000,
                output_tokens=500, cache_hit_ratio=0.0, turns=3, tokens_per_turn=500.0,
                tool_uses=0, tool_errors=0, prompt_chars=100, system_prompt_chars=0,
                unresolved_placeholders=[], duration_ms=1000,
                model="sonnet", model_id="claude-sonnet-4-6",
                result_subtype="success", stop_reason=None, nonfatal_error=False,
            )
        ])
        findings = find_issues(m)
        assert not any(f.rule == "unresolved-placeholder" for f in findings)


class TestSilentError:
    def test_fires_on_success_with_error(self):
        m = _make_run_metrics(stages=[
            StageMetrics(
                name="gen", node_id="gen", attempt=1, kind="agent", status="passed",
                success=True, cost_usd=0.05, cost_share=100.0, billed_input_tokens=1000,
                output_tokens=500, cache_hit_ratio=0.0, turns=3, tokens_per_turn=500.0,
                tool_uses=0, tool_errors=0, prompt_chars=100, system_prompt_chars=0,
                unresolved_placeholders=[], duration_ms=1000,
                model="sonnet", model_id="claude-sonnet-4-6",
                result_subtype="success", stop_reason=None, nonfatal_error=True,
            )
        ])
        findings = find_issues(m)
        assert any(f.rule == "silent-error" for f in findings)

    def test_does_not_fire_on_clean_success(self):
        m = _make_run_metrics(stages=[
            StageMetrics(
                name="gen", node_id="gen", attempt=1, kind="agent", status="passed",
                success=True, cost_usd=0.05, cost_share=100.0, billed_input_tokens=1000,
                output_tokens=500, cache_hit_ratio=0.0, turns=3, tokens_per_turn=500.0,
                tool_uses=0, tool_errors=0, prompt_chars=100, system_prompt_chars=0,
                unresolved_placeholders=[], duration_ms=1000,
                model="sonnet", model_id="claude-sonnet-4-6",
                result_subtype="success", stop_reason=None, nonfatal_error=False,
            )
        ])
        findings = find_issues(m)
        assert not any(f.rule == "silent-error" for f in findings)


class TestRetryWaste:
    def test_fires_on_high_waste(self):
        # failed_attempt_cost_usd is 20% of total
        m = _make_run_metrics(
            cost_usd=1.0,
            failed_attempt_cost_usd=0.20,
        )
        findings = find_issues(m)
        assert any(f.rule == "retry-waste" for f in findings)

    def test_does_not_fire_on_low_waste(self):
        m = _make_run_metrics(
            cost_usd=1.0,
            failed_attempt_cost_usd=0.05,
        )
        findings = find_issues(m)
        assert not any(f.rule == "retry-waste" for f in findings)


class TestToolErrorRate:
    def test_fires_on_high_error_rate(self):
        m = _make_run_metrics(stages=[
            StageMetrics(
                name="gen", node_id="gen", attempt=1, kind="agent", status="passed",
                success=True, cost_usd=0.05, cost_share=100.0, billed_input_tokens=1000,
                output_tokens=500, cache_hit_ratio=0.0, turns=3, tokens_per_turn=500.0,
                tool_uses=10, tool_errors=5, prompt_chars=100, system_prompt_chars=0,
                unresolved_placeholders=[], duration_ms=1000,
                model="sonnet", model_id="claude-sonnet-4-6",
                result_subtype="success", stop_reason=None, nonfatal_error=False,
            )
        ])
        findings = find_issues(m)
        assert any(f.rule == "tool-error-rate" for f in findings)

    def test_does_not_fire_on_low_rate(self):
        m = _make_run_metrics(stages=[
            StageMetrics(
                name="gen", node_id="gen", attempt=1, kind="agent", status="passed",
                success=True, cost_usd=0.05, cost_share=100.0, billed_input_tokens=1000,
                output_tokens=500, cache_hit_ratio=0.0, turns=3, tokens_per_turn=500.0,
                tool_uses=10, tool_errors=1, prompt_chars=100, system_prompt_chars=0,
                unresolved_placeholders=[], duration_ms=1000,
                model="sonnet", model_id="claude-sonnet-4-6",
                result_subtype="success", stop_reason=None, nonfatal_error=False,
            )
        ])
        findings = find_issues(m)
        assert not any(f.rule == "tool-error-rate" for f in findings)


class TestLowCacheRatio:
    def test_fires_on_low_cache(self):
        m = _make_run_metrics(stages=[
            StageMetrics(
                name="gen", node_id="gen", attempt=1, kind="agent", status="passed",
                success=True, cost_usd=0.05, cost_share=100.0, billed_input_tokens=1000,
                output_tokens=500, cache_hit_ratio=0.1, turns=10, tokens_per_turn=150.0,
                tool_uses=0, tool_errors=0, prompt_chars=100, system_prompt_chars=0,
                unresolved_placeholders=[], duration_ms=1000,
                model="sonnet", model_id="claude-sonnet-4-6",
                result_subtype="success", stop_reason=None, nonfatal_error=False,
            )
        ])
        findings = find_issues(m)
        assert any(f.rule == "low-cache-ratio" for f in findings)

    def test_does_not_fire_on_high_cache(self):
        m = _make_run_metrics(stages=[
            StageMetrics(
                name="gen", node_id="gen", attempt=1, kind="agent", status="passed",
                success=True, cost_usd=0.05, cost_share=100.0, billed_input_tokens=1000,
                output_tokens=500, cache_hit_ratio=0.8, turns=10, tokens_per_turn=150.0,
                tool_uses=0, tool_errors=0, prompt_chars=100, system_prompt_chars=0,
                unresolved_placeholders=[], duration_ms=1000,
                model="sonnet", model_id="claude-sonnet-4-6",
                result_subtype="success", stop_reason=None, nonfatal_error=False,
            )
        ])
        findings = find_issues(m)
        assert not any(f.rule == "low-cache-ratio" for f in findings)


class TestOversizedPrompt:
    def test_fires_on_large_prompt(self):
        m = _make_run_metrics(stages=[
            StageMetrics(
                name="gen", node_id="gen", attempt=1, kind="agent", status="passed",
                success=True, cost_usd=0.05, cost_share=100.0, billed_input_tokens=1000,
                output_tokens=500, cache_hit_ratio=0.0, turns=3, tokens_per_turn=500.0,
                tool_uses=0, tool_errors=0,
                prompt_chars=150_000, system_prompt_chars=60_000,
                unresolved_placeholders=[], duration_ms=1000,
                model="sonnet", model_id="claude-sonnet-4-6",
                result_subtype="success", stop_reason=None, nonfatal_error=False,
            )
        ])
        findings = find_issues(m)
        assert any(f.rule == "oversized-prompt" for f in findings)

    def test_does_not_fire_on_small_prompt(self):
        m = _make_run_metrics(stages=[
            StageMetrics(
                name="gen", node_id="gen", attempt=1, kind="agent", status="passed",
                success=True, cost_usd=0.05, cost_share=100.0, billed_input_tokens=1000,
                output_tokens=500, cache_hit_ratio=0.0, turns=3, tokens_per_turn=500.0,
                tool_uses=0, tool_errors=0, prompt_chars=1000, system_prompt_chars=500,
                unresolved_placeholders=[], duration_ms=1000,
                model="sonnet", model_id="claude-sonnet-4-6",
                result_subtype="success", stop_reason=None, nonfatal_error=False,
            )
        ])
        findings = find_issues(m)
        assert not any(f.rule == "oversized-prompt" for f in findings)


class TestSlowCommand:
    def test_fires_on_slow_command(self):
        m = _make_run_metrics(stages=[
            StageMetrics(
                name="build", node_id="build", attempt=1, kind="command", status="passed",
                success=True, cost_usd=0.0, cost_share=0.0, billed_input_tokens=0,
                output_tokens=0, cache_hit_ratio=0.0, turns=0, tokens_per_turn=0.0,
                tool_uses=0, tool_errors=0, prompt_chars=0, system_prompt_chars=0,
                unresolved_placeholders=[], duration_ms=700_000,
                model=None, model_id=None, result_subtype=None,
                stop_reason=None, nonfatal_error=False,
            )
        ])
        findings = find_issues(m)
        assert any(f.rule == "slow-command" for f in findings)

    def test_does_not_fire_on_fast_command(self):
        m = _make_run_metrics(stages=[
            StageMetrics(
                name="build", node_id="build", attempt=1, kind="command", status="passed",
                success=True, cost_usd=0.0, cost_share=0.0, billed_input_tokens=0,
                output_tokens=0, cache_hit_ratio=0.0, turns=0, tokens_per_turn=0.0,
                tool_uses=0, tool_errors=0, prompt_chars=0, system_prompt_chars=0,
                unresolved_placeholders=[], duration_ms=5000,
                model=None, model_id=None, result_subtype=None,
                stop_reason=None, nonfatal_error=False,
            )
        ])
        findings = find_issues(m)
        assert not any(f.rule == "slow-command" for f in findings)


class TestBudgetHeadroom:
    def test_fires_on_high_usage(self):
        m = _make_run_metrics(
            budget_max_cost_usd=1.0,
            budget_used_pct=90.0,
        )
        findings = find_issues(m)
        assert any(f.rule == "budget-headroom" for f in findings)

    def test_does_not_fire_on_low_usage(self):
        m = _make_run_metrics(
            budget_max_cost_usd=1.0,
            budget_used_pct=50.0,
        )
        findings = find_issues(m)
        assert not any(f.rule == "budget-headroom" for f in findings)


class TestCostShare:
    def test_fires_on_single_stage_domination(self):
        m = _make_run_metrics(
            cost_usd=1.0,
            stages=[
                StageMetrics(
                    name="big", node_id="big", attempt=1, kind="agent", status="passed",
                    success=True, cost_usd=0.80, cost_share=80.0, billed_input_tokens=1000,
                    output_tokens=500, cache_hit_ratio=0.0, turns=3, tokens_per_turn=500.0,
                    tool_uses=0, tool_errors=0, prompt_chars=100, system_prompt_chars=0,
                    unresolved_placeholders=[], duration_ms=1000,
                    model="sonnet", model_id="claude-sonnet-4-6",
                    result_subtype="success", stop_reason=None, nonfatal_error=False,
                ),
                StageMetrics(
                    name="small", node_id="small", attempt=1, kind="agent", status="passed",
                    success=True, cost_usd=0.20, cost_share=20.0, billed_input_tokens=500,
                    output_tokens=200, cache_hit_ratio=0.0, turns=1, tokens_per_turn=700.0,
                    tool_uses=0, tool_errors=0, prompt_chars=50, system_prompt_chars=0,
                    unresolved_placeholders=[], duration_ms=500,
                    model="sonnet", model_id="claude-sonnet-4-6",
                    result_subtype="success", stop_reason=None, nonfatal_error=False,
                ),
            ],
        )
        findings = find_issues(m)
        assert any(f.rule == "cost-share" and f.stage == "big" for f in findings)

    def test_does_not_fire_on_even_distribution(self):
        stages = []
        for i in range(5):
            stages.append(StageMetrics(
                name=f"s{i}", node_id=f"s{i}", attempt=1, kind="agent", status="passed",
                success=True, cost_usd=0.02, cost_share=10.0, billed_input_tokens=200,
                output_tokens=100, cache_hit_ratio=0.0, turns=1, tokens_per_turn=300.0,
                tool_uses=0, tool_errors=0, prompt_chars=100, system_prompt_chars=0,
                unresolved_placeholders=[], duration_ms=500,
                model="sonnet", model_id="claude-sonnet-4-6",
                result_subtype="success", stop_reason=None, nonfatal_error=False,
            ))
        m = _make_run_metrics(cost_usd=0.10, stages=stages)
        findings = find_issues(m)
        # No single-stage cost-share finding
        assert not any(f.rule == "cost-share" and f.stage is not None for f in findings)


class TestRegressionVsPrior:
    def test_fires_on_cost_regression(self):
        prior = [
            _make_run_metrics(run_id=str(i), cost_usd=0.10, wall_ms=5000)
            for i in range(3)
        ]
        m = _make_run_metrics(cost_usd=0.20)  # 2x > 1.5x median
        findings = find_issues(m, prior)
        assert any(f.rule == "regression-vs-prior" and "cost" in f.evidence for f in findings)

    def test_fires_on_wall_regression(self):
        prior = [
            _make_run_metrics(run_id=str(i), cost_usd=0.10, wall_ms=5000)
            for i in range(3)
        ]
        m = _make_run_metrics(wall_ms=10000)  # 2x > 1.5x median
        findings = find_issues(m, prior)
        assert any(f.rule == "regression-vs-prior" and "wall" in f.evidence for f in findings)

    def test_does_not_fire_on_normal_run(self):
        prior = [
            _make_run_metrics(run_id=str(i), cost_usd=0.10, wall_ms=5000)
            for i in range(3)
        ]
        m = _make_run_metrics(cost_usd=0.10, wall_ms=5000)
        findings = find_issues(m, prior)
        assert not any(f.rule == "regression-vs-prior" for f in findings)


class TestQuotaPressure:
    def test_fires_on_high_quota_usage(self):
        # after_pct is % remaining; 15% remaining means 85% consumed → fires rule
        m = _make_run_metrics(
            quota=QuotaMetrics(
                before_pct=30.0, after_pct=15.0, delta_pct=-15.0,
                resets_at="2026-09-10T00:00:00Z", will_last_to_reset=True,
            ),
        )
        findings = find_issues(m)
        assert any(f.rule == "quota-pressure" for f in findings)

    def test_fires_on_will_not_last(self):
        # after_pct=40 means 40% remaining (not below the pressure threshold on its own),
        # but will_last_to_reset=False triggers the second quota-pressure branch.
        m = _make_run_metrics(
            quota=QuotaMetrics(
                before_pct=50.0, after_pct=40.0, delta_pct=-10.0,
                resets_at="2026-09-10T00:00:00Z", will_last_to_reset=False,
            ),
        )
        findings = find_issues(m)
        assert any(f.rule == "quota-pressure" for f in findings)

    def test_does_not_fire_on_low_usage(self):
        # after_pct is % remaining; 85% remaining means only 15% consumed — no pressure
        m = _make_run_metrics(
            quota=QuotaMetrics(
                before_pct=90.0, after_pct=85.0, delta_pct=-5.0,
                resets_at="2026-09-10T00:00:00Z", will_last_to_reset=True,
            ),
        )
        findings = find_issues(m)
        assert not any(f.rule == "quota-pressure" for f in findings)

        # 40% remaining (60% consumed) is also below the pressure threshold (< 80% consumed)
        m2 = _make_run_metrics(
            quota=QuotaMetrics(
                before_pct=50.0, after_pct=40.0, delta_pct=-10.0,
                resets_at="2026-09-10T00:00:00Z", will_last_to_reset=True,
            ),
        )
        findings2 = find_issues(m2)
        assert not any(f.rule == "quota-pressure" for f in findings2)


class TestCostNotTracked:
    def test_emits_info_when_cost_mode_not_tracked(self):
        m = _make_run_metrics(cost_mode="zero_unknown")
        findings = find_issues(m)
        assert any(f.rule == "cost-not-tracked" for f in findings)

    def test_no_info_when_tracked(self):
        m = _make_run_metrics(cost_mode="tracked")
        findings = find_issues(m)
        assert not any(f.rule == "cost-not-tracked" for f in findings)


# ---------------------------------------------------------------------------
# events_digest test
# ---------------------------------------------------------------------------


class TestEventsDigest:
    def test_counts_events(self, tmp_path):
        rd = make_run_dir(tmp_path, events=[
            {"type": "WaitingInput", "kind": "budget"},
            {"type": "WaitingInput", "kind": "budget"},
            {"type": "WaitingInput", "kind": "agent"},
            {"type": "LoopAttempt", "name": "loop1", "attempt": 1},
            {"type": "LoopAttempt", "name": "loop1", "attempt": 2},
            {"type": "StageRetrying", "next_attempt": 2},
            {"type": "LoopExhausted", "loop_id": "loop1"},
            {"type": "StageFinished", "name": "gen"},  # ignored
        ])
        digest = events_digest(rd)
        assert digest["waiting_input"] == {"budget": 2, "agent": 1}
        assert digest["loop_attempts"] == 2
        assert digest["stage_retries"] == 1
        assert digest["loop_exhausted"] == 1

    def test_empty_when_no_events(self, tmp_path):
        rd = make_run_dir(tmp_path)
        digest = events_digest(rd)
        assert digest["waiting_input"] == {}
        assert digest["loop_attempts"] == 0


# ---------------------------------------------------------------------------
# compute_metrics integration
# ---------------------------------------------------------------------------


class TestComputeMetrics:
    def test_basic_agent_stage(self, tmp_path):
        rd = make_run_dir(tmp_path, stages=[
            _make_stage_record(
                node_id="gen",
                usage=_agent_usage(
                    input_tokens=1000,
                    output_tokens=500,
                    cache_read_input_tokens=200,
                    total_cost_usd=0.05,
                    num_turns=3,
                ),
                details={"prompt": "Do something", "system_prompt": "You are a bot"},
            ),
        ])
        m = compute_metrics(rd)
        assert m.pipeline == "test"
        assert m.agent_stage_count == 1
        assert m.cost_usd == pytest.approx(0.05)
        assert m.turns == 3
        assert len(m.stages) == 1
        assert m.stages[0].kind == "agent"

    def test_command_stage(self, tmp_path):
        rd = make_run_dir(tmp_path, stages=[
            _make_stage_record(
                node_id="build",
                details={"cmd": "make build", "timeout": 60},
                duration_ms=2000,
            ),
        ])
        m = compute_metrics(rd)
        assert m.command_stage_count == 1
        assert m.stages[0].kind == "command"

    def test_failed_attempts_tracked(self, tmp_path):
        rd = make_run_dir(tmp_path, stages=[
            _make_stage_record(
                node_id="gen", attempt=1, success=False,
                usage=_agent_usage(total_cost_usd=0.03),
            ),
            _make_stage_record(
                node_id="gen", attempt=2, success=True,
                usage=_agent_usage(total_cost_usd=0.05),
            ),
        ])
        m = compute_metrics(rd)
        assert m.failed_attempts == 1
        assert m.failed_attempt_cost_usd == pytest.approx(0.03)


# ---------------------------------------------------------------------------
# QuotaMetrics from run.json
# ---------------------------------------------------------------------------


class TestQuotaMetrics:
    def test_quota_from_run_json(self, tmp_path):
        rd = make_run_dir(tmp_path, run_json={
            "pipeline": "test",
            "run_id": 1,
            "provider": "claude-code",
            "success": True,
            "duration_ms": 5000,
            "totals": {"retries": 0},
            "quota_before": {
                "remaining_pct": 80.0,
                "reset_at": "2026-09-10T00:00:00Z",
                "pace": {"will_last_to_reset": True},
            },
            "quota_after": {
                "remaining_pct": 70.0,
                "reset_at": "2026-09-10T00:00:00Z",
                "pace": {"will_last_to_reset": True},
            },
        })
        m = compute_metrics(rd)
        assert m.quota is not None
        assert m.quota.before_pct == 80.0
        assert m.quota.after_pct == 70.0
        assert m.quota.delta_pct == pytest.approx(-10.0)
        assert m.quota.will_last_to_reset is True

    def test_no_quota_when_absent(self, tmp_path):
        rd = make_run_dir(tmp_path)
        m = compute_metrics(rd)
        assert m.quota is None


# ---------------------------------------------------------------------------
# Codex cost estimate
# ---------------------------------------------------------------------------


class TestCodexCostEstimate:
    def test_codex_stage_cost(self, tmp_path):
        rd = make_run_dir(tmp_path, stages=[
            _make_stage_record(
                node_id="codex-run",
                details={"cmd": "codex --model o3 apply changes"},
                usage=_agent_usage(total_cost_usd=1.50),
            ),
        ])
        m = compute_metrics(rd)
        assert m.codex_cost_usd_estimate == pytest.approx(1.50)

    def test_no_codex_stage(self, tmp_path):
        rd = make_run_dir(tmp_path, stages=[
            _make_stage_record(node_id="gen", usage=_agent_usage()),
        ])
        m = compute_metrics(rd)
        assert m.codex_cost_usd_estimate is None


# ---------------------------------------------------------------------------
# write_metrics / load_metrics round trip
# ---------------------------------------------------------------------------


class TestSerialisation:
    def test_round_trip(self, tmp_path):
        rd = make_run_dir(tmp_path, stages=[
            _make_stage_record(node_id="gen", usage=_agent_usage()),
        ])
        m = compute_metrics(rd)
        findings = [
            Finding(
                rule="test-rule", severity="warn", stage="gen",
                evidence="test", suggestion="fix it",
            ),
        ]
        write_metrics(rd, m, findings)
        loaded_m = load_metrics(rd)
        loaded_f = load_findings(rd)
        assert loaded_m is not None
        assert loaded_m.pipeline == m.pipeline
        assert loaded_m.cost_usd == pytest.approx(m.cost_usd)
        assert len(loaded_m.stages) == len(m.stages)
        assert len(loaded_f) == 1
        assert loaded_f[0].rule == "test-rule"


# ---------------------------------------------------------------------------
# load_prior_metrics with multiple prior dirs
# ---------------------------------------------------------------------------


class TestLoadPriorMetrics:
    def test_loads_prior_runs(self, tmp_path):
        runs = tmp_path / "test.runs"
        runs.mkdir()
        for rid in [1, 2, 3, 4]:
            rd = make_run_dir(tmp_path, run_id=rid, stages=[
                _make_stage_record(node_id="gen", usage=_agent_usage(total_cost_usd=0.01 * rid)),
            ])
            m = compute_metrics(rd)
            write_metrics(rd, m, [])

        priors = load_prior_metrics(runs, before_run_id=4, limit=5)
        assert len(priors) == 3
        # Most recent first
        assert priors[0].run_id == "3"

    def test_empty_when_no_prior(self, tmp_path):
        runs = tmp_path / "test.runs"
        runs.mkdir()
        priors = load_prior_metrics(runs, before_run_id=1)
        assert priors == []


# ---------------------------------------------------------------------------
# unresolved_placeholders
# ---------------------------------------------------------------------------


class TestUnresolvedPlaceholdersFunc:
    def test_detects_placeholders(self):
        text = "Hello {param.args}, meet {read manifest 1.output}"
        result = unresolved_placeholders(text, {"read manifest 1"})
        assert "{param.args}" in result
        assert "{read manifest 1.output}" in result

    def test_no_false_positives(self):
        assert unresolved_placeholders("just normal text", set()) == []
        assert unresolved_placeholders(None, set()) == []
        assert unresolved_placeholders("", set()) == []

    def test_code_that_looks_like_a_placeholder_is_not_one(self):
        """A plan step quoting a dict literal is prose, not a placeholder.

        This exact text made review round 1 report a HIGH finding.
        """
        text = 'details={"cmd": self.cmd, "timeout": self.timeout} on both returns.'
        assert unresolved_placeholders(text, {"review 1"}) == []

    def test_stage_that_did_not_run_is_not_reported(self):
        """The prefix must name a stage of this run; an unknown one is prose."""
        assert unresolved_placeholders("{closeout step-09.output}", {"review 1"}) == []


class TestStageNameFromNodeId:
    def test_top_level_stage(self):
        assert stage_name_from_node_id("stage:review 1") == "review 1"

    def test_stage_inside_a_loop(self):
        node_id = "loop:loop aggregate/stage:aggregate compress"
        assert stage_name_from_node_id(node_id) == "aggregate compress"


# ---------------------------------------------------------------------------
# context-bloat rule
# ---------------------------------------------------------------------------


def _make_stage_sm(**overrides: Any) -> StageMetrics:
    base = dict(
        name="gen", node_id="gen", attempt=1, kind="agent", status="passed",
        success=True, cost_usd=0.05, cost_share=100.0, billed_input_tokens=1000,
        output_tokens=500, cache_hit_ratio=0.0, turns=3, tokens_per_turn=500.0,
        tool_uses=2, tool_errors=0, prompt_chars=100, system_prompt_chars=0,
        unresolved_placeholders=[], duration_ms=1000, model="sonnet",
        model_id="claude-sonnet-4-6", result_subtype="success",
        stop_reason=None, nonfatal_error=False,
    )
    base.update(overrides)
    return StageMetrics(**base)


class TestContextBloat:
    def test_fires_above_threshold(self):
        m = _make_run_metrics(stages=[
            _make_stage_sm(context_peak_tokens=CONTEXT_BLOAT_TOKENS + 1),
        ])
        findings = find_issues(m)
        assert any(f.rule == "context-bloat" for f in findings)

    def test_does_not_fire_at_or_below_threshold(self):
        m = _make_run_metrics(stages=[
            _make_stage_sm(context_peak_tokens=CONTEXT_BLOAT_TOKENS),
        ])
        findings = find_issues(m)
        assert not any(f.rule == "context-bloat" for f in findings)

    def test_does_not_fire_when_none(self):
        m = _make_run_metrics(stages=[
            _make_stage_sm(context_peak_tokens=None),
        ])
        findings = find_issues(m)
        assert not any(f.rule == "context-bloat" for f in findings)


# ---------------------------------------------------------------------------
# model-mix-surprise rule
# ---------------------------------------------------------------------------


class TestModelMixSurprise:
    def test_fires_when_other_model_exceeds_30pct(self):
        m = _make_run_metrics(stages=[
            _make_stage_sm(
                model_id="claude-sonnet-4-6",
                model_mix={
                    "claude-sonnet-4-6": {"calls": 8, "input": 8000, "cache_read": 0, "cache_creation": 0, "output": 600},
                    "claude-haiku-4-5": {"calls": 2, "input": 2000, "cache_read": 0, "cache_creation": 0, "output": 400},
                },
            ),
        ])
        findings = find_issues(m)
        assert any(f.rule == "model-mix-surprise" for f in findings)

    def test_does_not_fire_when_below_30pct(self):
        m = _make_run_metrics(stages=[
            _make_stage_sm(
                model_id="claude-sonnet-4-6",
                model_mix={
                    "claude-sonnet-4-6": {"calls": 9, "input": 9000, "cache_read": 0, "cache_creation": 0, "output": 900},
                    "claude-haiku-4-5": {"calls": 1, "input": 1000, "cache_read": 0, "cache_creation": 0, "output": 50},
                },
            ),
        ])
        findings = find_issues(m)
        assert not any(f.rule == "model-mix-surprise" for f in findings)

    def test_does_not_fire_with_single_model(self):
        m = _make_run_metrics(stages=[
            _make_stage_sm(
                model_id="claude-sonnet-4-6",
                model_mix={
                    "claude-sonnet-4-6": {"calls": 10, "input": 10000, "cache_read": 0, "cache_creation": 0, "output": 1000},
                },
            ),
        ])
        findings = find_issues(m)
        assert not any(f.rule == "model-mix-surprise" for f in findings)

    def test_does_not_fire_when_model_mix_none(self):
        m = _make_run_metrics(stages=[
            _make_stage_sm(model_mix=None),
        ])
        findings = find_issues(m)
        assert not any(f.rule == "model-mix-surprise" for f in findings)


# ---------------------------------------------------------------------------
# Integration: compute_metrics writes model_mix and context_peak_tokens
# ---------------------------------------------------------------------------


class TestTranscriptIntegration:
    def test_context_peak_and_model_mix_from_transcript(self, tmp_path, monkeypatch):
        """compute_metrics reads transcript and writes model_mix + context_peak_tokens."""
        import json
        from pathlib import Path
        from norn.metrics import compute_metrics

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        cwd = "/integration/test"
        session_id = "sess-integ"
        slug = cwd.replace("/", "-")

        # Write transcript
        transcript_dir = tmp_path / ".claude" / "projects" / slug
        transcript_dir.mkdir(parents=True)
        lines = [
            {
                "type": "assistant",
                "timestamp": "2026-09-05T10:00:10.000Z",
                "requestId": "req_1",
                "message": {
                    "id": "msg_1",
                    "model": "claude-sonnet-4-6",
                    "usage": {"input_tokens": 5000, "cache_read_input_tokens": 0,
                               "cache_creation_input_tokens": 0, "output_tokens": 300},
                },
            },
            {
                "type": "assistant",
                "timestamp": "2026-09-05T10:00:20.000Z",
                "requestId": "req_2",
                "message": {
                    "id": "msg_2",
                    "model": "claude-sonnet-4-6",
                    "usage": {"input_tokens": 8000, "cache_read_input_tokens": 2000,
                               "cache_creation_input_tokens": 0, "output_tokens": 500},
                },
            },
        ]
        (transcript_dir / f"{session_id}.jsonl").write_text(
            "\n".join(json.dumps(l) for l in lines)
        )

        # Build run dir manually to avoid colon issues in filenames from make_run_dir
        rd = tmp_path / "integ.runs" / "1"
        stages_dir = rd / "stages"
        stages_dir.mkdir(parents=True)
        (rd / "run.json").write_text(json.dumps({
            "pipeline": "test",
            "run_id": 1,
            "provider": "claude-code",
            "success": True,
            "duration_ms": 5000,
            "totals": {"retries": 0},
            "cwd": cwd,
        }))
        (stages_dir / "0001-gen-a1.json").write_text(json.dumps({
            "node_id": "gen",
            "attempt": 1,
            "status": "passed",
            "success": True,
            "started_at": "2026-09-05T10:00:00.000Z",
            "finished_at": "2026-09-05T10:01:00.000Z",
            "duration_ms": 1000,
            "usage": {
                "input_tokens": 13000,
                "output_tokens": 800,
                "cache_read_input_tokens": 2000,
                "cache_creation_input_tokens": 0,
                "total_cost_usd": 0.10,
                "duration_ms": 800,
                "num_turns": 2,
                "model_id": "claude-sonnet-4-6",
                "model": "sonnet",
                "result_subtype": "success",
                "stop_reason": None,
                "tool_uses": 0,
                "tool_errors": 0,
                "session_id": session_id,
            },
        }))

        m = compute_metrics(rd)
        assert len(m.stages) == 1
        sm = m.stages[0]
        assert sm.model_mix is not None
        assert "claude-sonnet-4-6" in sm.model_mix
        # context_peak = max(5000+0, 8000+2000) = 10000
        assert sm.context_peak_tokens == 10000
        assert sm.context_growth == [5000, 10000]

    def test_missing_transcript_sets_field(self, tmp_path, monkeypatch):
        """When transcript file is absent, stage.transcript == 'missing'."""
        import json
        from pathlib import Path
        from norn.metrics import compute_metrics

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        rd = make_run_dir(tmp_path, run_json={
            "pipeline": "test",
            "run_id": 1,
            "provider": "claude-code",
            "success": True,
            "duration_ms": 5000,
            "totals": {"retries": 0},
            "cwd": "/some/project",
        }, stages=[
            _make_stage_record(
                node_id="gen",
                usage=_agent_usage(session_id="sess-absent"),
            ),
        ])
        m = compute_metrics(rd)
        assert m.stages[0].transcript == "missing"

    def test_no_cwd_skips_transcripts(self, tmp_path):
        """When run.json has no cwd, transcript integration is silently skipped."""
        from norn.metrics import compute_metrics
        rd = make_run_dir(tmp_path, stages=[
            _make_stage_record(node_id="gen", usage=_agent_usage()),
        ])
        # run_json has no cwd
        m = compute_metrics(rd)
        assert m.stages[0].model_mix is None
        assert m.stages[0].context_peak_tokens is None

"""Tests for norn.transcripts — path slugging, reading, attribution, and rules."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from norn.transcripts import (
    Call,
    StageTranscriptData,
    attribute,
    read_assistant_calls,
    transcript_path,
)
from norn.model_labels import resolve_label
from norn.metrics import (
    CONTEXT_BLOAT_WINDOW_FRACTION,
    MODEL_MIX_SURPRISE_OUTPUT_PCT,
    Finding,
    StageMetrics,
    find_issues,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _assistant_line(
    message_id: str,
    request_id: str,
    model: str,
    timestamp: str,
    input_tokens: int = 1000,
    cache_read: int = 0,
    cache_creation: int = 0,
    output_tokens: int = 500,
) -> dict[str, Any]:
    return {
        "type": "assistant",
        "timestamp": timestamp,
        "requestId": request_id,
        "message": {
            "id": message_id,
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
                "output_tokens": output_tokens,
            },
        },
    }


def _write_transcript(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(l) for l in lines))


@pytest.fixture
def transcript_fixture(tmp_path):
    """Three assistant lines: two models, one duplicate (msg_001 appears twice)."""
    lines = [
        _assistant_line("msg_001", "req_001", "claude-sonnet-4-6", "2026-09-05T10:00:01.000Z",
                        input_tokens=1000, output_tokens=500),
        _assistant_line("msg_002", "req_002", "claude-haiku-4-5", "2026-09-05T10:01:01.000Z",
                        input_tokens=2000, output_tokens=100),
        # Duplicate of msg_001 with identical (message_id, requestId)
        _assistant_line("msg_001", "req_001", "claude-sonnet-4-6", "2026-09-05T10:00:01.000Z",
                        input_tokens=1000, output_tokens=500),
    ]
    path = tmp_path / "sess1.jsonl"
    _write_transcript(path, lines)
    return path


def _make_stage_sm(**kwargs: Any) -> StageMetrics:
    """Build a StageMetrics with sensible defaults for rule testing."""
    defaults = dict(
        name="gen",
        node_id="gen",
        attempt=1,
        kind="agent",
        status="passed",
        success=True,
        cost_usd=0.05,
        cost_share=100.0,
        billed_input_tokens=1000,
        output_tokens=500,
        cache_hit_ratio=0.0,
        turns=3,
        tokens_per_turn=500.0,
        tool_uses=2,
        tool_errors=0,
        prompt_chars=100,
        system_prompt_chars=0,
        unresolved_placeholders=[],
        duration_ms=1000,
        model="standard",
        model_id="claude-sonnet-4-6",
        result_subtype="success",
        stop_reason=None,
        nonfatal_error=False,
    )
    defaults.update(kwargs)
    return StageMetrics(**defaults)


def _run_metrics_with_stage(sm: StageMetrics) -> Any:
    """Wrap a single StageMetrics in a minimal RunMetrics-like object for find_issues."""
    from norn.metrics import RunMetrics
    return RunMetrics(
        run_id="1",
        pipeline="test",
        provider="claude-code",
        cost_mode="tracked",
        success=True,
        failed_stage=None,
        cost_usd=sm.cost_usd,
        billed_input_tokens=sm.billed_input_tokens,
        output_tokens=sm.output_tokens,
        cache_read_tokens=0,
        cache_hit_ratio=0.0,
        turns=sm.turns,
        wall_ms=5000,
        api_ms=3000,
        tool_time_ms=2000,
        agent_stage_count=1,
        command_stage_count=0,
        failed_attempts=0,
        failed_attempt_cost_usd=0.0,
        timed_out_attempts=0,
        retries=0,
        budget_max_cost_usd=None,
        budget_used_pct=None,
        waiting_input={},
        top_cost_stages=[sm.name],
        quota=None,
        codex_cost_usd_estimate=None,
        stages=[sm],
    )


# ---------------------------------------------------------------------------
# 1. Path slug construction
# ---------------------------------------------------------------------------


class TestTranscriptPath:
    def test_slug_replaces_slashes(self):
        p = transcript_path("/Users/foo/bar", "abc123")
        assert p.name == "abc123.jsonl"
        assert p.parent.name == "-Users-foo-bar"
        assert p.parent.parent.name == "projects"

    def test_known_slug(self):
        """Verify the slug against the known norn project path (dot preserved)."""
        p = transcript_path("/Users/erik/projects/erik.romson/norn", "sess")
        assert "-Users-erik-projects-erik.romson-norn" in str(p)

    def test_home_based(self, tmp_path, monkeypatch):
        """transcript_path uses Path.home()."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        p = transcript_path("/my/project", "s1")
        assert str(p).startswith(str(tmp_path))
        assert p == tmp_path / ".claude" / "projects" / "-my-project" / "s1.jsonl"


# ---------------------------------------------------------------------------
# 2. Deduplication
# ---------------------------------------------------------------------------


class TestReadAssistantCalls:
    def test_deduplicates_by_message_id_and_request_id(self, transcript_fixture):
        calls = read_assistant_calls(transcript_fixture)
        assert len(calls) == 2

    def test_returns_correct_models(self, transcript_fixture):
        calls = read_assistant_calls(transcript_fixture)
        models = {c.model for c in calls}
        assert "claude-sonnet-4-6" in models
        assert "claude-haiku-4-5" in models

    def test_skips_non_assistant_lines(self, tmp_path):
        lines = [
            {"type": "human", "timestamp": "2026-09-05T10:00:00Z", "message": {"content": "hi"}},
            _assistant_line("msg_001", "req_001", "claude-sonnet-4-6", "2026-09-05T10:00:01.000Z"),
        ]
        path = tmp_path / "sess.jsonl"
        _write_transcript(path, lines)
        calls = read_assistant_calls(path)
        assert len(calls) == 1

    def test_sets_session_id_on_calls(self, transcript_fixture):
        calls = read_assistant_calls(transcript_fixture, session_id="my-session")
        assert all(c.session_id == "my-session" for c in calls)

    def test_missing_file_returns_empty(self, tmp_path):
        calls = read_assistant_calls(tmp_path / "nonexistent.jsonl")
        assert calls == []


# ---------------------------------------------------------------------------
# 3. Attribution across two stages sharing one session
# ---------------------------------------------------------------------------


class TestAttributionTwoStages:
    def test_attributes_calls_by_timestamp(self):
        """Two stages sharing session 'sess1', distinguished by time window."""
        calls = [
            Call(
                timestamp="2026-09-05T10:00:30.000Z",
                model="claude-sonnet-4-6",
                input_tokens=1000,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
                output_tokens=500,
                message_id="msg_001",
                request_id="req_001",
                session_id="sess1",
            ),
            Call(
                timestamp="2026-09-05T10:02:30.000Z",
                model="claude-sonnet-4-6",
                input_tokens=2000,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
                output_tokens=600,
                message_id="msg_002",
                request_id="req_002",
                session_id="sess1",
            ),
        ]
        stage_records = [
            {
                "node_id": "stage-a",
                "attempt": 1,
                "started_at": "2026-09-05T10:00:00.000Z",
                "finished_at": "2026-09-05T10:01:00.000Z",
                "usage": {"session_id": "sess1"},
            },
            {
                "node_id": "stage-b",
                "attempt": 1,
                "started_at": "2026-09-05T10:02:00.000Z",
                "finished_at": "2026-09-05T10:03:00.000Z",
                "usage": {"session_id": "sess1"},
            },
        ]
        result = attribute(calls, stage_records)

        assert "stage-a:1" in result
        assert "stage-b:1" in result
        assert result["stage-a:1"].context_peak_tokens == 1000
        assert result["stage-b:1"].context_peak_tokens == 2000

    def test_model_mix_aggregated_per_stage(self):
        """model_mix correctly counts calls and tokens per model."""
        calls = [
            Call("2026-09-05T10:00:10.000Z", "claude-sonnet-4-6", 1000, 0, 0, 500, "m1", "r1", "sess1"),
            Call("2026-09-05T10:00:20.000Z", "claude-haiku-4-5", 500, 0, 0, 50, "m2", "r2", "sess1"),
        ]
        stage_records = [
            {
                "node_id": "gen",
                "attempt": 1,
                "started_at": "2026-09-05T10:00:00.000Z",
                "finished_at": "2026-09-05T10:01:00.000Z",
                "usage": {"session_id": "sess1"},
            },
        ]
        result = attribute(calls, stage_records)
        mix = result["gen:1"].model_mix

        assert "claude-sonnet-4-6" in mix
        assert "claude-haiku-4-5" in mix
        assert mix["claude-sonnet-4-6"]["calls"] == 1
        assert mix["claude-haiku-4-5"]["output"] == 50


# ---------------------------------------------------------------------------
# 4. Unattributed counting
# ---------------------------------------------------------------------------


class TestUnattributed:
    def test_calls_outside_window_go_to_unattributed(self):
        calls = [
            # In window
            Call("2026-09-05T10:00:30.000Z", "claude-sonnet-4-6", 1000, 0, 0, 500, "m1", "r1", "sess1"),
            # Outside window (after finished_at)
            Call("2026-09-05T10:05:00.000Z", "claude-sonnet-4-6", 500, 0, 0, 200, "m2", "r2", "sess1"),
        ]
        stage_records = [
            {
                "node_id": "gen",
                "attempt": 1,
                "started_at": "2026-09-05T10:00:00.000Z",
                "finished_at": "2026-09-05T10:01:00.000Z",
                "usage": {"session_id": "sess1"},
            },
        ]
        result = attribute(calls, stage_records)
        assert "gen:1" in result
        assert "unattributed" in result
        assert result["unattributed"].model_mix["claude-sonnet-4-6"]["calls"] == 1

    def test_no_unattributed_key_when_all_matched(self):
        calls = [
            Call("2026-09-05T10:00:30.000Z", "claude-sonnet-4-6", 1000, 0, 0, 500, "m1", "r1", "sess1"),
        ]
        stage_records = [
            {
                "node_id": "gen",
                "attempt": 1,
                "started_at": "2026-09-05T10:00:00.000Z",
                "finished_at": "2026-09-05T10:01:00.000Z",
                "usage": {"session_id": "sess1"},
            },
        ]
        result = attribute(calls, stage_records)
        assert "unattributed" not in result


# ---------------------------------------------------------------------------
# 5. Missing file -> "missing" on stage, no exception
# ---------------------------------------------------------------------------


class TestMissingTranscript:
    def test_missing_file_marked_on_stage(self, tmp_path, monkeypatch):
        """compute_metrics sets transcript='missing' when transcript file absent."""
        import json as _json
        from norn.metrics import compute_metrics

        # Override Path.home so transcript_path looks under tmp_path
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        cwd = "/test/project"
        session_id = "sess-missing"

        # Create run dir with an agent stage that has a session_id
        rd = tmp_path / "run.runs" / "1"
        stages_dir = rd / "stages"
        stages_dir.mkdir(parents=True)

        run_json = {
            "pipeline": "test",
            "run_id": 1,
            "provider": "claude-code",
            "success": True,
            "duration_ms": 5000,
            "totals": {"retries": 0},
            "cwd": cwd,
        }
        (rd / "run.json").write_text(_json.dumps(run_json))

        stage_rec = {
            "node_id": "gen",
            "attempt": 1,
            "status": "passed",
            "success": True,
            "started_at": "2026-09-05T10:00:00.000Z",
            "finished_at": "2026-09-05T10:01:00.000Z",
            "duration_ms": 1000,
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 500,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "total_cost_usd": 0.05,
                "duration_ms": 800,
                "num_turns": 3,
                "model_id": "claude-sonnet-4-6",
                "model": "sonnet",
                "result_subtype": "success",
                "stop_reason": None,
                "tool_uses": 2,
                "tool_errors": 0,
                "session_id": session_id,
            },
        }
        (stages_dir / "0001-gen-a1.json").write_text(_json.dumps(stage_rec))

        # Transcript file does NOT exist — no file created

        m = compute_metrics(rd)
        assert len(m.stages) == 1
        assert m.stages[0].transcript == "missing"

    def test_missing_file_read_returns_empty(self, tmp_path):
        """read_assistant_calls returns [] for a missing file without raising."""
        calls = read_assistant_calls(tmp_path / "no_such_file.jsonl")
        assert calls == []


# ---------------------------------------------------------------------------
# 6 & 7. context-bloat rule: positive and negative
# ---------------------------------------------------------------------------


# The fixture stage runs on the "standard" label, whose window model_labels.toml
# declares; the rule fires on a fraction of that window.
_STANDARD_WINDOW = resolve_label("claude-code", "standard").context_window
_BLOAT_LIMIT = int(_STANDARD_WINDOW * CONTEXT_BLOAT_WINDOW_FRACTION)


class TestContextBloatRule:
    def test_fires_when_peak_exceeds_threshold(self):
        sm = _make_stage_sm(context_peak_tokens=_BLOAT_LIMIT + 1)
        m = _run_metrics_with_stage(sm)
        findings = find_issues(m)
        assert any(f.rule == "context-bloat" for f in findings)

    def test_evidence_names_the_window(self):
        sm = _make_stage_sm(context_peak_tokens=_BLOAT_LIMIT + 1)
        findings = find_issues(_run_metrics_with_stage(sm))
        evidence = next(f.evidence for f in findings if f.rule == "context-bloat")
        assert f"{_STANDARD_WINDOW:,}-token window" in evidence

    def test_does_not_fire_at_threshold(self):
        sm = _make_stage_sm(context_peak_tokens=_BLOAT_LIMIT)
        m = _run_metrics_with_stage(sm)
        findings = find_issues(m)
        assert not any(f.rule == "context-bloat" for f in findings)

    def test_does_not_fire_for_a_label_without_a_window(self):
        """No declared window means no scale to judge the peak against."""
        sm = _make_stage_sm(model="no-such-label", context_peak_tokens=10_000_000)
        m = _run_metrics_with_stage(sm)
        findings = find_issues(m)
        assert not any(f.rule == "context-bloat" for f in findings)

    def test_does_not_fire_when_below_threshold(self):
        sm = _make_stage_sm(context_peak_tokens=50_000)
        m = _run_metrics_with_stage(sm)
        findings = find_issues(m)
        assert not any(f.rule == "context-bloat" for f in findings)

    def test_does_not_fire_when_none(self):
        sm = _make_stage_sm(context_peak_tokens=None)
        m = _run_metrics_with_stage(sm)
        findings = find_issues(m)
        assert not any(f.rule == "context-bloat" for f in findings)


# ---------------------------------------------------------------------------
# 8 & 9. model-mix-surprise rule: positive and negative
# ---------------------------------------------------------------------------


class TestModelMixSurpriseRule:
    def test_fires_when_other_model_exceeds_threshold(self):
        """A secondary model with >30% of output tokens triggers the rule."""
        sm = _make_stage_sm(
            model_id="claude-sonnet-4-6",
            model_mix={
                "claude-sonnet-4-6": {"calls": 10, "input": 10000, "cache_read": 0, "cache_creation": 0, "output": 600},
                "claude-haiku-4-5": {"calls": 3, "input": 3000, "cache_read": 0, "cache_creation": 0, "output": 400},
            },
        )
        m = _run_metrics_with_stage(sm)
        findings = find_issues(m)
        assert any(f.rule == "model-mix-surprise" for f in findings)

    def test_does_not_fire_when_other_model_below_threshold(self):
        """A secondary model with <30% of output tokens does not trigger."""
        sm = _make_stage_sm(
            model_id="claude-sonnet-4-6",
            model_mix={
                "claude-sonnet-4-6": {"calls": 10, "input": 10000, "cache_read": 0, "cache_creation": 0, "output": 900},
                "claude-haiku-4-5": {"calls": 1, "input": 1000, "cache_read": 0, "cache_creation": 0, "output": 50},
            },
        )
        m = _run_metrics_with_stage(sm)
        findings = find_issues(m)
        assert not any(f.rule == "model-mix-surprise" for f in findings)

    def test_does_not_fire_with_single_model(self):
        """Only one model in mix → no surprise."""
        sm = _make_stage_sm(
            model_id="claude-sonnet-4-6",
            model_mix={
                "claude-sonnet-4-6": {"calls": 10, "input": 10000, "cache_read": 0, "cache_creation": 0, "output": 1000},
            },
        )
        m = _run_metrics_with_stage(sm)
        findings = find_issues(m)
        assert not any(f.rule == "model-mix-surprise" for f in findings)

    def test_does_not_fire_when_model_mix_none(self):
        """No model_mix data → no rule."""
        sm = _make_stage_sm(model_mix=None)
        m = _run_metrics_with_stage(sm)
        findings = find_issues(m)
        assert not any(f.rule == "model-mix-surprise" for f in findings)

    def test_evidence_names_surprising_model(self):
        """Evidence string names the unexpected model."""
        sm = _make_stage_sm(
            model_id="claude-sonnet-4-6",
            model_mix={
                "claude-sonnet-4-6": {"calls": 5, "input": 5000, "cache_read": 0, "cache_creation": 0, "output": 500},
                "claude-opus-4-5": {"calls": 5, "input": 5000, "cache_read": 0, "cache_creation": 0, "output": 500},
            },
        )
        m = _run_metrics_with_stage(sm)
        findings = find_issues(m)
        surprise = [f for f in findings if f.rule == "model-mix-surprise"]
        assert surprise
        assert "claude-opus-4-5" in surprise[0].evidence


# ---------------------------------------------------------------------------
# Compute metrics + transcript integration
# ---------------------------------------------------------------------------


class TestComputeMetricsTranscriptIntegration:
    def test_model_mix_written_to_stage_metrics(self, tmp_path, monkeypatch):
        """compute_metrics reads the transcript and populates model_mix on stage."""
        import json as _json
        from norn.metrics import compute_metrics

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        cwd = "/test/my-project"
        session_id = "sess-abc"
        slug = cwd.replace("/", "-")

        # Write transcript file
        transcript_dir = tmp_path / ".claude" / "projects" / slug
        transcript_dir.mkdir(parents=True)
        transcript_lines = [
            _assistant_line("m1", "r1", "claude-sonnet-4-6", "2026-09-05T10:00:30.000Z",
                            input_tokens=1000, output_tokens=500),
            _assistant_line("m2", "r2", "claude-haiku-4-5", "2026-09-05T10:00:45.000Z",
                            input_tokens=500, output_tokens=50),
        ]
        (transcript_dir / f"{session_id}.jsonl").write_text(
            "\n".join(_json.dumps(l) for l in transcript_lines)
        )

        # Create run dir (using plain node_id to avoid colon in filename)
        rd = tmp_path / "run.runs" / "1"
        stages_dir = rd / "stages"
        stages_dir.mkdir(parents=True)

        run_json = {
            "pipeline": "test",
            "run_id": 1,
            "provider": "claude-code",
            "success": True,
            "duration_ms": 5000,
            "totals": {"retries": 0},
            "cwd": cwd,
        }
        (rd / "run.json").write_text(_json.dumps(run_json))

        stage_rec = {
            "node_id": "gen",
            "attempt": 1,
            "status": "passed",
            "success": True,
            "started_at": "2026-09-05T10:00:00.000Z",
            "finished_at": "2026-09-05T10:01:00.000Z",
            "duration_ms": 1000,
            "usage": {
                "input_tokens": 1500,
                "output_tokens": 550,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "total_cost_usd": 0.05,
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
        }
        (stages_dir / "0001-gen-a1.json").write_text(_json.dumps(stage_rec))

        m = compute_metrics(rd)
        assert len(m.stages) == 1
        sm = m.stages[0]
        assert sm.model_mix is not None
        assert "claude-sonnet-4-6" in sm.model_mix
        assert "claude-haiku-4-5" in sm.model_mix
        assert sm.context_peak_tokens == 1000  # max of (1000+0) and (500+0)
        assert sm.context_growth == [1000, 500]

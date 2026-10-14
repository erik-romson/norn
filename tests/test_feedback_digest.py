"""Tests for norn.feedback_digest: baseline, bar validation, numbering, cost, compare round-trip."""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import pytest

from norn import compare, feedback_digest as fd
from norn.agents.base import AgentEvent, AgentRequest, AgentUsage
from norn.cli import main
from norn.metrics import Finding, RunMetrics


def _metrics(cost: float, **kw) -> RunMetrics:
    fields = dict(
        run_id="1", pipeline="p", provider="claude-code", cost_mode="tracked",
        success=True, failed_stage=None, cost_usd=cost, billed_input_tokens=100,
        output_tokens=100, cache_read_tokens=0, cache_hit_ratio=0.0, turns=1,
        wall_ms=1, api_ms=1, tool_time_ms=1, agent_stage_count=1,
        command_stage_count=0, failed_attempts=0, failed_attempt_cost_usd=0.0,
        timed_out_attempts=0, retries=2, budget_max_cost_usd=None,
        budget_used_pct=None, waiting_input={}, top_cost_stages=["stage:review 1"],
        quota=None, codex_cost_usd_estimate=None, stages=[],
    )
    fields.update(kw)
    return RunMetrics(**fields)


def _write_run(root: Path, key: str, jid: int, hid: int | None, cost: float, **extra) -> Path:
    d = root / f"{key}.runs" / str(jid)
    d.mkdir(parents=True)
    (d / "run.json").write_text(json.dumps({} if hid is None else {"history_run_id": hid}))
    (d / "metrics.json").write_text(json.dumps(asdict(_metrics(cost))))
    (d / "findings.json").write_text(
        json.dumps([asdict(Finding("retry-storm", "warn", "fix step", "e", "s"))])
    )
    if extra.get("rating"):
        (d / "rating.json").write_text(json.dumps({"score": 2, "note": "slow"}))
    if extra.get("feedback"):
        (d / "feedback.md").write_text(extra["feedback"])
    return d


@pytest.fixture
def work(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


GOOD = """### P1 — Trim the review prompt
Change: Shorten the review prompt in implement_features_v3.
Evidence runs: 1, 2
Bar: metric=cost_usd direction=lower threshold=20%

### P2 — Cap review fix rounds
Change: Cap the fix rounds.
Evidence runs: 2, 3
Bar: metric=group_share:review direction=lower threshold=10
Group: review=^review
"""


class FakeProvider:
    name = "fake"

    def __init__(self, text: str, cost: float = 0.25):
        self.text = text
        self.cost = cost
        self.requests: list[AgentRequest] = []

    async def run(self, request: AgentRequest):
        self.requests.append(request)
        yield AgentEvent(text=self.text)
        yield AgentEvent(
            usage=AgentUsage(provider=self.name, total_cost_usd=self.cost, input_tokens=10, output_tokens=5)
        )


def _seed(root: Path, n: int = 3, key: str = "pa") -> None:
    for i in range(1, n + 1):
        _write_run(root, key, i, i, float(i), rating=(i == 1), feedback="FEEDBACK " + "x" * 5000)


async def _digest(provider: FakeProvider, **kw):
    with patch("norn.agents.registry.get_provider", return_value=provider):
        return await fd.feedback_digest("pa", **kw)


# --- baseline ---------------------------------------------------------------


def test_collect_baseline_reads_run_artifacts(work):
    _seed(work)
    runs = fd.collect_baseline("pa", 10)
    assert [r.history_id for r in runs] == [1, 2, 3]
    assert runs[0].findings == [{"rule": "retry-storm", "severity": "warn", "stage": "fix step"}]
    assert runs[0].rating == {"score": 2, "note": "slow"}
    assert runs[0].top_cost_stages == ["stage:review 1"]
    assert runs[0].retries == 2
    assert len(runs[0].feedback) == fd.FEEDBACK_CAP
    assert runs[1].rating is None


def test_collect_baseline_takes_the_last_n(work):
    _seed(work, 5)
    assert [r.history_id for r in fd.collect_baseline("pa", 2)] == [4, 5]


def test_fewer_than_two_runs_is_an_error(work):
    _seed(work, 1)
    with pytest.raises(fd.FeedbackDigestError, match="at least 2"):
        fd.collect_baseline("pa", 10)


def test_runs_without_history_id_are_not_baseline(work):
    _write_run(work, "pa", 1, None, 1.0)
    _write_run(work, "pa", 2, 7, 1.0)
    with pytest.raises(fd.FeedbackDigestError, match="found 1"):
        fd.collect_baseline("pa", 10)


# --- validation -------------------------------------------------------------


def test_good_proposals_validate():
    props = fd.validate_proposals(GOOD, [1, 2, 3])
    assert [p.key for p in props] == ["P1", "P2"]
    assert props[0].evidence == [1, 2] and props[0].threshold == "20%"
    assert props[1].group == ("review", "^review")


def test_unknown_metric_is_rejected():
    text = GOOD.replace("metric=cost_usd", "metric=vibes")
    with pytest.raises(fd.FeedbackDigestError, match="unknown metric 'vibes'"):
        fd.validate_proposals(text, [1, 2, 3])


def test_evidence_outside_baseline_is_rejected():
    text = GOOD.replace("Evidence runs: 1, 2", "Evidence runs: 1, 99")
    with pytest.raises(fd.FeedbackDigestError, match=r"\[99\]"):
        fd.validate_proposals(text, [1, 2, 3])


def test_bad_threshold_and_direction_are_rejected():
    text = GOOD.replace("direction=lower threshold=20%", "direction=sideways threshold=lots")
    with pytest.raises(fd.FeedbackDigestError) as exc:
        fd.validate_proposals(text, [1, 2, 3])
    assert "direction" in str(exc.value) and "threshold" in str(exc.value)


def test_group_metric_needs_a_group_line():
    text = GOOD.replace("Group: review=^review\n", "")
    with pytest.raises(fd.FeedbackDigestError, match="Group: review"):
        fd.validate_proposals(text, [1, 2, 3])


def test_no_proposals_is_rejected():
    with pytest.raises(fd.FeedbackDigestError, match="no '### P"):
        fd.validate_proposals("nothing useful", [1, 2])


# --- the agent pass ---------------------------------------------------------


@pytest.mark.asyncio
async def test_digest_writes_file_and_calls_agent_read_only(work):
    _seed(work)
    provider = FakeProvider(GOOD)
    result = await _digest(provider, last=10)
    assert result.path == work / "pa.runs" / "feedback-digest-1.md"
    assert result.cost_usd == 0.25 and result.baseline_ids == [1, 2, 3]
    req = provider.requests[0]
    assert req.permission_mode == "plan" and req.max_turns == 20
    assert req.allowed_tools == ["Read", "Grep", "Glob"]
    assert req.model == "standard"
    assert req.cwd == str(work / "pa.runs")
    assert "cost_usd" in req.prompt and "FEEDBACK" in req.prompt
    text = result.path.read_text()
    assert "## Pass bar" in text and "Fixed before any change" in text
    assert "baseline: 1,2,3" in text and "group: review=^review" in text


@pytest.mark.asyncio
async def test_existing_digest_is_never_overwritten(work):
    _seed(work)
    existing = work / "pa.runs" / "feedback-digest-1.md"
    existing.write_text("mine")
    result = await _digest(FakeProvider(GOOD))
    assert result.path.name == "feedback-digest-2.md"
    assert existing.read_text() == "mine"
    third = await _digest(FakeProvider(GOOD))
    assert third.path.name == "feedback-digest-3.md"


@pytest.mark.asyncio
async def test_invalid_proposals_write_rejected_file(work):
    _seed(work)
    bad = GOOD.replace("metric=cost_usd", "metric=vibes")
    with pytest.raises(fd.FeedbackDigestError, match="unknown metric"):
        await _digest(FakeProvider(bad))
    runs = work / "pa.runs"
    assert (runs / "feedback-digest-1.rejected.md").read_text() == bad
    assert not (runs / "feedback-digest-1.md").exists()
    # the rejected file also reserves k, so a retry does not reuse it
    ok = await _digest(FakeProvider(GOOD))
    assert ok.path.name == "feedback-digest-2.md"


@pytest.mark.asyncio
async def test_cost_over_max_cost_fails_without_a_digest(work):
    _seed(work)
    with pytest.raises(fd.FeedbackDigestError, match=r"\$6.00 exceeds limit \$5.00"):
        await _digest(FakeProvider(GOOD, cost=6.0), max_cost_usd=5.0)
    assert not (work / "pa.runs" / "feedback-digest-1.md").exists()
    assert (work / "pa.runs" / "feedback-digest-1.rejected.md").exists()


@pytest.mark.asyncio
async def test_cost_is_not_added_to_history(work):
    _seed(work)
    result = await _digest(FakeProvider(GOOD, cost=1.5))
    assert result.cost_usd == 1.5
    assert not list(work.glob("*.history"))


@pytest.mark.asyncio
async def test_unknown_model_label_fails_before_any_agent_call(work):
    _seed(work)
    provider = FakeProvider(GOOD)
    with pytest.raises(fd.FeedbackDigestError, match="unknown model label"):
        await _digest(provider, model="gigantic")
    assert provider.requests == []


@pytest.mark.asyncio
async def test_unknown_pipeline_is_a_compare_error(work):
    with pytest.raises(compare.CompareError):
        await fd.feedback_digest("nope")


# --- round trip with norn compare --bar -------------------------------------


@pytest.mark.asyncio
async def test_written_digest_is_read_by_compare_bar(work):
    _seed(work)
    result = await _digest(FakeProvider(GOOD))
    barfile = compare.parse_bar_file(result.path)
    assert [(b.metric, b.direction, b.threshold, b.baseline) for b in barfile.bars] == [
        ("cost_usd", "lower", "20%", [1, 2, 3]),
        ("group_share:review", "lower", "10", [1, 2, 3]),
    ]
    assert "review" in barfile.groups
    # and the full check runs against the same runs
    results = compare.check_bars("pa", barfile)
    assert [r.status for r in results] == ["unproven", "unproven"]


# --- CLI --------------------------------------------------------------------


def test_cli_feedback_digest_prints_summary(work, capsys):
    _seed(work)
    with patch("norn.agents.registry.get_provider", return_value=FakeProvider(GOOD)):
        with patch("sys.argv", ["norn", "feedback-digest", "pa", "--last", "3"]):
            main()
    out = capsys.readouterr().out
    assert "feedback-digest-1.md" in out
    assert "outside the pipeline budget and history" in out
    assert "norn compare pa --bar" in out


def test_cli_feedback_digest_too_few_runs_exits_1(work, capsys):
    _seed(work, 1)
    with patch("sys.argv", ["norn", "feedback-digest", "pa"]):
        with pytest.raises(SystemExit) as exc:
            main()
    assert exc.value.code == 1
    assert "at least 2" in capsys.readouterr().err


def test_cli_feedback_digest_over_cost_exits_1(work, capsys):
    _seed(work)
    with patch("norn.agents.registry.get_provider", return_value=FakeProvider(GOOD, cost=3.0)):
        with patch("sys.argv", ["norn", "feedback-digest", "pa", "--max-cost", "1"]):
            with pytest.raises(SystemExit) as exc:
                main()
    assert exc.value.code == 1
    assert "exceeds limit" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["feedback-digest"], ["feedback-digest", "pa", "--last", "0"], ["feedback-digest", "pa", "--bogus"]])
def test_cli_feedback_digest_argument_errors_exit_2(argv, work):
    with patch("sys.argv", ["norn", *argv]):
        with pytest.raises(SystemExit) as exc:
            main()
    assert exc.value.code == 2

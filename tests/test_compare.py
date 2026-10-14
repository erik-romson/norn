"""Tests for norn.compare: statistics, group matching, verdicts and pass bars."""
from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path

import pytest

from norn import compare
from norn.metrics import Finding, RunMetrics, StageMetrics


def _stage(node_id: str, cost: float, status: str = "passed") -> StageMetrics:
    return StageMetrics(
        name=node_id, node_id=node_id, attempt=1, kind="agent", status=status,
        success=True, cost_usd=cost, cost_share=0.0, billed_input_tokens=10,
        output_tokens=10, cache_hit_ratio=0.0, turns=1, tokens_per_turn=10.0,
        tool_uses=0, tool_errors=0, prompt_chars=10, system_prompt_chars=0,
        unresolved_placeholders=[], duration_ms=1, model=None, model_id=None,
        result_subtype=None, stop_reason=None, nonfatal_error=False,
    )


def _metrics(cost: float, stages: list[StageMetrics] | None = None, **kw) -> RunMetrics:
    fields = dict(
        run_id="1", pipeline="p", provider="claude-code", cost_mode="tracked",
        success=True, failed_stage=None, cost_usd=cost, billed_input_tokens=100,
        output_tokens=int(cost * 1000), cache_read_tokens=0, cache_hit_ratio=0.0,
        turns=1, wall_ms=int(cost * 10_000), api_ms=1, tool_time_ms=1,
        agent_stage_count=1, command_stage_count=0, failed_attempts=0,
        failed_attempt_cost_usd=0.0, timed_out_attempts=0, retries=0,
        budget_max_cost_usd=None, budget_used_pct=None, waiting_input={},
        top_cost_stages=[], quota=None, codex_cost_usd_estimate=None,
        stages=stages or [],
    )
    fields.update(kw)
    return RunMetrics(**fields)


def _write_run(
    root: Path, key: str, journal_id: int, hist_id: int | None, metrics: RunMetrics,
    findings: list[Finding] | None = None,
) -> Path:
    d = root / f"{key}.runs" / str(journal_id)
    d.mkdir(parents=True)
    run_json = {} if hist_id is None else {"history_run_id": hist_id}
    (d / "run.json").write_text(json.dumps(run_json))
    (d / "metrics.json").write_text(json.dumps(asdict(metrics)))
    if findings is not None:
        (d / "findings.json").write_text(json.dumps([asdict(f) for f in findings]))
    return d


def _costs(root: Path, key: str, costs: list[float], start_hist: int = 1, **kw) -> None:
    for i, c in enumerate(costs):
        _write_run(root, key, i + 1, start_hist + i, _metrics(c, **kw))


@pytest.fixture
def work(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


REPRESENTATIVE = [
    _stage("stage:review 1", 2.0),
    _stage("stage:review fix 1", 1.0),
    _stage("stage:validate findings 1", 1.0),
    _stage("loop:validate step-01/stage:fix step-01", 4.0),
    _stage("stage:learn", 2.0, status="skipped"),
]


def test_metric_vocabulary():
    assert compare.is_metric("cost_usd")
    assert compare.is_metric("loop_exhausted")
    assert compare.is_metric("group_share:review")
    assert compare.is_metric("group_count:review")
    assert compare.is_metric("finding:context-bloat")
    assert not compare.is_metric("group_share:")
    assert not compare.is_metric("bogus")
    assert compare.is_threshold("0.5") and compare.is_threshold("20%")
    assert not compare.is_threshold("abc")


def test_verdict_rules():
    assert compare.verdict([1, 2], [5, 6, 7]) == "too few runs (need 3)"
    assert compare.verdict([1, 2, 3], [2, 3, 4]) == "within noise"
    assert compare.verdict([10, 11, 12], [1, 2, 3]) == "B lower"
    assert compare.verdict([1, 2, 3], [10, 11, 12]) == "B higher"


def test_load_runs_takes_last_n_with_metrics(work):
    _costs(work, "a", [1, 2, 3, 4, 5, 6])
    d = work / "a.runs" / "7"
    d.mkdir()  # a run dir without metrics.json is skipped
    runs = compare.load_runs("a", 3)
    assert [r.journal_id for r in runs] == [4, 5, 6]
    assert [r.history_id for r in runs] == [4, 5, 6]


def test_unknown_pipeline_and_no_runs(work):
    with pytest.raises(compare.CompareError):
        compare.load_runs("nope")
    (work / "empty.runs" / "1").mkdir(parents=True)
    with pytest.raises(compare.CompareError):
        compare.load_runs("empty")


def test_compare_medians_ranges_and_verdicts(work):
    _costs(work, "a", [10, 11, 12, 13, 14])
    _costs(work, "b", [1, 2, 3, 4, 5])
    cmp = compare.compare_pipelines("a", "b")
    row = {r.metric: r for r in cmp.rows}["cost_usd"]
    assert (row.a.median, row.a.low, row.a.high) == (12, 10, 14)
    assert (row.b.median, row.b.low, row.b.high) == (3, 1, 5)
    assert row.verdict == "B lower"
    text = compare.format_comparison(cmp)
    assert "5 runs, 5 succeeded" in text
    assert "B lower" in text


def test_compare_too_few_runs(work):
    _costs(work, "a", [1, 2])
    _costs(work, "b", [1, 2, 3])
    cmp = compare.compare_pipelines("a", "b")
    assert all(r.verdict == "too few runs (need 3)" for r in cmp.rows)


def test_success_count(work):
    _write_run(work, "a", 1, 1, _metrics(1, success=False))
    _write_run(work, "a", 2, 2, _metrics(1))
    runs = compare.load_runs("a")
    assert sum(r.metrics.success for r in runs) == 1


def test_group_matches_bare_stage_name_not_node_id():
    m = _metrics(10.0, REPRESENTATIVE)
    _, review = compare.parse_group("review=^(review|validate findings)")
    # review 1 (2.0) + review fix 1 (1.0) + validate findings 1 (1.0) = 4 of 10
    assert compare.group_share(m, review) == pytest.approx(40.0)
    assert compare.group_count(m, review) == 3


def test_group_regex_anchored_on_loop_stage_bare_name():
    m = _metrics(10.0, REPRESENTATIVE)
    _, fix = compare.parse_group("fix=^fix step-01$")
    assert compare.group_count(m, fix) == 1
    assert compare.group_share(m, fix) == pytest.approx(40.0)


def test_group_does_not_match_node_id_prefix():
    m = _metrics(10.0, REPRESENTATIVE)
    _, node = compare.parse_group("n=^stage:")
    assert compare.group_count(m, node) == 0


def test_skipped_stages_not_counted():
    m = _metrics(10.0, REPRESENTATIVE + [_stage("stage:learn x", 0.0, status="skipped_condition")])
    _, learn = compare.parse_group("learn=^learn")
    assert compare.group_count(m, learn) == 0


def test_group_share_zero_total_cost():
    m = _metrics(0.0, [_stage("stage:review 1", 0.0)])
    _, review = compare.parse_group("r=review")
    assert compare.group_share(m, review) == 0.0


def test_groups_in_comparison(work):
    _costs(work, "a", [10, 10, 10], stages=REPRESENTATIVE)
    _costs(work, "b", [10, 10, 10], stages=REPRESENTATIVE)
    groups = dict([compare.parse_group("review=^(review|validate findings)")])
    cmp = compare.compare_pipelines("a", "b", groups=groups)
    rows = {r.metric: r for r in cmp.rows}
    assert rows["group_share:review"].a.median == pytest.approx(40.0)
    assert rows["group_count:review"].a.median == 3


@pytest.mark.parametrize("spec", ["", "noequals", "=x", "n=", "n=(", "a b=x", "a:b=x"])
def test_parse_group_rejects_malformed(spec):
    with pytest.raises(ValueError):
        compare.parse_group(spec)


def test_finding_metric(work):
    f = Finding(rule="context-bloat", severity="warn", stage=None, evidence="", suggestion="")
    _write_run(work, "a", 1, 1, _metrics(1), findings=[f, f])
    _write_run(work, "a", 2, 2, _metrics(1))
    runs = compare.load_runs("a")
    assert [compare.metric_value(r, "finding:context-bloat") for r in runs] == [2.0, 0.0]


def test_undeclared_group_metric_errors(work):
    _costs(work, "a", [1])
    with pytest.raises(compare.CompareError):
        compare.metric_value(compare.load_runs("a")[0], "group_share:x", {})


DIGEST = """\
# Feedback digest

Pipeline: implement_features_v3
group: review=^(review|validate findings)

### P1 — shorter reviews

## Pass bar

- metric=cost_usd direction=lower threshold=20% baseline=1,2,3
metric=group_share:review direction=lower threshold=5 baseline=1,2,3

Fixed before any change.
"""


def test_parse_bar_text():
    bf = compare.parse_bar_text(DIGEST)
    assert [b.metric for b in bf.bars] == ["cost_usd", "group_share:review"]
    assert bf.bars[0].baseline == [1, 2, 3]
    assert bf.bars[0].threshold == "20%"
    assert "review" in bf.groups


@pytest.mark.parametrize(
    "line",
    [
        "metric=bogus direction=lower threshold=1 baseline=1",
        "metric=cost_usd direction=sideways threshold=1 baseline=1",
        "metric=cost_usd direction=lower threshold=lots baseline=1",
        "metric=cost_usd direction=lower threshold=1 baseline=x",
        "metric=cost_usd direction=lower threshold=1",
    ],
)
def test_parse_bar_text_rejects_bad_lines(line):
    with pytest.raises(compare.CompareError):
        compare.parse_bar_text(f"## Pass bar\n{line}\n")


def test_parse_bar_text_needs_bars_and_declared_groups():
    with pytest.raises(compare.CompareError):
        compare.parse_bar_text("# nothing\n")
    with pytest.raises(compare.CompareError):
        compare.parse_bar_text("## Pass bar\nmetric=group_count:x direction=lower threshold=1 baseline=1\n")


def _bar_runs(work, baseline: list[float], post: list[float]):
    _costs(work, "p", baseline + post)


def _status(work, text: str, pipeline: str = "p") -> list[str]:
    return [r.status for r in compare.check_bars(pipeline, compare.parse_bar_text(text))]


def _bar(threshold: str, direction: str = "lower", n: int = 3, metric: str = "cost_usd") -> str:
    ids = ",".join(str(i) for i in range(1, n + 1))
    return f"## Pass bar\nmetric={metric} direction={direction} threshold={threshold} baseline={ids}\n"


def test_bar_met_lower_percent(work):
    _bar_runs(work, [10, 11, 12], [4, 5, 6])
    assert _status(work, _bar("50%")) == ["met"]


def test_bar_met_higher_absolute(work):
    _bar_runs(work, [1, 2, 3], [10, 11, 12])
    assert _status(work, _bar("5", "higher")) == ["met"]


def test_bar_not_met_threshold_not_reached(work):
    _bar_runs(work, [10, 11, 12], [8, 8, 9])
    assert _status(work, _bar("50%")) == ["not met"]


def test_bar_not_met_wrong_direction(work):
    _bar_runs(work, [10, 11, 12], [20, 21, 22])
    assert _status(work, _bar("1")) == ["not met"]


def test_bar_unproven_within_baseline_range(work):
    _bar_runs(work, [10, 11, 12], [11, 11, 11])
    assert _status(work, _bar("1%")) == ["unproven"]


def test_bar_unproven_too_few_post_runs(work):
    _bar_runs(work, [10, 11, 12], [1, 1])
    assert _status(work, _bar("1%")) == ["unproven"]


def test_bar_post_set_is_runs_after_every_baseline_id(work):
    # history ids 1..3 baseline, post 4..6; an older run (hist 0) is neither
    _write_run(work, "p", 1, 0, _metrics(1))
    for i, c in enumerate([10, 11, 12, 1, 1, 1], start=2):
        _write_run(work, "p", i, i - 1, _metrics(c))
    (r,) = compare.check_bars("p", compare.parse_bar_text(_bar("50%")))
    assert (r.status, r.baseline_n, r.post_n) == ("met", 3, 3)


def test_bar_missing_baseline_run_errors(work):
    _bar_runs(work, [10, 11, 12], [])
    with pytest.raises(compare.CompareError):
        compare.check_bars("p", compare.parse_bar_text(_bar("1", n=4)))


def test_bar_group_metric_uses_digest_groups(work):
    _costs(work, "p", [10] * 3, stages=REPRESENTATIVE)
    cheap = [_stage("stage:review 1", 0.5), _stage("stage:other", 9.5)]
    for i in range(4, 7):
        _write_run(work, "p", i, i, _metrics(10.0, cheap))
    text = "group: review=^review\n" + _bar("10", metric="group_share:review")
    assert _status(work, text) == ["met"]


def test_format_bar_results(work):
    _bar_runs(work, [10, 11, 12], [4, 5, 6])
    out = compare.format_bar_results("p", compare.check_bars("p", compare.parse_bar_text(_bar("50%"))))
    assert "met" in out and "cost_usd" in out

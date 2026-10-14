"""Compare journaled runs of two pipelines, and check registered pass bars.

``norn compare A B`` reads the last N journaled runs of two state keys from
their ``metrics.json`` (and ``findings.json``) and prints medians, ranges and a
verdict per metric.  ``norn compare A --bar <digest.md>`` reads the
``## Pass bar`` lines of a digest written by ``norn feedback-digest`` and says
whether each bar is ``met``, ``unproven`` or ``not met``.

Deterministic and zero tokens.  This is a core module: it must not import from
``norn.contrib`` or ``norn.pipelines``.

Digest grammar read by :func:`parse_bar_file`:

* header lines ``group: NAME=REGEX`` (an optional leading ``- ``) declare the
  groups the bars use;
* each line under ``## Pass bar`` is
  ``metric=<METRICS item> direction=lower|higher threshold=<number or %>
  baseline=<id>,<id>…`` (an optional leading ``- ``).

Group regexes are matched with ``re.search`` against the bare human stage name
(:func:`norn.metrics.stage_name_from_node_id`), never against ``name`` or the
journal node id.  ``group_share:<name>`` is a percentage (0–100) of the run's
cost; ``group_count:<name>`` counts matching stages whose status is not
skipped.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from norn.journal import history_run_id, run_dir, runs_dir
from norn.metrics import (
    Finding,
    RunMetrics,
    load_findings,
    load_metrics,
    stage_name_from_node_id,
)
from norn.state import _state_key_candidates

DEFAULT_LAST = 5
MIN_RUNS = 3

# The metrics shown for every side of a comparison, in print order.
SUMMARY_METRICS = ("cost_usd", "output_tokens", "retries", "failed_attempts", "wall_ms")

# Plain RunMetrics attributes a bar or summary may name.
SIMPLE_METRICS = (
    "cost_usd",
    "output_tokens",
    "billed_input_tokens",
    "retries",
    "failed_attempts",
    "wall_ms",
    "loop_exhausted",
)
# Parameterised metrics: ``<prefix><name>``.
METRIC_PREFIXES = ("group_share:", "group_count:", "finding:")

METRICS = SIMPLE_METRICS + tuple(p + "<name>" for p in METRIC_PREFIXES)

_DIRECTIONS = ("lower", "higher")
_GROUP_LINE_RE = re.compile(r"^\s*(?:[-*]\s+)?groups?\s*:\s*([^=\s]+)=(.+?)\s*$", re.IGNORECASE)
_THRESHOLD_RE = re.compile(r"^\d+(?:\.\d+)?%?$")


class CompareError(Exception):
    """A comparison that cannot be made (unknown pipeline, no runs, bad digest)."""


def is_metric(metric: str) -> bool:
    """True when *metric* belongs to the ``METRICS`` vocabulary."""
    if metric in SIMPLE_METRICS:
        return True
    for prefix in METRIC_PREFIXES:
        if metric.startswith(prefix) and len(metric) > len(prefix):
            return True
    return False


def is_threshold(text: str) -> bool:
    """True when *text* is a number or a percentage (``0.5``, ``10``, ``20%``)."""
    return bool(_THRESHOLD_RE.match(text))


# ---------------------------------------------------------------------------
# Loading runs
# ---------------------------------------------------------------------------


@dataclass
class RunSample:
    journal_id: int
    history_id: int | None
    metrics: RunMetrics
    findings: list[Finding] = field(default_factory=list)


def resolve_state_key(pipeline: str) -> str:
    """Resolve *pipeline* to the state key whose ``.runs/`` directory exists."""
    candidates = _state_key_candidates(pipeline)
    for candidate in candidates:
        if runs_dir(candidate).is_dir():
            return candidate
    raise CompareError(f"No journaled runs for {pipeline!r}: {runs_dir(candidates[0])} does not exist.")


def load_runs(pipeline: str, last: int | None = DEFAULT_LAST) -> list[RunSample]:
    """Return the last *last* journaled runs of *pipeline* that have ``metrics.json``.

    Oldest first.  ``last=None`` returns every such run.
    """
    key = resolve_state_key(pipeline)
    base = runs_dir(key)
    ids = sorted(int(p.name) for p in base.iterdir() if p.is_dir() and p.name.isdigit())
    samples: list[RunSample] = []
    for jid in ids:
        rd = run_dir(key, jid)
        metrics = load_metrics(rd)
        if metrics is None:
            continue
        samples.append(
            RunSample(
                journal_id=jid,
                history_id=history_run_id(rd),
                metrics=metrics,
                findings=load_findings(rd),
            )
        )
    if not samples:
        raise CompareError(f"No journaled runs with metrics.json for {pipeline!r} in {base}.")
    if last is not None:
        samples = samples[-last:] if last > 0 else []
    return samples


# ---------------------------------------------------------------------------
# Groups and metric values
# ---------------------------------------------------------------------------


def parse_group(spec: str) -> tuple[str, re.Pattern[str]]:
    """Parse ``NAME=REGEX``; raise :class:`ValueError` when malformed."""
    name, sep, pattern = spec.partition("=")
    name = name.strip()
    if not sep or not name or not pattern:
        raise ValueError(f"malformed group {spec!r}: expected NAME=REGEX")
    if ":" in name or any(c.isspace() for c in name):
        raise ValueError(f"malformed group name {name!r}: no spaces or ':'")
    try:
        return name, re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"malformed group regex {pattern!r}: {exc}") from exc


def _matching_stages(metrics: RunMetrics, regex: re.Pattern[str]):
    for stage in metrics.stages:
        if regex.search(stage_name_from_node_id(stage.node_id)):
            yield stage


def group_share(metrics: RunMetrics, regex: re.Pattern[str]) -> float:
    """Cost of the stages matching *regex* as a percentage of the run's cost."""
    total = metrics.cost_usd
    if total <= 0:
        return 0.0
    return 100.0 * sum(s.cost_usd for s in _matching_stages(metrics, regex)) / total


def group_count(metrics: RunMetrics, regex: re.Pattern[str]) -> int:
    """Number of matching stage records whose status is not skipped."""
    return sum(1 for s in _matching_stages(metrics, regex) if not s.status.startswith("skipped"))


def metric_value(
    sample: RunSample,
    metric: str,
    groups: dict[str, re.Pattern[str]] | None = None,
) -> float:
    """Value of one ``METRICS`` item for one run."""
    if not is_metric(metric):
        raise CompareError(f"unknown metric {metric!r}")
    if metric in SIMPLE_METRICS:
        return float(getattr(sample.metrics, metric))
    prefix, _, name = metric.partition(":")
    if prefix == "finding":
        return float(sum(1 for f in sample.findings if f.rule == name))
    regex = (groups or {}).get(name)
    if regex is None:
        raise CompareError(f"metric {metric!r} uses group {name!r}, which is not defined")
    if prefix == "group_share":
        return group_share(sample.metrics, regex)
    return float(group_count(sample.metrics, regex))


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


@dataclass
class Stat:
    median: float
    low: float
    high: float
    n: int


def summarize(values: list[float]) -> Stat:
    return Stat(median=statistics.median(values), low=min(values), high=max(values), n=len(values))


def verdict(a: list[float], b: list[float]) -> str:
    """Compare two samples: ``too few runs``, ``within noise``, ``B lower`` or ``B higher``."""
    if len(a) < MIN_RUNS or len(b) < MIN_RUNS:
        return f"too few runs (need {MIN_RUNS})"
    sa, sb = summarize(a), summarize(b)
    if sb.low <= sa.median <= sb.high or sa.low <= sb.median <= sa.high:
        return "within noise"
    return "B lower" if sb.median < sa.median else "B higher"


@dataclass
class MetricRow:
    metric: str
    a: Stat
    b: Stat
    verdict: str


@dataclass
class Comparison:
    pipeline_a: str
    pipeline_b: str
    runs_a: list[RunSample]
    runs_b: list[RunSample]
    rows: list[MetricRow]


def compare_pipelines(
    pipeline_a: str,
    pipeline_b: str,
    *,
    last: int = DEFAULT_LAST,
    groups: dict[str, re.Pattern[str]] | None = None,
) -> Comparison:
    """Compare the last *last* journaled runs of two pipelines."""
    runs_a = load_runs(pipeline_a, last)
    runs_b = load_runs(pipeline_b, last)
    metrics = list(SUMMARY_METRICS)
    for name in groups or {}:
        metrics += [f"group_share:{name}", f"group_count:{name}"]
    rows = []
    for metric in metrics:
        va = [metric_value(s, metric, groups) for s in runs_a]
        vb = [metric_value(s, metric, groups) for s in runs_b]
        rows.append(MetricRow(metric, summarize(va), summarize(vb), verdict(va, vb)))
    return Comparison(pipeline_a, pipeline_b, runs_a, runs_b, rows)


def _fmt(value: float) -> str:
    if value == int(value) and abs(value) >= 1:
        return str(int(value))
    return f"{value:.4g}"


def _side_line(label: str, runs: list[RunSample]) -> str:
    ok = sum(1 for s in runs if s.metrics.success)
    return f"{label}: {len(runs)} runs, {ok} succeeded"


def format_comparison(cmp: Comparison) -> str:
    """Render a :class:`Comparison` as text."""
    lines = [
        _side_line(f"A {cmp.pipeline_a}", cmp.runs_a),
        _side_line(f"B {cmp.pipeline_b}", cmp.runs_b),
        "",
    ]
    width = max(len(r.metric) for r in cmp.rows)
    for row in cmp.rows:
        def side(st: Stat) -> str:
            return f"{_fmt(st.median)} ({_fmt(st.low)}–{_fmt(st.high)})"

        lines.append(f"{row.metric:<{width}}  A {side(row.a)}  B {side(row.b)}  {row.verdict}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pass bars
# ---------------------------------------------------------------------------


@dataclass
class Bar:
    metric: str
    direction: str
    threshold: str
    baseline: list[int]


@dataclass
class BarFile:
    groups: dict[str, re.Pattern[str]]
    bars: list[Bar]


def _parse_bar_line(line: str) -> Bar:
    fields: dict[str, str] = {}
    for token in line.split():
        key, sep, value = token.partition("=")
        if sep:
            fields[key] = value
    missing = [k for k in ("metric", "direction", "threshold", "baseline") if k not in fields]
    if missing:
        raise CompareError(f"pass bar line missing {', '.join(missing)}: {line!r}")
    if not is_metric(fields["metric"]):
        raise CompareError(f"pass bar names unknown metric {fields['metric']!r}")
    if fields["direction"] not in _DIRECTIONS:
        raise CompareError(f"pass bar direction must be lower or higher: {line!r}")
    if not is_threshold(fields["threshold"]):
        raise CompareError(f"pass bar threshold is not a number or percentage: {line!r}")
    try:
        baseline = [int(x) for x in fields["baseline"].split(",") if x.strip()]
    except ValueError:
        raise CompareError(f"pass bar baseline is not a list of run ids: {line!r}") from None
    if not baseline:
        raise CompareError(f"pass bar has an empty baseline: {line!r}")
    return Bar(fields["metric"], fields["direction"], fields["threshold"], baseline)


def parse_bar_text(text: str) -> BarFile:
    """Parse a digest: group declarations from the header, lines under ``## Pass bar``."""
    groups: dict[str, re.Pattern[str]] = {}
    bars: list[Bar] = []
    in_bar = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("#"):
            in_bar = stripped.lstrip("#").strip().lower() == "pass bar"
            continue
        if not stripped:
            continue
        if in_bar:
            body = re.sub(r"^[-*]\s+", "", stripped)
            if body.startswith("metric="):
                bars.append(_parse_bar_line(body))
            continue
        m = _GROUP_LINE_RE.match(raw)
        if m:
            try:
                name, regex = parse_group(f"{m.group(1)}={m.group(2)}")
            except ValueError as exc:
                raise CompareError(str(exc)) from exc
            groups[name] = regex
    if not bars:
        raise CompareError("no '## Pass bar' lines found in the digest")
    for bar in bars:
        prefix, _, name = bar.metric.partition(":")
        if prefix in ("group_share", "group_count") and name not in groups:
            raise CompareError(f"pass bar metric {bar.metric!r} uses group {name!r}, not declared in the digest header")
    return BarFile(groups, bars)


def parse_bar_file(path: Path) -> BarFile:
    return parse_bar_text(Path(path).read_text())


@dataclass
class BarResult:
    bar: Bar
    status: str  # "met" | "unproven" | "not met"
    baseline_n: int
    post_n: int
    baseline_median: float | None = None
    post_median: float | None = None
    detail: str = ""


def _improvement(bar: Bar, base_median: float, post_median: float) -> tuple[float, float]:
    """Return (improvement in the bar's direction, required improvement)."""
    gain = base_median - post_median if bar.direction == "lower" else post_median - base_median
    if bar.threshold.endswith("%"):
        pct = float(bar.threshold[:-1])
        required = abs(base_median) * pct / 100.0
    else:
        required = float(bar.threshold)
    return gain, required


def evaluate_bar(
    bar: Bar,
    baseline: list[RunSample],
    post: list[RunSample],
    groups: dict[str, re.Pattern[str]],
) -> BarResult:
    bv = [metric_value(s, bar.metric, groups) for s in baseline]
    pv = [metric_value(s, bar.metric, groups) for s in post]
    if len(pv) < MIN_RUNS:
        return BarResult(bar, "unproven", len(bv), len(pv), detail=f"fewer than {MIN_RUNS} post runs")
    sb, sp = summarize(bv), summarize(pv)
    base = dict(baseline_n=len(bv), post_n=len(pv), baseline_median=sb.median, post_median=sp.median)
    if sb.low <= sp.median <= sb.high:
        return BarResult(bar, "unproven", **base, detail="post median within the baseline range")
    gain, required = _improvement(bar, sb.median, sp.median)
    if gain > 0 and gain >= required:
        return BarResult(bar, "met", **base)
    return BarResult(bar, "not met", **base)


def check_bars(pipeline: str, barfile: BarFile) -> list[BarResult]:
    """Check every bar of a digest against the journaled runs of *pipeline*."""
    runs = load_runs(pipeline, None)
    by_hist = {s.history_id: s for s in runs if s.history_id is not None}
    results = []
    for bar in barfile.bars:
        absent = [i for i in bar.baseline if i not in by_hist]
        if absent:
            raise CompareError(
                f"baseline run ids {absent} for metric {bar.metric!r} have no journaled "
                f"metrics.json for {pipeline!r}"
            )
        baseline = [by_hist[i] for i in bar.baseline]
        newest = max(bar.baseline)
        post = [s for hid, s in sorted(by_hist.items()) if hid > newest]
        results.append(evaluate_bar(bar, baseline, post, barfile.groups))
    return results


def format_bar_results(pipeline: str, results: list[BarResult]) -> str:
    lines = [f"Pass bars for {pipeline}", ""]
    for r in results:
        bar = r.bar
        head = f"{r.status:<8} {bar.metric} {bar.direction} {bar.threshold}"
        if r.baseline_median is not None and r.post_median is not None:
            nums = f"baseline {_fmt(r.baseline_median)} (n={r.baseline_n}), post {_fmt(r.post_median)} (n={r.post_n})"
        else:
            nums = f"baseline n={r.baseline_n}, post n={r.post_n}"
        tail = f" — {r.detail}" if r.detail else ""
        lines.append(f"{head}: {nums}{tail}")
    return "\n".join(lines)

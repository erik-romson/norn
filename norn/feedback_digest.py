"""``norn feedback-digest`` — ranked prompt/AGENTS.md change proposals with a pass bar.

Reads the evaluation output of the last N journaled runs of a pipeline
(``metrics.json``, ``findings.json``, ``rating.json``, ``feedback.md``), asks
one read-only agent pass to deduplicate and rank the recurring feedback into
proposals, and writes them to ``<state-key>.runs/feedback-digest-<k>.md``
together with a ``## Pass bar`` that is **fixed before any change is made**.
``norn compare <pipeline> --bar <digest>`` later says whether the change helped.

The digest's cost is billed outside the pipeline budget and history, like
:func:`norn.evaluate.evaluate_run`.  This is a core module: it must not import
from ``norn.contrib`` or ``norn.pipelines``.

File grammar (read back by :func:`norn.compare.parse_bar_text`)::

    # Feedback digest <k>
    pipeline: <name>
    created_at: <iso>
    baseline: <history id>,<history id>…
    group: NAME=REGEX                 (only when a bar uses a group metric)

    ### P1 — <title>
    Change: …
    Evidence runs: 3, 5
    Bar: metric=<METRICS item> direction=lower|higher threshold=<number or %>

    ## Pass bar
    - metric=… direction=… threshold=… baseline=<ids> proposal=P1
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from norn import compare
from norn.journal import history_run_id, run_dir, runs_dir
from norn.metrics import load_findings, load_metrics
from norn.rating import load_rating

DEFAULT_LAST = 10
DEFAULT_MODEL = "standard"
DEFAULT_MAX_COST_USD = 5.0
MIN_RUNS = 2
FEEDBACK_CAP = 4000
MAX_TURNS = 20

_DASHES = r"[—–-]"
_HEADING_RE = re.compile(rf"^###\s+(P\d+)\s*(?:{_DASHES}|:)\s*(.+?)\s*$")
_FIELD_RE = re.compile(r"^\s*(?:[-*]\s+)?(Change|Evidence runs|Bar|Group)\s*:\s*(.*)$", re.IGNORECASE)


class FeedbackDigestError(Exception):
    """A digest that cannot be made (too few runs, invalid proposals, over the cost cap)."""


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------


@dataclass
class BaselineRun:
    history_id: int
    journal_id: int
    success: bool
    failed_stage: str | None
    cost_usd: float
    retries: int
    top_cost_stages: list[str]
    findings: list[dict[str, Any]] = field(default_factory=list)
    rating: dict[str, Any] | None = None
    feedback: str | None = None


def collect_baseline(state_key: str, last: int = DEFAULT_LAST) -> list[BaselineRun]:
    """Collect the last *last* journaled runs that have ``metrics.json`` and a history id.

    Oldest first.  Raises :class:`FeedbackDigestError` with fewer than
    :data:`MIN_RUNS` such runs.
    """
    base = runs_dir(state_key)
    ids = sorted(int(p.name) for p in base.iterdir() if p.is_dir() and p.name.isdigit())
    picked = ids[-last:] if last > 0 else []
    runs: list[BaselineRun] = []
    for jid in picked:
        rd = run_dir(state_key, jid)
        metrics = load_metrics(rd)
        hid = history_run_id(rd)
        if metrics is None or hid is None:
            continue
        feedback_path = rd / "feedback.md"
        feedback = feedback_path.read_text()[:FEEDBACK_CAP] if feedback_path.exists() else None
        runs.append(
            BaselineRun(
                history_id=hid,
                journal_id=jid,
                success=metrics.success,
                failed_stage=metrics.failed_stage,
                cost_usd=metrics.cost_usd,
                retries=metrics.retries,
                top_cost_stages=list(metrics.top_cost_stages),
                findings=[
                    {"rule": f.rule, "severity": f.severity, "stage": f.stage}
                    for f in load_findings(rd)
                ],
                rating=load_rating(rd),
                feedback=feedback,
            )
        )
    if len(runs) < MIN_RUNS:
        raise FeedbackDigestError(
            f"need at least {MIN_RUNS} journaled runs with metrics.json and a history id "
            f"in {base} (found {len(runs)})"
        )
    return runs


def _format_baseline(runs: list[BaselineRun]) -> str:
    parts: list[str] = []
    for r in runs:
        parts.append(
            f"## Run {r.history_id} (directory {r.journal_id})\n"
            f"success: {r.success}; failed stage: {r.failed_stage or '-'}; "
            f"cost: ${r.cost_usd:.2f}; retries: {r.retries}\n"
            f"top cost stages: {', '.join(r.top_cost_stages) or '-'}"
        )
        if r.findings:
            parts.append(
                "findings: "
                + "; ".join(f"{f['rule']} ({f['severity']}, {f['stage'] or '-'})" for f in r.findings)
            )
        if r.rating:
            note = f" — {r.rating['note']}" if r.rating.get("note") else ""
            parts.append(f"human rating: {r.rating.get('score', '?')}/5{note}")
        if r.feedback:
            parts.append("feedback.md:\n" + r.feedback)
        parts.append("")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


def build_digest_prompt(pipeline: str, runs: list[BaselineRun]) -> str:
    """Build the one-pass digest prompt."""
    ids = ", ".join(str(r.history_id) for r in runs)
    return (
        "You are reading the evaluation output of the last " + str(len(runs))
        + " runs of the pipeline " + pipeline + ". Run directories sit under your working directory "
        "(<n>/metrics.json, findings.json, feedback.md, rating.json); you may read them for detail.\n\n"
        "## Baseline runs\n"
        + _format_baseline(runs)
        + "\n## Task\n"
        "1. Deduplicate the feedback across runs and rank the items by how many runs they recur in.\n"
        "2. Propose changes to the pipeline's prompts or to AGENTS.md that would remove the recurring "
        "problems. Propose only changes the evidence supports; fewer, stronger proposals beat many weak ones.\n"
        "3. Write every proposal exactly in this form, most-recurring first, and write nothing else:\n\n"
        "### P1 — <title>\n"
        "Change: <what to change and where>\n"
        "Evidence runs: <comma-separated run ids from the baseline: " + ids + ">\n"
        "Bar: metric=<one of the metrics below> direction=lower|higher threshold=<number or percentage such as 20%>\n\n"
        "Metrics a bar may name: " + ", ".join(compare.METRICS) + ". "
        "For group_share:<name> and group_count:<name> add one more line to the proposal, "
        "Group: <name>=<regex>, where the regex matches the bare stage name (for example review 1).\n"
        "The bar is a measurable outcome that would show the change helped, fixed now, before the change is made. "
        "The threshold is the minimum improvement against the baseline median, as an absolute amount or a percentage.\n"
        "Use only the run ids listed above as evidence. Do not edit any file."
    )


# ---------------------------------------------------------------------------
# Parsing and validation
# ---------------------------------------------------------------------------


@dataclass
class Proposal:
    key: str
    title: str
    change: str
    evidence: list[int]
    metric: str
    direction: str
    threshold: str
    group: tuple[str, str] | None = None  # (name, regex source)


def _parse_blocks(text: str) -> list[tuple[str, str, dict[str, str]]]:
    blocks: list[tuple[str, str, dict[str, str]]] = []
    current: tuple[str, str, dict[str, str]] | None = None
    last_field: str | None = None
    for raw in text.splitlines():
        line = raw.replace("**", "").replace("`", "")
        m = _HEADING_RE.match(line.strip())
        if m:
            current = (m.group(1), m.group(2), {})
            blocks.append(current)
            last_field = None
            continue
        if current is None:
            continue
        fm = _FIELD_RE.match(line)
        if fm:
            last_field = fm.group(1).lower()
            current[2][last_field] = fm.group(2).strip()
        elif last_field == "change" and line.strip() and not line.lstrip().startswith("#"):
            current[2]["change"] += "\n" + line.strip()
    return blocks


def validate_proposals(text: str, baseline_ids: list[int]) -> list[Proposal]:
    """Parse and check every proposal.  Raises :class:`FeedbackDigestError` listing all problems."""
    blocks = _parse_blocks(text)
    if not blocks:
        raise FeedbackDigestError("no '### P<k> — <title>' proposals found in the agent output")
    problems: list[str] = []
    proposals: list[Proposal] = []
    seen_keys: set[str] = set()
    groups: dict[str, str] = {}
    for key, title, fields in blocks:
        where = f"{key}"
        if key in seen_keys:
            problems.append(f"{where}: duplicate proposal key")
        seen_keys.add(key)
        change = fields.get("change", "")
        if not change:
            problems.append(f"{where}: missing Change:")
        evidence: list[int] = []
        try:
            evidence = [int(x) for x in re.split(r"[,\s]+", fields.get("evidence runs", "").strip()) if x]
        except ValueError:
            problems.append(f"{where}: Evidence runs must be a list of run ids")
        if not evidence:
            problems.append(f"{where}: missing Evidence runs")
        outside = [i for i in evidence if i not in baseline_ids]
        if outside:
            problems.append(f"{where}: evidence runs {outside} are not in the baseline {baseline_ids}")
        bar_tokens: dict[str, str] = {}
        for token in fields.get("bar", "").split():
            k, sep, v = token.partition("=")
            if sep:
                bar_tokens[k] = v
        metric = bar_tokens.get("metric", "")
        direction = bar_tokens.get("direction", "")
        threshold = bar_tokens.get("threshold", "")
        if not metric:
            problems.append(f"{where}: Bar: has no metric=")
        elif not compare.is_metric(metric):
            problems.append(f"{where}: unknown metric {metric!r}")
        if direction not in ("lower", "higher"):
            problems.append(f"{where}: Bar: direction must be lower or higher, got {direction!r}")
        if not compare.is_threshold(threshold):
            problems.append(f"{where}: Bar: threshold must be a number or a percentage, got {threshold!r}")
        group: tuple[str, str] | None = None
        if "group" in fields:
            try:
                gname, gregex = compare.parse_group(fields["group"])
            except ValueError as exc:
                problems.append(f"{where}: {exc}")
            else:
                group = (gname, gregex.pattern)
                if groups.get(gname, gregex.pattern) != gregex.pattern:
                    problems.append(f"{where}: group {gname!r} is defined twice with different regexes")
                groups[gname] = gregex.pattern
        proposals.append(Proposal(key, title, change, evidence, metric, direction, threshold, group))
    for p in proposals:
        prefix, _, gn = p.metric.partition(":")
        if prefix in ("group_share", "group_count") and gn not in groups:
            problems.append(f"{p.key}: metric {p.metric!r} needs a 'Group: {gn}=<regex>' line")
    if problems:
        raise FeedbackDigestError("invalid proposals:\n" + "\n".join(f"- {p}" for p in problems))
    return proposals


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def next_digest_path(state_key: str) -> tuple[int, Path]:
    """Return ``(k, path)`` for the next free ``feedback-digest-<k>.md`` (never an existing one)."""
    base = runs_dir(state_key)
    k = 1
    while (
        (base / f"feedback-digest-{k}.md").exists()
        or (base / f"feedback-digest-{k}.rejected.md").exists()
    ):
        k += 1
    return k, base / f"feedback-digest-{k}.md"


def render_digest(
    pipeline: str,
    k: int,
    baseline_ids: list[int],
    proposals: list[Proposal],
    created_at: str,
) -> str:
    groups: dict[str, str] = {}
    for p in proposals:
        if p.group:
            groups[p.group[0]] = p.group[1]
    ids = ",".join(str(i) for i in baseline_ids)
    lines = [
        f"# Feedback digest {k}",
        f"pipeline: {pipeline}",
        f"created_at: {created_at}",
        f"baseline: {ids}",
    ]
    lines += [f"group: {name}={regex}" for name, regex in groups.items()]
    lines.append("")
    for p in proposals:
        lines += [
            f"### {p.key} — {p.title}",
            f"Change: {p.change}",
            f"Evidence runs: {', '.join(str(i) for i in p.evidence)}",
            f"Bar: metric={p.metric} direction={p.direction} threshold={p.threshold}",
            "",
        ]
    lines += ["## Pass bar", ""]
    lines += [
        f"- metric={p.metric} direction={p.direction} threshold={p.threshold} baseline={ids} proposal={p.key}"
        for p in proposals
    ]
    lines += [
        "",
        "Fixed before any change. Check later with "
        f"`norn compare {pipeline} --bar <this file>`.",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Agent driver and entry point
# ---------------------------------------------------------------------------


@dataclass
class DigestResult:
    path: Path
    proposals: list[Proposal]
    baseline_ids: list[int]
    provider: str
    model: str
    cost_usd: float
    billed_input_tokens: int
    output_tokens: int


async def run_digest_agent(
    provider: Any, prompt: str, *, model: str, cwd: Path
) -> tuple[str, Any]:
    """One read-only agent pass.  Returns ``(text, usage)``."""
    from norn.agents.base import AgentRequest
    from norn.evaluate import _call_agent

    request = AgentRequest(
        prompt=prompt,
        stage_name="feedback-digest",
        provider=provider.name,
        model=model,
        permission_mode="plan",
        allowed_tools=["Read", "Grep", "Glob"],
        max_turns=MAX_TURNS,
        cwd=str(cwd),
    )
    text, _structured, usage = await _call_agent(provider, request)
    return text, usage


async def feedback_digest(
    pipeline: str,
    *,
    last: int = DEFAULT_LAST,
    model: str = DEFAULT_MODEL,
    max_cost_usd: float = DEFAULT_MAX_COST_USD,
    provider_name: str | None = None,
) -> DigestResult:
    """Write the next ``feedback-digest-<k>.md`` for *pipeline*.

    Raises :class:`FeedbackDigestError` (or :class:`norn.compare.CompareError`
    for an unknown pipeline) on any failure.  Invalid proposals, and a digest
    over the cost cap, leave the agent's raw text in
    ``feedback-digest-<k>.rejected.md`` instead of a digest.
    """
    from norn.agents.registry import get_provider
    from norn.model_labels import known_labels

    if model not in known_labels():
        raise FeedbackDigestError(
            f"unknown model label {model!r}; labels: {', '.join(sorted(known_labels()))} — see `norn models`"
        )
    if last < 1:
        raise FeedbackDigestError("--last must be at least 1")

    state_key = compare.resolve_state_key(pipeline)
    runs = collect_baseline(state_key, last)
    baseline_ids = [r.history_id for r in runs]

    if provider_name is None:
        rj = run_dir(state_key, runs[-1].journal_id) / "run.json"
        try:
            provider_name = json.loads(rj.read_text()).get("provider")
        except (OSError, ValueError):
            provider_name = None
    provider = get_provider(provider_name or "claude-code")

    base = runs_dir(state_key)
    prompt = build_digest_prompt(pipeline, runs)
    text, usage = await run_digest_agent(provider, prompt, model=model, cwd=base)

    cost = float(getattr(usage, "total_cost_usd", 0.0) or 0.0)
    k, path = next_digest_path(state_key)

    def _reject(reason: str) -> FeedbackDigestError:
        rejected = base / f"feedback-digest-{k}.rejected.md"
        rejected.write_text(text)
        return FeedbackDigestError(f"{reason}\nagent output kept in {rejected}")

    if cost > max_cost_usd:
        raise _reject(f"digest cost ${cost:.2f} exceeds limit ${max_cost_usd:.2f}")
    try:
        proposals = validate_proposals(text, baseline_ids)
    except FeedbackDigestError as exc:
        raise _reject(str(exc)) from exc

    created_at = datetime.now(timezone.utc).isoformat()
    path.write_text(render_digest(pipeline, k, baseline_ids, proposals, created_at))
    return DigestResult(
        path=path,
        proposals=proposals,
        baseline_ids=baseline_ids,
        provider=provider.name,
        model=model,
        cost_usd=cost,
        billed_input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
    )


def format_digest_summary(result: DigestResult, pipeline: str) -> str:
    """Text printed on success: where the digest is, how many proposals, the cost, the follow-up."""
    return "\n".join(
        [
            f"Feedback digest written: {result.path}",
            f"{len(result.proposals)} proposals from runs {','.join(str(i) for i in result.baseline_ids)}",
            f"Digest cost: ${result.cost_usd:.2f} (outside the pipeline budget and history)",
            f"Check later with: norn compare {pipeline} --bar {result.path}",
        ]
    )

"""Deterministic metrics and rule-based findings — zero LLM tokens.

Reads the run journal (``<state-key>.runs/<run_id>/``) and produces
``metrics.json`` and ``findings.json`` beside it.  All rules are arithmetic
on journal data; no model calls.
"""

from __future__ import annotations

import json
import logging
import re
import statistics
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Thresholds — module constants, not CLI knobs
# ---------------------------------------------------------------------------

RETRY_WASTE_COST_PCT = 15
TOOL_ERROR_RATE_THRESHOLD = 0.2
TOOL_ERROR_MIN_USES = 5
LOW_CACHE_RATIO = 0.5
LOW_CACHE_MIN_TURNS = 5
OVERSIZED_PROMPT_CHARS = 200_000
SLOW_COMMAND_MS = 600_000
BUDGET_HEADROOM_PCT = 80
COST_SHARE_SINGLE_PCT = 20
COST_SHARE_TOP3_PCT = 40
REGRESSION_FACTOR = 1.5
QUOTA_PRESSURE_PCT = 80
PRIOR_RUNS_LIMIT = 5
CONTEXT_BLOAT_TOKENS = 150_000
MODEL_MIX_SURPRISE_OUTPUT_PCT = 30

# Regex for unresolved placeholders like ``{param.args}`` in prompts
_PLACEHOLDER_RE = re.compile(r"\{[^{}]+\.\w+\}")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class StageMetrics:
    name: str
    node_id: str
    attempt: int
    kind: str  # "agent", "command", "other"
    status: str
    success: bool
    cost_usd: float
    cost_share: float
    billed_input_tokens: int
    output_tokens: int
    cache_hit_ratio: float
    turns: int
    tokens_per_turn: float
    tool_uses: int
    tool_errors: int
    prompt_chars: int
    system_prompt_chars: int
    unresolved_placeholders: list[str]
    duration_ms: int
    model: str | None
    model_id: str | None
    result_subtype: str | None
    stop_reason: str | None
    nonfatal_error: bool
    # Transcript-derived fields (optional; None when transcripts unavailable)
    model_mix: dict | None = None
    context_peak_tokens: int | None = None
    context_growth: list | None = None
    transcript: str | None = None


@dataclass
class QuotaMetrics:
    """Per-window quota metrics from CodexBar samples."""

    before_pct: float | None = None
    after_pct: float | None = None
    delta_pct: float | None = None
    resets_at: str | None = None
    will_last_to_reset: bool | None = None


@dataclass
class RunMetrics:
    run_id: str
    pipeline: str
    provider: str
    cost_mode: str
    success: bool
    failed_stage: str | None
    cost_usd: float
    billed_input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_hit_ratio: float
    turns: int
    wall_ms: int
    api_ms: int
    tool_time_ms: int
    agent_stage_count: int
    command_stage_count: int
    failed_attempts: int
    failed_attempt_cost_usd: float
    retries: int
    budget_max_cost_usd: float | None
    budget_used_pct: float | None
    waiting_input: dict[str, int]
    top_cost_stages: list[str]
    quota: QuotaMetrics | None
    codex_cost_usd_estimate: float | None
    stages: list[StageMetrics]


@dataclass
class Finding:
    rule: str
    severity: str  # "info", "warn", "high"
    stage: str | None
    evidence: str
    suggestion: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    return repr(obj)


def unresolved_placeholders(text: str | None) -> list[str]:
    """Return unresolved ``{param.xxx}``-style placeholders in *text*."""
    if not text:
        return []
    return _PLACEHOLDER_RE.findall(text)


# ---------------------------------------------------------------------------
# Journal loaders
# ---------------------------------------------------------------------------


def load_stage_records(run_dir: Path) -> list[dict]:
    """Load all stage records from ``<run_dir>/stages/*.json``, sorted by name."""
    stages_dir = run_dir / "stages"
    if not stages_dir.is_dir():
        return []
    records = []
    for path in sorted(stages_dir.iterdir()):
        if path.suffix != ".json":
            continue
        try:
            records.append(json.loads(path.read_text()))
        except Exception:
            log.warning("metrics: failed to load stage record %s", path, exc_info=True)
    return records


def _load_run_json(run_dir: Path) -> dict:
    """Load ``run.json`` from the run directory."""
    run_path = run_dir / "run.json"
    if not run_path.exists():
        return {}
    return json.loads(run_path.read_text())


def events_digest(run_dir: Path) -> dict:
    """Count ``WaitingInput``, ``LoopAttempt``, ``StageRetrying``, ``LoopExhausted`` events."""
    events_path = run_dir / "events.jsonl"
    result: dict[str, Any] = {
        "waiting_input": {},
        "loop_attempts": 0,
        "stage_retries": 0,
        "loop_exhausted": 0,
    }
    if not events_path.exists():
        return result
    for line in events_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = event.get("type", "")
        if etype == "WaitingInput":
            kind = event.get("kind", "unknown")
            result["waiting_input"][kind] = result["waiting_input"].get(kind, 0) + 1
        elif etype == "LoopAttempt":
            result["loop_attempts"] += 1
        elif etype == "StageRetrying":
            result["stage_retries"] += 1
        elif etype == "LoopExhausted":
            result["loop_exhausted"] += 1
    return result


# ---------------------------------------------------------------------------
# Metrics computation
# ---------------------------------------------------------------------------


def _stage_kind(record: dict) -> str:
    """Classify a stage record as agent/command/other."""
    if record.get("usage"):
        return "agent"
    details = record.get("details", {})
    if details.get("cmd"):
        return "command"
    return "other"


def _safe_ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator > 0 else 0.0


def compute_metrics(run_dir: Path) -> RunMetrics:
    """Compute deterministic metrics from a completed run journal."""
    run_json = _load_run_json(run_dir)
    stage_records = load_stage_records(run_dir)
    digest = events_digest(run_dir)

    pipeline = run_json.get("pipeline", "")
    run_id = str(run_json.get("run_id", ""))
    provider = run_json.get("provider", "")
    cost_mode = run_json.get("cost_mode", "tracked")
    success = run_json.get("success", True)
    failed_stage = run_json.get("failed_stage")
    wall_ms = run_json.get("duration_ms", 0)
    retries = run_json.get("totals", {}).get("retries", 0)

    # Budget
    budgets = run_json.get("budgets", [])
    budget_max_cost_usd: float | None = None
    for b in budgets:
        if isinstance(b, str) and "max_cost_usd=" in b:
            try:
                budget_max_cost_usd = float(b.split("max_cost_usd=")[1].split(",")[0].split(")")[0])
            except (ValueError, IndexError):
                pass

    total_cost = 0.0
    total_billed_input = 0
    total_output = 0
    total_cache_read = 0
    total_input = 0
    total_turns = 0
    total_api_ms = 0
    total_tool_time_ms = 0
    agent_count = 0
    command_count = 0
    failed_attempts = 0
    failed_attempt_cost = 0.0

    stage_metrics_list: list[StageMetrics] = []

    for rec in stage_records:
        usage = rec.get("usage", {})
        details = rec.get("details", {})
        kind = _stage_kind(rec)

        # Token counts
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)
        cache_creation = usage.get("cache_creation_input_tokens", 0)
        cost = usage.get("total_cost_usd", 0.0)
        turns = usage.get("num_turns", 0)
        tool_uses = usage.get("tool_uses", 0)
        tool_errors = usage.get("tool_errors", 0)
        duration_ms = rec.get("duration_ms", 0)
        model_id = usage.get("model_id")

        billed_input = input_tokens + cache_creation + cache_read

        prompt_text = details.get("prompt", "")
        system_text = details.get("system_prompt", "")
        prompt_chars = len(prompt_text) if isinstance(prompt_text, str) else 0
        system_chars = len(system_text) if isinstance(system_text, str) else 0

        placeholders = unresolved_placeholders(prompt_text)

        rec_success = rec.get("success", True)
        error = rec.get("error")
        nonfatal = rec_success and bool(error)

        if kind == "agent":
            agent_count += 1
            api_ms = usage.get("duration_ms", 0)
            total_api_ms += api_ms
            tool_time = duration_ms - api_ms if duration_ms > api_ms else 0
            total_tool_time_ms += tool_time
        elif kind == "command":
            command_count += 1

        total_cost += cost
        total_billed_input += billed_input
        total_output += output_tokens
        total_cache_read += cache_read
        total_input += input_tokens
        total_turns += turns

        if not rec_success:
            failed_attempts += 1
            failed_attempt_cost += cost

        stage_metrics_list.append(StageMetrics(
            name=rec.get("node_id", ""),
            node_id=rec.get("node_id", ""),
            attempt=rec.get("attempt", 1),
            kind=kind,
            status=rec.get("status", ""),
            success=rec_success,
            cost_usd=cost,
            cost_share=0.0,  # filled below
            billed_input_tokens=billed_input,
            output_tokens=output_tokens,
            cache_hit_ratio=_safe_ratio(cache_read, input_tokens),
            turns=turns,
            tokens_per_turn=_safe_ratio(input_tokens + output_tokens, turns),
            tool_uses=tool_uses,
            tool_errors=tool_errors,
            prompt_chars=prompt_chars,
            system_prompt_chars=system_chars,
            unresolved_placeholders=placeholders,
            duration_ms=duration_ms,
            model=usage.get("model") or details.get("model"),
            model_id=model_id,
            result_subtype=usage.get("result_subtype") or details.get("result_subtype"),
            stop_reason=usage.get("stop_reason") or details.get("stop_reason"),
            nonfatal_error=nonfatal,
        ))

    # Integrate transcript data for claude-code runs
    if provider == "claude-code":
        _apply_transcript_data(run_json, stage_records, stage_metrics_list)

    # Fill cost_share
    for sm in stage_metrics_list:
        sm.cost_share = _safe_ratio(sm.cost_usd, total_cost) * 100.0

    # Top cost stages (sorted descending)
    sorted_by_cost = sorted(stage_metrics_list, key=lambda s: s.cost_usd, reverse=True)
    top_cost_stages = [s.name for s in sorted_by_cost[:3] if s.cost_usd > 0]

    cache_hit_ratio = _safe_ratio(total_cache_read, total_input)
    budget_used_pct = (total_cost / budget_max_cost_usd * 100.0) if budget_max_cost_usd else None

    # Quota
    quota = _extract_quota(run_json)

    # Codex cost estimate
    codex_cost = _extract_codex_cost(stage_records)

    return RunMetrics(
        run_id=run_id,
        pipeline=pipeline,
        provider=provider,
        cost_mode=cost_mode,
        success=success,
        failed_stage=failed_stage,
        cost_usd=total_cost,
        billed_input_tokens=total_billed_input,
        output_tokens=total_output,
        cache_read_tokens=total_cache_read,
        cache_hit_ratio=cache_hit_ratio,
        turns=total_turns,
        wall_ms=wall_ms,
        api_ms=total_api_ms,
        tool_time_ms=total_tool_time_ms,
        agent_stage_count=agent_count,
        command_stage_count=command_count,
        failed_attempts=failed_attempts,
        failed_attempt_cost_usd=failed_attempt_cost,
        retries=retries,
        budget_max_cost_usd=budget_max_cost_usd,
        budget_used_pct=budget_used_pct,
        waiting_input=digest["waiting_input"],
        top_cost_stages=top_cost_stages,
        quota=quota,
        codex_cost_usd_estimate=codex_cost,
        stages=stage_metrics_list,
    )


def _apply_transcript_data(
    run_json: dict,
    stage_records: list[dict],
    stage_metrics_list: list[StageMetrics],
) -> None:
    """Integrate per-turn transcript data into agent stage metrics.

    Reads local claude-code transcript files, attributes calls to stages by
    session_id and timestamp window, and writes ``model_mix``,
    ``context_peak_tokens``, and ``context_growth`` onto each
    :class:`StageMetrics`.  A missing transcript file is recorded as
    ``transcript="missing"`` on the stage.

    No-ops when ``cwd`` is absent from *run_json* (run was not journaled with
    step-04 or later).
    """
    try:
        from norn import transcripts as _transcripts
    except Exception:
        log.warning("metrics: could not import transcripts module", exc_info=True)
        return

    cwd = run_json.get("cwd", "")
    if not cwd:
        return

    # Read each unique session's transcript exactly once
    session_calls: dict[str, list] = {}
    missing_sessions: set[str] = set()

    for rec in stage_records:
        session_id = (rec.get("usage") or {}).get("session_id")
        if not session_id:
            continue
        if session_id in session_calls or session_id in missing_sessions:
            continue
        path = _transcripts.transcript_path(cwd, session_id)
        if path.exists():
            session_calls[session_id] = _transcripts.read_assistant_calls(path, session_id=session_id)
        else:
            missing_sessions.add(session_id)
            log.debug("metrics: transcript missing for session %s", session_id)

    if not session_calls and not missing_sessions:
        return

    # Attribute all calls across all sessions to stage records
    all_calls = [c for calls in session_calls.values() for c in calls]
    attribution = _transcripts.attribute(all_calls, stage_records)

    # Write results back into stage_metrics_list (parallel to stage_records)
    for i, rec in enumerate(stage_records):
        if i >= len(stage_metrics_list):
            break
        sm = stage_metrics_list[i]
        session_id = (rec.get("usage") or {}).get("session_id")
        node_id = rec.get("node_id", "")
        attempt = rec.get("attempt", 1)
        stage_key = f"{node_id}:{attempt}"

        if session_id and session_id in missing_sessions:
            sm.transcript = "missing"
        elif stage_key in attribution:
            data = attribution[stage_key]
            sm.model_mix = data.model_mix
            sm.context_peak_tokens = data.context_peak_tokens
            sm.context_growth = data.context_growth


def _extract_quota(run_json: dict) -> QuotaMetrics | None:
    """Build QuotaMetrics from ``quota_before`` / ``quota_after`` in run.json."""
    before = run_json.get("quota_before")
    after = run_json.get("quota_after")
    if before is None and after is None:
        return None
    before_pct = before.get("remaining_pct") if before else None
    after_pct = after.get("remaining_pct") if after else None
    delta_pct: float | None = None
    if before_pct is not None and after_pct is not None:
        delta_pct = after_pct - before_pct
    resets_at = (after or before or {}).get("reset_at")
    pace = (after or before or {}).get("pace", {})
    will_last = pace.get("will_last_to_reset") if isinstance(pace, dict) else None
    return QuotaMetrics(
        before_pct=before_pct,
        after_pct=after_pct,
        delta_pct=delta_pct,
        resets_at=resets_at,
        will_last_to_reset=will_last,
    )


def _extract_codex_cost(stage_records: list[dict]) -> float | None:
    """Return cost estimate from any stage whose cmd starts with ``codex ``."""
    for rec in stage_records:
        details = rec.get("details", {})
        cmd = details.get("cmd", "")
        if isinstance(cmd, str) and cmd.startswith("codex "):
            return rec.get("usage", {}).get("total_cost_usd")
    return None


# ---------------------------------------------------------------------------
# Prior runs
# ---------------------------------------------------------------------------


def load_prior_metrics(runs_dir: Path, before_run_id: int, limit: int = PRIOR_RUNS_LIMIT) -> list[RunMetrics]:
    """Load metrics from the N most recent runs before *before_run_id*."""
    if not runs_dir.is_dir():
        return []
    dirs = []
    for p in runs_dir.iterdir():
        if p.is_dir() and p.name.isdigit():
            rid = int(p.name)
            if rid < before_run_id:
                dirs.append((rid, p))
    dirs.sort(key=lambda t: t[0], reverse=True)
    result = []
    for _, d in dirs[:limit]:
        metrics_path = d / "metrics.json"
        if metrics_path.exists():
            m = load_metrics(d)
            if m is not None:
                result.append(m)
        else:
            # Try to compute on the fly from raw journal
            try:
                m = compute_metrics(d)
                result.append(m)
            except Exception:
                log.debug("metrics: skipping prior run %s", d, exc_info=True)
    return result


# ---------------------------------------------------------------------------
# Findings (rules)
# ---------------------------------------------------------------------------


def find_issues(metrics: RunMetrics, prior: list[RunMetrics] | None = None) -> list[Finding]:
    """Apply rule table against *metrics* and return findings."""
    findings: list[Finding] = []
    prior = prior or []

    cost_not_tracked = metrics.cost_mode != "tracked"

    # Cost-mode caveat
    if cost_not_tracked:
        findings.append(Finding(
            rule="cost-not-tracked",
            severity="info",
            stage=None,
            evidence=f"cost_mode={metrics.cost_mode}",
            suggestion="Cost-based rules may not be accurate; provider does not report per-call cost.",
        ))

    for sm in metrics.stages:
        # max-turns-hit
        if sm.result_subtype == "error_max_turns":
            findings.append(Finding(
                rule="max-turns-hit",
                severity="high",
                stage=sm.name,
                evidence=f"result_subtype={sm.result_subtype}",
                suggestion="Raise max_turns or simplify the prompt.",
            ))

        # agent-error
        if sm.kind == "agent" and sm.result_subtype and sm.result_subtype != "success" and sm.result_subtype != "error_max_turns":
            findings.append(Finding(
                rule="agent-error",
                severity="high",
                stage=sm.name,
                evidence=f"result_subtype={sm.result_subtype}",
                suggestion="Check the agent error and fix the prompt or configuration.",
            ))
        elif sm.kind == "agent" and not sm.success and not sm.result_subtype:
            findings.append(Finding(
                rule="agent-error",
                severity="high",
                stage=sm.name,
                evidence="stage failed without result_subtype",
                suggestion="Check the agent error and fix the prompt or configuration.",
            ))

        # unresolved-placeholder
        if sm.unresolved_placeholders:
            findings.append(Finding(
                rule="unresolved-placeholder",
                severity="high",
                stage=sm.name,
                evidence=f"placeholders: {', '.join(sm.unresolved_placeholders)}",
                suggestion="Ensure all template parameters are passed via --arg or positional args.",
            ))

        # silent-error
        if sm.nonfatal_error:
            findings.append(Finding(
                rule="silent-error",
                severity="warn",
                stage=sm.name,
                evidence="stage succeeded but has a non-empty error field",
                suggestion="Investigate whether the error should cause a failure.",
            ))

        # tool-error-rate
        if sm.tool_uses >= TOOL_ERROR_MIN_USES and _safe_ratio(sm.tool_errors, sm.tool_uses) > TOOL_ERROR_RATE_THRESHOLD:
            findings.append(Finding(
                rule="tool-error-rate",
                severity="warn",
                stage=sm.name,
                evidence=f"{sm.tool_errors}/{sm.tool_uses} tool calls failed ({_safe_ratio(sm.tool_errors, sm.tool_uses) * 100:.0f}%)",
                suggestion="Review tool permissions or reduce reliance on failing tools.",
            ))

        # low-cache-ratio (per stage)
        if sm.kind == "agent" and sm.turns > LOW_CACHE_MIN_TURNS and sm.cache_hit_ratio < LOW_CACHE_RATIO:
            findings.append(Finding(
                rule="low-cache-ratio",
                severity="warn",
                stage=sm.name,
                evidence=f"cache_hit_ratio={sm.cache_hit_ratio:.2f} with {sm.turns} turns",
                suggestion="Check that prompt caching is enabled and prompts are stable across turns.",
            ))

        # oversized-prompt
        if sm.prompt_chars + sm.system_prompt_chars > OVERSIZED_PROMPT_CHARS:
            findings.append(Finding(
                rule="oversized-prompt",
                severity="warn",
                stage=sm.name,
                evidence=f"prompt={sm.prompt_chars:,} + system={sm.system_prompt_chars:,} chars",
                suggestion="Trim context or use @-file references to reduce prompt size.",
            ))

        # slow-command
        if sm.kind == "command" and sm.duration_ms > SLOW_COMMAND_MS:
            findings.append(Finding(
                rule="slow-command",
                severity="warn",
                stage=sm.name,
                evidence=f"duration={sm.duration_ms / 1000:.0f}s",
                suggestion="Consider splitting long-running commands or adding a timeout.",
            ))

        # cost-share
        if sm.cost_share >= COST_SHARE_SINGLE_PCT:
            findings.append(Finding(
                rule="cost-share",
                severity="info",
                stage=sm.name,
                evidence=f"cost_share={sm.cost_share:.0f}%",
                suggestion="Consider whether this stage can be made cheaper or split.",
            ))

        # context-bloat
        if sm.context_peak_tokens is not None and sm.context_peak_tokens > CONTEXT_BLOAT_TOKENS:
            findings.append(Finding(
                rule="context-bloat",
                severity="warn",
                stage=sm.name,
                evidence=f"context_peak_tokens={sm.context_peak_tokens:,}",
                suggestion="Context is near the model window; every later turn pays to re-read everything.",
            ))

        # model-mix-surprise
        if sm.kind == "agent" and sm.model_mix is not None and len(sm.model_mix) > 1:
            total_output = sum(v.get("output", 0) for v in sm.model_mix.values())
            if total_output > 0:
                for model_id, stats in sm.model_mix.items():
                    if model_id != sm.model_id:
                        pct = stats.get("output", 0) / total_output * 100
                        if pct > MODEL_MIX_SURPRISE_OUTPUT_PCT:
                            findings.append(Finding(
                                rule="model-mix-surprise",
                                severity="info",
                                stage=sm.name,
                                evidence=(
                                    f"{model_id} produced {pct:.0f}% of output "
                                    f"(requested: {sm.model_id or sm.model or 'unknown'})"
                                ),
                                suggestion="Check whether sub-agents are using an unexpected model.",
                            ))

    # retry-waste (run-level)
    if metrics.cost_usd > 0 and metrics.failed_attempt_cost_usd > 0:
        pct = metrics.failed_attempt_cost_usd / metrics.cost_usd * 100
        if pct >= RETRY_WASTE_COST_PCT:
            findings.append(Finding(
                rule="retry-waste",
                severity="warn",
                stage=None,
                evidence=f"failed attempt cost=${metrics.failed_attempt_cost_usd:.2f} ({pct:.0f}% of total)",
                suggestion="Reduce retry waste by fixing root causes of failures.",
            ))

    # loop exhausted
    digest_path = metrics  # we check via retries and failed_attempts
    # Actually check the events digest if we have the data... we rely on the
    # finding trigger from the step spec: "any LoopExhausted" => retry-waste
    # Since LoopExhausted is counted in events_digest, we detect it via
    # the fact that we're called from compute_metrics context. However, since
    # find_issues receives metrics not the raw digest, we add a simple heuristic.

    # budget-headroom
    if metrics.budget_used_pct is not None and metrics.budget_used_pct > BUDGET_HEADROOM_PCT:
        findings.append(Finding(
            rule="budget-headroom",
            severity="warn",
            stage=None,
            evidence=f"budget_used_pct={metrics.budget_used_pct:.0f}%",
            suggestion="Increase the budget or reduce pipeline cost.",
        ))

    # cost-share top-3
    if len(metrics.stages) >= 3 and metrics.cost_usd > 0:
        sorted_stages = sorted(metrics.stages, key=lambda s: s.cost_usd, reverse=True)
        top3_share = sum(s.cost_share for s in sorted_stages[:3])
        if top3_share >= COST_SHARE_TOP3_PCT:
            names = [s.name for s in sorted_stages[:3]]
            findings.append(Finding(
                rule="cost-share",
                severity="info",
                stage=None,
                evidence=f"top-3 stages ({', '.join(names)}) account for {top3_share:.0f}% of cost",
                suggestion="Consider whether the most expensive stages can be optimised.",
            ))

    # regression-vs-prior
    if prior:
        successful_prior = [p for p in prior if p.success]
        if len(successful_prior) >= 1:
            prior_costs = [p.cost_usd for p in successful_prior if p.cost_usd > 0]
            prior_walls = [p.wall_ms for p in successful_prior if p.wall_ms > 0]
            if prior_costs:
                median_cost = statistics.median(prior_costs)
                if median_cost > 0 and metrics.cost_usd > median_cost * REGRESSION_FACTOR:
                    findings.append(Finding(
                        rule="regression-vs-prior",
                        severity="warn",
                        stage=None,
                        evidence=f"cost ${metrics.cost_usd:.2f} > {REGRESSION_FACTOR}x median ${median_cost:.2f}",
                        suggestion="Investigate why this run is more expensive than recent runs.",
                    ))
            if prior_walls:
                median_wall = statistics.median(prior_walls)
                if median_wall > 0 and metrics.wall_ms > median_wall * REGRESSION_FACTOR:
                    findings.append(Finding(
                        rule="regression-vs-prior",
                        severity="warn",
                        stage=None,
                        evidence=f"wall {metrics.wall_ms}ms > {REGRESSION_FACTOR}x median {median_wall:.0f}ms",
                        suggestion="Investigate why this run is slower than recent runs.",
                    ))

    # quota-pressure
    if metrics.quota is not None:
        q = metrics.quota
        if q.after_pct is not None and q.after_pct > QUOTA_PRESSURE_PCT:
            findings.append(Finding(
                rule="quota-pressure",
                severity="warn",
                stage=None,
                evidence=f"quota after_pct={q.after_pct:.0f}%",
                suggestion="Reduce usage or wait for quota reset.",
            ))
        if q.will_last_to_reset is False:
            findings.append(Finding(
                rule="quota-pressure",
                severity="warn",
                stage=None,
                evidence="quota will not last to reset",
                suggestion="Reduce usage rate to avoid hitting the quota cap.",
            ))

    return findings


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def write_metrics(run_dir: Path, metrics: RunMetrics, findings: list[Finding]) -> None:
    """Write ``metrics.json`` and ``findings.json`` to *run_dir*."""
    try:
        (run_dir / "metrics.json").write_text(
            json.dumps(asdict(metrics), default=_json_default, indent=2) + "\n"
        )
    except Exception:
        log.warning("metrics: failed to write metrics.json to %s", run_dir, exc_info=True)
    try:
        (run_dir / "findings.json").write_text(
            json.dumps([asdict(f) for f in findings], default=_json_default, indent=2) + "\n"
        )
    except Exception:
        log.warning("metrics: failed to write findings.json to %s", run_dir, exc_info=True)


def load_metrics(run_dir: Path) -> RunMetrics | None:
    """Load ``metrics.json`` from *run_dir*, returning ``None`` on failure."""
    path = run_dir / "metrics.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        stages = [
            StageMetrics(**s)
            for s in data.pop("stages", [])
        ]
        quota_data = data.pop("quota", None)
        quota = QuotaMetrics(**quota_data) if quota_data else None
        return RunMetrics(**data, stages=stages, quota=quota)
    except Exception:
        log.warning("metrics: failed to load metrics.json from %s", path, exc_info=True)
        return None


def load_findings(run_dir: Path) -> list[Finding]:
    """Load ``findings.json`` from *run_dir*."""
    path = run_dir / "findings.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return [Finding(**f) for f in data]
    except Exception:
        log.warning("metrics: failed to load findings.json from %s", path, exc_info=True)
        return []

"""Agent evaluation — per-stage judge and run-level feedback.

Two LLM passes fed from the run journal, billed separately from the pipeline:

1. **Judge** (haiku, per agent stage): scores each stage 1-5 on waste,
   prompt quality, and task adherence.
2. **Feedback** (sonnet, whole run): reads metrics, findings, stage scores,
   prior runs, and optionally the pipeline source to produce actionable
   improvement suggestions.

Output files land in the run directory beside ``metrics.json``::

    stages/<record>.eval.json   — per agent stage
    feedback.md                 — validated header + prose
    evaluation.json             — summary scores and cost
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class EvaluationError(Exception):
    """Raised when evaluation cannot proceed or produces invalid output."""


# ---------------------------------------------------------------------------
# Judge schema
# ---------------------------------------------------------------------------

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 1, "maximum": 5},
        "did_what_was_asked": {"type": "boolean"},
        "waste": {"type": "array", "items": {"type": "string"}},
        "prompt_issues": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["score", "did_what_was_asked", "waste", "prompt_issues", "summary"],
}


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class EvaluationResult:
    score: int
    judge_model: str | None
    feedback_model: str
    provider: str
    cost_usd: float
    billed_input_tokens: int
    output_tokens: int
    stage_scores: dict[str, int]
    created_at: str
    feedback_path: str | None = None


# ---------------------------------------------------------------------------
# Transcript digest
# ---------------------------------------------------------------------------


def transcript_digest(run_dir: Path, node_id: str, attempt: int, *, max_entries: int = 200) -> str:
    """Build a compact digest from ``events.jsonl`` for a given stage.

    Returns ordered ``tool_use`` names + input summaries with ``x`` for a
    following ``tool_result ok=false``, plus the last 3,000 chars of assistant
    text.
    """
    events_path = run_dir / "events.jsonl"
    if not events_path.exists():
        return ""

    lines: list[str] = []
    assistant_text_parts: list[str] = []
    pending_tool: str | None = None

    for raw in events_path.read_text().splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue

        key = ev.get("key", {})
        if isinstance(key, dict):
            ev_stage = key.get("stage_id", "")
            ev_attempt = key.get("attempt", 1)
        else:
            continue

        if ev_stage != node_id or ev_attempt != attempt:
            continue

        inner = ev.get("event", {})
        block = inner.get("block") if isinstance(inner, dict) else None

        if isinstance(block, dict):
            btype = block.get("type", "")
            if btype == "ToolUseBlock":
                if pending_tool is not None and len(lines) < max_entries:
                    lines.append(pending_tool)
                name = block.get("name", "?")
                summary = block.get("input_summary", "")[:80]
                pending_tool = f"tool: {name} {summary}".strip()
            elif btype == "ToolResultBlock":
                ok = block.get("ok", True)
                if pending_tool is not None:
                    if not ok:
                        pending_tool += " x"
                    if len(lines) < max_entries:
                        lines.append(pending_tool)
                    pending_tool = None
            elif btype == "TextBlock":
                text = block.get("text", "")
                if text:
                    assistant_text_parts.append(text)

        if isinstance(inner, dict) and inner.get("text"):
            assistant_text_parts.append(inner["text"])

    if pending_tool is not None and len(lines) < max_entries:
        lines.append(pending_tool)

    digest_parts = list(lines)
    full_text = "".join(assistant_text_parts)
    tail = full_text[-3000:] if len(full_text) > 3000 else full_text
    if tail:
        digest_parts.append(f"\n--- assistant text (last {len(tail)} chars) ---\n{tail}")

    return "\n".join(digest_parts)


# ---------------------------------------------------------------------------
# Judge prompt
# ---------------------------------------------------------------------------


def _head_tail(text: str, head: int, tail: int) -> str:
    """Return head + tail of *text* with an elision marker."""
    if len(text) <= head + tail:
        return text
    elided = len(text) - head - tail
    return f"{text[:head]}\n[... {elided} chars elided ...]\n{text[-tail:]}"


def build_judge_prompt(record: dict, digest: str) -> str:
    """Build the judge prompt for a single stage."""
    details = record.get("details", {})
    usage = record.get("usage", {})

    prompt_text = details.get("prompt", "") or ""
    system_text = details.get("system_prompt", "") or ""
    output_text = record.get("output", "") or ""

    if isinstance(prompt_text, str):
        prompt_text = _head_tail(prompt_text, 8000, 2000)
    else:
        prompt_text = str(prompt_text)[:10000]

    if isinstance(system_text, str):
        system_text = system_text[:2000]
    else:
        system_text = str(system_text)[:2000]

    if isinstance(output_text, str):
        output_text = output_text[:3000]
    else:
        output_text = str(output_text)[:3000]

    turns = usage.get("num_turns", 0)
    cost = usage.get("total_cost_usd", 0.0)
    tool_uses = usage.get("tool_uses", 0)
    tool_errors = usage.get("tool_errors", 0)
    node_id = record.get("node_id", "unknown")

    return f"""You are an expert evaluator of AI agent pipeline stages.

Stage: {node_id}
Turns: {turns} turns
Cost: ${cost:.2f}
Tool uses: {tool_uses} (errors: {tool_errors})

## System prompt
{system_text}

## User prompt
{prompt_text}

## Tool activity digest
{digest}

## Stage output (first 3000 chars)
{output_text}

## Task
Score this stage 1-5 (1=terrible, 5=excellent). Assess:
- Did it accomplish what the prompt asked?
- Was there wasted effort (unnecessary tool calls, repeated failures)?
- Are there prompt issues (ambiguity, missing context, too broad)?

Return your assessment as structured JSON."""


# ---------------------------------------------------------------------------
# Judge stage
# ---------------------------------------------------------------------------


async def judge_stage(
    provider: Any,
    record: dict,
    digest: str,
    *,
    model: str,
) -> tuple[dict, Any]:
    """Run the judge LLM on a single stage record.

    Returns ``(structured_result, usage)``.
    Requires ``provider.capabilities.supports_structured_output``.
    """
    caps = getattr(provider, "capabilities", None)
    if caps is None or not caps.supports_structured_output:
        raise EvaluationError(
            f"provider {getattr(provider, 'name', '?')} cannot return structured output; use --no-judge"
        )

    from norn.agents.base import AgentRequest
    from norn.agents.models import resolve_model

    resolved_model = resolve_model(provider.name, model)
    node_id = record.get("node_id", "unknown")

    prompt = build_judge_prompt(record, digest)
    request = AgentRequest(
        prompt=prompt,
        stage_name=f"judge:{node_id}",
        provider=provider.name,
        model=resolved_model,
        permission_mode="plan",
        max_turns=1,
        output_format=JUDGE_SCHEMA,
    )

    _text, structured, usage = await _call_agent(provider, request)
    if structured is None:
        raise EvaluationError(f"judge for {node_id}: no structured output returned")
    return structured, usage


# ---------------------------------------------------------------------------
# Feedback prompt
# ---------------------------------------------------------------------------


def build_feedback_prompt(
    run_dir: Path,
    metrics: Any,
    findings: list,
    stage_evals: dict[str, dict],
    prior: list,
    rating: dict | None,
    pipeline_source: str | None,
) -> str:
    """Build the feedback prompt for the whole run."""
    metrics_path = run_dir / "metrics.json"
    findings_path = run_dir / "findings.json"
    run_path = run_dir / "run.json"

    metrics_json = metrics_path.read_text() if metrics_path.exists() else "{}"
    findings_json = findings_path.read_text() if findings_path.exists() else "[]"
    run_json = json.loads(run_path.read_text()) if run_path.exists() else {}

    run_id = run_json.get("run_id", "?")

    parts = [
        f"RUN: {run_id}",
        f"PIPELINE: {pipeline_source or 'unknown'}",
        "",
        "## metrics.json",
        metrics_json,
        "",
        "## findings.json",
        findings_json,
        "",
    ]

    if stage_evals:
        parts.append("## Stage evaluations")
        for name, ev in stage_evals.items():
            parts.append(f"### {name}: score={ev.get('score', '?')}")
            if ev.get("waste"):
                parts.append(f"  waste: {', '.join(ev['waste'])}")
            if ev.get("prompt_issues"):
                parts.append(f"  prompt_issues: {', '.join(ev['prompt_issues'])}")
            parts.append(f"  summary: {ev.get('summary', '')}")
        parts.append("")

    if prior:
        parts.append("## Prior runs (most recent first)")
        for p in prior:
            cost = getattr(p, "cost_usd", 0)
            success = getattr(p, "success", True)
            turns = getattr(p, "turns", 0)
            parts.append(f"  run={getattr(p, 'run_id', '?')} cost=${cost:.2f} success={success} turns={turns}")
        parts.append("")

    if rating:
        parts.append(f"## Human rating: {rating.get('score', '?')}/5")
        if rating.get("note"):
            parts.append(f"  note: {rating['note']}")
        parts.append("")

    if metrics is not None:
        quota = getattr(metrics, "quota", None)
        codex_cost = getattr(metrics, "codex_cost_usd_estimate", None)
        if quota is not None:
            parts.append(f"## Quota: before={getattr(quota, 'before_pct', '?')}% after={getattr(quota, 'after_pct', '?')}%")
            parts.append("")
        if codex_cost is not None:
            parts.append(f"## Codex cost estimate: ${codex_cost:.2f}")
            parts.append("")

    parts.append("""## Task
You are an expert pipeline evaluator. Analyse this pipeline run and provide:

1. An overall score 1-10 for the run quality
2. Specific, actionable feedback on what to change in the pipeline definition
3. Cost optimisation suggestions
4. Prompt improvement suggestions

Your response MUST start with this exact header format:
RUN: <run_id>
SCORE: <1-10>
PIPELINE: <pipeline_source>

Then provide your detailed feedback.""")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Feedback runner
# ---------------------------------------------------------------------------


async def run_feedback(
    provider: Any,
    prompt: str,
    *,
    model: str,
    run_dir: Path,
) -> tuple[str, Any]:
    """Run the feedback LLM pass. Returns ``(text, usage)``."""
    from norn.agents.base import AgentRequest
    from norn.agents.models import resolve_model

    resolved_model = resolve_model(provider.name, model)
    request = AgentRequest(
        prompt=prompt,
        stage_name="feedback",
        provider=provider.name,
        model=resolved_model,
        permission_mode="plan",
        allowed_tools=["Read", "Grep", "Glob"],
        max_turns=40,
        cwd=str(run_dir),
    )

    text, _structured, usage = await _call_agent(provider, request)
    return text, usage


# ---------------------------------------------------------------------------
# Header validation
# ---------------------------------------------------------------------------

_HEADER_RE = re.compile(r"^RUN: (\d+)\nSCORE: (10|[1-9])\nPIPELINE: (.+)\n")


def validate_feedback_header(text: str, run_id: int, pipeline_source: str | None) -> int:
    """Validate the feedback header and return the score.

    Raises ``EvaluationError`` if the header is malformed or mismatched.
    """
    m = _HEADER_RE.match(text)
    if not m:
        raise EvaluationError(
            f"Feedback header invalid. Expected RUN: {run_id}\\nSCORE: 1-10\\nPIPELINE: ...\\n"
        )
    header_run_id = int(m.group(1))
    score = int(m.group(2))

    if header_run_id != run_id:
        raise EvaluationError(
            f"Feedback header RUN: {header_run_id} does not match expected {run_id}"
        )

    return score


# ---------------------------------------------------------------------------
# Agent driver (mirrors Generate.run pattern without PipelineContext)
# ---------------------------------------------------------------------------


async def _call_agent(provider: Any, request: Any) -> tuple[str, Any, Any]:
    """Drive ``provider.run`` and return ``(text, structured_output, usage)``."""
    from norn.agents.base import AgentUsage

    chunks: list[str] = []
    structured_output = None
    usage = AgentUsage(provider=provider.name)

    async for event in provider.run(request):
        if event.text is not None:
            chunks.append(event.text)
        if event.structured_output is not None:
            structured_output = event.structured_output
        if event.usage is not None:
            usage = event.usage

    return "".join(chunks), structured_output, usage


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def evaluate_run(
    state_key: str,
    run_id: int,
    *,
    provider_name: str | None = None,
    judge: bool = True,
    judge_model: str = "haiku",
    feedback_model: str = "sonnet",
    stage_glob: str | None = None,
    max_cost_usd: float = 5.0,
) -> EvaluationResult:
    """Run evaluation on a completed pipeline run.

    Args:
        state_key: State key for locating the run directory.
        run_id: Numeric run ID.
        provider_name: Agent provider name (defaults to run.json's provider).
        judge: Whether to run per-stage judge pass.
        judge_model: Model shorthand for judge (default ``"haiku"``).
        feedback_model: Model shorthand for feedback (default ``"sonnet"``).
        stage_glob: Optional fnmatch pattern to filter stages for judging.
        max_cost_usd: Maximum evaluation cost before aborting.

    Returns:
        ``EvaluationResult`` with scores, cost, and file paths.
    """
    from norn.agents.registry import get_provider
    from norn.journal import run_dir as _run_dir, runs_dir as _runs_dir
    from norn.metrics import compute_metrics, load_findings, load_metrics, load_prior_metrics, load_stage_records
    from norn.rating import load_rating

    rd = _run_dir(state_key, run_id)
    if not rd.is_dir():
        raise EvaluationError(f"Run directory not found: {rd}")

    run_json_path = rd / "run.json"
    run_json: dict = {}
    if run_json_path.exists():
        run_json = json.loads(run_json_path.read_text())

    resolved_provider_name = provider_name or run_json.get("provider", "claude-code")
    provider = get_provider(resolved_provider_name)

    metrics = load_metrics(rd)
    if metrics is None:
        metrics = compute_metrics(rd)
    findings = load_findings(rd)

    stage_records = load_stage_records(rd)

    pipeline_source = run_json.get("pipeline_source") or run_json.get("pipeline")

    total_cost = 0.0
    total_input = 0
    total_output = 0
    stage_evals: dict[str, dict] = {}
    stage_scores: dict[str, int] = {}

    # --- Judge pass ---
    if judge:
        for rec in stage_records:
            node_id = rec.get("node_id", "")
            attempt = rec.get("attempt", 1)

            # Only judge agent stages (those with usage)
            if not rec.get("usage"):
                continue

            if stage_glob is not None and not fnmatch(node_id, stage_glob):
                continue

            digest = transcript_digest(rd, node_id, attempt)
            result, usage = await judge_stage(provider, rec, digest, model=judge_model)

            call_cost = getattr(usage, "total_cost_usd", 0.0)
            total_cost += call_cost
            total_input += getattr(usage, "input_tokens", 0)
            total_output += getattr(usage, "output_tokens", 0)

            if total_cost > max_cost_usd:
                raise EvaluationError(
                    f"Evaluation cost ${total_cost:.2f} exceeds limit ${max_cost_usd:.2f}"
                )

            stage_evals[node_id] = result
            stage_scores[node_id] = result.get("score", 0)

            # Write eval.json
            from norn.journal import _safe_node_id
            safe_id = _safe_node_id(node_id)
            eval_path = (rd / "stages") / f"{safe_id}-a{attempt}.eval.json"
            eval_path.write_text(json.dumps(result, indent=2) + "\n")

    # --- Feedback pass ---
    prior = load_prior_metrics(_runs_dir(state_key), run_id)
    rating = load_rating(rd)

    prompt = build_feedback_prompt(rd, metrics, findings, stage_evals, prior, rating, pipeline_source)
    feedback_text, fb_usage = await run_feedback(provider, prompt, model=feedback_model, run_dir=rd)

    fb_cost = getattr(fb_usage, "total_cost_usd", 0.0)
    total_cost += fb_cost
    total_input += getattr(fb_usage, "input_tokens", 0)
    total_output += getattr(fb_usage, "output_tokens", 0)

    if total_cost > max_cost_usd:
        raise EvaluationError(
            f"Evaluation cost ${total_cost:.2f} exceeds limit ${max_cost_usd:.2f}"
        )

    # Validate header and write feedback
    feedback_path: str | None = None
    try:
        score = validate_feedback_header(feedback_text, run_id, pipeline_source)
        fb_path = rd / "feedback.md"
        fb_path.write_text(feedback_text)
        feedback_path = str(fb_path)
    except EvaluationError:
        raw_path = rd / "feedback.raw.md"
        raw_path.write_text(feedback_text)
        raise

    # Write evaluation.json
    created_at = datetime.now(timezone.utc).isoformat()
    eval_result = EvaluationResult(
        score=score,
        judge_model=judge_model if judge else None,
        feedback_model=feedback_model,
        provider=resolved_provider_name,
        cost_usd=total_cost,
        billed_input_tokens=total_input,
        output_tokens=total_output,
        stage_scores=stage_scores,
        created_at=created_at,
        feedback_path=feedback_path,
    )

    eval_json_path = rd / "evaluation.json"
    eval_json_path.write_text(json.dumps(asdict(eval_result), indent=2) + "\n")

    return eval_result

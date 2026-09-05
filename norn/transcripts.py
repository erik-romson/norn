"""Per-turn model mix from local claude-code transcripts.

Reads ``~/.claude/projects/<cwd_slug>/<session_id>.jsonl`` to produce
per-turn model mix, context peak, and context growth for each agent stage.
Claude-code only; skipped transparently for other providers.

Design rules:
- ``read_assistant_calls`` deduplicates by ``(message.id, requestId)``.
- ``attribute`` assigns calls to stages by session_id and timestamp window.
- Unmatched calls are accumulated under the ``"unattributed"`` key.
- A missing transcript file returns an empty list; ``compute_metrics`` marks
  the stage ``transcript="missing"`` instead of raising.
- JSON errors and I/O failures are logged; never fatal.
"""

from __future__ import annotations

import datetime
import json
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class Call:
    """One assistant turn from a claude-code transcript JSONL line."""

    timestamp: str
    model: str
    input_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    output_tokens: int
    message_id: str
    request_id: str
    session_id: str = ""
    """Set by the caller to the session this call was read from."""


@dataclass
class StageTranscriptData:
    """Transcript-derived data for a single agent stage."""

    model_mix: dict[str, dict[str, int]]
    """model_id -> {calls, input, cache_read, cache_creation, output}"""
    context_peak_tokens: int
    """Maximum (input_tokens + cache_read_input_tokens) in any single turn."""
    context_growth: list[int]
    """Per-turn context size (input + cache_read) in chronological order."""


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def transcript_path(cwd: str, session_id: str) -> Path:
    """Return the local claude-code transcript path for *session_id* in *cwd*.

    The path slug is *cwd* with every ``/`` replaced by ``-``, matching
    claude-code's ``~/.claude/projects/`` directory convention::

        /Users/alice/myproject  ->  ~/.claude/projects/-Users-alice-myproject/

    Example confirmed against ``-Users-erik-projects-erik-romson-norn``.
    """
    slug = cwd.replace("/", "-")
    return Path.home() / ".claude" / "projects" / slug / f"{session_id}.jsonl"


# ---------------------------------------------------------------------------
# Reading transcripts
# ---------------------------------------------------------------------------


def read_assistant_calls(path: Path, session_id: str = "") -> list[Call]:
    """Read and deduplicate assistant turns from a transcript JSONL file.

    Returns an empty list if *path* does not exist (caller decides how to
    handle the absence).  Lines that are not ``type == "assistant"`` are
    skipped.  Duplicates are detected by ``(message.id, requestId)``.
    JSON parse errors are logged and the line is skipped.
    """
    if not path.exists():
        return []

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        log.warning("transcripts: cannot read %s", path, exc_info=True)
        return []

    seen: set[tuple[str, str]] = set()
    calls: list[Call] = []

    for line_no, raw in enumerate(text.splitlines(), 1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("transcripts: JSON error at line %d in %s", line_no, path)
            continue

        if data.get("type") != "assistant":
            continue

        message = data.get("message") or {}
        usage = message.get("usage") or {}
        message_id = str(message.get("id") or "")
        request_id = str(data.get("requestId") or "")
        model = str(message.get("model") or "")

        if not model:
            continue

        key = (message_id, request_id)
        if key in seen:
            continue
        seen.add(key)

        calls.append(Call(
            timestamp=str(data.get("timestamp") or ""),
            model=model,
            input_tokens=int(usage.get("input_tokens") or 0),
            cache_read_input_tokens=int(usage.get("cache_read_input_tokens") or 0),
            cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            message_id=message_id,
            request_id=request_id,
            session_id=session_id,
        ))

    return calls


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------


def _parse_ts(ts_str: str) -> float | None:
    """Parse an ISO 8601 timestamp string to a POSIX float.  Returns ``None`` on failure."""
    if not ts_str:
        return None
    try:
        normalized = ts_str.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def _build_stage_data(calls: list[Call]) -> StageTranscriptData:
    """Aggregate *calls* into model mix and context metrics."""
    sorted_calls = sorted(calls, key=lambda c: c.timestamp)

    model_mix: dict[str, dict[str, int]] = {}
    context_growth: list[int] = []

    for call in sorted_calls:
        ctx_size = call.input_tokens + call.cache_read_input_tokens
        context_growth.append(ctx_size)

        if call.model not in model_mix:
            model_mix[call.model] = {
                "calls": 0,
                "input": 0,
                "cache_read": 0,
                "cache_creation": 0,
                "output": 0,
            }
        mix = model_mix[call.model]
        mix["calls"] += 1
        mix["input"] += call.input_tokens
        mix["cache_read"] += call.cache_read_input_tokens
        mix["cache_creation"] += call.cache_creation_input_tokens
        mix["output"] += call.output_tokens

    peak = max(context_growth) if context_growth else 0

    return StageTranscriptData(
        model_mix=model_mix,
        context_peak_tokens=peak,
        context_growth=context_growth,
    )


def attribute(calls: list[Call], stage_records: list[dict]) -> dict[str, StageTranscriptData]:
    """Assign *calls* to stage records by session_id and timestamp window.

    Each call is attributed to the first stage record that satisfies both:

    1. ``record["usage"]["session_id"] == call.session_id`` (when both are set)
    2. ``record["started_at"] <= call.timestamp <= record["finished_at"]``

    Calls that do not match any stage window are accumulated under the
    ``"unattributed"`` key.  Unmatched calls are counted and logged; never
    silently dropped.

    Returns ``{stage_key: StageTranscriptData}`` where *stage_key* is
    ``"<node_id>:<attempt>"``.
    """
    # Build window tuples: (session_id | None, started_ts, finished_ts, stage_key)
    windows: list[tuple[str | None, float | None, float | None, str]] = []
    for rec in stage_records:
        usage = rec.get("usage") or {}
        session_id: str | None = usage.get("session_id") or None
        node_id = rec.get("node_id", "")
        attempt = rec.get("attempt", 1)
        key = f"{node_id}:{attempt}"
        started = _parse_ts(rec.get("started_at", ""))
        finished = _parse_ts(rec.get("finished_at", ""))
        windows.append((session_id, started, finished, key))

    assigned: dict[str, list[Call]] = {}
    unattributed: list[Call] = []

    for call in calls:
        call_ts = _parse_ts(call.timestamp)
        matched = False

        for sess_id, started, finished, key in windows:
            # Session must match when both are known
            if sess_id is not None and call.session_id and sess_id != call.session_id:
                continue

            # Timestamp window check (only when timestamps are available)
            if call_ts is not None:
                if started is not None and call_ts < started:
                    continue
                if finished is not None and call_ts > finished:
                    continue

            if key not in assigned:
                assigned[key] = []
            assigned[key].append(call)
            matched = True
            break

        if not matched:
            unattributed.append(call)

    result: dict[str, StageTranscriptData] = {}
    for key, stage_calls in assigned.items():
        result[key] = _build_stage_data(stage_calls)

    if unattributed:
        log.debug("transcripts: %d call(s) not attributed to any stage", len(unattributed))
        result["unattributed"] = _build_stage_data(unattributed)

    return result

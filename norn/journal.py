"""Run journal — persisted, redacted, append-only record of every run.

Writes events, prompts, outputs, and stage records to a per-run directory
structure:

    <state-key>.runs/<run_id>/
        run.json         — header + finish metadata
        events.jsonl     — redacted events, one JSON line per event
        stages/          — one JSON file per stage execution

This is the single evidence source for metrics and evaluation.

Write failures log a warning and continue (same choice as
``norn/runner.py:836-838``).  Never fail-fast; never swallow silently.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, fields
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from norn import ui
from norn.checkpoint import serialise_output
from norn.event_sink import redact_event
from norn.events import RunEvent

log = logging.getLogger(__name__)

MAX_FIELD_CHARS = 2_000_000


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def runs_dir(state_key: str) -> Path:
    """Return the ``.runs/`` directory for a given state key."""
    return Path(state_key).resolve().with_suffix(".runs")


def run_dir(state_key: str, run_id: int) -> Path:
    """Return the directory for a specific run."""
    return runs_dir(state_key) / str(run_id)


def latest_run_id(state_key: str) -> int | None:
    """Return the highest numeric child directory name, or ``None``."""
    d = runs_dir(state_key)
    if not d.is_dir():
        return None
    ids = [int(p.name) for p in d.iterdir() if p.is_dir() and p.name.isdigit()]
    return max(ids) if ids else None


def next_run_id(state_key: str) -> int:
    """Return the next run id (1-based, auto-incrementing)."""
    latest = latest_run_id(state_key)
    return (latest or 0) + 1


# ---------------------------------------------------------------------------
# JSON serialisation helpers
# ---------------------------------------------------------------------------


def _json_default(obj: Any) -> Any:
    """Best-effort JSON serialiser for non-standard types."""
    if isinstance(obj, Enum):
        return obj.name
    if isinstance(obj, (set, frozenset)):
        return sorted(obj, key=str)
    if isinstance(obj, Path):
        return str(obj)
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    return repr(obj)


def _truncate_string(value: str, field_name: str, record: dict[str, Any]) -> str:
    """Truncate *value* if longer than ``MAX_FIELD_CHARS``, adding a sibling flag."""
    if len(value) > MAX_FIELD_CHARS:
        record[f"{field_name}_truncated"] = True
        return value[:MAX_FIELD_CHARS]
    return value


def _mask_field(value: str | None) -> str | None:
    """Mask secrets in a string field."""
    if value is None:
        return None
    return ui.mask(value)


def _safe_node_id(node_id: str) -> str:
    """Sanitise a node_id for use as a filename component."""
    return re.sub(r"[/:]", "_", node_id)


# ---------------------------------------------------------------------------
# Event serialisation
# ---------------------------------------------------------------------------


def event_to_dict(event: RunEvent) -> dict[str, Any]:
    """Serialise any run-event to a JSON-safe dict."""
    result: dict[str, Any] = {"type": type(event).__name__}
    for f in fields(event):
        value = getattr(event, f.name)
        # The delivery field is metadata, not data — skip it.
        if f.name == "delivery":
            continue
        try:
            json.dumps(value, default=_json_default)
            result[f.name] = value
        except (TypeError, ValueError):
            result[f.name] = _json_default(value)
    return result


# ---------------------------------------------------------------------------
# RunJournal
# ---------------------------------------------------------------------------


class RunJournal:
    """Append-only run journal writing to a per-run directory."""

    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._stages_dir = directory / "stages"
        self._events_path = directory / "events.jsonl"
        self._run_path = directory / "run.json"
        self._stage_seq = 0

    @classmethod
    def open(cls, state_key: str, journal_run_id: int) -> RunJournal:
        """Create a journal for a new run, creating the directory."""
        d = run_dir(state_key, journal_run_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "stages").mkdir(exist_ok=True)
        return cls(d)

    # -- Header / finish ---------------------------------------------------

    def write_header(self, **fields: Any) -> None:
        """Write the initial ``run.json`` with header metadata."""
        try:
            self._run_path.write_text(
                json.dumps(fields, default=_json_default, indent=2) + "\n"
            )
        except Exception:
            log.warning("journal: failed to write header to %s", self._run_path, exc_info=True)

    def finish(self, *, success: bool, failed_stage: str | None, duration_ms: int, totals: dict[str, Any]) -> None:
        """Update ``run.json`` with finish metadata."""
        try:
            existing: dict[str, Any] = {}
            if self._run_path.exists():
                existing = json.loads(self._run_path.read_text())
            existing["finished_at"] = datetime.now(timezone.utc).isoformat()
            existing["success"] = success
            existing["failed_stage"] = failed_stage
            existing["duration_ms"] = duration_ms
            existing["totals"] = totals
            self._run_path.write_text(
                json.dumps(existing, default=_json_default, indent=2) + "\n"
            )
        except Exception:
            log.warning("journal: failed to finish %s", self._run_path, exc_info=True)

    # -- Events ------------------------------------------------------------

    def append_event(self, event: RunEvent) -> None:
        """Append one redacted event as a JSON line to ``events.jsonl``."""
        try:
            d = event_to_dict(event)
            line = json.dumps(d, default=_json_default) + "\n"
            with self._events_path.open("a") as f:
                f.write(line)
        except Exception:
            log.warning("journal: failed to append event to %s", self._events_path, exc_info=True)

    # -- Stage records -----------------------------------------------------

    def record_stage(
        self,
        *,
        node_id: str,
        attempt: int,
        status: str,
        started_at: str,
        finished_at: str,
        duration_ms: int,
        result: Any,
    ) -> None:
        """Write one stage record to ``stages/<seq>-<node_id>-a<attempt>.json``."""
        self._stage_seq += 1
        safe_id = _safe_node_id(node_id)
        filename = f"{self._stage_seq:04d}-{safe_id}-a{attempt}.json"
        path = self._stages_dir / filename

        record: dict[str, Any] = {
            "node_id": node_id,
            "attempt": attempt,
            "status": status,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
        }

        # Serialise result fields with masking and truncation
        if result is not None:
            output = serialise_output(result.output)
            if isinstance(output, str):
                output = _truncate_string(output, "output", record)
            output = _mask_field(output) if isinstance(output, str) else output
            record["output"] = output

            error = _mask_field(result.error)
            record["error"] = error
            record["success"] = result.success
            record["artifacts"] = list(result.artifacts) if result.artifacts else []

            if result.details:
                details = dict(result.details)
                for key in ("prompt", "system_prompt"):
                    if key in details and isinstance(details[key], str):
                        details[key] = _truncate_string(details[key], key, record)
                        details[key] = _mask_field(details[key])
                record["details"] = details

            if result.usage:
                record["usage"] = {
                    "input_tokens": result.usage.input_tokens,
                    "output_tokens": result.usage.output_tokens,
                    "cache_read_input_tokens": result.usage.cache_read_input_tokens,
                    "cache_creation_input_tokens": result.usage.cache_creation_input_tokens,
                    "total_cost_usd": result.usage.total_cost_usd,
                    "duration_ms": result.usage.duration_ms,
                    "num_turns": result.usage.num_turns,
                    "model_id": result.usage.model_id,
                    "model": result.usage.model,
                    "result_subtype": result.usage.result_subtype,
                    "stop_reason": result.usage.stop_reason,
                    "tool_uses": result.usage.tool_uses,
                    "tool_errors": result.usage.tool_errors,
                }

        try:
            path.write_text(json.dumps(record, default=_json_default, indent=2) + "\n")
        except Exception:
            log.warning("journal: failed to write stage record to %s", path, exc_info=True)


# ---------------------------------------------------------------------------
# JournalSink — wraps an inner EventSink, journals every event
# ---------------------------------------------------------------------------


class JournalSink:
    """Event sink wrapper that records every event to a :class:`RunJournal`.

    Redacts events before journaling, then forwards everything to the inner
    sink.  Explicitly delegates consumer methods to the inner sink (no
    ``__getattr__``).
    """

    def __init__(self, inner: Any, journal: RunJournal) -> None:
        self._inner = inner
        self._journal = journal

    def emit(self, event: RunEvent) -> None:
        """Redact, journal, then forward to inner sink."""
        redacted = redact_event(event)
        self._journal.append_event(redacted)
        self._inner.emit(event)

    # -- Consumer API (forwarded explicitly) --------------------------------

    @property
    def lossless_events(self) -> list[RunEvent]:
        return self._inner.lossless_events

    def latest_coalescible(self, stage_id: str | None = None, attempt: int = 0) -> RunEvent | None:
        return self._inner.latest_coalescible(stage_id=stage_id, attempt=attempt)

    def all_coalescible(self) -> dict:
        return self._inner.all_coalescible()

    def transcript(self, stage_id: str | None = None, attempt: int = 0, *, after_seq: int = -1) -> list[RunEvent]:
        return self._inner.transcript(stage_id=stage_id, attempt=attempt, after_seq=after_seq)

    def bind_loop(self, loop: Any = None) -> None:
        self._inner.bind_loop(loop)

    async def drain(self) -> None:
        await self._inner.drain()

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class StageHistoryEntry:
    """Per-stage summary stored in a run record.

    Attributes:
        name: Stage name.
        success: Whether the stage completed successfully.
        cost_usd: Cumulative API cost for this stage across all attempts.
    """

    name: str
    success: bool
    cost_usd: float = 0.0


@dataclass
class RunRecord:
    """Metadata for a single pipeline run, persisted to JSONL.

    Appended to ``<config>.history`` after every run (success or failure).
    Used by ``norn history`` to display run history and comparisons.

    Attributes:
        run_id: Auto-incrementing 1-based run number.
        timestamp: ISO-8601 UTC timestamp of the run.
        success: ``True`` if the pipeline completed without errors.
        total_cost_usd: Total API cost across all stages.
        total_tokens: Total input + output tokens.
        duration_ms: Wall-clock time for the entire run.
        stages: Per-stage summary entries.
        retries: Total number of loop retries during this run.
        session_id: Final Claude session ID (for debugging/resumption).
        failed_stage: Name of the stage that caused failure (if any).
    """

    run_id: int
    timestamp: str
    success: bool
    total_cost_usd: float
    total_tokens: int
    duration_ms: int
    stages: list[StageHistoryEntry]
    retries: int
    session_id: str | None = None
    failed_stage: str | None = None


def history_file(config_path: str) -> Path:
    """Return the .history path for a given config file."""
    return Path(config_path).resolve().with_suffix(".history")


def append_run(config_path: str, record: RunRecord) -> None:
    """Append a RunRecord as a JSON line to the history file beside the config."""
    path = history_file(config_path)
    entry: dict[str, Any] = {
        "run_id": record.run_id,
        "timestamp": record.timestamp,
        "success": record.success,
        "total_cost_usd": record.total_cost_usd,
        "total_tokens": record.total_tokens,
        "duration_ms": record.duration_ms,
        "stages": [{"name": s.name, "success": s.success, "cost": s.cost_usd} for s in record.stages],
        "retries": record.retries,
        "session_id": record.session_id,
        "failed_stage": record.failed_stage,
    }
    with path.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    log.debug("Run #%d appended to %s", record.run_id, path)


def load_history(config_path: str) -> list[RunRecord]:
    """Load all run records from the JSONL history file."""
    path = history_file(config_path)
    if not path.exists():
        return []
    records: list[RunRecord] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            stages = [
                StageHistoryEntry(name=s["name"], success=s["success"], cost_usd=s.get("cost", 0.0))
                for s in data.get("stages", [])
            ]
            records.append(RunRecord(
                run_id=data["run_id"],
                timestamp=data["timestamp"],
                success=data["success"],
                total_cost_usd=data.get("total_cost_usd", 0.0),
                total_tokens=data.get("total_tokens", 0),
                duration_ms=data.get("duration_ms", 0),
                stages=stages,
                retries=data.get("retries", 0),
                session_id=data.get("session_id"),
                failed_stage=data.get("failed_stage"),
            ))
        except Exception:
            log.warning("Skipping malformed history line: %.80s", line)
    return records


def next_run_id(config_path: str) -> int:
    """Return the next available run ID (1-based, increments from the last recorded run)."""
    records = load_history(config_path)
    return (max(r.run_id for r in records) + 1) if records else 1

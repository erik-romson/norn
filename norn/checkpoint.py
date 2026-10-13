from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from norn.agents.session_totals import SessionTotals

log = logging.getLogger(__name__)


@dataclass
class Checkpoint:
    """Saved pipeline state for resumption after a failure or interruption.

    Written atomically to ``<config>.checkpoint`` after each successful stage.
    On ``--resume``, the runner loads this checkpoint, restores completed stage
    results into ``PipelineContext``, and skips those stages (showing them as
    cached).

    Attributes:
        pipeline: Name of the pipeline that created this checkpoint.
        timestamp: ISO-8601 timestamp of the last save.
        session_id: Agent session ID for agent session resumption.
        completed_stages: Ordered list of stage names that finished successfully.
        results: Serialised stage outputs keyed by stage name.
        usage: Reserved for future per-stage usage data.
        file_checkpoint_id: Reserved for SDK-level file checkpointing.
        params: The string params the run was launched with (the positional
            ``args`` and any ``--arg`` values).  A resume has to rebuild the
            same pipeline, and pipelines like ``implement_features_v2`` build
            their stages from these at import time.  Empty in checkpoints
            written before params were recorded.
        session_totals: Running cost and API time ``session_id`` had reached
            when the checkpoint was saved, so the first stage that resumes it
            in a new process is charged only for its own work.  ``None`` when
            there is no session or the checkpoint predates the field.
    """

    pipeline: str
    timestamp: str
    session_id: str | None
    completed_stages: list[str]
    results: dict[str, Any]
    usage: list[dict[str, Any]]
    file_checkpoint_id: str | None = None
    agent_provider: str = "claude-code"
    params: dict[str, str] = field(default_factory=dict)
    session_totals: SessionTotals | None = None


def checkpoint_file(config_path: str) -> Path:
    """Return the .checkpoint path for a given config file."""
    return Path(config_path).resolve().with_suffix(".checkpoint")


def save_checkpoint(
    config_path: str,
    pipeline_name: str,
    session_id: str | None,
    completed_stages: list[str],
    stage_outputs: dict[str, Any],
    agent_provider: str = "claude-code",
    params: dict[str, str] | None = None,
    session_totals: SessionTotals | None = None,
) -> None:
    """Atomically write checkpoint state beside the config file."""
    path = checkpoint_file(config_path)
    data: dict[str, Any] = {
        "pipeline": pipeline_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "completed_stages": list(completed_stages),
        "results": stage_outputs,
        "usage": [],
        "file_checkpoint_id": None,
        "agent_provider": agent_provider,
        "params": dict(params or {}),
        "session_totals": asdict(session_totals) if session_totals is not None else None,
    }
    tmp = path.with_suffix(".checkpoint.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)
    log.debug("Checkpoint saved to %s (%d stages)", path, len(completed_stages))


def load_checkpoint(config_path: str) -> Checkpoint | None:
    """Load a checkpoint from disk, or return None if none exists."""
    path = checkpoint_file(config_path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return Checkpoint(
            pipeline=data["pipeline"],
            timestamp=data["timestamp"],
            session_id=data.get("session_id"),
            completed_stages=data.get("completed_stages", []),
            results=data.get("results", {}),
            usage=data.get("usage", []),
            file_checkpoint_id=data.get("file_checkpoint_id"),
            agent_provider=data.get("agent_provider", "claude-code"),
            params=data.get("params", {}),
            session_totals=SessionTotals(**data["session_totals"]) if data.get("session_totals") else None,
        )
    except Exception:
        log.warning("Failed to load checkpoint from %s — ignoring", path)
        return None


def serialise_output(value: Any) -> Any:
    """Best-effort JSON-safe serialisation of a stage output value.

    Objects that expose a ``to_dict()`` method (e.g. domain model dataclasses)
    are serialised via that method so they survive checkpoint round-trips.
    """
    if hasattr(value, "to_dict"):
        return value.to_dict()
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)

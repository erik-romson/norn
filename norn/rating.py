"""Human rating for pipeline runs (1–5 stars).

Writes ``rating.json`` beside the other journal artefacts in the run directory::

    <state-key>.runs/<run_id>/rating.json

The file format is::

    {"score": 4, "note": "optional text", "rated_at": "2026-09-05T12:00:00+00:00"}
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def write_rating(run_dir: Path, score: int, note: str | None = None) -> None:
    """Write ``rating.json`` to *run_dir*.

    Args:
        run_dir: Path to the run directory (e.g. ``<state-key>.runs/<run_id>/``).
        score: Integer 1–5 inclusive.
        note: Optional free-text annotation.

    Raises:
        ValueError: If *score* is outside 1–5.
    """
    if score < 1 or score > 5:
        raise ValueError(f"score must be between 1 and 5 inclusive, got {score!r}")
    rated_at = datetime.now(timezone.utc).isoformat()
    data: dict = {"score": score, "note": note, "rated_at": rated_at}
    path = Path(run_dir) / "rating.json"
    path.write_text(json.dumps(data, indent=2))


def load_rating(run_dir: Path) -> dict | None:
    """Return the parsed ``rating.json`` from *run_dir*, or ``None`` if absent."""
    path = Path(run_dir) / "rating.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())

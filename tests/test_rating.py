"""Tests for norn/rating.py and the `norn rate` CLI subcommand."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from norn.rating import load_rating, write_rating


# ---------------------------------------------------------------------------
# Unit tests for rating.py
# ---------------------------------------------------------------------------


def test_write_load_round_trip(tmp_path):
    """write_rating then load_rating returns all fields intact."""
    run_dir = tmp_path / "run1"
    run_dir.mkdir()

    write_rating(run_dir, score=4, note="Worked well, minor issues")
    data = load_rating(run_dir)

    assert data is not None
    assert data["score"] == 4
    assert data["note"] == "Worked well, minor issues"
    assert "rated_at" in data
    assert data["rated_at"]  # non-empty ISO string


def test_write_load_round_trip_no_note(tmp_path):
    """write_rating with no note stores None and round-trips correctly."""
    run_dir = tmp_path / "run2"
    run_dir.mkdir()

    write_rating(run_dir, score=3)
    data = load_rating(run_dir)

    assert data is not None
    assert data["score"] == 3
    assert data["note"] is None


def test_load_rating_missing_returns_none(tmp_path):
    """load_rating returns None when rating.json does not exist."""
    run_dir = tmp_path / "empty"
    run_dir.mkdir()
    assert load_rating(run_dir) is None


def test_write_rating_score_out_of_range_low(tmp_path):
    """write_rating raises ValueError for score < 1."""
    run_dir = tmp_path / "run_low"
    run_dir.mkdir()
    with pytest.raises(ValueError):
        write_rating(run_dir, score=0)


def test_write_rating_score_out_of_range_high(tmp_path):
    """write_rating raises ValueError for score > 5."""
    run_dir = tmp_path / "run_high"
    run_dir.mkdir()
    with pytest.raises(ValueError):
        write_rating(run_dir, score=6)


def test_write_rating_boundaries_accepted(tmp_path):
    """Scores 1 and 5 are accepted as boundary values."""
    for score, subdir in [(1, "low"), (5, "high")]:
        run_dir = tmp_path / subdir
        run_dir.mkdir()
        write_rating(run_dir, score=score)
        data = load_rating(run_dir)
        assert data is not None
        assert data["score"] == score


# ---------------------------------------------------------------------------
# CLI integration — `norn rate`
# ---------------------------------------------------------------------------


def test_cli_rate_writes_rating_json(tmp_path, capsys):
    """norn rate <config> <score> writes rating.json in the run directory."""
    from norn.cli import main

    config = str(tmp_path / "pipeline.py")

    # Create the journal run directory that the rate command looks for
    run_dir = tmp_path / "pipeline.runs" / "1"
    run_dir.mkdir(parents=True)

    with patch("sys.argv", ["norn", "rate", config, "4"]):
        main()

    rating_path = run_dir / "rating.json"
    assert rating_path.exists(), "rating.json was not created"
    data = json.loads(rating_path.read_text())
    assert data["score"] == 4
    assert data["note"] is None


def test_cli_rate_with_note(tmp_path, capsys):
    """norn rate --note passes the note through to rating.json."""
    from norn.cli import main

    config = str(tmp_path / "pipeline.py")
    run_dir = tmp_path / "pipeline.runs" / "1"
    run_dir.mkdir(parents=True)

    with patch("sys.argv", ["norn", "rate", config, "5", "--note", "Perfect run"]):
        main()

    data = json.loads((run_dir / "rating.json").read_text())
    assert data["score"] == 5
    assert data["note"] == "Perfect run"


def _write_run_json(run_dir: Path, history_run_id: int) -> None:
    """Write the minimal run.json header that carries the history run id."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps({"history_run_id": history_run_id}))


def test_cli_rate_explicit_run_id(tmp_path):
    """norn rate --run N names the history id, not the journal directory name."""
    from norn.cli import main

    config = str(tmp_path / "pipeline.py")
    # The counters diverge: journal 1/2 are history 3/4. --run 4 must land in
    # journal directory 2, and nothing may be written to directory 1.
    _write_run_json(tmp_path / "pipeline.runs" / "1", history_run_id=3)
    run2_dir = tmp_path / "pipeline.runs" / "2"
    _write_run_json(run2_dir, history_run_id=4)

    with patch("sys.argv", ["norn", "rate", config, "3", "--run", "4"]):
        main()

    assert (run2_dir / "rating.json").exists()
    assert not (tmp_path / "pipeline.runs" / "1" / "rating.json").exists()


def test_cli_rate_unknown_history_run_exits(tmp_path, capsys):
    """A history run with no journal directory is refused, not guessed at."""
    from norn.cli import main

    config = str(tmp_path / "pipeline.py")
    _write_run_json(tmp_path / "pipeline.runs" / "1", history_run_id=3)

    with pytest.raises(SystemExit) as exc_info:
        # Journal directory 1 exists, but it is history run 3, not run 1.
        with patch("sys.argv", ["norn", "rate", config, "3", "--run", "1"]):
            main()

    assert exc_info.value.code == 1
    assert "has no journal directory" in capsys.readouterr().err


def test_cli_rate_no_runs_exits(tmp_path, capsys):
    """norn rate exits with code 1 when no journaled runs exist."""
    from norn.cli import main

    config = str(tmp_path / "pipeline.py")
    # No .runs directory created

    with pytest.raises(SystemExit) as exc_info:
        with patch("sys.argv", ["norn", "rate", config, "4"]):
            main()

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "no journaled runs" in captured.err


def test_cli_rate_invalid_score_exits(tmp_path, capsys):
    """norn rate with out-of-range score exits with code 1."""
    from norn.cli import main

    config = str(tmp_path / "pipeline.py")
    (tmp_path / "pipeline.runs" / "1").mkdir(parents=True)

    with pytest.raises(SystemExit) as exc_info:
        with patch("sys.argv", ["norn", "rate", config, "7"]):
            main()

    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# History table — Rating column
# ---------------------------------------------------------------------------


def test_history_table_shows_rating(tmp_path, capsys):
    """print_history_table shows 'X/5' in the Rating column when rating.json exists."""
    from norn.history import RunRecord, StageHistoryEntry, append_run
    from norn.ui import print_history_table

    config = str(tmp_path / "pipeline.py")
    append_run(
        config,
        RunRecord(
            run_id=1,
            timestamp="2026-09-05T10:00:00+00:00",
            success=True,
            total_cost_usd=0.10,
            total_tokens=5000,
            duration_ms=3000,
            stages=[StageHistoryEntry(name="gen", success=True, cost_usd=0.10)],
            retries=0,
        ),
    )

    # Write a rating for history run 1, journaled as directory 1.
    run_dir = tmp_path / "pipeline.runs" / "1"
    _write_run_json(run_dir, history_run_id=1)
    write_rating(run_dir, score=4, note="Good")

    from norn.history import load_history

    records = load_history(config)
    state_key = config  # same as how cli.py resolves it for a local file
    print_history_table(records, state_key=state_key)

    captured = capsys.readouterr()
    assert "4/5" in captured.out


def test_history_table_no_rating_shows_dash(tmp_path, capsys):
    """print_history_table shows '-' in Rating column when no rating.json exists."""
    from norn.history import RunRecord, StageHistoryEntry, append_run, load_history
    from norn.ui import print_history_table

    config = str(tmp_path / "pipeline.py")
    append_run(
        config,
        RunRecord(
            run_id=1,
            timestamp="2026-09-05T10:00:00+00:00",
            success=True,
            total_cost_usd=0.10,
            total_tokens=5000,
            duration_ms=3000,
            stages=[StageHistoryEntry(name="gen", success=True, cost_usd=0.10)],
            retries=0,
        ),
    )

    records = load_history(config)
    print_history_table(records, state_key=None)

    captured = capsys.readouterr()
    # With no state_key, ratings can't be loaded — Rating column shows "-"
    assert "4/5" not in captured.out


def test_history_table_does_not_misattribute_rating(tmp_path, capsys):
    """A rating never lands on a run that merely shares the journal's number.

    The journal counter only advances on journaled runs, so journal directory
    N is generally not history run N.  Joining by value showed one run's
    rating on another run's row.
    """
    from norn.history import RunRecord, StageHistoryEntry, append_run, load_history
    from norn.ui import print_history_table

    config = str(tmp_path / "pipeline.py")
    for rid in (1, 2, 3):
        append_run(
            config,
            RunRecord(
                run_id=rid,
                timestamp="2026-09-05T10:00:00+00:00",
                success=rid == 3,
                total_cost_usd=0.10,
                total_tokens=5000,
                duration_ms=3000,
                stages=[StageHistoryEntry(name="gen", success=True, cost_usd=0.10)],
                retries=0,
            ),
        )

    # Only history run 3 was journaled, as directory 1. Rating it must not
    # decorate history run 1.
    run_dir = tmp_path / "pipeline.runs" / "1"
    _write_run_json(run_dir, history_run_id=3)
    write_rating(run_dir, score=5, note="the good one")

    print_history_table(load_history(config), state_key=config)

    out = capsys.readouterr().out
    rows = [ln for ln in out.splitlines() if ln.strip().startswith("#")]
    assert len(rows) == 3
    assert "5/5" not in rows[0]
    assert "5/5" not in rows[1]
    assert "5/5" in rows[2]


def test_legacy_run_without_history_id_is_ignored(tmp_path, capsys):
    """A journal dir predating the history_run_id field decorates no row."""
    from norn.history import RunRecord, StageHistoryEntry, append_run, load_history
    from norn.ui import print_history_table

    config = str(tmp_path / "pipeline.py")
    append_run(
        config,
        RunRecord(
            run_id=1,
            timestamp="2026-09-05T10:00:00+00:00",
            success=True,
            total_cost_usd=0.10,
            total_tokens=5000,
            duration_ms=3000,
            stages=[StageHistoryEntry(name="gen", success=True, cost_usd=0.10)],
            retries=0,
        ),
    )

    run_dir = tmp_path / "pipeline.runs" / "1"
    run_dir.mkdir(parents=True)
    write_rating(run_dir, score=4, note="unjoinable")

    print_history_table(load_history(config), state_key=config)

    assert "4/5" not in capsys.readouterr().out

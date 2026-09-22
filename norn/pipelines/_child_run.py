"""Read a child pipeline's run journal and explain how its last run ended.

A pipeline that spawns another norn run as a `RunCommand` (``fix_jira_issue``
runs ``implement_features_v2``) sees only the child's exit code.  When the
child aborts, the parent's next gate checks for artifacts the child never got
far enough to write, and reports a missing file — a symptom, several stages
removed from the cause.  This helper turns that exit code back into the child's
own failed stage and error text.

Usage
-----
``_child_run.py report --launch-dir <dir> --state-key <key> [--tail N]``
    Find the newest run under ``<dir>/<key>.runs/``, print what happened, and
    exit 1 when that run failed (or when no journal can be read at all), 0 when
    it succeeded.  ``--tail`` caps how many lines of the failed stage's error
    are printed (default 40).

The journal layout is norn's own: ``<key>.runs/<n>/run.json`` holds
``success`` and ``failed_stage``; ``<key>.runs/<n>/stages/*.json`` holds one
file per stage attempt, each with a ``node_id`` and an ``error``.

This script is **stdlib-only** and must not import anything from ``norn`` — it
runs as a plain subprocess from a pipeline shell command.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_TAIL = 40


def latest_run_dir(runs_dir: Path) -> Path | None:
    """Return the highest-numbered run directory, or None when there is none."""
    if not runs_dir.is_dir():
        return None
    numbered = [p for p in runs_dir.iterdir() if p.is_dir() and p.name.isdigit()]
    if not numbered:
        return None
    return max(numbered, key=lambda p: int(p.name))


def _load_json(path: Path) -> dict:
    """Return the parsed object at *path*, or an empty dict when unreadable."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def failed_stage_error(run_dir: Path, stage_name: str) -> str | None:
    """Return the error text of *stage_name*'s last attempt in *run_dir*.

    Stage files are named ``NNNN-stage_<name>-a<attempt>.json`` but the name is
    not a reliable key — a nested stage's file carries a parent prefix — so the
    match is made on the recorded ``node_id`` instead, and the highest
    ``attempt`` wins.
    """
    stages_dir = run_dir / "stages"
    if not stages_dir.is_dir():
        return None

    best: tuple[int, str] | None = None
    for path in sorted(stages_dir.glob("*.json")):
        data = _load_json(path)
        node_id = data.get("node_id")
        if not isinstance(node_id, str) or not node_id.endswith(f":{stage_name}"):
            continue
        error = data.get("error")
        if not isinstance(error, str) or not error.strip():
            continue
        attempt = data.get("attempt")
        attempt = attempt if isinstance(attempt, int) else 0
        if best is None or attempt >= best[0]:
            best = (attempt, error)
    return best[1] if best else None


def _tail_lines(text: str, limit: int) -> str:
    """Return at most *limit* lines of *text*, noting anything dropped."""
    lines = text.rstrip().splitlines()
    if len(lines) <= limit:
        return "\n".join(lines)
    dropped = len(lines) - limit
    return "\n".join([f"[... {dropped} earlier line(s) omitted ...]", *lines[-limit:]])


def report(launch_dir: Path, state_key: str, tail: int = DEFAULT_TAIL) -> int:
    """Print how the newest child run ended.  Return 1 when it did not pass."""
    runs_dir = (launch_dir / f"{state_key}.runs").resolve()
    run_dir = latest_run_dir(runs_dir)

    print("=== child run diagnosis ===")
    print(f"child pipeline: {state_key}")

    if run_dir is None:
        print(f"journal:        none found at {runs_dir}")
        print(
            "\nThe child run left no journal, so it failed before norn started "
            "it — read the 'implement' stage's own output above for the "
            "command that could not run."
        )
        return 1

    run = _load_json(run_dir / "run.json")
    print(f"journal:        {run_dir}")

    if run.get("success") is True:
        print("outcome:        passed")
        return 0

    stage_name = run.get("failed_stage")
    if isinstance(stage_name, str) and stage_name:
        print(f"outcome:        failed at stage {stage_name!r}")
        error = failed_stage_error(run_dir, stage_name)
        if error:
            print("\n--- child stage error ---")
            print(_tail_lines(error, tail))
    else:
        print("outcome:        failed, and the journal names no stage")

    print(
        "\nThe child run never reached its review and handoff stages, so the "
        "artifacts this run checks for were never written.  Fix the cause "
        "above, then rerun: committed steps are skipped on resume."
    )
    return 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="_child_run.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    rep = sub.add_parser("report", help="explain how the newest child run ended")
    rep.add_argument("--launch-dir", required=True, help="directory the child ran in")
    rep.add_argument("--state-key", required=True, help="child pipeline's state key")
    rep.add_argument(
        "--tail",
        type=int,
        default=DEFAULT_TAIL,
        help=f"lines of the failed stage's error to print (default {DEFAULT_TAIL})",
    )

    args = parser.parse_args(argv[1:])
    if args.command == "report":
        return report(Path(args.launch_dir), args.state_key, tail=args.tail)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

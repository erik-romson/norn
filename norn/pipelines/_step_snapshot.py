"""Content-aware worktree snapshot helper for implement_features_v2.

A *snapshot* is a JSON object mapping each path that appears in
``git status --porcelain -uall -z`` to ``[status, digest]``, where *status*
is the two-character porcelain status string and *digest* is the SHA-1 of the
file's worktree bytes (or the sentinel ``"-"`` for paths that are missing from
the worktree — deleted files, rename sources).

Why content hashing?
--------------------
The status-only approach in ``_snapshot_diff.py`` has a correctness gap: if a
file is `` M`` (worktree-modified, index clean) before a step and still `` M``
after the step, its status string is identical even though the agent may have
rewritten the file.  The test suite passes, but the edit is never committed.
Hashing the worktree bytes catches this case — the digest changes when the
content changes, regardless of the porcelain status character.

Three subcommands
-----------------
``snapshot --root <dir> <out.json>``
    Run ``git status`` in *dir*, build the snapshot, write JSON.  Non-zero git
    exit propagates immediately.

``diff <pre.json> <post.json> [--ignore PATH]... [--ignore-file NULLIST]...``
    Print NUL-terminated changed paths after filtering any ignore lists.
    Missing *pre.json* is a hard error (exit 2) — a stale resume must be loud.
    Missing *post.json* is also a hard error.

``hook-fixes --root <dir>``
    Print NUL-terminated partially-staged paths (index column in ``MARC`` and
    worktree column ``M``) — the state left by auto-fixing pre-commit hooks
    such as ruff-format, black, or prettier.

This script is **stdlib-only** and must not import anything from ``norn``.
It is invoked as ``python3 <abs path> <subcommand> ...`` from RunCommand shell
stages; the current working directory is irrelevant.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------


def parse_status_z(data: bytes) -> list[tuple[str, str]]:
    """Parse ``git status --porcelain -uall -z`` output into ``(status, path)`` pairs.

    With ``-z`` each entry is terminated by a NUL character; no shell quoting
    is applied, so paths are transmitted as raw bytes.  Rename and copy records
    (``R`` or ``C`` in either status column) carry two NUL-separated paths; this
    function reports the **new** name and skips the orig-path field.

    Paths are decoded with ``surrogateescape`` so any byte sequence round-trips
    faithfully through Python str.
    """
    result: list[tuple[str, str]] = []
    if not data:
        return result
    # git -z terminates every field (including the last one) with NUL, so the
    # final split always produces a trailing empty bytes object — harmless.
    fields = data.split(b"\0")
    i = 0
    while i < len(fields):
        field = fields[i]
        i += 1
        # Each status entry is at least "XY p" = 4 bytes.  Trailing empty
        # fields and malformed entries are skipped silently.
        if len(field) < 4:
            continue
        xy = field[:2].decode("ascii", errors="replace")
        path = field[3:].decode("utf-8", errors="surrogateescape")
        result.append((xy, path))
        # Rename/copy: the very next NUL-separated field is the orig path.
        if xy[0] in "RC" or xy[1] in "RC":
            i += 1  # skip orig path
    return result


def build_snapshot(entries: list[tuple[str, str]], root: str) -> dict[str, list[str]]:
    """Build a content-aware snapshot from parsed status entries.

    Returns a dict mapping ``path -> [status, digest]`` where *digest* is the
    hex SHA-1 of the file's worktree bytes, or ``"-"`` when the file is absent
    from the worktree (deleted, or a rename source that no longer exists there).

    Only paths in the status output are hashed; a clean tree costs nothing.
    Directories never appear in ``-uall`` output, so ``IsADirectoryError`` is
    treated identically to ``FileNotFoundError`` (both yield ``"-"``).
    """
    result: dict[str, list[str]] = {}
    for status, path in entries:
        full = os.path.join(root, path)
        try:
            with open(full, "rb") as fh:
                digest = hashlib.sha1(fh.read()).hexdigest()
        except (FileNotFoundError, IsADirectoryError, PermissionError):
            digest = "-"
        result[path] = [status, digest]
    return result


def diff_snapshots(
    pre: dict[str, list[str]],
    post: dict[str, list[str]],
) -> list[str]:
    """Return sorted paths whose ``[status, digest]`` pair changed between snapshots.

    Covers four cases:
    - status changed (e.g. `` M`` → ``M ``),
    - status identical but content changed (the P0-2b regression in v1),
    - path appeared in *post* (newly untracked, new file, etc.),
    - path disappeared from *post* (e.g. a tracked-modified file reverted to clean).
    """
    changed: set[str] = set()
    for path, val in post.items():
        if pre.get(path) != val:
            changed.add(path)
    for path in pre:
        if path not in post:
            changed.add(path)
    return sorted(changed)


def hook_fixes(entries: list[tuple[str, str]]) -> list[str]:
    """Return sorted partially-staged paths from parsed status entries.

    A path qualifies when the index column is in ``MARC`` (something was
    staged) and the worktree column is ``M`` (a pre-commit hook rewrote the
    file after staging).  Untracked (``??``) paths are never included — they
    were never staged, so sweeping them in would commit files that were dirty
    before the step ran.
    """
    return sorted(
        path
        for status, path in entries
        if len(status) == 2 and status[0] in "MARC" and status[1] == "M"
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _run_git_status(root: str) -> bytes:
    """Run ``git status --porcelain -uall -z`` in *root* and return stdout bytes."""
    result = subprocess.run(
        ["git", "-C", root, "status", "--porcelain", "-uall", "-z"],
        capture_output=True,
    )
    if result.returncode != 0:
        sys.stderr.buffer.write(result.stderr)
        sys.exit(result.returncode)
    return result.stdout


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def _cmd_snapshot(args: list[str]) -> int:
    """snapshot --root <dir> <out.json>"""
    if len(args) < 3 or args[0] != "--root":
        print("usage: _step_snapshot.py snapshot --root <dir> <out.json>", file=sys.stderr)
        return 2
    root = args[1]
    out_path = args[2]
    raw = _run_git_status(root)
    entries = parse_status_z(raw)
    snapshot = build_snapshot(entries, root)
    with open(out_path, "w") as fh:
        json.dump(snapshot, fh)
    return 0


def _cmd_diff(args: list[str]) -> int:
    """diff <pre.json> <post.json> [--ignore PATH]... [--ignore-file NULLIST]..."""
    positional: list[str] = []
    ignore: set[str] = set()
    i = 0
    while i < len(args):
        if args[i] == "--ignore":
            if i + 1 >= len(args):
                print("--ignore requires a PATH argument", file=sys.stderr)
                return 2
            ignore.add(args[i + 1])
            i += 2
        elif args[i] == "--ignore-file":
            if i + 1 >= len(args):
                print("--ignore-file requires a NULLIST argument", file=sys.stderr)
                return 2
            nullist_path = args[i + 1]
            try:
                raw = open(nullist_path, "rb").read()
            except FileNotFoundError:
                print(f"--ignore-file: {nullist_path!r} not found", file=sys.stderr)
                return 2
            for part in raw.split(b"\0"):
                if part:
                    ignore.add(part.decode("utf-8", errors="surrogateescape"))
            i += 2
        else:
            positional.append(args[i])
            i += 1

    if len(positional) < 2:
        print(
            "usage: _step_snapshot.py diff <pre.json> <post.json>"
            " [--ignore PATH]... [--ignore-file NULLIST]...",
            file=sys.stderr,
        )
        return 2

    pre_path, post_path = positional[0], positional[1]

    if not os.path.exists(pre_path):
        print(
            f"diff: pre snapshot {pre_path!r} not found"
            " — stale resume or missing baseline?",
            file=sys.stderr,
        )
        return 2

    if not os.path.exists(post_path):
        print(
            f"diff: post snapshot {post_path!r} not found"
            " — the snapshot stage did not run?",
            file=sys.stderr,
        )
        return 2

    with open(pre_path) as fh:
        pre: dict[str, list[str]] = json.load(fh)
    with open(post_path) as fh:
        post: dict[str, list[str]] = json.load(fh)

    for path in diff_snapshots(pre, post):
        if path not in ignore:
            sys.stdout.buffer.write(path.encode("utf-8", errors="surrogateescape") + b"\0")
    return 0


def _cmd_hook_fixes(args: list[str]) -> int:
    """hook-fixes --root <dir>"""
    if len(args) < 2 or args[0] != "--root":
        print("usage: _step_snapshot.py hook-fixes --root <dir>", file=sys.stderr)
        return 2
    root = args[1]
    raw = _run_git_status(root)
    entries = parse_status_z(raw)
    for path in hook_fixes(entries):
        sys.stdout.buffer.write(path.encode("utf-8", errors="surrogateescape") + b"\0")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_USAGE = (
    "usage: _step_snapshot.py snapshot --root <dir> <out.json>\n"
    "       _step_snapshot.py diff <pre.json> <post.json>"
    " [--ignore PATH]... [--ignore-file NULLIST]...\n"
    "       _step_snapshot.py hook-fixes --root <dir>"
)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(_USAGE, file=sys.stderr)
        return 2

    cmd = argv[1]
    rest = argv[2:]

    if cmd == "snapshot":
        return _cmd_snapshot(rest)
    elif cmd == "diff":
        return _cmd_diff(rest)
    elif cmd == "hook-fixes":
        return _cmd_hook_fixes(rest)
    else:
        print(f"unknown subcommand: {cmd!r}\n{_USAGE}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

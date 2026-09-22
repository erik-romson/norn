"""Diff two `git status --porcelain -uall` snapshots and print changed paths.

Usage: python _snapshot_diff.py <pre> <post> [--root <dir>]
                                [--ignore <path>]... [--ignore-prefix <path>]...
       python _snapshot_diff.py --hook-fixes <status>

A path is "changed during this step" if its porcelain status in <post>
differs from its status in <pre>. Newly-appearing paths are always changed.
Paths that disappear (e.g. a tracked-modified file got reverted) are also
emitted so `git add -A -- <path>` can stage the cleanup.

``--root`` filters that list through ``unstageable_paths`` so a path
``git add`` would refuse never reaches the commit stage. Without it a single
such path aborts the whole ``git add`` and kills the step.

``--ignore`` drops one exact path and ``--ignore-prefix`` drops a whole
subtree — the path itself and everything under it. norn's own run state is the
caller for both: ``<key>.checkpoint`` and ``<key>.history`` are exact paths,
while ``<key>.runs/`` is a directory whose files ``git status`` names
individually. All three are written while the run is in flight, so committing
them makes the commit dirty itself the moment the next event is journalled.

Renames are reported under the new name. Quoted paths (filenames with
special chars) are decoded.

This lets a pipeline commit only the paths that changed *during* a step,
leaving any pre-existing dirty files untouched — so a dirty worktree no
longer blocks the run.

``--hook-fixes`` reads a single snapshot and prints the paths that are
*partially staged* — staged in the index (``M``/``A``/``R``/``C``) and also
modified in the worktree (``M``). An auto-fixing pre-commit hook (ruff-format,
``ruff --fix``, black, prettier) leaves exactly this state: it rewrites a staged
file and rejects the commit. Re-staging these paths and committing again is the
standard protocol. Untracked (``??``) paths are never included — they were never
staged, so sweeping them in would break the dirty-worktree promise above.
"""
from __future__ import annotations

import os
import subprocess
import sys


def _decode_quoted(name: str) -> str:
    if name.startswith('"') and name.endswith('"'):
        try:
            return name[1:-1].encode("latin-1").decode("unicode_escape")
        except (UnicodeDecodeError, UnicodeEncodeError):
            return name[1:-1]
    return name


def parse(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        with open(path) as f:
            text = f.read()
    except FileNotFoundError:
        return out
    for line in text.splitlines():
        if len(line) < 4:
            continue
        status = line[:2]
        rest = line[3:]
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        out[_decode_quoted(rest)] = status
    return out


def under_any_prefix(path: str, prefixes: list[str]) -> bool:
    """Return True when *path* is one of *prefixes* or lives inside one.

    Path components are compared whole, so the prefix ``a.runs`` covers
    ``a.runs`` and ``a.runs/2/events.jsonl`` but not ``a.runs-backup``.
    """
    for prefix in prefixes:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def hook_fixes(path: str) -> list[str]:
    """Return staged paths that also have unstaged modifications."""
    return sorted(
        p
        for p, status in parse(path).items()
        if len(status) == 2 and status[0] in "MARC" and status[1] == "M"
    )


# ---------------------------------------------------------------------------
# "Can git stage this path?" probes
#
# Deliberately duplicated from `_step_snapshot.py` (the v2 helper) rather than
# imported: both files are standalone scripts run as `python3 <abs path>` from
# a RunCommand shell, where a sibling import resolves through sys.path[0] but
# the same import fails when the module is loaded as `norn.pipelines.*`. A
# two-branch import would be silent in exactly the environment where breaking
# it costs a run. Keep the two copies in step.
# ---------------------------------------------------------------------------


def _chunks(items: list[str], size: int = 256) -> list[list[str]]:
    """Split *items* into argv-sized chunks so long path lists cannot blow ARG_MAX."""
    return [items[i : i + size] for i in range(0, len(items), size)]


def _git_ignored(root: str, paths: list[str]) -> set[str]:
    """Return the subset of *paths* that ``.gitignore`` rules exclude.

    ``git check-ignore`` consults the index, so a *tracked* file matching an
    ignore pattern is not reported — matching ``git add``, which stages it
    happily. Exit 0 means "some were ignored", exit 1 means "none were";
    anything higher is a real git error and aborts.
    """
    if not paths:
        return set()
    payload = b"".join(
        p.encode("utf-8", errors="surrogateescape") + b"\0" for p in paths
    )
    result = subprocess.run(
        ["git", "-C", root, "check-ignore", "-z", "--stdin"],
        input=payload,
        capture_output=True,
    )
    if result.returncode > 1:
        sys.stderr.buffer.write(result.stderr)
        sys.exit(result.returncode)
    return {
        part.decode("utf-8", errors="surrogateescape")
        for part in result.stdout.split(b"\0")
        if part
    }


def _git_indexed(root: str, paths: list[str]) -> set[str]:
    """Return the subset of *paths* that the git index knows about."""
    found: set[str] = set()
    for chunk in _chunks(paths):
        result = subprocess.run(
            ["git", "-C", root, "ls-files", "-z", "--", *chunk],
            capture_output=True,
        )
        if result.returncode != 0:
            sys.stderr.buffer.write(result.stderr)
            sys.exit(result.returncode)
        found.update(
            part.decode("utf-8", errors="surrogateescape")
            for part in result.stdout.split(b"\0")
            if part
        )
    return found


def unstageable_paths(paths: list[str], root: str) -> set[str]:
    """Return the subset of *paths* that ``git add -A --`` would refuse.

    A single refused pathspec aborts the whole ``git add``, taking the commit
    stage down with it, so these have to be dropped before staging. Two cases
    reach here, both via the "path disappeared from the post snapshot" arm of
    the diff:

    - **Newly ignored.** The path was untracked when the pre-snapshot was
      taken and a ``.gitignore`` rule started covering it during the step (for
      example norn's own ``*.checkpoint`` / ``*.runs`` state files being added
      to the target repo's ignore list). ``git add`` exits 1 with "The
      following paths are ignored by one of your .gitignore files".
    - **Gone and never tracked.** The step deleted an untracked file that was
      dirty before it ran. ``git add`` exits 128 with "pathspec ... did not
      match any files".

    A tracked file that was deleted is *not* unstageable — the index still
    carries it, and staging the deletion is exactly right.
    """
    if not paths:
        return set()
    dropped = _git_ignored(root, paths)
    missing = [
        path
        for path in paths
        if path not in dropped and not os.path.lexists(os.path.join(root, path))
    ]
    if missing:
        indexed = _git_indexed(root, missing)
        dropped.update(path for path in missing if path not in indexed)
    return dropped


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "--hook-fixes":
        for p in hook_fixes(argv[2]):
            print(p)
        return 0
    root: str | None = None
    ignore: set[str] = set()
    ignore_prefixes: list[str] = []
    positional: list[str] = []
    i = 1
    while i < len(argv):
        if argv[i] == "--root":
            if i + 1 >= len(argv):
                print("--root requires a DIR argument", file=sys.stderr)
                return 2
            root = argv[i + 1]
            i += 2
        elif argv[i] == "--ignore":
            if i + 1 >= len(argv):
                print("--ignore requires a PATH argument", file=sys.stderr)
                return 2
            ignore.add(argv[i + 1])
            i += 2
        elif argv[i] == "--ignore-prefix":
            if i + 1 >= len(argv):
                print("--ignore-prefix requires a PATH argument", file=sys.stderr)
                return 2
            # A trailing slash would break the `path == prefix` arm of the
            # comparison, so normalise it away here rather than at every call site.
            ignore_prefixes.append(argv[i + 1].rstrip("/"))
            i += 2
        else:
            positional.append(argv[i])
            i += 1

    if len(positional) != 2:
        print(
            "usage: _snapshot_diff.py <pre> <post> [--root <dir>]\n"
            "                        [--ignore <path>]... [--ignore-prefix <path>]...\n"
            "       _snapshot_diff.py --hook-fixes <status>",
            file=sys.stderr,
        )
        return 2
    pre = parse(positional[0])
    post = parse(positional[1])
    changed = set()
    for p, s in post.items():
        if pre.get(p) != s:
            changed.add(p)
    for p in pre:
        if p not in post:
            changed.add(p)

    paths = [
        p
        for p in sorted(changed)
        if p not in ignore and not under_any_prefix(p, ignore_prefixes)
    ]
    if root is not None:
        dropped = unstageable_paths(paths, root)
        if dropped:
            # Loud on stderr: these paths changed during the step but are not
            # going into the commit, and silence there would look like data loss.
            print("diff: skipping paths git cannot stage:", file=sys.stderr)
            for p in sorted(dropped):
                print(f"  {p}", file=sys.stderr)
        paths = [p for p in paths if p not in dropped]

    for p in paths:
        print(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

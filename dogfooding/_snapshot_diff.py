"""Diff two `git status --porcelain -uall` snapshots and print changed paths.

Usage: python _snapshot_diff.py <pre> <post>

A path is "changed during this step" if its porcelain status in <post>
differs from its status in <pre>. Newly-appearing paths are always changed.
Paths that disappear (e.g. a tracked-modified file got reverted) are also
emitted so `git add -A -- <path>` can stage the cleanup.

Renames are reported under the new name. Quoted paths (filenames with
special chars) are decoded.
"""
from __future__ import annotations

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


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: _snapshot_diff.py <pre> <post>", file=sys.stderr)
        return 2
    pre = parse(argv[1])
    post = parse(argv[2])
    changed = set()
    for p, s in post.items():
        if pre.get(p) != s:
            changed.add(p)
    for p in pre:
        if p not in post:
            changed.add(p)
    for p in sorted(changed):
        print(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

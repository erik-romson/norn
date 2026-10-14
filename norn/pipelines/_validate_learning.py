"""Check a learning proposal before a human commits it.

Usage::

    <python> _validate_learning.py <doc> --repo <dir> --target <repo-rel> [--base <sha>]

``--target`` is where the proposal will be copied (a repo-relative path). The
check is ``norn.pipelines._learnings.validate_learning``; it fails closed.

Exit codes: 0 clean, 1 problems found (printed one per line), 2 usage error
(bad arguments, unreadable doc, unusable ``docs_root``).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from norn.pipelines._learnings import (
    StoreConfigError,
    resolve_store,
    store_relpath,
    validate_learning,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="_validate_learning.py", description=__doc__)
    parser.add_argument("doc", help="the proposed learning document")
    parser.add_argument("--repo", required=True, help="the repository the learning belongs to")
    parser.add_argument("--target", required=True, help="repo-relative path the doc will be copied to")
    parser.add_argument("--base", default=None, help="base sha the cited commits must descend from")
    args = parser.parse_args(argv)

    repo = Path(args.repo)
    try:
        doc_text = Path(args.doc).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"cannot read {args.doc}: {exc}", file=sys.stderr)
        return 2
    try:
        store = resolve_store(repo)
        store_rel = store_relpath(repo, store)
    except StoreConfigError as exc:
        print(f"store config error: {exc}", file=sys.stderr)
        return 2

    existing_text: str | None = None
    target_path = repo / args.target
    if "\n" not in args.target and target_path.is_file():
        try:
            existing_text = target_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"cannot read existing target {args.target}: {exc}", file=sys.stderr)
            return 2

    problems = validate_learning(
        doc_text,
        target=args.target,
        repo=repo,
        store_rel=store_rel,
        base_sha=args.base,
        existing_text=existing_text,
    )
    if problems:
        for line in problems:
            print(line)
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())

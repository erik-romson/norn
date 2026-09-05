"""Private helper for plan_with_review — argv → pre-plan path and derived artifact paths."""
from __future__ import annotations

import os
import re
from pathlib import Path

# Flags that consume the following token as their value.
_VALUE_FLAGS = {"--arg", "--skip", "--org", "--agent-provider"}


def parse_arg_flags(argv: list[str]) -> dict[str, str]:
    """Return a dict of --arg KEY=VALUE pairs found in *argv*.

    Mirrors the idiom in norn/pipelines/check_and_fix_ci.py:136-149.
    """
    out: dict[str, str] = {}
    i = 0
    while i < len(argv):
        if argv[i] == "--arg" and i + 1 < len(argv) and "=" in argv[i + 1]:
            k, _, v = argv[i + 1].partition("=")
            out[k] = v
            i += 2
        else:
            i += 1
    return out


def resolve_preplan(argv: list[str], repo_dir: str) -> str:
    """Return the absolute path to the pre-plan file named in *argv*.

    Filters *argv* rather than indexing it so that both the ``norn run`` shape
    (``["run", "plan_with_review", "tmp/x-preplan.md", "--arg", "model=sonnet"]``)
    and the TUI shape (``["tmp/x-preplan.md"]``) work correctly.

    Raises ``ValueError`` (never ``SystemExit``) when zero or multiple candidates
    are found.
    """
    candidates: list[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token.startswith("-"):
            # --flag=value is one token; drop it whole.
            # --flag value: drop the flag and skip the value token too.
            if "=" not in token and token in _VALUE_FLAGS and i + 1 < len(argv):
                i += 2
            else:
                i += 1
            continue
        # Non-flag token: check whether it names an existing readable .md file.
        if token.endswith(".md"):
            if os.path.isfile(token) and os.access(token, os.R_OK):
                candidates.append(token)
            else:
                joined = os.path.join(repo_dir, token)
                if os.path.isfile(joined) and os.access(joined, os.R_OK):
                    candidates.append(joined)
        i += 1

    if len(candidates) == 1:
        return str(Path(candidates[0]).resolve())

    if len(candidates) == 0:
        raise ValueError(
            "plan_with_review needs the pre-plan markdown path as its positional "
            "argument, e.g. norn run plan_with_review tmp/x-preplan.md"
        )

    # More than one candidate — name them all.
    found = ", ".join(candidates)
    raise ValueError(
        f"plan_with_review found multiple pre-plan candidates and cannot choose: {found}"
    )


def slug_of(preplan_path: str) -> str:
    """Return the slug derived from a pre-plan path.

    Strips the ``.md`` suffix then one trailing ``-preplan`` or ``_preplan``
    if present.

    Examples::

        tmp/norn-fleet-preplan.md  → norn-fleet
        tmp/norn_fleet_preplan.md  → norn_fleet
        tmp/brief.md               → brief
    """
    stem = Path(preplan_path).stem  # e.g. "norn-fleet-preplan"
    stem = re.sub(r"[-_]preplan$", "", stem)
    return stem


def derive_paths(preplan_path: str) -> tuple[str, str, str, str]:
    """Return ``(plan, questions, review, response)`` as absolute sibling paths.

    The deliverable is ``<slug>-final-plan.md``; the working files keep the
    ``<slug>-plan`` base.  "final" is what tells the deliverable apart from the
    brief that produced it: a pre-plan named ``x-plan.md`` keeps its whole stem
    as the slug (only ``-preplan``/``_preplan`` is stripped), so the old
    ``<slug>-plan.md`` deliverable came out as ``x-plan-plan.md`` and read like
    a second copy of the input.

    All paths are absolute so they pass through ``resolve_run_path`` unchanged
    and land in the launch repo rather than any active worktree.
    """
    parent = Path(preplan_path).resolve().parent
    slug = slug_of(preplan_path)
    base = slug + "-plan"
    plan = str(parent / f"{slug}-final-plan.md")
    questions = str(parent / f"{base}-questions.md")
    review = str(parent / f"{base}-review.md")
    response = str(parent / f"{base}-review-response.md")
    return plan, questions, review, response


def steps_dir_of(plan_path: str) -> str:
    """Return the step-files directory for a plan path — the plan minus ``.md``.

    Examples::

        tmp/norn-fleet-plan.md  → tmp/norn-fleet-plan
    """
    path = Path(plan_path)
    return str(path.parent / path.stem)

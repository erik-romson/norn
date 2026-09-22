"""Resolution of per-pipeline runtime state (history / checkpoint) paths.

A pipeline's ``.history`` and ``.checkpoint`` live beside a *state key* derived
from how the pipeline was referenced. External ``.py`` files outside the cwd
use a cwd-local key first with the original location as a read fallback;
everything else uses the reference as-is. Shared by ``norn/cli.py`` (run /
history / resume) and the TUI history browser so both resolve identically.

``state_ignore_flags`` serves the other direction: a pipeline that computes
which files a step changed by diffing worktree snapshots has to subtract these
same paths, because norn writes them while the run is in flight.

``prepare_state_isolation`` is the belt to that suspenders: it registers the
same paths in ``.git/info/exclude`` so git never reports them at all, and fails
the run up front when they are already tracked.
"""
from __future__ import annotations

import logging
import os
import shlex
import subprocess
from pathlib import Path

from norn.checkpoint import Checkpoint, load_checkpoint
from norn.history import load_history

log = logging.getLogger(__name__)


def _state_key_candidates(config_arg: str, *, cwd: Path | None = None) -> list[str]:
    """Return preferred state-key paths for history/checkpoint files.

    External pipeline files that live outside the current working directory use
    a cwd-local state key first, with the legacy config-adjacent location as a
    fallback for reads.
    """
    cwd_path = (cwd or Path.cwd()).resolve()
    raw = Path(config_arg)
    if raw.suffix == ".py" and raw.exists():
        resolved = raw.resolve()
        if resolved.is_relative_to(cwd_path):
            return [str(resolved)]
        return [str((cwd_path / resolved.name).resolve()), str(resolved)]
    return [config_arg]


def _primary_state_key(config_arg: str, *, cwd: Path | None = None) -> str:
    """Return the preferred state key for new checkpoint/history writes."""
    return _state_key_candidates(config_arg, cwd=cwd)[0]


def _load_checkpoint_for_config(config_arg: str, *, cwd: Path | None = None) -> Checkpoint | None:
    """Load a checkpoint using primary state resolution with legacy fallback."""
    for candidate in _state_key_candidates(config_arg, cwd=cwd):
        checkpoint = load_checkpoint(candidate)
        if checkpoint is not None:
            return checkpoint
    return None


def _load_history_for_config(config_arg: str, *, cwd: Path | None = None) -> list:
    """Load history using primary state resolution with legacy fallback."""
    for candidate in _state_key_candidates(config_arg, cwd=cwd):
        records = load_history(candidate)
        if records:
            return records
    return load_history(_primary_state_key(config_arg, cwd=cwd))


# ---------------------------------------------------------------------------
# Subtracting norn's own state from a worktree diff
# ---------------------------------------------------------------------------


def state_artifact_paths(state_key: str | None, root: str) -> tuple[list[str], list[str]]:
    """Return ``(exact, prefix)`` paths of norn's run state, relative to *root*.

    norn writes ``<key>.checkpoint``, ``<key>.history`` and ``<key>.runs/<n>/``
    beside the launch directory and keeps writing to them for the whole run —
    the checkpoint is rewritten after every stage and the journal gains a line
    per event. When the launch directory sits inside the target repository (the
    normal case), those files land in the worktree, so every snapshot diff sees
    them change.

    ``.runs`` is returned as a prefix because ``git status`` names the files
    inside it individually; the other two are exact paths. Anything outside
    *root* is dropped — it can never appear in ``git status``, so ignoring it
    would only add noise to the command line.
    """
    if not state_key:
        return [], []
    # Mirror journal.runs_dir / checkpoint_file / history_file exactly:
    # resolve first, then swap the suffix.
    base = Path(state_key).resolve()
    exact: list[str] = []
    prefix: list[str] = []
    for suffix, bucket in ((".checkpoint", exact), (".history", exact), (".runs", prefix)):
        try:
            rel = os.path.relpath(base.with_suffix(suffix), root)
        except ValueError:
            # On Windows, relpath raises when paths are on different drives.
            continue
        if rel == ".." or rel.startswith(".." + os.sep):
            continue
        bucket.append(rel)
    return exact, prefix


def state_ignore_flags(state_key: str | None, root: str) -> str:
    """Render :func:`state_artifact_paths` as snapshot-helper ignore flags.

    Both snapshot helpers (``_snapshot_diff.py`` for v1, ``_step_snapshot.py``
    for v2) accept the same ``--ignore`` / ``--ignore-prefix`` spelling, so one
    string serves either. Empty when there is no state key — importing a
    pipeline outside a run (the TUI, ``--dry-run``, tests) must not change the
    shell it builds.
    """
    exact, prefixes = state_artifact_paths(state_key, root)
    return "".join(f" --ignore {shlex.quote(p)}" for p in exact) + "".join(
        f" --ignore-prefix {shlex.quote(p)}" for p in prefixes
    )


# ---------------------------------------------------------------------------
# Keeping norn's own state off the target repository's git surface
#
# Subtracting the state paths from a snapshot diff (above) only works when the
# pipeline module knows the state key, which means every entry point has to
# export NORN_STATE_KEY *before* importing the pipeline.  `norn run` did; the
# TUI (`norn ui`) did not, so a TUI-launched implement_features_v2 committed its
# own run journal as a step-owned change and then failed its own "owned paths
# are clean" postcondition on the next journal write.
#
# Exporting the key from every entry point fixes that instance.  The functions
# below remove the whole class: a run whose state lands inside a git worktree
# registers those paths in `.git/info/exclude`, so git never reports them in
# `git status` at all.  No gate, present or future, in any pipeline, has to
# remember to subtract them, and an agent running `git add -A` cannot sweep
# them in either.
#
# `.git/info/exclude` is the git-native place for local, per-clone ignores: it
# is never committed and never shows up as a working-tree change, which is why
# the repository's own `.gitignore` is the wrong file to write.
# ---------------------------------------------------------------------------


class StateTrackedError(RuntimeError):
    """norn's own run state is tracked by git in the target repository.

    Ignore rules do not apply to tracked files, so the journal keeps showing up
    as a modification for the rest of the run.  Every commit gate that asserts
    "owned paths are clean" then fails, and no amount of filtering can fix it
    from inside the run — the file has to be untracked first.
    """


def _git_toplevel(start: str) -> str | None:
    """Return the git worktree root containing *start*, or ``None``."""
    result = subprocess.run(
        ["git", "-C", start, "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _git_common_dir(root: str) -> Path | None:
    """Return the shared git directory for *root* (where ``info/exclude`` lives).

    ``--git-common-dir`` rather than ``--git-dir`` so a linked worktree writes
    to the main repository's exclude file, which applies to every worktree.
    """
    result = subprocess.run(
        ["git", "-C", root, "rev-parse", "--git-common-dir"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    return Path(raw) if os.path.isabs(raw) else (Path(root) / raw)


def tracked_state_paths(state_key: str | None, root: str) -> list[str]:
    """Return norn's state artifacts that git already tracks under *root*.

    A directory pathspec matches everything beneath it, so passing the
    ``.runs`` prefix finds any journal file inside it.
    """
    exact, prefixes = state_artifact_paths(state_key, root)
    pathspecs = exact + prefixes
    if not pathspecs:
        return []
    result = subprocess.run(
        ["git", "-C", root, "ls-files", "-z", "--", *pathspecs],
        capture_output=True,
    )
    if result.returncode != 0:
        # The caller has already established that *root* is a git worktree, so
        # a failure here is a real git error, not "no repository".
        raise RuntimeError(
            f"git ls-files failed in {root}: "
            f"{result.stderr.decode('utf-8', errors='replace').strip()}"
        )
    return [
        part.decode("utf-8", errors="surrogateescape")
        for part in result.stdout.split(b"\0")
        if part
    ]


def _escape_gitignore(path: str) -> str:
    """Escape the characters git treats as pattern syntax in an ignore line."""
    escaped = "".join("\\" + ch if ch in "\\*?[]" else ch for ch in path)
    # A trailing space is stripped by git unless it is escaped.
    return escaped[:-1] + "\\ " if escaped.endswith(" ") else escaped


def state_exclude_lines(state_key: str | None, root: str) -> list[str]:
    """Return `.gitignore`-syntax lines covering norn's state under *root*.

    Anchored with a leading ``/`` so the pattern names exactly this path and not
    a same-named file elsewhere in the tree; directories carry a trailing ``/``.
    """
    exact, prefixes = state_artifact_paths(state_key, root)
    lines = [f"/{_escape_gitignore(p)}" for p in exact]
    lines += [f"/{_escape_gitignore(p)}/" for p in prefixes]
    return lines


_EXCLUDE_HEADER = "# norn: run state written into this worktree while a run is in flight"


def register_git_exclude(state_key: str | None, root: str) -> list[str]:
    """Add norn's state paths to ``.git/info/exclude``; return the lines added.

    Idempotent: a line already present anywhere in the file is not repeated, so
    a repeat run of the same pipeline adds nothing.  Returns an empty list when
    *root* has no git directory or every line is already there.
    """
    wanted = state_exclude_lines(state_key, root)
    if not wanted:
        return []
    common = _git_common_dir(root)
    if common is None:
        return []
    path = common / "info" / "exclude"
    current = path.read_text() if path.exists() else ""
    present = set(current.splitlines())
    missing = [line for line in wanted if line not in present]
    if not missing:
        return []
    block: list[str] = []
    if current and not current.endswith("\n"):
        block.append("")          # close the unterminated last line
    if current.strip():
        block.append("")          # blank line between the old content and ours
    if _EXCLUDE_HEADER not in present:
        block.append(_EXCLUDE_HEADER)
    block.extend(missing)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write("\n".join(block) + "\n")
    return missing


def publish_state_key(config_arg: str, *, cwd: Path | None = None) -> str:
    """Resolve the state key for *config_arg* and export it as ``NORN_STATE_KEY``.

    Pipeline modules read this env var **at import time** to build their
    snapshot-diff shell, so every entry point must call this before the pipeline
    is loaded.  There are three: ``norn run`` (``norn/cli.py``), the TUI launcher
    (``norn/tui/launch.py``), and a child run, which is a ``norn run``
    subprocess and so goes through the first.
    """
    key = _primary_state_key(config_arg, cwd=cwd)
    os.environ["NORN_STATE_KEY"] = key
    return key


def prepare_state_isolation(state_key: str, *, root: str | None = None) -> None:
    """Keep this run's state files out of the target repository's git surface.

    Called by the runner for every run that has a state key, so it covers every
    entry point — including one added later that forgets to call
    :func:`publish_state_key`.

    Three things, in order:

    1. Repair ``NORN_STATE_KEY`` when an entry point left it unset or stale, and
       warn — the pipeline module is already imported by now, so the repair
       helps only lazily-built commands, but the warning names the bug.
    2. Fail hard when git already **tracks** norn's state.  Ignore rules never
       apply to tracked files, so the run is guaranteed to fail a commit gate
       later; failing at second zero costs nothing instead of many dollars.
    3. Register the state paths in ``.git/info/exclude``, which makes every
       git-based gate in every pipeline blind to them.
    """
    published = os.environ.get("NORN_STATE_KEY")
    if published != state_key:
        log.warning(
            "NORN_STATE_KEY was %s at run start, expected %r — the entry point "
            "that loaded this pipeline should call norn.state.publish_state_key "
            "before importing it.",
            f"{published!r}" if published else "unset",
            state_key,
        )
        os.environ["NORN_STATE_KEY"] = state_key

    resolved_root = root or _git_toplevel(os.getcwd())
    if resolved_root is None:
        return

    tracked = tracked_state_paths(state_key, resolved_root)
    if tracked:
        listing = "\n".join(f"  {p}" for p in tracked[:10])
        more = f"\n  ... and {len(tracked) - 10} more" if len(tracked) > 10 else ""
        top = sorted({p.split("/", 1)[0] for p in tracked})
        raise StateTrackedError(
            "norn's own run state is tracked by git in this repository:\n"
            f"{listing}{more}\n"
            "norn rewrites these files for the whole run, and git ignore rules "
            "do not apply to tracked files, so every commit gate that asserts "
            '"owned paths are clean" will fail.\n'
            "Untrack them and rerun:\n"
            f"  git rm -r --cached {' '.join(top)}\n"
            "  git commit -m 'chore: untrack norn run state'"
        )

    added = register_git_exclude(state_key, resolved_root)
    if added:
        log.info(
            "norn: added %s to .git/info/exclude so git ignores this run's state",
            ", ".join(added),
        )

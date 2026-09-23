"""Test-only fakes shared by the merge_branch test modules.

No ``test_`` prefix, so pytest does not collect this module itself. Step 01
added ``ctx_for``; step 02 adds ``install_fake_gh`` and
``make_repo_with_remote``.
"""
from __future__ import annotations

import json
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import git

from norn.models import PipelineContext


def ctx_for(
    params: dict[str, Any] | None = None,
    checkpoint: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
    working_dir: str | None = None,
) -> PipelineContext:
    """Build a ``PipelineContext`` for offline stage tests.

    ``checkpoint`` seeds ``ctx.checkpoint_stages`` — the stage names a real
    ``--resume`` would have restored — so ``AssertFreshRun``'s resume guard
    can be exercised without a real checkpoint file.
    """
    return PipelineContext(
        params=dict(params or {}),
        checkpoint_stages=set(checkpoint),
        env=dict(env or {}),
        working_dir=working_dir,
    )


# ---------------------------------------------------------------------------
# install_fake_gh
# ---------------------------------------------------------------------------

# Written out as a standalone script (not a template with literal braces
# elsewhere) so f-string interpolation of the three state-file paths below is
# safe without any brace-escaping gymnastics.
_FAKE_GH_SCRIPT = """#!{python}
import fcntl
import json
import sys

ROUTES_PATH = {routes_path!r}
COUNTERS_PATH = {counters_path!r}
CALLS_LOG = {calls_log!r}

argv = sys.argv[1:]

with open(CALLS_LOG, "a") as fh:
    fcntl.flock(fh, fcntl.LOCK_EX)
    fh.write(json.dumps(argv) + "\\n")
    fcntl.flock(fh, fcntl.LOCK_UN)

with open(ROUTES_PATH) as fh:
    routes = json.load(fh)

best = None
for i, route in enumerate(routes):
    prefix = route["prefix"]
    if argv[: len(prefix)] == prefix:
        if best is None or len(prefix) > len(routes[best]["prefix"]):
            best = i

if best is None:
    sys.stderr.write("fake gh: no route for " + " ".join(argv) + "\\n")
    sys.exit(127)

with open(COUNTERS_PATH) as fh:
    counters = json.load(fh)

responses = routes[best]["responses"]
idx = min(counters[best], len(responses) - 1)
rc, stdout, stderr = responses[idx]
counters[best] = min(counters[best] + 1, len(responses) - 1)

with open(COUNTERS_PATH, "w") as fh:
    json.dump(counters, fh)

sys.stdout.write(stdout)
sys.stderr.write(stderr)
sys.exit(rc)
"""


def install_fake_gh(
    tmp_path: Path, routes: dict[tuple[str, ...], list[tuple[int, str, str]]]
) -> tuple[str, str]:
    """Install a fake ``gh`` executable under a throwaway bin dir.

    *routes* maps an argv prefix (the args after ``gh``, e.g.
    ``("repo", "view")``) to a list of ``(rc, stdout, stderr)`` responses,
    consumed in call order with the last one repeating. The longest matching
    prefix wins when more than one route matches a call.

    Every invocation is appended as one JSON line (the full argv) to the
    returned calls-log path, so tests can assert on exactly what was called
    and in what order.

    Returns ``(bin_dir, calls_log)``. Prepend ``bin_dir`` to ``PATH`` (e.g.
    via ``ctx_for(env={"PATH": f"{bin_dir}:{os.environ['PATH']}"})``) so
    stages that shell out to ``gh`` find this fake instead of the real thing.
    """
    state_dir = tmp_path / "fake-gh"
    state_dir.mkdir(parents=True, exist_ok=True)
    routes_path = state_dir / "routes.json"
    counters_path = state_dir / "counters.json"
    calls_log = state_dir / "calls.jsonl"

    encoded_routes = [
        {"prefix": list(prefix), "responses": [list(response) for response in responses]}
        for prefix, responses in routes.items()
    ]
    routes_path.write_text(json.dumps(encoded_routes))
    counters_path.write_text(json.dumps([0] * len(encoded_routes)))
    calls_log.write_text("")

    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    script_path = bin_dir / "gh"
    script_path.write_text(
        _FAKE_GH_SCRIPT.format(
            python=sys.executable,
            routes_path=str(routes_path),
            counters_path=str(counters_path),
            calls_log=str(calls_log),
        )
    )
    mode = script_path.stat().st_mode
    script_path.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    return str(bin_dir), str(calls_log)


# ---------------------------------------------------------------------------
# make_repo_with_remote
# ---------------------------------------------------------------------------


@dataclass
class RepoWithRemote:
    """A working repo plus its bare ``origin``, both under ``tmp_path``."""

    path: str
    origin_path: str
    repo: git.Repo


def make_repo_with_remote(tmp_path: Path) -> RepoWithRemote:
    """Build a gitpython working repo with a bare ``origin``, ``main`` and ``develop`` pushed.

    ``user.email``/``user.name`` are configured on the working repo so every
    commit made against it (including by a test that wants a "same author"
    remote-only commit) carries a real identity.
    """
    origin_path = tmp_path / "origin.git"
    git.Repo.init(str(origin_path), bare=True, initial_branch="main")

    work_path = tmp_path / "work"
    repo = git.Repo.init(str(work_path), initial_branch="main")
    with repo.config_writer() as cw:
        cw.set_value("user", "email", "erik@zenior.no")
        cw.set_value("user", "name", "Erik")

    (work_path / "README.md").write_text("hello\n")
    repo.index.add(["README.md"])
    repo.index.commit("initial commit")

    origin = repo.create_remote("origin", str(origin_path))
    origin.push("main")

    repo.git.checkout("-b", "develop")
    (work_path / "DEVELOP.md").write_text("develop\n")
    repo.index.add(["DEVELOP.md"])
    repo.index.commit("develop commit")
    origin.push("develop")

    repo.git.checkout("main")

    return RepoWithRemote(path=str(work_path), origin_path=str(origin_path), repo=repo)

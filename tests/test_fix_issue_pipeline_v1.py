"""Tests for the fix_issue_v1 bundled pipeline.

Covers parity with fix_jira_issue_v3 for a Jira key, the per-source values
threaded into the stages for GitHub references (same repo, other repo,
look-alike slugs), the stage names, the absence of Jira wording in a GitHub
run's prompts, the preflight and prepare-branch commands under ``sh`` with
real git in ``tmp_path``, the run identity on resume, the import-time errors,
and the catalog entry.
"""
from __future__ import annotations

import importlib
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from norn.alerts import MacOSChannel
from norn.catalog import get_pipeline_info
from norn.checkpoint import load_checkpoint, save_checkpoint
from norn.dsl import ClearContext, Loop, Stage
from norn.event_sink import EventSink
from norn.events import StageStarted
from norn.graph import build_graph
from norn.models import PipelineContext, StageResult
from norn.pipelines import (
    _child_cost_v3,
    _fix_jira_v3,
    _merge_gh,
    _merge_state,
    _pr_v3,
)
from norn.pipelines._ship_v3 import BODY_END, BODY_START
from norn.responder import NonInteractiveResponder
from norn.runner import PipelineError, run_pipeline
from norn.stages.generate import Generate

ORIGIN = "https://github.com/acme/app.git"
OTHER_URL = "https://github.com/other/lib/issues/45"
OTHER_KEY = "issue-other-lib-45-2b9ddc19"

# ---------------------------------------------------------------------------
# Import helpers
# ---------------------------------------------------------------------------


# Copied from tests/test_fix_jira_issue_pipeline_v3.py:54-72 (do not import from
# it: it is the v3 control), generalised over the module and renamed.
def _import_module(name: str, argv_tail: list[str], cwd: str | Path | None = None):
    """Import ``norn.pipelines.<name>`` with patched argv and cwd."""
    saved_argv = list(sys.argv)
    saved_cwd = os.getcwd()
    try:
        sys.argv = [saved_argv[0], *argv_tail]
        if cwd is not None:
            os.chdir(str(cwd))
        module = importlib.import_module(f"norn.pipelines.{name}")
        return importlib.reload(module)
    finally:
        sys.argv = saved_argv
        os.chdir(saved_cwd)


def _import_fix_issue(argv_tail: list[str], cwd: str | Path | None = None):
    """Import fix_issue_v1 with *argv_tail* as its positionals and *cwd* as the launch dir."""
    return _import_module("fix_issue_v1", argv_tail, cwd)


def _import_v3(argv_tail: list[str], cwd: str | Path | None = None):
    return _import_module("fix_jira_issue_v3", argv_tail, cwd)


# Copied from tests/test_fix_jira_issue_pipeline_v3.py:83-98, with notify fakes
# that take **kwargs (fix_issue_v1 passes pipeline=) and no GH_HOST leaking in.
@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    """No osascript, and no user env leaking into the import-time knobs."""
    monkeypatch.delenv("JIRA_BROWSE_BASE", raising=False)
    monkeypatch.delenv("GH_HOST", raising=False)
    notices: list[tuple[str, str]] = []

    async def _notify(title: str, detail: str, **kwargs) -> None:
        notices.append((title, detail))

    async def _send(self, msg) -> None:
        notices.append((msg.title, msg.body))

    for module in (_merge_gh, _merge_state, _pr_v3, _child_cost_v3, _fix_jira_v3):
        monkeypatch.setattr(module, "notify", _notify)
    monkeypatch.setattr(MacOSChannel, "send", _send)
    return notices


# ---------------------------------------------------------------------------
# Walkers — copied from tests/test_fix_jira_issue_pipeline_v3.py:106-160
# ---------------------------------------------------------------------------


def _collect_top_names(items):
    """Return a list of top-level item names (or 'ClearContext')."""
    names = []
    for item in items:
        if isinstance(item, (Stage, Loop)):
            names.append(item.name)
        elif isinstance(item, ClearContext):
            names.append("ClearContext")
    return names


def _all_stage_names(items) -> list[str]:
    """Every stage name in run order: top-level stages and loop bodies (not loop names)."""
    names: list[str] = []
    for item in items:
        if isinstance(item, Stage):
            names.append(item.name)
        elif isinstance(item, Loop):
            names.extend(s.name for s in item.stages)
    return names


def _find_stage(items, name):
    for item in items:
        if isinstance(item, Stage) and item.name == name:
            return item
    raise KeyError(f"Stage {name!r} not found")


def _find_any(items, name):
    """A Stage by name, at the top level or inside a loop body."""
    for item in items:
        if isinstance(item, Stage) and item.name == name:
            return item
        if isinstance(item, Loop):
            for stage in item.stages:
                if stage.name == name:
                    return stage
    raise KeyError(f"Stage {name!r} not found")


def _collect_generates(items):
    """Yield ``(stage name, Generate)`` for every Generate in the pipeline."""
    for item in items:
        if isinstance(item, Stage) and isinstance(item.impl, Generate):
            yield item.name, item.impl
        elif isinstance(item, Loop):
            yield from _collect_generates(item.stages)


# ---------------------------------------------------------------------------
# Git helpers — copied from tests/test_fix_jira_issue_pipeline_v3.py:600-693,
# parametrised for the origin, the exclude content, the JIRA_* env and the
# gh answers.
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _fake_bin(tmp_path: Path, name: str, body: str) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    tool = bin_dir / name
    tool.write_text(f"#!/bin/sh\n{body}\n")
    tool.chmod(0o755)
    return bin_dir


def _repo_on_branch(tmp_path: Path, branch: str, *, origin: str | None = ORIGIN) -> Path:
    """Init a repo with one commit on main, checked out on *branch*, with *origin*."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "commit.gpgsign", "false")
    if origin is not None:
        _git(repo, "remote", "add", "origin", origin)
    (repo / "README.md").write_text("# test\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "initial")
    if branch != "main":
        _git(repo, "switch", "-q", "-c", branch)
    return repo


def _commit(repo: Path, name: str, text: str, message: str) -> str:
    (repo / name).write_text(text)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def gh_repo(tmp_path) -> Path:
    """A launch repo whose origin is acme/app on github.com."""
    return _repo_on_branch(tmp_path, "main")


def _preflight_repo(tmp_path: Path, *, exclude: str) -> Path:
    repo = _repo_on_branch(tmp_path, "main")
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "exclude").write_text(exclude)
    return repo


def _run_preflight(
    mod,
    repo: Path,
    tmp_path: Path,
    *,
    push: str = "true",
    is_pr: str = "false",
    jira_env: bool = False,
) -> subprocess.CompletedProcess:
    gh = (
        'case "$1" in\n'
        "  auth) exit 0;;\n"
        "  repo) echo acme/app;;\n"
        f'  api) case "$2" in */issues/*) echo {is_pr};; *) echo {push};; esac;;\n'
        "  *) exit 9;;\n"
        "esac"
    )
    bin_dir = _fake_bin(tmp_path, "gh", gh)
    for tool in ("curl", "jq", "codex"):
        _fake_bin(tmp_path, tool, "exit 0")
    env = {k: v for k, v in os.environ.items() if not k.startswith("JIRA_")}
    env["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
    # The developer's own git config (commit.cleanup, a global excludes file)
    # must not decide these checks.
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    if jira_env:
        env["JIRA_AUTH"] = "x"
        env["JIRA_BASE"] = "https://jira.example.com/"
    cmd = _find_stage(mod.config.items, "preflight environment").impl.cmd
    return subprocess.run(cmd, shell=True, cwd=repo, capture_output=True, text=True, env=env)


def _run_prepare_branch(mod, repo: Path) -> subprocess.CompletedProcess:
    cmd = _find_stage(mod.config.items, "prepare branch").impl.cmd
    return subprocess.run(cmd, shell=True, cwd=repo, capture_output=True, text=True)


# ---------------------------------------------------------------------------
# Parity with fix_jira_issue_v3 (Jira)
# ---------------------------------------------------------------------------

_JIRA_ARGVS = [
    ["CBS-1234"],
    ["CBS-1234", "auto"],
    ["CBS-1234", "--arg", "plan_gate=off"],
]


@pytest.mark.parametrize("argv", _JIRA_ARGVS, ids=["interactive", "auto", "plan_gate_off"])
def test_jira_parity_with_v3(tmp_path, argv):
    v3 = _import_v3(argv, cwd=tmp_path)
    v1 = _import_fix_issue(argv, cwd=tmp_path)

    # The one stage v1 adds: the PR base is checked right after the run identity.
    v1_top = _collect_top_names(v1.config.items)
    assert v1_top[v1_top.index("check run identity") + 1] == "check pr base"
    v1_top.remove("check pr base")
    v1_all = _all_stage_names(v1.config.items)
    v1_all.remove("check pr base")
    assert v1_top == _collect_top_names(v3.config.items)
    assert v1_all == _all_stage_names(v3.config.items)
    assert (
        _find_stage(v1.config.items, "fetch issue").impl.cmd
        == _find_stage(v3.config.items, "fetch issue").impl.cmd
    )
    # `prepare branch` differs only in also writing base.branch with base.sha.
    v1_prepare = _find_stage(v1.config.items, "prepare branch").impl.cmd
    v3_prepare = _find_stage(v3.config.items, "prepare branch").impl.cmd
    assert v1_prepare.split("mkdir -p ")[0] == v3_prepare.split("mkdir -p ")[0]
    assert v1.START_BRANCH_FILE in v1_prepare

    abs_dir = v3.ART.dir.rstrip("/")
    expected = (
        v3._preflight_cmd
        .replace("fix_jira_issue_v3.checkpoint", "fix_issue_v1.checkpoint")
        .replace(
            f'Add to .git/info/exclude: {abs_dir}"', 'Add to .git/info/exclude: tmp/jira/CBS-1234/"'
        )
        # The check-ignore path keeps its trailing slash (a directory to git).
        .replace(
            f"git check-ignore -q {shlex.quote(abs_dir)} ||",
            f"git check-ignore -q {shlex.quote(v3.ART.dir)} ||",
        )
    )
    assert v1._preflight_cmd == expected
    assert (v1.V3, v1.BASE_FILE, v1.STEPS_DIR, v1.CHILD) == (v3.V3, v3.BASE_FILE, v3.STEPS_DIR, v3.CHILD)
    assert v1.AUTO == v3.AUTO
    assert v1.BRANCH == v3.BRANCH == "CBS-1234"


def test_jira_has_no_fetch_notes(tmp_path):
    mod = _import_fix_issue(["CBS-1234"], cwd=tmp_path)
    items = mod.config.items
    checks = _find_stage(items, "check issue files").impl.checks
    assert [c.path for c in checks] == [mod.ART.issue_json, mod.ART.issue_md]
    assert _find_stage(items, "assemble pr body").impl.extra_checklist is None
    for name in ("stop for human", "announce pr ready", "summary"):
        impl = _find_stage(items, name).impl
        assert impl.fetch_notes is None and impl.notes_path is None, name
    assert _find_stage(items, "ship decision recorded").impl.key == "CBS-1234"
    assert _find_stage(items, "check pr provenance").impl.key == "CBS-1234"
    assert mod.metadata["env_vars"] == ["ANTHROPIC_API_KEY"]


# ---------------------------------------------------------------------------
# GitHub sources
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ref", ["123", "acme/app#123"])
def test_github_same_repo_values(gh_repo, ref):
    mod = _import_fix_issue([ref], cwd=gh_repo)
    items = mod.config.items

    assert mod.ART.dir.endswith("/tmp/github/123/")
    assert mod.V3.key == "issue-123"
    assert mod.BRANCH == "issue-123"

    fetch = _find_stage(items, "fetch issue").impl.cmd
    assert "acme/app 123" in fetch
    assert "-I -m norn.pipelines._github_issue notes" in fetch

    preflight = _find_stage(items, "preflight environment").impl.cmd
    assert "JIRA_" not in preflight
    assert "repos/acme/app/issues/123" in preflight

    assert _find_stage(items, "ship decision recorded").impl.key == "#123"
    assert _find_stage(items, "commit review fixes").impl.message.startswith("#123: ")
    assert _find_stage(items, "update pr body").impl.subject_prefix == "#123"
    assert _find_stage(items, "refresh pr body").impl.subject_prefix == "#123"
    provenance = _find_stage(items, "check pr provenance").impl
    assert provenance.key == "123"
    assert provenance.pipeline_name == "fix_issue_v1"
    assert _find_stage(items, "check run identity").impl._key == "acme/app#123"


def test_github_fetch_notes_are_threaded(gh_repo):
    mod = _import_fix_issue(["123"], cwd=gh_repo)
    items = mod.config.items

    checks = [c.path for c in _find_stage(items, "check issue files").impl.checks]
    assert checks[:2] == [mod.ART.issue_json, mod.ART.issue_md]
    assert len(checks) == 3 and checks[2].endswith("/tmp/github/123/fetch-notes.json")
    assert _find_stage(items, "assemble pr body").impl.extra_checklist is mod.SOURCE.fetch_checklist
    assert mod.SOURCE.fetch_checklist is not None
    for name in ("stop for human", "announce pr ready", "summary"):
        impl = _find_stage(items, name).impl
        assert impl.fetch_notes is not None and impl.fetch_notes is mod.SOURCE.fetch_notes, name
        assert impl.notes_path == mod.ART.issue_md, name


@pytest.mark.parametrize("ref", ["other/lib#45", OTHER_URL])
def test_github_other_repo_values(gh_repo, ref):
    mod = _import_fix_issue([ref], cwd=gh_repo)
    items = mod.config.items

    assert mod.ART.dir.endswith("/tmp/github/other-lib-45-2b9ddc19/")
    assert mod.V3.key == OTHER_KEY
    assert mod.BRANCH == OTHER_KEY
    assert mod.SOURCE.subject_prefix == "other/lib#45"
    assert mod.SOURCE.resume_arg == OTHER_URL
    assert "other/lib 45" in _find_stage(items, "fetch issue").impl.cmd
    assert _find_stage(items, "ship decision recorded").impl.key == "other/lib#45"
    assert _find_stage(items, "update pr body").impl.subject_prefix == "other/lib#45"
    assert _find_stage(items, "check pr provenance").impl.key == OTHER_URL


def test_look_alike_slugs_stay_apart(gh_repo):
    a = _import_fix_issue(["a-b/c#1"], cwd=gh_repo)
    a_values = (a.ART.dir, a.V3.key, a.BRANCH)
    b = _import_fix_issue(["a/b-c#1"], cwd=gh_repo)
    b_values = (b.ART.dir, b.V3.key, b.BRANCH)
    for left, right in zip(a_values, b_values):
        assert left != right


# ---------------------------------------------------------------------------
# Stage names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("auto", [False, True], ids=["interactive", "auto"])
def test_stage_names_are_the_same_for_every_source(gh_repo, auto):
    tail = ["auto"] if auto else []
    shapes = []
    for ref in ("CBS-1234", "123", "other/lib#45"):
        mod = _import_fix_issue([ref, *tail], cwd=gh_repo)
        build_graph(mod.config)
        shapes.append(
            (_collect_top_names(mod.config.items), _all_stage_names(mod.config.items))
        )
    assert shapes[0] == shapes[1] == shapes[2]
    top, every = shapes[0]
    for name in set(top) | set(every):
        for bad in ("#", "/", "123", "45", "CBS"):
            assert bad not in name, (name, bad)


# ---------------------------------------------------------------------------
# No Jira wording in a GitHub run
# ---------------------------------------------------------------------------

_JIRA_WORDS = ("Jira", "jira-fetch", "tmp/jira", "fix_jira_issue_v3")


@pytest.mark.asyncio
async def test_github_texts_carry_no_jira_wording(gh_repo):
    mod = _import_fix_issue(["123"], cwd=gh_repo)
    items = mod.config.items

    texts = {f"prompt of {name}": gen.prompt for name, gen in _collect_generates(items)}
    assert "prompt of write commit plan" in texts
    texts["preview of write commit plan"] = _find_any(items, "write commit plan").impl.prompt
    texts["preview of run codex code review"] = _find_any(items, "run codex code review").impl.cmd

    marker = Path(mod.V3.rewritten)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("")
    result = await _find_stage(items, "implement").impl.run(PipelineContext())
    assert not result.success
    assert f"delete {mod.ART.dir} " in result.error
    texts["rewritten-marker error"] = result.error

    for label, text in texts.items():
        # The PR-body markers are identifiers, not wording.
        text = text.replace(BODY_START, "").replace(BODY_END, "")
        for word in _JIRA_WORDS:
            assert word not in text, (label, word)


# ---------------------------------------------------------------------------
# Preflight under sh
# ---------------------------------------------------------------------------


def test_github_preflight_passes_without_jira_env(tmp_path):
    repo = _preflight_repo(tmp_path, exclude="tmp/\nfix_issue_v1.checkpoint\n")
    mod = _import_fix_issue(["123"], cwd=repo)
    r = _run_preflight(mod, repo, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr


def test_github_preflight_refuses_a_pull_request(tmp_path):
    repo = _preflight_repo(tmp_path, exclude="tmp/\nfix_issue_v1.checkpoint\n")
    mod = _import_fix_issue(["123"], cwd=repo)
    r = _run_preflight(mod, repo, tmp_path, is_pr="true")
    assert r.returncode == 1
    assert "#123 is a pull request, not an issue" in r.stdout


def test_github_preflight_prints_an_exclude_line_that_works(tmp_path):
    repo = _preflight_repo(tmp_path, exclude="fix_issue_v1.checkpoint\n")
    mod = _import_fix_issue(["123"], cwd=repo)

    r = _run_preflight(mod, repo, tmp_path)

    assert r.returncode == 1
    assert "Add to .git/info/exclude: tmp/github/123/" in r.stdout
    line = r.stdout.split("Add to .git/info/exclude: ", 1)[1].strip()
    assert line == "tmp/github/123/"
    # A first run: `prepare branch` has not created the directory yet.
    assert not Path(mod.ART.dir).exists()
    with (repo / ".git" / "info" / "exclude").open("a") as fh:
        fh.write(line + "\n")

    r = _run_preflight(mod, repo, tmp_path)

    assert r.returncode == 0, r.stdout + r.stderr


def test_jira_preflight_still_needs_the_jira_env(tmp_path):
    repo = _preflight_repo(tmp_path, exclude="tmp/\nfix_issue_v1.checkpoint\n")
    mod = _import_fix_issue(["CBS-1234"], cwd=repo)
    assert _run_preflight(mod, repo, tmp_path, jira_env=True).returncode == 0
    r = _run_preflight(mod, repo, tmp_path)
    assert r.returncode == 1
    assert "JIRA_AUTH is empty" in r.stdout


def test_preflight_names_this_pipelines_checkpoint(tmp_path):
    repo = _preflight_repo(tmp_path, exclude="tmp/\n")
    mod = _import_fix_issue(["123"], cwd=repo)
    r = _run_preflight(mod, repo, tmp_path)
    assert r.returncode == 1
    assert "Add to .git/info/exclude: fix_issue_v1.checkpoint" in r.stdout


# ---------------------------------------------------------------------------
# Prepare branch (real git)
# ---------------------------------------------------------------------------


def test_prepare_branch_github_leaves_the_starting_branch(tmp_path):
    repo = _repo_on_branch(tmp_path, "fix/123-x")
    head = _commit(repo, "a.txt", "a\n", "work on fix/123-x")
    mod = _import_fix_issue(["123"], cwd=repo)

    r = _run_prepare_branch(mod, repo)

    assert r.returncode == 0, r.stderr
    assert _git(repo, "branch", "--show-current") == "issue-123"
    assert _git(repo, "rev-parse", "issue-123") == head
    assert "Staying on branch" not in r.stdout


def test_prepare_branch_github_from_main(tmp_path):
    repo = _repo_on_branch(tmp_path, "main")
    mod = _import_fix_issue(["123"], cwd=repo)

    r = _run_prepare_branch(mod, repo)

    assert r.returncode == 0, r.stderr
    assert _git(repo, "branch", "--show-current") == "issue-123"


def test_prepare_branch_github_stays_on_its_own_branch(tmp_path):
    repo = _repo_on_branch(tmp_path, "issue-123")
    head = _commit(repo, "a.txt", "a\n", "step one")
    mod = _import_fix_issue(["123"], cwd=repo)

    r = _run_prepare_branch(mod, repo)

    assert r.returncode == 0, r.stderr
    assert _git(repo, "branch", "--show-current") == "issue-123"
    assert _git(repo, "rev-parse", "HEAD") == head


def test_prepare_branch_github_explicit_branch(tmp_path):
    repo = _repo_on_branch(tmp_path, "main")
    mod = _import_fix_issue(["123", "--arg", "branch=work"], cwd=repo)

    r = _run_prepare_branch(mod, repo)

    assert r.returncode == 0, r.stderr
    assert _git(repo, "branch", "--show-current") == "work"


def test_prepare_branch_jira_stays_on_a_branch_naming_the_key(tmp_path):
    repo = _repo_on_branch(tmp_path, "feature/cbs-1234-x")
    mod = _import_fix_issue(["CBS-1234"], cwd=repo)

    r = _run_prepare_branch(mod, repo)

    assert r.returncode == 0, r.stderr
    assert "Staying on branch feature/cbs-1234-x (it already names CBS-1234)" in r.stdout
    assert _git(repo, "branch", "--show-current") == "feature/cbs-1234-x"


def test_prepare_branch_writes_base_sha_only_when_missing(tmp_path):
    repo = _repo_on_branch(tmp_path, "main")
    first = _git(repo, "rev-parse", "HEAD")
    mod = _import_fix_issue(["123"], cwd=repo)
    assert _run_prepare_branch(mod, repo).returncode == 0
    assert Path(mod.BASE_FILE).read_text().strip() == first
    _commit(repo, "a.txt", "a\n", "step one")

    r = _run_prepare_branch(mod, repo)

    assert r.returncode == 0, r.stderr
    assert Path(mod.BASE_FILE).read_text().strip() == first
    assert _git(repo, "rev-parse", "HEAD") != first


def test_prepare_branch_records_the_branch_it_was_cut_from(tmp_path):
    repo = _repo_on_branch(tmp_path, "main")
    mod = _import_fix_issue(["123"], cwd=repo)

    r = _run_prepare_branch(mod, repo)

    assert r.returncode == 0, r.stderr
    assert _git(repo, "branch", "--show-current") == "issue-123"
    assert Path(mod.START_BRANCH_FILE).read_text() == "main\n"


def test_prepare_branch_records_no_start_branch_when_it_stays(tmp_path):
    repo = _repo_on_branch(tmp_path, "feature/cbs-1234-x")
    mod = _import_fix_issue(["CBS-1234"], cwd=repo)

    r = _run_prepare_branch(mod, repo)

    assert r.returncode == 0, r.stderr
    assert Path(mod.START_BRANCH_FILE).read_text() == "\n"


def test_prepare_branch_writes_base_branch_only_with_base_sha(tmp_path):
    repo = _repo_on_branch(tmp_path, "main")
    mod = _import_fix_issue(["123"], cwd=repo)
    assert _run_prepare_branch(mod, repo).returncode == 0
    _git(repo, "switch", "-q", "-c", "elsewhere")

    r = _run_prepare_branch(mod, repo)

    assert r.returncode == 0, r.stderr
    assert Path(mod.START_BRANCH_FILE).read_text() == "main\n"


def test_check_pr_base_and_resolve_branch_read_the_start_branch(tmp_path):
    mod = _import_fix_issue(["CBS-1234"], cwd=tmp_path)
    items = mod.config.items
    for name in ("check pr base", "resolve branch"):
        impl = _find_stage(items, name).impl
        assert impl._start_branch_file == mod.START_BRANCH_FILE, name
        assert impl._base_file == mod.BASE_FILE, name
    assert isinstance(_find_stage(items, "check pr base").impl, _pr_v3.CheckPrBase)
    assert _find_stage(items, "check pr base").always_run


# ---------------------------------------------------------------------------
# Run identity on resume (mocked agents, real git, notify patched by _quiet)
# ---------------------------------------------------------------------------


# Copied from tests/test_fix_jira_issue_pipeline_v3.py:890-973, with an origin,
# the issue-123 branch and this pipeline's state key.
class _Run:
    """A launch repo on branch issue-123 with a base commit and one step commit."""

    branch = "issue-123"

    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.tmp = tmp_path
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        for key, value in (
            ("user.name", "t"),
            ("user.email", "t@x"),
            ("commit.gpgsign", "false"),
            # No hook of the user's own may touch these commits.
            ("core.hooksPath", str(hooks)),
        ):
            _git(self.repo, "config", key, value)
        _git(self.repo, "remote", "add", "origin", ORIGIN)
        (self.repo / ".git" / "info").mkdir(exist_ok=True)
        (self.repo / ".git" / "info" / "exclude").write_text("tmp/\n")
        self.base = _commit(self.repo, "README.md", "# test\n", "initial")
        _git(self.repo, "switch", "-q", "-c", self.branch)
        self.step = _commit(self.repo, "a.txt", "one\n", "refactor: step-01 — add a")

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        self.state = tmp_path / "state"
        self.state.mkdir()
        self.config_path = str(self.state / "fix_issue_v1")
        monkeypatch.setenv("NORN_STATE_KEY", self.config_path)

    def load(self, argv: list[str]):
        mod = _import_fix_issue(argv, cwd=self.repo)
        self.mod = mod
        return mod

    def write_base(self, mod) -> None:
        Path(mod.V3.work).mkdir(parents=True, exist_ok=True)
        Path(mod.BASE_FILE).write_text(f"{self.base}\n")

    def checkpoint(self, mod, *, before: str, identity: str) -> None:
        """Save a checkpoint holding every stage before *before*."""
        names = _all_stage_names(mod.config.items)
        done = [n for n in names[: names.index(before)] if n != "diagnose implementation"]
        outputs: dict = {n: None for n in done}
        outputs["record run identity"] = {
            "issue": identity, "branch": self.branch, "kind": "github",
        }
        save_checkpoint(self.config_path, "fix_issue_v1", None, done, outputs)

    async def resume(self, mod) -> tuple[list[str], BaseException | None]:
        started: list[str] = []

        def on_event(event) -> None:
            if isinstance(event, StageStarted) and event.name not in started:
                started.append(event.name)

        checkpoint = load_checkpoint(self.config_path)
        assert checkpoint is not None
        saved_cwd = os.getcwd()
        # Not a git repository: the runner registers no exclude lines anywhere.
        os.chdir(self.state)
        try:
            await run_pipeline(
                mod.config,
                config_path=self.config_path,
                resume_checkpoint=checkpoint,
                event_sink=EventSink(on_event=on_event),
                input_responder=NonInteractiveResponder(),
            )
        except PipelineError as exc:
            return started, exc
        finally:
            os.chdir(saved_cwd)
        return started, None


def _no_agents(monkeypatch) -> list[str]:
    calls: list[str] = []

    async def fake_run(self, ctx, **kwargs):
        calls.append(self.prompt)
        return StageResult(name="", success=True)

    monkeypatch.setattr(Generate, "run", fake_run)
    return calls


@pytest.mark.asyncio
async def test_resume_with_another_issue_fails_at_check_run_identity(tmp_path, monkeypatch):
    run = _Run(tmp_path, monkeypatch)
    first = run.load(["123"])
    run.write_base(first)
    run.checkpoint(first, before="code review decided", identity="acme/app#123")
    calls = _no_agents(monkeypatch)

    other = run.load(["124"])
    started, error = await run.resume(other)

    assert started == ["assert launch tree", "check run identity"]
    assert isinstance(error, PipelineError) and error.stage_name == "check run identity"
    assert error.result.error.splitlines()[0] == (
        "checkpoint belongs to acme/app#123, not acme/app#124"
    )
    assert calls == []


@pytest.mark.asyncio
async def test_stale_base_names_the_absolute_base_file(tmp_path, monkeypatch):
    run = _Run(tmp_path, monkeypatch)
    mod = run.load(["123"])
    run.write_base(mod)
    run.checkpoint(mod, before="code review decided", identity="acme/app#123")
    # Another remote branch already holds the run's first commit.
    _git(run.repo, "update-ref", "refs/remotes/origin/develop", run.step)
    calls = _no_agents(monkeypatch)

    started, error = await run.resume(mod)

    assert started == ["assert launch tree", "check run identity"]
    assert isinstance(error, PipelineError) and error.stage_name == "check run identity"
    text = error.result.error
    assert text.splitlines()[0] == f"base.sha is stale: origin/develop already contains {run.step[:7]}"
    assert os.path.isabs(mod.BASE_FILE)
    assert mod.BASE_FILE.endswith("/tmp/github/123/base.sha")
    assert f"Delete {mod.BASE_FILE} and start from a fresh branch." in text
    assert "tmp/jira" not in text
    assert calls == []


# ---------------------------------------------------------------------------
# Import-time errors
# ---------------------------------------------------------------------------


def test_empty_branch_arg_raises(gh_repo):
    with pytest.raises(ValueError, match="--arg branch: empty value"):
        _import_fix_issue(["123", "--arg", "branch="], cwd=gh_repo)


@pytest.mark.parametrize("argv", [[], ["describe", "fix_issue_v1"]], ids=["empty", "describe"])
def test_no_issue_raises_without_running_git(tmp_path, monkeypatch, argv):
    marker = tmp_path / "git-called"
    shim = tmp_path / "shim"
    shim.mkdir()
    git = shim / "git"
    git.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 1\n")
    git.chmod(git.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{shim}:{os.environ['PATH']}")

    with pytest.raises(ValueError, match="fix_issue_v1"):
        _import_fix_issue(argv, cwd=tmp_path)

    assert not marker.exists()


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

_DOC_MARKERS = (
    "auto --non-interactive",
    "--resume",
    "--arg fix_retries=3",
    "--arg budget=75",
    "--arg jira_browse=",
    "plan-questions.md",
    "plan-approval.md",
    "code-review.md",
    "ship.md",
    "shipped-heads",
    "merge_branch",
    "checks_none",
)


def test_catalog_entry():
    from norn.catalog import arg_help, is_switch_arg
    from norn.tui.args_prompt import is_path_arg

    info = get_pipeline_info("fix_issue_v1")
    assert info is not None
    assert info.env_vars == ["ANTHROPIC_API_KEY"]
    assert list(info.args) == ["args", "auto"]
    assert not is_path_arg(info.args["args"])
    assert is_switch_arg(info.args["auto"])
    assert "never pauses to ask you" in arg_help(info.args["auto"])
    assert info.short.startswith("Fix a Jira or GitHub issue")
    for marker in _DOC_MARKERS:
        assert marker in info.long, marker
    for form in ("123", "'#123'", "owner/repo#123", "/issues/", "CBS-1234", "Jira issues only"):
        assert form in info.long, form


def test_metadata_matches_the_catalog(gh_repo):
    mod = _import_fix_issue(["123"], cwd=gh_repo)
    info = get_pipeline_info("fix_issue_v1")
    assert info is not None
    assert mod.metadata == {"env_vars": info.env_vars, "args": info.args}

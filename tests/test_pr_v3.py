"""Tests for the fix_jira_issue_v3 PR phase (stages 69-82): real git with a bare
remote, a fake ``gh`` (tests.merge_fakes.install_fake_gh), no agents."""
from __future__ import annotations

import inspect
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from norn.alerts import MacOSChannel
from norn.dsl import Pipeline
from norn.models import StageResult, UsageRecord
from norn.pipelines import _child_cost_v3, _fix_jira_v3, _merge_gh, _merge_state, _pr_v3
from norn.pipelines._child_cost_v3 import CiFixChildV3
from norn.pipelines._comment_style import ATTRIBUTION_ERE
from norn.pipelines._fix_jira_v3 import PIPELINE, V3Paths
from norn.pipelines._merge_gh import ResolveBranch, WaitForChecks
from norn.pipelines._merge_state import HaltingCommand, MergePaths
from norn.pipelines._pr_v3 import (
    NATIVE_PR_Q,
    WAIT_FOR_CHECKS_TIMEOUT,
    AnnouncePrReady,
    ChecksGreen,
    CheckPrBase,
    CheckPrProvenance,
    CheckPrRange,
    ResetPrState,
    ResolveBranchV3,
    RunSummary,
    StopForHuman,
    UpdatePrBody,
    add_pr_stages,
    draft_pr_cmd,
    pr_paths,
    refresh_sections,
    resume_hint,
    title_file,
)
from norn.pipelines._ship_v3 import BODY_END, BODY_START, render_pr_body
from tests.merge_fakes import ctx_for, install_fake_gh, make_repo_with_remote

KEY = "CBS-1"
REPO_NAME = "acme/widgets"
PR_URL = "https://github.com/acme/widgets/pull/42"
CI_FIX_SUBJECT = "fix(ci): auto-fix from check_and_fix_ci"

EXPECTED_STAGES = [
    "check pr provenance",
    "reset pr state",
    "resolve branch",
    "check pr range",
    "push",
    "update pr body",
    "open draft pr",
    "wait for checks 1",
    "fix ci 1",
    "sync after ci fix 1",
    "wait for checks 2",
    "fix ci 2",
    "sync after ci fix 2",
    "wait for checks final",
    "stop for human",
    "checks green",
    "refresh pr body",
    "announce pr ready",
    "summary",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeClock:
    """Deterministic clock/sleep pair: ``sleep`` advances the clock instead of
    waiting for real time, so a WaitForChecks poll loop never actually sleeps.
    """

    # Copied from tests/test_merge_branch_v1.py:1465-1477 (that file is hash-pinned)

    def __init__(self) -> None:
        self.t = 0.0

    def clock(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture(autouse=True)
def notices(monkeypatch) -> dict[str, list[tuple[str, str]]]:
    """Record both notifiers, so no test fires a real ``osascript``."""
    calls: dict[str, list[tuple[str, str]]] = {"merge": [], "v3": []}

    async def merge_notify(heading: str, detail: str) -> None:
        calls["merge"].append((heading, detail))

    async def v3_notify(title: str, detail: str) -> None:
        calls["v3"].append((title, detail))

    monkeypatch.setattr(_merge_gh, "notify", merge_notify)
    monkeypatch.setattr(_pr_v3, "notify", v3_notify)
    return calls


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(repo: Path, message: str, files: dict[str, str]) -> str:
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _gh(tmp_path: Path, routes: dict) -> tuple[dict[str, str], str]:
    bin_dir, calls_log = install_fake_gh(tmp_path, routes)
    return {"PATH": f"{bin_dir}:{os.environ['PATH']}"}, calls_log


def _calls(calls_log: str) -> list[list[str]]:
    return [json.loads(line) for line in Path(calls_log).read_text().splitlines() if line.strip()]


def _sh(script: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", "-c", script], cwd=cwd, env={**os.environ, **env}, capture_output=True, text=True
    )


@dataclass
class Run:
    repo: Path
    paths: V3Paths
    pr: MergePaths
    base_file: Path
    base: str
    head: str


def _run_repo(tmp_path: Path, *, from_branch: str = "main", push: bool = True) -> Run:
    """A branch with one reviewed commit on top of ``base.sha``, shipped at HEAD.

    The artifacts live outside the checkout, so ``git status`` sees only the repo.
    """
    rr = make_repo_with_remote(tmp_path / "checkout")
    repo = Path(rr.path)
    _git(repo, "checkout", "-q", "-b", KEY, from_branch)
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, "CBS-1: change the greeting", {"greet.txt": "hi\n", "other.txt": "o\n"})
    art = tmp_path / "art"
    art.mkdir()
    base_file = art / "base.sha"
    base_file.write_text(base + "\n")
    paths = V3Paths(str(art), KEY)
    Path(paths.work).mkdir(parents=True)
    Path(paths.shipped_heads).write_text(head + "\n")
    if push:
        _git(repo, "push", "-q", "-u", "origin", KEY)
    return Run(repo=repo, paths=paths, pr=pr_paths(paths), base_file=base_file, base=base, head=head)


def _write_root(root: MergePaths, /, **files: str) -> None:
    Path(root.root).mkdir(parents=True, exist_ok=True)
    names = {
        "repo": root.repo, "branch": root.branch, "base": root.base, "pr": root.pr,
        "pushed_head": root.pushed_head, "remote_head": root.remote_head,
        "green_head": root.green_head,
    }
    for name, value in files.items():
        Path(names[name]).write_text(value + "\n")


def _halt_root(pr: MergePaths, kind: str = "checks_red") -> None:
    Path(pr.root).mkdir(parents=True, exist_ok=True)
    Path(pr.halt_reason).write_text(f"KIND: {kind}\nheading\ndetail\n")
    Path(pr.halt).touch()


def _build(tmp_path: Path, *, run: Run | None = None, ci_rounds: int = 2, auto: bool = False):
    if run is None:
        paths = V3Paths(str(tmp_path / "art"), KEY)
        project_dir, base_file = str(tmp_path / "repo"), str(tmp_path / "art" / "base.sha")
    else:
        paths, project_dir, base_file = run.paths, str(run.repo), str(run.base_file)
    pipeline = add_pr_stages(
        Pipeline("t"),
        paths=paths,
        project_dir=project_dir,
        key=KEY,
        base_file=base_file,
        base_arg=None,
        overwrite_remote=None,
        ci_rounds=ci_rounds,
        ci_timeout_minutes=60,
        budget_pipeline=Pipeline("budget"),
        auto=auto,
    )
    return paths, pipeline, {s.name: s for s in pipeline.items}


def _checks_file(pr: MergePaths, n: int, buckets: list[str], outcome: str = "green") -> None:
    checks = [{"name": f"check-{i}", "bucket": b} for i, b in enumerate(buckets)]
    Path(pr.root).mkdir(parents=True, exist_ok=True)
    Path(pr.checks(n)).write_text(json.dumps({"outcome": outcome, "checks": checks}))


def _pr_json(*, is_draft: bool, head: str = "deadbeef") -> str:
    return json.dumps({"number": 42, "url": PR_URL, "isDraft": is_draft, "headRefOid": head})


# ---------------------------------------------------------------------------
# Stage set
# ---------------------------------------------------------------------------


def test_stage_names_and_order(tmp_path):
    _paths, pipeline, _stages = _build(tmp_path)
    assert [s.name for s in pipeline.items] == EXPECTED_STAGES
    assert EXPECTED_STAGES.index("check pr provenance") < EXPECTED_STAGES.index("reset pr state")


def test_every_stage_is_always_run(tmp_path):
    _paths, pipeline, _stages = _build(tmp_path)
    assert pipeline.items
    assert all(stage.always_run for stage in pipeline.items)


def test_gated_stages_run_only_while_running(tmp_path):
    paths, _pipeline, stages = _build(tmp_path)
    pr = pr_paths(paths)
    ctx = ctx_for()
    for name in ("open draft pr", "update pr body", "checks green", "refresh pr body", "announce pr ready"):
        assert stages[name].when(ctx) is True, name
    _halt_root(pr)
    for name in ("open draft pr", "update pr body", "checks green", "refresh pr body", "announce pr ready"):
        assert stages[name].when(ctx) is False, name
    assert stages["stop for human"].when(ctx) is True


def test_round_two_waits_only_with_ci_red_1(tmp_path):
    paths, _pipeline, stages = _build(tmp_path)
    pr = pr_paths(paths)
    ctx = ctx_for()
    Path(pr.root).mkdir(parents=True)
    assert stages["wait for checks 1"].when(ctx) is True
    assert stages["wait for checks 2"].when(ctx) is False
    assert stages["fix ci 1"].when(ctx) is False
    Path(pr.ci_red(1)).touch()
    assert stages["wait for checks 2"].when(ctx) is True
    assert stages["fix ci 1"].when(ctx) is True
    assert stages["sync after ci fix 1"].when(ctx) is True
    assert stages["fix ci 2"].when(ctx) is False


@pytest.mark.parametrize("ci_rounds", [1, 2, 3])
def test_final_wait_runs_only_with_last_ci_red(tmp_path, ci_rounds):
    paths, _pipeline, stages = _build(tmp_path, ci_rounds=ci_rounds)
    pr = pr_paths(paths)
    ctx = ctx_for()
    Path(pr.root).mkdir(parents=True)
    final = stages["wait for checks final"]
    for n in range(1, ci_rounds):
        Path(pr.ci_red(n)).touch()
    assert final.when(ctx) is False
    Path(pr.ci_red(ci_rounds)).touch()
    assert final.when(ctx) is True
    _halt_root(pr)
    assert final.when(ctx) is False
    assert final.impl._on_red == "halt"
    assert final.impl._out_name == pr.checks_after_fix(ci_rounds)


def test_wait_stages_have_the_7800s_backstop_and_fix_stages_none(tmp_path):
    _paths, pipeline, stages = _build(tmp_path)
    assert WAIT_FOR_CHECKS_TIMEOUT == 7800
    waits = [s for s in pipeline.items if s.name.startswith("wait for checks")]
    assert [s.name for s in waits] == ["wait for checks 1", "wait for checks 2", "wait for checks final"]
    for stage in waits:
        assert isinstance(stage.impl, WaitForChecks)
        assert stage.timeout == 7800, stage.name
    for n in (1, 2):
        assert stages[f"wait for checks {n}"].impl._on_red == "fix"
        fix = stages[f"fix ci {n}"]
        assert isinstance(fix.impl, CiFixChildV3)
        assert fix.timeout is None
        assert fix.impl._ci_pipeline == "check_and_fix_ci_v2"


def test_push_and_sync_are_halting_commands(tmp_path):
    paths, _pipeline, stages = _build(tmp_path)
    assert isinstance(stages["push"].impl, HaltingCommand)
    assert isinstance(stages["open draft pr"].impl, HaltingCommand)
    sync = stages["sync after ci fix 1"].impl
    assert isinstance(sync, HaltingCommand)
    assert sync.cmd.endswith(f"git rev-parse HEAD >> {paths.shipped_heads} || exit 1\n")
    assert isinstance(stages["resolve branch"].impl, ResolveBranchV3)


# ---------------------------------------------------------------------------
# CheckPrProvenance (A18)
# ---------------------------------------------------------------------------


async def _provenance(run: Run, *, auto: bool = False):
    stage = CheckPrProvenance(paths=run.paths, project_dir=str(run.repo), key=KEY, auto=auto)
    return await stage.run(ctx_for())


@pytest.mark.asyncio
async def test_provenance_passes_on_the_shipped_head_and_leaves_the_root(tmp_path):
    run = _run_repo(tmp_path)
    _halt_root(run.pr)

    result = await _provenance(run)

    assert result.success, result.error
    assert result.output == run.head
    assert Path(run.pr.halt).exists() and Path(run.pr.halt_reason).exists()


@pytest.mark.asyncio
async def test_provenance_fails_on_a_commit_added_after_a_halt(tmp_path):
    run = _run_repo(tmp_path)
    _halt_root(run.pr)
    new = _commit(run.repo, "hand fix after the halt", {"greet.txt": "hello\n"})

    result = await _provenance(run, auto=True)

    assert not result.success
    lines = result.error.splitlines()
    assert lines[0] == f"HEAD {new[:7]} was not shipped by this run"
    assert f"commits after {run.head[:7]}:" in lines
    assert any("hand fix after the halt" in line for line in lines)
    assert f"git reset --hard {run.head}" in lines[-1]
    assert f"append `{new}` to `{run.paths.shipped_heads}`" in lines[-1]
    assert lines[-1].endswith(f"norn run fix_jira_issue_v3 {KEY} auto --resume`")
    assert len(lines) <= 8


@pytest.mark.asyncio
async def test_provenance_fails_after_an_amend_of_the_shipped_head(tmp_path):
    run = _run_repo(tmp_path)
    _git(run.repo, "commit", "-q", "--amend", "-m", "CBS-1: change the greeting again")

    result = await _provenance(run)

    assert not result.success
    assert f"HEAD does not descend from {run.head[:7]}" in result.error
    assert "history was rewritten after gate 4" in result.error
    assert result.error.splitlines()[-1].endswith(f"{KEY} --resume`")


@pytest.mark.asyncio
async def test_provenance_fails_on_a_modified_tracked_file(tmp_path):
    run = _run_repo(tmp_path)
    (run.repo / "greet.txt").write_text("edited\n")

    result = await _provenance(run)

    assert not result.success
    assert "greet.txt" in result.error
    assert "need a clean tree" in result.error


@pytest.mark.asyncio
async def test_provenance_fails_on_an_untracked_file(tmp_path):
    run = _run_repo(tmp_path)
    (run.repo / "stray.log").write_text("x\n")

    result = await _provenance(run)

    assert not result.success
    assert "stray.log" in result.error


@pytest.mark.asyncio
async def test_provenance_passes_after_the_human_appends_the_new_head(tmp_path):
    run = _run_repo(tmp_path)
    new = _commit(run.repo, "hand fix", {"greet.txt": "hello\n"})
    assert not (await _provenance(run)).success
    with open(run.paths.shipped_heads, "a") as fh:
        fh.write(new + "\n")

    result = await _provenance(run)

    assert result.success, result.error
    assert result.output == new


@pytest.mark.asyncio
async def test_provenance_fails_without_a_shipped_head(tmp_path):
    run = _run_repo(tmp_path)
    Path(run.paths.shipped_heads).unlink()

    result = await _provenance(run)

    assert not result.success
    assert result.error.splitlines()[0] == "no shipped head recorded: the ship phase did not finish"


def _sync_ready(run: Run) -> None:
    _write_root(run.pr, branch=KEY, pushed_head=run.head, remote_head=run.head)


@pytest.mark.asyncio
async def test_resume_passes_on_the_ci_fix_head_a_round_1_sync_accepted(tmp_path):
    run = _run_repo(tmp_path)
    _sync_ready(run)
    _paths, _pipeline, stages = _build(tmp_path, run=run)
    # The child commits its fix and pushes it.
    fix = _commit(run.repo, CI_FIX_SUBJECT, {"greet.txt": "fixed\n"})
    _git(run.repo, "push", "-q", "origin", KEY)

    result = await stages["sync after ci fix 1"].impl.run(ctx_for())

    assert result.success, result.error
    assert not Path(run.pr.halt).exists()
    assert Path(run.paths.shipped_heads).read_text().split() == [run.head, fix]
    resumed = await _provenance(run)
    assert resumed.success, resumed.error
    assert resumed.output == fix


@pytest.mark.asyncio
async def test_a_ci_fix_unpushed_halt_appends_nothing_and_provenance_stops(tmp_path):
    run = _run_repo(tmp_path)
    _sync_ready(run)
    _paths, _pipeline, stages = _build(tmp_path, run=run)
    fix = _commit(run.repo, CI_FIX_SUBJECT, {"greet.txt": "fixed\n"})  # never pushed

    result = await stages["sync after ci fix 1"].impl.run(ctx_for())

    assert result.success
    assert Path(run.pr.halt_reason).read_text().splitlines()[0] == "KIND: ci_fix_unpushed"
    assert Path(run.paths.shipped_heads).read_text().split() == [run.head]
    resumed = await _provenance(run)
    assert not resumed.success
    assert resumed.error.splitlines()[0] == f"HEAD {fix[:7]} was not shipped by this run"


# ---------------------------------------------------------------------------
# ResolveBranchV3 (finding 19)
# ---------------------------------------------------------------------------


def _resolve_routes(open_prs: list[dict]) -> dict:
    return {
        ("repo", "view"): [
            (0, json.dumps({"nameWithOwner": REPO_NAME, "defaultBranchRef": {"name": "main"}}), "")
        ],
        ("pr", "list"): [(0, json.dumps(open_prs), "")],
    }


async def _resolve(tmp_path: Path, run: Run, open_prs: list[dict]):
    env, calls_log = _gh(tmp_path, _resolve_routes(open_prs))
    _paths, _pipeline, stages = _build(tmp_path, run=run)
    result = await stages["resolve branch"].impl.run(ctx_for(env=env))
    return result, _calls(calls_log)


@pytest.mark.asyncio
async def test_resolve_ignores_a_fork_only_pr(tmp_path):
    run = _run_repo(tmp_path, from_branch="develop", push=False)
    fork = {"number": 9, "baseRefName": "release", "isCrossRepository": True}

    result, calls = await _resolve(tmp_path, run, [fork])

    assert result.success, result.error
    assert result.output["base"] == "develop"
    assert result.output["source"] == "fork point"
    assert result.output["source"] != "open PR"
    assert Path(run.pr.base).read_text().strip() == "develop"
    listing = next(c for c in calls if c[:2] == ["pr", "list"])
    assert listing[listing.index("--json") + 1] == "number,baseRefName,isCrossRepository"


@pytest.mark.asyncio
async def test_resolve_uses_the_native_pr_next_to_a_fork_pr(tmp_path):
    run = _run_repo(tmp_path, push=False)
    prs = [
        {"number": 9, "baseRefName": "release", "isCrossRepository": True},
        {"number": 7, "baseRefName": "develop", "isCrossRepository": False},
    ]

    result, _calls_ = await _resolve(tmp_path, run, prs)

    assert result.success, result.error
    assert result.output["base"] == "develop"
    assert result.output["source"] == "open PR"


@pytest.mark.asyncio
async def test_resolve_still_fails_on_two_native_prs(tmp_path):
    run = _run_repo(tmp_path, push=False)
    prs = [
        {"number": 7, "baseRefName": "develop", "isCrossRepository": False},
        {"number": 8, "baseRefName": "main", "isCrossRepository": False},
    ]

    result, _calls_ = await _resolve(tmp_path, run, prs)

    assert not result.success
    assert "more than one open PR" in result.error


def _start_branch_run(tmp_path: Path, *, push_tip: bool = True, work: bool = True) -> tuple[Run, Path]:
    """CBS-1 cut from ``feature-x``, a branch with a commit of its own on top of main.

    With *push_tip* False, origin/feature-x lacks the tip the run started from.
    With *work* False, CBS-1 has no commit yet, as before any work.
    """
    rr = make_repo_with_remote(tmp_path / "checkout")
    repo = Path(rr.path)
    _git(repo, "checkout", "-q", "-b", "feature-x", "main")
    _commit(repo, "feature: pushed part", {"f1.txt": "1\n"})
    _git(repo, "push", "-q", "-u", "origin", "feature-x")
    if not push_tip:
        _commit(repo, "feature: unpushed part", {"f2.txt": "2\n"})
    _git(repo, "checkout", "-q", "-b", KEY)
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, "CBS-1: change the greeting", {"greet.txt": "hi\n"}) if work else base
    art = tmp_path / "art"
    art.mkdir()
    base_file = art / "base.sha"
    base_file.write_text(base + "\n")
    start_file = art / "base.branch"
    start_file.write_text("feature-x\n")
    paths = V3Paths(str(art), KEY)
    Path(paths.work).mkdir(parents=True)
    Path(paths.shipped_heads).write_text(head + "\n")
    run = Run(repo=repo, paths=paths, pr=pr_paths(paths), base_file=base_file, base=base, head=head)
    return run, start_file


def _resolver(
    run: Run, start_file: Path, cls=ResolveBranchV3, base_arg: str = "", check_base: bool = True
):
    return cls(
        paths=run.pr,
        project_dir=str(run.repo),
        base_arg=base_arg,
        overwrite_remote="",
        start_branch_file=str(start_file),
        base_file=str(run.base_file) if check_base else None,
    )


@pytest.mark.asyncio
async def test_resolve_takes_the_start_branch_ahead_of_the_fork_point(tmp_path):
    run, start_file = _start_branch_run(tmp_path)
    env, _log = _gh(tmp_path, _resolve_routes([]))

    result = await _resolver(run, start_file).run(ctx_for(env=env))

    assert result.success, result.error
    assert (result.output["base"], result.output["source"]) == ("feature-x", "start branch")
    assert Path(run.pr.base).read_text().strip() == "feature-x"


@pytest.mark.asyncio
async def test_resolve_with_an_empty_start_branch_falls_to_the_fork_point(tmp_path):
    run = _run_repo(tmp_path, push=False)
    start_file = tmp_path / "art" / "base.branch"
    start_file.write_text("\n")
    env, _log = _gh(tmp_path, _resolve_routes([]))

    result = await _resolver(run, start_file).run(ctx_for(env=env))

    assert result.success, result.error
    assert result.output["base"] == "main"
    assert result.output["source"] != "start branch"


@pytest.mark.asyncio
async def test_resolve_prefers_an_open_pr_and_base_arg_over_the_start_branch(tmp_path):
    run, start_file = _start_branch_run(tmp_path)
    # Only the order is under test: base.sha is on feature-x, not on main.
    pr = {"number": 7, "baseRefName": "main", "isCrossRepository": False}
    env, _log = _gh(tmp_path, _resolve_routes([pr]))
    by_pr = await _resolver(run, start_file, check_base=False).run(ctx_for(env=env))
    env, _log = _gh(tmp_path, _resolve_routes([]))
    by_arg = await _resolver(run, start_file, base_arg="main", check_base=False).run(
        ctx_for(env=env)
    )

    assert by_pr.success, by_pr.error
    assert (by_pr.output["base"], by_pr.output["source"]) == ("main", "open PR")
    assert by_arg.success, by_arg.error
    assert (by_arg.output["base"], by_arg.output["source"]) == ("main", "--arg base")


@pytest.mark.asyncio
async def test_resolve_stops_when_origin_base_lacks_base_sha(tmp_path):
    run, start_file = _start_branch_run(tmp_path, push_tip=False)
    env, _log = _gh(tmp_path, _resolve_routes([]))

    result = await _resolver(run, start_file).run(ctx_for(env=env))

    assert not result.success
    lines = result.error.splitlines()
    assert lines[0] == (
        f"origin/feature-x (start branch) does not contain base.sha {run.base[:7]}: "
        "the PR would carry 1 commits this run did not make"
    )
    assert "feature: unpushed part" in lines[1]
    assert "`git push origin feature-x`" in lines[-1]
    assert not Path(run.pr.base).exists()


@pytest.mark.asyncio
async def test_resolve_stops_when_an_open_pr_base_lacks_base_sha(tmp_path):
    run, start_file = _start_branch_run(tmp_path)
    pr = {"number": 7, "baseRefName": "main", "isCrossRepository": False}
    env, _log = _gh(tmp_path, _resolve_routes([pr]))

    result = await _resolver(run, start_file).run(ctx_for(env=env))

    assert not result.success
    assert result.error.splitlines()[0].startswith("origin/main (open PR) does not contain base.sha")
    assert "feature: pushed part" in result.error


@pytest.mark.asyncio
async def test_check_pr_base_passes_before_any_work_and_writes_nothing(tmp_path):
    run, start_file = _start_branch_run(tmp_path, work=False)
    env, _log = _gh(tmp_path, _resolve_routes([]))

    result = await _resolver(run, start_file, cls=CheckPrBase).run(ctx_for(env=env))

    assert result.success, result.error
    assert (result.output["base"], result.output["source"]) == ("feature-x", "start branch")
    assert not Path(run.pr.root).exists()


@pytest.mark.asyncio
async def test_check_pr_base_stops_before_any_work_on_an_unpushed_base(tmp_path):
    run, start_file = _start_branch_run(tmp_path, push_tip=False, work=False)
    env, _log = _gh(tmp_path, _resolve_routes([]))

    result = await _resolver(run, start_file, cls=CheckPrBase).run(ctx_for(env=env))

    assert not result.success
    assert "does not contain base.sha" in result.error.splitlines()[0]


def test_v3_resolve_branch_has_no_start_branch_or_base_check(tmp_path):
    _paths, _pipeline, stages = _build(tmp_path)
    impl = stages["resolve branch"].impl

    assert impl._start_branch_file is None
    assert impl._base_file is None


def test_reused_resolver_methods_keep_their_keyword_only_signatures():
    kw = inspect.Parameter.KEYWORD_ONLY
    resolve = inspect.signature(ResolveBranch._resolve_base).parameters
    for name in ("branch", "project_dir", "env", "default_branch", "open_pr"):
        assert resolve[name].kind is kw, name
    check = inspect.signature(ResolveBranch._check_remote_branch).parameters
    for name in ("branch", "project_dir", "env"):
        assert check[name].kind is kw, name
    init = inspect.signature(ResolveBranch.__init__).parameters
    for name in ("paths", "project_dir", "base_arg", "overwrite_remote"):
        assert init[name].kind is kw, name
    assert ResolveBranchV3._resolve_base is ResolveBranch._resolve_base
    assert ResolveBranchV3._check_remote_branch is ResolveBranch._check_remote_branch
    assert ResolveBranchV3.run is not ResolveBranch.run


# ---------------------------------------------------------------------------
# ResetPrState (A12)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_pr_state_empties_the_root_and_keeps_shipped_heads(tmp_path):
    paths = V3Paths(str(tmp_path / "art"), KEY)
    pr = pr_paths(paths)
    _halt_root(pr)
    Path(pr.ci_red(1)).touch()
    Path(pr.green_head).write_text("abc\n")
    Path(pr.checks(1)).write_text("{}")
    Path(paths.shipped_heads).write_text("abc\n")

    result = await ResetPrState(paths=paths).run(ctx_for())

    assert result.success
    assert Path(pr.root).is_dir()
    assert list(Path(pr.root).iterdir()) == []
    assert Path(paths.shipped_heads).read_text() == "abc\n"


# ---------------------------------------------------------------------------
# draft_pr_cmd
# ---------------------------------------------------------------------------

BLOCK = f"{BODY_START}\nJira: CBS-1 — Summary\n\n## Commits\n- CBS-1: change (1 files)\n\n## Testing\nLocal\n{BODY_END}\n"


def _draft_setup(tmp_path: Path, *, title: str = "CBS-1: change the greeting", base: str = "main"):
    pr = MergePaths(str(tmp_path / "pr"))
    _write_root(pr, repo=REPO_NAME, branch=KEY, base=base)
    Path(title_file(pr)).write_text(title + "\n")
    body = tmp_path / "CBS-1-pr-body.md"
    body.write_text(BLOCK)
    return pr, body


def _draft(pr: MergePaths, body: Path, tmp_path: Path, env: dict[str, str]):
    return _sh(draft_pr_cmd(pr, str(tmp_path), title_file(pr), str(body)), tmp_path, env)


def test_draft_pr_created_when_none_is_open(tmp_path):
    pr, body = _draft_setup(tmp_path)
    env, calls_log = _gh(
        tmp_path,
        {
            ("pr", "list"): [(0, "", ""), (0, "42 main\n", "")],
            ("pr", "create"): [(0, PR_URL + "\n", "")],
            ("pr", "view"): [(0, _pr_json(is_draft=True), "")],
        },
    )

    result = _draft(pr, body, tmp_path, env)

    assert result.returncode == 0, result.stderr
    assert not Path(pr.halt).exists()
    assert Path(pr.pr).read_text().strip() == "42"
    assert Path(pr.pr_origin).read_text().strip() == "created"
    assert json.loads(Path(pr.pr_json).read_text())["url"] == PR_URL
    calls = _calls(calls_log)
    creates = [c for c in calls if c[:2] == ["pr", "create"]]
    assert len(creates) == 1
    create = creates[0]
    assert "--draft" in create
    assert create[create.index("-R") + 1] == REPO_NAME
    assert create[create.index("--base") + 1] == "main"
    assert create[create.index("--head") + 1] == KEY
    assert create[create.index("--body-file") + 1] == str(body)
    assert create[create.index("--title") + 1] == "CBS-1: change the greeting"
    assert not [c for c in calls if c[:2] in (["pr", "ready"], ["pr", "edit"])]
    for call in calls:
        assert call[call.index("-R") + 1] == REPO_NAME


def test_draft_pr_ignores_a_fork_pr_with_the_same_branch_name(tmp_path):
    pr, body = _draft_setup(tmp_path)
    # gh applies the -q filter itself; the fake returns what that filter
    # leaves of a list holding only a fork PR: nothing.
    env, calls_log = _gh(
        tmp_path,
        {
            ("pr", "list"): [(0, "", ""), (0, "43 main\n", "")],
            ("pr", "create"): [(0, "", "")],
            ("pr", "view"): [(0, _pr_json(is_draft=True), "")],
        },
    )

    result = _draft(pr, body, tmp_path, env)

    assert result.returncode == 0, result.stderr
    calls = _calls(calls_log)
    listing = next(c for c in calls if c[:2] == ["pr", "list"])
    assert listing[listing.index("--json") + 1] == "number,baseRefName,isCrossRepository"
    assert listing[listing.index("-q") + 1] == NATIVE_PR_Q
    assert "select(.isCrossRepository == false)" in NATIVE_PR_Q
    assert [c for c in calls if c[:2] == ["pr", "create"]]
    assert Path(pr.pr).read_text().strip() == "43"


@pytest.mark.skipif(shutil.which("jq") is None, reason="needs jq to evaluate the -q filter")
def test_native_pr_filter_drops_fork_prs():
    prs = [
        {"number": 9, "baseRefName": "release", "isCrossRepository": True},
        {"number": 7, "baseRefName": "develop", "isCrossRepository": False},
    ]
    out = subprocess.run(
        ["jq", "-r", NATIVE_PR_Q], input=json.dumps(prs), capture_output=True, text=True, check=True
    ).stdout
    assert out == "7 develop\n"


def _reuse_routes(live_body: str) -> dict:
    return {
        ("pr", "list"): [(0, "42 main\n", "")],
        ("pr", "view"): [(0, live_body, ""), (0, _pr_json(is_draft=False), "")],
        ("pr", "edit"): [(0, "", "")],
    }


def test_draft_pr_reuses_an_open_pr_and_replaces_its_block_once(tmp_path):
    pr, body = _draft_setup(tmp_path)
    old = f"Human intro.\n\n{BODY_START}\nold block\n{BODY_END}\n\nHuman outro.\n"
    env, calls_log = _gh(tmp_path, _reuse_routes(old))

    first = _draft(pr, body, tmp_path, env)

    assert first.returncode == 0, first.stderr
    assert Path(pr.pr_origin).read_text().strip() == "reused"
    assert Path(pr.pr).read_text().strip() == "42"
    calls = _calls(calls_log)
    assert not [c for c in calls if c[:2] in (["pr", "create"], ["pr", "ready"])]
    edits = [c for c in calls if c[:2] == ["pr", "edit"]]
    assert len(edits) == 1
    assert "--title" not in edits[0] and "--add-reviewer" not in edits[0]
    live = Path(edits[0][edits[0].index("--body-file") + 1])
    edited = live.read_text()
    assert edited.count(BODY_START) == 1 and edited.count(BODY_END) == 1
    assert "old block" not in edited
    assert "Human intro." in edited and "Human outro." in edited
    assert BLOCK in edited

    env, calls_log = _gh(tmp_path, _reuse_routes(edited))
    second = _draft(pr, body, tmp_path, env)

    assert second.returncode == 0, second.stderr
    assert live.read_text() == edited


def test_draft_pr_on_another_base_halts(tmp_path):
    pr, body = _draft_setup(tmp_path, base="main")
    env, calls_log = _gh(tmp_path, {("pr", "list"): [(0, "42 develop\n", "")]})

    result = _draft(pr, body, tmp_path, env)

    assert result.returncode == 0, result.stderr
    reason = Path(pr.halt_reason).read_text()
    assert reason.splitlines()[0] == "KIND: pr_base_mismatch"
    assert "develop" in reason and "main" in reason
    calls = _calls(calls_log)
    assert not [c for c in calls if c[:2] in (["pr", "create"], ["pr", "edit"])]


def test_draft_pr_title_reaches_gh_literally(tmp_path):
    title = 'CBS-1: keep "quotes" and $HOME literal'
    pr, body = _draft_setup(tmp_path, title=title)
    env, calls_log = _gh(
        tmp_path,
        {
            ("pr", "list"): [(0, "", ""), (0, "42 main\n", "")],
            ("pr", "create"): [(0, "", "")],
            ("pr", "view"): [(0, _pr_json(is_draft=True), "")],
        },
    )

    result = _draft(pr, body, tmp_path, env)

    assert result.returncode == 0, result.stderr
    create = next(c for c in _calls(calls_log) if c[:2] == ["pr", "create"])
    assert create[create.index("--title") + 1] == title


# ---------------------------------------------------------------------------
# Gating after push_rejected (finding 13)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_draft_pr_is_skipped_after_a_push_rejected_halt(tmp_path):
    run = _run_repo(tmp_path)  # origin/CBS-1 exists
    _commit(run.repo, "CBS-1: one more", {"more.txt": "m\n"})
    # An empty remote-head leases "the branch must not exist on origin".
    _write_root(run.pr, branch=KEY, remote_head="")
    _paths, _pipeline, stages = _build(tmp_path, run=run)
    ctx = ctx_for()

    result = await stages["push"].impl.run(ctx)

    assert result.success
    assert Path(run.pr.halt_reason).read_text().splitlines()[0] == "KIND: push_rejected"
    assert stages["update pr body"].when(ctx) is False
    assert stages["open draft pr"].when(ctx) is False
    assert stages["wait for checks 1"].when(ctx) is False
    assert stages["stop for human"].when(ctx) is True


# ---------------------------------------------------------------------------
# CheckPrRange (finding 20)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_pr_range_passes_on_a_matching_range(tmp_path):
    run = _run_repo(tmp_path)
    _write_root(run.pr, base="main")

    result = await CheckPrRange(
        paths=run.paths, project_dir=str(run.repo), base_file=str(run.base_file)
    ).run(ctx_for())

    assert result.success, result.error


@pytest.mark.asyncio
async def test_check_pr_range_lists_commits_from_before_base_sha(tmp_path):
    run = _run_repo(tmp_path)  # one commit on main..HEAD already
    _commit(run.repo, "unreviewed second", {"b.txt": "b\n"})
    run.base_file.write_text(_git(run.repo, "rev-parse", "HEAD") + "\n")
    _commit(run.repo, "CBS-1: reviewed change", {"c.txt": "c\n"})
    _write_root(run.pr, base="main")

    result = await CheckPrRange(
        paths=run.paths, project_dir=str(run.repo), base_file=str(run.base_file)
    ).run(ctx_for())

    assert not result.success
    assert "has 3 commits; this run reviewed 1" in result.error.splitlines()[0]
    assert "unreviewed second" in result.error
    assert "CBS-1: change the greeting" in result.error
    assert "reviewed change" not in result.error
    assert "did not review" in result.error.splitlines()[-1]
    assert "--arg base=<branch>" in result.error.splitlines()[-1]


# ---------------------------------------------------------------------------
# UpdatePrBody (A11)
# ---------------------------------------------------------------------------


def _body_run(tmp_path: Path) -> Run:
    run = _run_repo(tmp_path)
    _commit(run.repo, "CBS-1: add the notes", {"notes.txt": "n\n"})
    head = _git(run.repo, "rev-parse", "HEAD")
    tree = _git(run.repo, "rev-parse", "HEAD^{tree}")
    Path(run.paths.rewrite_range).write_text(
        f"base {run.base}\nstart {run.head}\nhead {head}\ntree {tree}\n"
    )
    Path(run.paths.ship).write_text(
        "# Ship — CBS-1\n\nSTATUS: DECIDED\nCOMMITS: rewrite\nTITLE: CBS-1: change the greeting\n"
    )
    body = render_pr_body(
        jira_line="Jira: CBS-1 — Change the greeting",
        summary="The greeting changes.",
        commits=["- CBS-1: change the greeting (2 files)", "- CBS-1: add the notes (1 files)"],
        checklist=["- [ ] Apply the patch"],
        instructions="",
        plan_path="",
        followups=[],
        nits=[],
        reword=[],
        testing_line="Local, passed on tree 1234567 (gate 3):",
        test_labels=["unit tests"],
        details=False,
    )
    Path(run.paths.pr_body).write_text(body)
    _write_root(run.pr, repo=REPO_NAME, pr="42")
    return run


def _section(text: str, title: str) -> list[str]:
    lines = text.splitlines()
    start = lines.index(f"## {title}")
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ") or lines[i] == BODY_END),
        len(lines),
    )
    return [line for line in lines[start + 1:end] if line.strip()]


@pytest.mark.asyncio
async def test_update_pr_body_unchanged_makes_no_gh_call(tmp_path):
    run = _body_run(tmp_path)
    before = Path(run.paths.pr_body).read_text()
    env, calls_log = _gh(tmp_path, {("pr", "view"): [(0, before, "")], ("pr", "edit"): [(0, "", "")]})

    result = await UpdatePrBody(paths=run.paths, project_dir=str(run.repo), publish=True).run(
        ctx_for(env=env)
    )

    assert result.success, result.error
    assert result.output["changed"] is False
    assert Path(run.paths.pr_body).read_text() == before
    assert _calls(calls_log) == []
    assert Path(title_file(run.pr)).read_text() == "CBS-1: change the greeting\n"


@pytest.mark.asyncio
async def test_refresh_keeps_human_edits_and_lists_the_ci_fix_commit(tmp_path):
    run = _body_run(tmp_path)
    body_path = Path(run.paths.pr_body)
    original = body_path.read_text()
    edited = original.replace("- [ ] Apply the patch", "- [ ] Apply the patch\n- [ ] Also tell ops")
    body_path.write_text(edited)
    fix = _commit(run.repo, CI_FIX_SUBJECT, {"greet.txt": "fixed\n"})
    env, calls_log = _gh(tmp_path, {("pr", "view"): [(0, original, "")], ("pr", "edit"): [(0, "", "")]})
    stage = UpdatePrBody(paths=run.paths, project_dir=str(run.repo), publish=True)

    result = await stage.run(ctx_for(env=env))

    assert result.success, result.error
    assert result.output == {"changed": True, "published": True, "commits": 3}
    text = body_path.read_text()
    assert "- [ ] Also tell ops" in text
    assert _section(text, "For the human") == ["- [ ] Apply the patch", "- [ ] Also tell ops"]
    assert _section(text, "Commits") == [
        "- CBS-1: change the greeting (2 files)",
        "- CBS-1: add the notes (1 files)",
        f"- {CI_FIX_SUBJECT} (1 files)",
    ]
    assert _section(text, "Reword at squash/merge") == [f"- {fix[:7]} {CI_FIX_SUBJECT}"]
    assert text.index("## Reword at squash/merge") < text.index("## Testing")
    calls = _calls(calls_log)
    edits = [c for c in calls if c[:2] == ["pr", "edit"]]
    assert len(edits) == 1
    assert edits[0][edits[0].index("-R") + 1] == REPO_NAME
    assert edits[0][2] == "42"
    live = Path(edits[0][edits[0].index("--body-file") + 1]).read_text()
    assert live.count(BODY_START) == 1
    assert f"- {CI_FIX_SUBJECT} (1 files)" in live

    again = await stage.run(ctx_for(env=env))

    assert again.success, again.error
    assert again.output["changed"] is False
    assert len(_calls(calls_log)) == len(calls)


@pytest.mark.asyncio
async def test_update_pr_body_without_publish_never_calls_gh(tmp_path):
    run = _body_run(tmp_path)
    _commit(run.repo, CI_FIX_SUBJECT, {"greet.txt": "fixed\n"})
    env, calls_log = _gh(tmp_path, {})

    result = await UpdatePrBody(paths=run.paths, project_dir=str(run.repo), publish=False).run(
        ctx_for(env=env)
    )

    assert result.success, result.error
    assert result.output["changed"] is True and result.output["published"] is False
    assert _calls(calls_log) == []


def test_refresh_sections_removes_an_empty_reword_section():
    text = (
        f"{BODY_START}\nJira: CBS-1 — S\n\n## Commits\n- old (1 files)\n\n"
        f"## Reword at squash/merge\n- abc1234 fix(ci): x\n\n## Testing\nLocal\n{BODY_END}\n"
    )

    new = refresh_sections(text, commits=["- CBS-1: new (2 files)"], reword=[])

    assert "## Reword at squash/merge" not in new
    assert "- CBS-1: new (2 files)" in new and "- old (1 files)" not in new
    assert new.endswith(f"## Testing\nLocal\n{BODY_END}\n")


# ---------------------------------------------------------------------------
# ChecksGreen (findings 11, 14)
# ---------------------------------------------------------------------------


def _green_run(tmp_path: Path) -> Run:
    run = _run_repo(tmp_path)
    _write_root(run.pr, branch=KEY, green_head=run.head)
    _checks_file(run.pr, 1, ["pass", "skipping"])
    return run


async def _checks_green(run: Run):
    return await ChecksGreen(
        paths=run.paths, project_dir=str(run.repo), base_file=str(run.base_file)
    ).run(ctx_for())


@pytest.mark.asyncio
async def test_checks_green_passes(tmp_path):
    run = _green_run(tmp_path)

    result = await _checks_green(run)

    assert result.success, result.error


@pytest.mark.asyncio
async def test_checks_green_fails_when_green_head_is_not_head(tmp_path):
    run = _green_run(tmp_path)
    Path(run.pr.green_head).write_text(run.base + "\n")

    result = await _checks_green(run)

    assert not result.success
    assert "not the pushed HEAD" in result.error.splitlines()[0]


@pytest.mark.asyncio
async def test_checks_green_fails_on_an_all_skipping_checks_file(tmp_path):
    run = _green_run(tmp_path)
    _checks_file(run.pr, 1, ["skipping", "skipping"])

    result = await _checks_green(run)

    assert not result.success
    assert "no check passed" in result.error.splitlines()[0]


@pytest.mark.asyncio
async def test_checks_green_fails_on_a_claude_session_trailer(tmp_path):
    run = _green_run(tmp_path)
    message = f"{CI_FIX_SUBJECT}\n\nClaude-Session: 0123abcd"
    assert re.search(ATTRIBUTION_ERE, message, re.I) is None  # the old ERE passes it
    fix = _commit(run.repo, message, {"greet.txt": "fixed\n"})
    _git(run.repo, "push", "-q", "origin", KEY)
    Path(run.pr.green_head).write_text(fix + "\n")

    result = await _checks_green(run)

    assert not result.success
    assert "attribution" in result.error.splitlines()[0]
    assert "Claude-Session: 0123abcd" in result.error


# ---------------------------------------------------------------------------
# StopForHuman
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_for_human_reports_kind_url_and_auto_resume(tmp_path, notices):
    paths = V3Paths(str(tmp_path / "art"), KEY)
    pr = pr_paths(paths)
    Path(pr.root).mkdir(parents=True)
    heading = "The pull request's checks settled red"
    Path(pr.halt_reason).write_text(f"KIND: checks_red\n{heading}\nbuild (https://ci/1)\n")
    Path(pr.halt).touch()
    Path(pr.pr_json).write_text(_pr_json(is_draft=True))

    result = await StopForHuman(paths=paths, key=KEY, auto=True).run(ctx_for())

    assert not result.success
    lines = result.error.splitlines()
    assert lines[0] == f"{heading} (checks_red)"
    assert "build (https://ci/1)" in lines
    assert f"PR: {PR_URL}" in lines
    assert lines[-1] == f"norn run fix_jira_issue_v3 {KEY} auto --resume"
    assert "Fix CI by hand" not in result.error  # merge_branch's next_step text
    assert notices["v3"] == [("Stopped", heading)]


@pytest.mark.asyncio
async def test_stop_for_human_explains_checks_none_on_a_draft(tmp_path):
    paths = V3Paths(str(tmp_path / "art"), KEY)
    pr = pr_paths(paths)
    Path(pr.root).mkdir(parents=True)
    Path(pr.halt_reason).write_text("KIND: checks_none\nNo checks were reported on the pull request\nx\n")
    Path(pr.halt).touch()

    result = await StopForHuman(paths=paths, key=KEY, auto=False).run(ctx_for())

    assert not result.success
    assert "workflows may skip draft PRs, or the PR has a merge conflict" in result.error
    assert "PR:" not in result.error
    assert result.error.splitlines()[-1] == f"norn run fix_jira_issue_v3 {KEY} --resume"


# ---------------------------------------------------------------------------
# AnnouncePrReady
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_announce_a_reused_ready_pr_says_ready_for_review(tmp_path, notices):
    paths = V3Paths(str(tmp_path / "art"), KEY)
    pr = pr_paths(paths)
    _write_root(pr, repo=REPO_NAME, pr="42")
    _checks_file(pr, 1, ["pass", "pass", "skipping"])
    env, calls_log = _gh(
        tmp_path, {("pr", "view"): [(0, json.dumps({"isDraft": False, "url": PR_URL}), "")]}
    )

    result = await AnnouncePrReady(paths=paths, project_dir=str(tmp_path)).run(ctx_for(env=env))

    assert result.success, result.error
    assert result.output == "ready"
    assert len(notices["v3"]) == 1
    title, detail = notices["v3"][0]
    assert title == "PR ready"
    assert detail == f"PR #42 ready for review (not marked by this run), 2 checks green — {PR_URL}"
    assert "draft" not in detail.lower()
    calls = _calls(calls_log)
    assert calls == [["pr", "view", "-R", REPO_NAME, "42", "--json", "isDraft,url"]]
    assert not [c for c in calls if c[:2] == ["pr", "ready"]]


@pytest.mark.asyncio
async def test_announce_a_pr_draft_pr_cmd_created_says_draft(tmp_path, notices):
    paths = V3Paths(str(tmp_path / "art"), KEY)
    pr = pr_paths(paths)
    _write_root(pr, repo=REPO_NAME, branch=KEY, base="main")
    Path(title_file(pr)).write_text("CBS-1: change the greeting\n")
    body = tmp_path / "body.md"
    body.write_text(BLOCK)
    env, calls_log = _gh(
        tmp_path,
        {
            ("pr", "list"): [(0, "", ""), (0, "42 main\n", "")],
            ("pr", "create"): [(0, "", "")],
            ("pr", "view"): [(0, _pr_json(is_draft=True), "")],
        },
    )
    created = _sh(draft_pr_cmd(pr, str(tmp_path), title_file(pr), str(body)), tmp_path, env)
    assert created.returncode == 0, created.stderr
    _checks_file(pr, 1, ["pass"])

    result = await AnnouncePrReady(paths=paths, project_dir=str(tmp_path)).run(ctx_for(env=env))

    assert result.success, result.error
    assert result.output == "draft"
    assert notices["v3"] == [("PR ready", f"PR #42 draft, 1 checks green — {PR_URL}")]
    assert not [c for c in _calls(calls_log) if c[:2] == ["pr", "ready"]]


# ---------------------------------------------------------------------------
# A green draft PR end to end: open, wait, check, announce, summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_green_draft_pr_flow(tmp_path, notices):
    run = _run_repo(tmp_path)
    _write_root(run.pr, repo=REPO_NAME, branch=KEY, base="main", pushed_head=run.head)
    Path(title_file(run.pr)).write_text("CBS-1: change the greeting\n")
    Path(run.paths.pr_body).write_text(BLOCK)
    green = json.dumps([{"name": "build", "workflow": "CI", "state": "COMPLETED", "bucket": "pass",
                         "link": "https://ci/1", "startedAt": "", "completedAt": ""}])
    env, calls_log = _gh(
        tmp_path,
        {
            ("pr", "list"): [(0, "", ""), (0, "42 main\n", "")],
            ("pr", "create"): [(0, "", "")],
            ("pr", "view"): [(0, _pr_json(is_draft=True, head=run.head), "")],
            ("pr", "checks"): [(0, green, "")],
            ("run", "list"): [(0, "[]", "")],
        },
    )
    _paths, _pipeline, stages = _build(tmp_path, run=run)
    ctx = ctx_for(env=env)

    opened = await stages["open draft pr"].impl.run(ctx)
    assert opened.success and not Path(run.pr.halt).exists(), opened.error

    fc = _FakeClock()
    wait = WaitForChecks(
        paths=run.pr, stage="wait for checks 1", n=1, out_name=run.pr.checks(1),
        project_dir=str(run.repo), on_red="fix", sleep=fc.sleep, clock=fc.clock,
    )
    waited = await wait.run(ctx)
    assert waited.success and not Path(run.pr.halt).exists()
    assert Path(run.pr.green_head).read_text().strip() == run.head

    for name in ("checks green", "announce pr ready"):
        assert stages[name].when(ctx)
        result = await stages[name].impl.run(ctx)
        assert result.success, (name, result.error)
        ctx.results[name] = result
    assert ctx.results["announce pr ready"].output == "draft"
    assert notices["v3"] == [("PR ready", f"PR #42 draft, 1 checks green — {PR_URL}")]

    ctx.usage_tracker.add(UsageRecord(stage_name="x", total_cost_usd=12.345))
    summary = await RunSummary(
        paths=run.paths, project_dir=str(run.repo), base_file=str(run.base_file)
    ).run(ctx)
    assert summary.success, summary.error
    assert f"branch: {KEY}" in summary.output
    assert "CBS-1: change the greeting" in summary.output
    assert f"PR: {PR_URL} (draft)" in summary.output
    for gate_file in (run.paths.approval, run.paths.code_review, run.paths.ship):
        assert gate_file in summary.output
    assert f"{KEY}-plan-questions.md" in summary.output
    assert "spend this process: $12.35 (the ceiling restarts on --resume)" in summary.output
    assert not [c for c in _calls(calls_log) if c[:2] == ["pr", "ready"]]


# ---------------------------------------------------------------------------
# fix_issue_v1: subject prefix, pipeline name, resume argument, fetch notes
# ---------------------------------------------------------------------------

OTHER = "fix_issue_v1"
GH_KEY = "issue-123"
GH_PREFIX = "#123"
NOTES_PATH = "/x/tmp/github/123/issue.md"
HALT_HEADING = "The pull request's checks settled red"
NOTES_ACTION = "Run without --resume so 'fetch issue' writes it again."


@pytest.fixture
def quiet(monkeypatch) -> list[tuple[str, str, dict]]:
    """The shared quiet-notify rule: every notifier accepts **kwargs, and no osascript."""
    calls: list[tuple[str, str, dict]] = []

    async def fake_notify(title: str, detail: str, **kwargs) -> None:
        calls.append((title, detail, kwargs))

    async def fake_send(self, msg) -> None:
        calls.append((msg.title, msg.body, {"channel": "macos"}))

    for module in (_merge_gh, _merge_state, _pr_v3, _child_cost_v3, _fix_jira_v3):
        monkeypatch.setattr(module, "notify", fake_notify)
    monkeypatch.setattr(MacOSChannel, "send", fake_send)
    return calls


def test_resume_hint_names_another_pipeline():
    assert resume_hint("123", True, pipeline=OTHER) == "norn run fix_issue_v1 123 auto --resume"
    assert resume_hint("123", False, pipeline=OTHER) == "norn run fix_issue_v1 123 --resume"
    assert resume_hint(KEY, True) == f"norn run fix_jira_issue_v3 {KEY} auto --resume"
    assert resume_hint(KEY, False) == f"norn run fix_jira_issue_v3 {KEY} --resume"


def _prefix_run(tmp_path: Path) -> Run:
    """Two ``#123:`` commits on ``issue-123``, plus the files the PR body refresh reads.

    The PR body lists only the first commit, so a refresh always changes it.
    """
    rr = make_repo_with_remote(tmp_path / "checkout")
    repo = Path(rr.path)
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "commit.cleanup", "whitespace")  # keeps a `#123:` subject
    _git(repo, "checkout", "-q", "-b", GH_KEY, "main")
    base = _git(repo, "rev-parse", "HEAD")
    start = _commit(repo, "#123: add x", {"x.txt": "x\n"})
    head = _commit(repo, "#123: fix y", {"y.txt": "y\n"})
    assert _git(repo, "log", "--format=%s", f"{base}..HEAD").splitlines() == [
        "#123: fix y",
        "#123: add x",
    ]
    art = tmp_path / "art"
    art.mkdir()
    base_file = art / "base.sha"
    base_file.write_text(base + "\n")
    paths = V3Paths(str(art), GH_KEY)
    Path(paths.work).mkdir(parents=True)
    Path(paths.shipped_heads).write_text(head + "\n")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    Path(paths.rewrite_range).write_text(f"base {base}\nstart {start}\nhead {head}\ntree {tree}\n")
    Path(paths.ship).write_text(
        "# Ship — issue-123\n\nSTATUS: DECIDED\nCOMMITS: rewrite\nTITLE: #123: add x and fix y\n"
    )
    body = render_pr_body(
        jira_line="Closes #123 — Add x",
        summary="X is added and y is fixed.",
        commits=["- #123: add x (1 files)"],
        checklist=["- [ ] Check x"],
        instructions="",
        plan_path="",
        followups=[],
        nits=[],
        reword=[],
        testing_line="Local, passed on tree 1234567 (gate 3):",
        test_labels=["unit tests"],
        details=False,
    )
    Path(paths.pr_body).write_text(body)
    pr = pr_paths(paths)
    _write_root(pr, repo=REPO_NAME, pr="42")
    return Run(repo=repo, paths=paths, pr=pr, base_file=base_file, base=base, head=head)


async def _refresh(tmp_path: Path, run: Run, *, publish: bool, **kwargs):
    original = Path(run.paths.pr_body).read_text()
    env, calls_log = _gh(
        tmp_path, {("pr", "view"): [(0, original, "")], ("pr", "edit"): [(0, "", "")]}
    )
    stage = UpdatePrBody(paths=run.paths, project_dir=str(run.repo), publish=publish, **kwargs)
    result = await stage.run(ctx_for(env=env))
    edits = [c for c in _calls(calls_log) if c[:2] == ["pr", "edit"]]
    lives = [Path(e[e.index("--body-file") + 1]).read_text() for e in edits]
    return result, _calls(calls_log), lives


@pytest.mark.asyncio
@pytest.mark.parametrize("publish", [False, True])
async def test_update_pr_body_with_the_subject_prefix_lists_no_reword(tmp_path, publish):
    run = _prefix_run(tmp_path)
    assert run.paths.key == GH_KEY

    result, calls, lives = await _refresh(tmp_path, run, publish=publish, subject_prefix=GH_PREFIX)

    assert result.success, result.error
    assert result.output == {"changed": True, "published": publish, "commits": 2}
    text = Path(run.paths.pr_body).read_text()
    assert _section(text, "Commits") == ["- #123: add x (1 files)", "- #123: fix y (1 files)"]
    assert "## Reword at squash/merge" not in text
    assert Path(title_file(run.pr)).read_text() == "#123: add x and fix y\n"
    if publish:
        assert len(lives) == 1
        assert "## Reword at squash/merge" not in lives[0]
        assert "- #123: fix y (1 files)" in lives[0]
        assert lives[0].count(BODY_START) == 1
    else:
        assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("publish", [False, True])
async def test_update_pr_body_without_the_prefix_rewords_every_hash_commit(tmp_path, publish):
    run = _prefix_run(tmp_path)
    first = _git(run.repo, "rev-parse", "HEAD~1")

    result, calls, lives = await _refresh(tmp_path, run, publish=publish)

    assert result.success, result.error
    expected = [f"- {first[:7]} #123: add x", f"- {run.head[:7]} #123: fix y"]
    text = Path(run.paths.pr_body).read_text()
    assert _section(text, "Reword at squash/merge") == expected
    if publish:
        assert len(lives) == 1
        assert _section(lives[0], "Reword at squash/merge") == expected
    else:
        assert calls == []


@pytest.mark.asyncio
async def test_provenance_resume_hint_names_the_pipeline_and_the_resume_arg(tmp_path):
    paths = V3Paths(str(tmp_path / "art"), "issue-o-r-5-12345678")
    stage = CheckPrProvenance(
        paths=paths,
        project_dir=str(tmp_path),
        key="https://github.com/o/r/issues/5",
        auto=False,
        pipeline_name=OTHER,
    )

    result = await stage.run(ctx_for())

    assert not result.success
    lines = result.error.splitlines()
    assert lines[0] == "no shipped head recorded: the ship phase did not finish"
    assert lines[-1] == (
        "Finish the ship phase: norn run fix_issue_v1 https://github.com/o/r/issues/5 --resume"
    )


def _halted(tmp_path: Path) -> V3Paths:
    paths = V3Paths(str(tmp_path / "art"), GH_KEY)
    pr = pr_paths(paths)
    Path(pr.root).mkdir(parents=True)
    Path(pr.halt_reason).write_text(f"KIND: checks_red\n{HALT_HEADING}\nbuild (https://ci/1)\n")
    Path(pr.halt).touch()
    return paths


@pytest.mark.asyncio
async def test_stop_for_human_names_another_pipeline(tmp_path, quiet):
    paths = _halted(tmp_path)

    result = await StopForHuman(paths=paths, key="123", auto=False, pipeline_name=OTHER).run(
        ctx_for()
    )

    assert not result.success
    assert result.error.splitlines()[-1] == "norn run fix_issue_v1 123 --resume"
    assert "fetch notes" not in result.error
    assert quiet == [("Stopped", HALT_HEADING, {"pipeline": OTHER})]


@pytest.mark.asyncio
async def test_stop_for_human_counts_the_fetch_notes(tmp_path, quiet):
    paths = _halted(tmp_path)
    stage = StopForHuman(
        paths=paths,
        key="123",
        auto=False,
        pipeline_name=OTHER,
        fetch_notes=lambda: ["a", "b"],
        notes_path=NOTES_PATH,
    )

    result = await stage.run(ctx_for())

    assert not result.success
    lines = result.error.splitlines()
    assert lines[0] == f"{HALT_HEADING} (checks_red)"
    assert lines[-2:] == [f"2 fetch notes: {NOTES_PATH}", "norn run fix_issue_v1 123 --resume"]
    assert quiet == [
        ("Stopped", f"{HALT_HEADING} — 2 fetch notes, see issue.md", {"pipeline": OTHER})
    ]


@pytest.mark.asyncio
async def test_stop_for_human_with_no_fetch_notes_keeps_todays_texts(tmp_path, quiet):
    paths = _halted(tmp_path)

    plain = await StopForHuman(paths=paths, key="123", auto=False).run(ctx_for())
    empty = await StopForHuman(
        paths=paths, key="123", auto=False, fetch_notes=lambda: [], notes_path=NOTES_PATH
    ).run(ctx_for())

    assert not plain.success and not empty.success
    assert empty.error == plain.error
    assert empty.output == plain.output == "checks_red"
    assert plain.error.splitlines()[-1] == "norn run fix_jira_issue_v3 123 --resume"
    assert "fetch notes" not in empty.error
    # The default pipeline name keeps the two-argument notify call.
    assert quiet == [("Stopped", HALT_HEADING, {}), ("Stopped", HALT_HEADING, {})]


def _announce_paths(tmp_path: Path) -> tuple[V3Paths, dict[str, str], str]:
    paths = V3Paths(str(tmp_path / "art"), GH_KEY)
    pr = pr_paths(paths)
    _write_root(pr, repo=REPO_NAME, pr="42")
    _checks_file(pr, 1, ["pass"])
    env, calls_log = _gh(
        tmp_path, {("pr", "view"): [(0, json.dumps({"isDraft": True, "url": PR_URL}), "")]}
    )
    return paths, env, calls_log


@pytest.mark.asyncio
async def test_announce_names_another_pipeline(tmp_path, quiet):
    paths, env, calls_log = _announce_paths(tmp_path)

    result = await AnnouncePrReady(
        paths=paths, project_dir=str(tmp_path), pipeline_name=OTHER
    ).run(ctx_for(env=env))

    assert result.success, result.error
    assert result.output == "draft"
    assert quiet == [("PR ready", f"PR #42 draft, 1 checks green — {PR_URL}", {"pipeline": OTHER})]
    assert _calls(calls_log) == [["pr", "view", "-R", REPO_NAME, "42", "--json", "isDraft,url"]]


@pytest.mark.asyncio
async def test_announce_counts_the_fetch_notes_and_keeps_the_output(tmp_path, quiet):
    paths, env, _calls_log = _announce_paths(tmp_path)

    result = await AnnouncePrReady(
        paths=paths,
        project_dir=str(tmp_path),
        pipeline_name=OTHER,
        fetch_notes=lambda: ["a", "b"],
        notes_path=NOTES_PATH,
    ).run(ctx_for(env=env))

    assert result.success, result.error
    assert result.output == "draft"
    assert len(quiet) == 1
    title, detail, kwargs = quiet[0]
    assert (title, kwargs) == ("PR ready", {"pipeline": OTHER})
    assert detail == f"PR #42 draft, 1 checks green — {PR_URL} — 2 fetch notes, see issue.md"


@pytest.mark.asyncio
async def test_announce_with_no_fetch_notes_keeps_todays_detail(tmp_path, quiet):
    paths, env, _calls_log = _announce_paths(tmp_path)

    result = await AnnouncePrReady(
        paths=paths, project_dir=str(tmp_path), fetch_notes=lambda: [], notes_path=NOTES_PATH
    ).run(ctx_for(env=env))

    assert result.success, result.error
    assert quiet == [("PR ready", f"PR #42 draft, 1 checks green — {PR_URL}", {})]


def _summary_run(tmp_path: Path):
    run = _run_repo(tmp_path)
    _write_root(run.pr, branch=KEY)
    Path(run.pr.pr_json).write_text(_pr_json(is_draft=True, head=run.head))
    ctx = ctx_for()
    ctx.results[_pr_v3.ANNOUNCE_STAGE] = StageResult(
        name=_pr_v3.ANNOUNCE_STAGE, success=True, output="draft"
    )
    ctx.usage_tracker.add(UsageRecord(stage_name="x", total_cost_usd=1.5))
    return run, ctx


async def _summary(run: Run, ctx, **kwargs) -> StageResult:
    return await RunSummary(
        paths=run.paths, project_dir=str(run.repo), base_file=str(run.base_file), **kwargs
    ).run(ctx)


@pytest.mark.asyncio
async def test_summary_lists_the_fetch_notes_before_the_spend_line(tmp_path):
    run, ctx = _summary_run(tmp_path)

    plain = await _summary(run, ctx)
    noted = await _summary(run, ctx, fetch_notes=lambda: ["a", "b"], notes_path=NOTES_PATH)
    none = await _summary(run, ctx, fetch_notes=lambda: [], notes_path=NOTES_PATH)

    assert plain.success and noted.success and none.success
    oneline = _git(run.repo, "log", "--oneline", f"{run.base}..HEAD")
    spend = "spend this process: $1.50 (the ceiling restarts on --resume)"
    today = [
        f"branch: {KEY}",
        f"commits ({run.base[:7]}..HEAD):",
        f"  {oneline}",
        f"PR: {PR_URL} (draft)",
        "gate files:",
        f"  {_pr_v3.questions_file(run.paths)}",
        f"  {run.paths.approval}",
        f"  {run.paths.code_review}",
        f"  {run.paths.ship}",
        spend,
    ]
    assert plain.output.splitlines() == today
    assert noted.output.splitlines() == [
        *today[:-1], f"fetch notes ({NOTES_PATH}):", "  - a", "  - b", spend
    ]
    assert none.output.splitlines() == [*today[:-1], "fetch notes: none", spend]


def _missing_notes() -> list[str]:
    raise FileNotFoundError(2, "No such file or directory", "/x/tmp/github/123/fetch-notes.json")


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["stop for human", "announce pr ready", "summary"])
async def test_a_raising_fetch_notes_fails_the_stage_naming_the_error(tmp_path, quiet, name):
    paths = _halted(tmp_path)
    env, calls_log = _gh(tmp_path, {})
    notes = {"fetch_notes": _missing_notes, "notes_path": NOTES_PATH}
    stage = {
        "stop for human": lambda: StopForHuman(
            paths=paths, key="123", auto=False, pipeline_name=OTHER, **notes
        ),
        "announce pr ready": lambda: AnnouncePrReady(
            paths=paths, project_dir=str(tmp_path), pipeline_name=OTHER, **notes
        ),
        "summary": lambda: RunSummary(
            paths=paths, project_dir=str(tmp_path), base_file=str(tmp_path / "base.sha"), **notes
        ),
    }[name]()

    result = await stage.run(ctx_for(env=env))

    assert not result.success
    lines = result.error.splitlines()
    assert lines[0] == "cannot read the fetch notes: FileNotFoundError"
    assert "/x/tmp/github/123/fetch-notes.json" in result.error
    assert lines[-1] == NOTES_ACTION
    assert quiet == []
    assert _calls(calls_log) == []


@pytest.mark.parametrize(
    "given", [{"fetch_notes": lambda: []}, {"notes_path": NOTES_PATH}], ids=["callable", "path"]
)
def test_fetch_notes_and_notes_path_come_together(tmp_path, given):
    paths = V3Paths(str(tmp_path / "art"), GH_KEY)
    together = "pass fetch_notes and notes_path together, or neither"
    with pytest.raises(ValueError, match=f"StopForHuman: {together}"):
        StopForHuman(paths=paths, key="123", auto=False, **given)
    with pytest.raises(ValueError, match=f"AnnouncePrReady: {together}"):
        AnnouncePrReady(paths=paths, project_dir=str(tmp_path), **given)
    with pytest.raises(ValueError, match=f"RunSummary: {together}"):
        RunSummary(paths=paths, project_dir=str(tmp_path), base_file="/b/base.sha", **given)


def test_builder_passes_the_fix_issue_v1_values_to_every_stage(tmp_path):
    def f() -> list[str]:
        return []

    paths = V3Paths(str(tmp_path / "art"), GH_KEY)
    pipeline = add_pr_stages(
        Pipeline("t"),
        paths=paths,
        project_dir=str(tmp_path / "repo"),
        key=GH_KEY,
        base_file=str(tmp_path / "art" / "base.sha"),
        base_arg=None,
        overwrite_remote=None,
        ci_rounds=2,
        ci_timeout_minutes=60,
        budget_pipeline=Pipeline("budget"),
        auto=False,
        subject_prefix=GH_PREFIX,
        pipeline_name=OTHER,
        resume_arg="123",
        fetch_notes=f,
        notes_path=NOTES_PATH,
    )
    assert [s.name for s in pipeline.items] == EXPECTED_STAGES
    impls = {s.name: s.impl for s in pipeline.items}

    for name in ("update pr body", "refresh pr body"):
        assert isinstance(impls[name], UpdatePrBody)
        assert impls[name].subject_prefix == GH_PREFIX
    provenance = impls["check pr provenance"]
    assert (provenance.key, provenance.pipeline_name) == ("123", OTHER)
    stop = impls["stop for human"]
    assert (stop.key, stop.pipeline_name) == ("123", OTHER)
    fixes = [s.impl for s in pipeline.items if s.name.startswith("fix ci ")]
    assert [type(fix) for fix in fixes] == [CiFixChildV3, CiFixChildV3]
    assert all(fix._pipeline_name == OTHER for fix in fixes)
    assert impls["announce pr ready"].pipeline_name == OTHER
    for name in ("stop for human", "announce pr ready", "summary"):
        assert impls[name].fetch_notes is f, name
        assert impls[name].notes_path == NOTES_PATH, name


def test_builder_defaults_keep_the_v3_values(tmp_path):
    _paths, pipeline, stages = _build(tmp_path)
    impls = {name: stage.impl for name, stage in stages.items()}

    for name in ("update pr body", "refresh pr body"):
        assert impls[name].subject_prefix is None
    for name in ("check pr provenance", "stop for human"):
        assert (impls[name].key, impls[name].pipeline_name) == (KEY, PIPELINE)
    assert impls["announce pr ready"].pipeline_name == PIPELINE
    assert all(
        s.impl._pipeline_name == PIPELINE for s in pipeline.items if s.name.startswith("fix ci ")
    )
    for name in ("stop for human", "announce pr ready", "summary"):
        assert impls[name].fetch_notes is None and impls[name].notes_path is None, name

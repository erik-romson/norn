"""Tests for the fix_jira_issue_v3 bundled pipeline.

Covers the pipeline's shape (stage names in every mode, unique names, gate
twins, the always-run PR phase), its import-time knobs, the copied v2 stages
(preflight, prepare branch, the child command), the Generate stages, the
catalog entry, and three resume flows driven end to end through
``run_pipeline`` with mocked agents and real git in ``tmp_path``.
"""
from __future__ import annotations

import importlib
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from norn.alerts import MacOSChannel
from norn.catalog import list_pipelines
from norn.checkpoint import load_checkpoint, save_checkpoint
from norn.dsl import ClearContext, Loop, OnFailure, Stage
from norn.event_sink import EventSink
from norn.events import StageStarted
from norn.graph import build_graph
from norn.model_labels import ModelLabelError, resolve_label
from norn.models import PipelineContext, StageResult
from norn.pipelines import (
    _child_cost_v3,
    _fix_jira_v3,
    _merge_gh,
    _merge_state,
    _pr_v3,
)
from norn.pipelines._child_cost_v3 import RunChildPipelineV3
from norn.pipelines._comment_style import COMMENT_RULE
from norn.pipelines._launch_tree import AssertLaunchTree
from norn.pipelines._merge_state import HaltingCommand, halt
from norn.pipelines._pr_v3 import pr_paths
from norn.responder import NonInteractiveResponder
from norn.runner import PipelineError, run_pipeline
from norn.stages.generate import Generate

from tests.merge_fakes import install_fake_gh

# ---------------------------------------------------------------------------
# Import helper
# ---------------------------------------------------------------------------


# Copied from tests/test_fix_jira_issue_pipeline_v2.py:30-48 (that file is
# hash-pinned as the frozen control), pointed at fix_jira_issue_v3.
def _import_fix_jira(argv_tail: list[str], cwd: str | Path | None = None):
    """Import fix_jira_issue_v3 with patched argv and cwd.

    *argv_tail* contains the positional args (e.g. ``["CBS-2249"]`` or
    ``["CBS-2249", "auto"]``).  *cwd* overrides ``os.getcwd()`` so the module
    resolves artifact paths under a temp directory.
    """
    saved_argv = list(sys.argv)
    saved_cwd = os.getcwd()
    try:
        sys.argv = [saved_argv[0], *argv_tail]
        if cwd is not None:
            os.chdir(str(cwd))
        from norn.pipelines import fix_jira_issue_v3 as fix_jira_issue

        return importlib.reload(fix_jira_issue)
    finally:
        sys.argv = saved_argv
        os.chdir(saved_cwd)


def _import_default(tmp_path):
    return _import_fix_jira(["CBS-2249"], cwd=tmp_path)


def _import_auto(tmp_path):
    return _import_fix_jira(["CBS-2249", "auto"], cwd=tmp_path)


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    """No osascript, and no user env leaking into the import-time knobs."""
    monkeypatch.delenv("JIRA_BROWSE_BASE", raising=False)
    notices: list[tuple[str, str]] = []

    async def _notify(title: str, detail: str) -> None:
        notices.append((title, detail))

    async def _send(self, msg) -> None:
        notices.append((msg.title, msg.body))

    for module in (_merge_gh, _merge_state, _pr_v3, _child_cost_v3, _fix_jira_v3):
        monkeypatch.setattr(module, "notify", _notify)
    monkeypatch.setattr(MacOSChannel, "send", _send)
    return notices


# ---------------------------------------------------------------------------
# Walkers
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


def _find_loop(items, name):
    for item in items:
        if isinstance(item, Loop) and item.name == name:
            return item
    raise KeyError(f"Loop {name!r} not found")


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
# Stage names
# ---------------------------------------------------------------------------

_INTERACTIVE_NAMES = [
    "assert launch tree",
    "preflight environment",
    "register child state",
    "prepare branch",
    "record run identity",
    "check run identity",
    "fetch issue",
    "check issue files",
    "write brief",
    "brief written",
    "ClearContext",
    # Plan review (add_plan_review_stages_v3):
    "preflight",
    "check learning store",
    "draft plan",
    "resolve open questions",
    "open questions resolved",
    "complete plan sections",
    "plan sections present",
    "review plan",
    "review shape ok",
    "ClearContext",
    "apply review",
    "dispositions recorded",
    "plan sections kept",
    "write plan approval",
    "plan approved",
    "plan approval recorded",
    "plan sections approved",
    "ClearContext",
    "split plan",
    "step files written",
    "plan summary",
    # The child run:
    "resolve test command",
    "implement",
    "diagnose implementation",
    "check implementation",
    # Gate 3 (add_code_review_stages):
    "record review base",
    "codex code review",
    "codex code review written",
    "ClearContext",
    "claude code review",
    "claude code review written",
    "review left tree unchanged",
    "ClearContext",
    "triage code review",
    "code review shape ok",
    "code review decided",
    "code review dispositions recorded",
    "fix code review",
    "review fixes green",
    "fixes recorded",
    "commit review fixes",
    "worktree clean",
    "write review followups",
    # Gate 4 (add_ship_stages):
    "record rewrite range",
    "check commit skill",
    "ClearContext",
    "plan commits",
    "commit plan valid",
    "ClearContext",
    "summarize pr",
    "pr summary valid",
    "assemble pr body",
    "write ship decision",
    "ship decided",
    "ship decision recorded",
    "commit plan still valid",
    "pr body still complete",
    "rewrite commits",
    "rewrite matches tree",
    "check commit messages",
    "record shipped head",
    # PR phase (add_pr_stages, ci_rounds=2):
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

_PR_PHASE = _INTERACTIVE_NAMES[_INTERACTIVE_NAMES.index("check pr provenance"):]


def _with_inserted(names: list[str], after_to_new: dict[str, str]) -> list[str]:
    out: list[str] = []
    for name in names:
        out.append(name)
        if name in after_to_new:
            out.append(after_to_new[name])
    return out


_AUTO_NAMES = _with_inserted(
    _INTERACTIVE_NAMES,
    {
        "write plan approval": "auto approve plan",
        "code review shape ok": "auto decide code review",
        "write ship decision": "auto decide ship",
    },
)
_PLAN_GATE_OFF_NAMES = _with_inserted(_INTERACTIVE_NAMES, {"write plan approval": "auto approve plan"})


def test_top_level_stage_names_interactive(tmp_path):
    mod = _import_default(tmp_path)
    assert mod.AUTO is False
    assert _collect_top_names(mod.config.items) == _INTERACTIVE_NAMES


def test_top_level_stage_names_auto(tmp_path):
    mod = _import_auto(tmp_path)
    assert _collect_top_names(mod.config.items) == _AUTO_NAMES


def test_top_level_stage_names_plan_gate_off(tmp_path):
    mod = _import_fix_jira(["CBS-2249", "--arg", "plan_gate=off"], cwd=tmp_path)
    assert _collect_top_names(mod.config.items) == _PLAN_GATE_OFF_NAMES


def test_ci_rounds_sets_the_number_of_ci_fix_rounds(tmp_path):
    mod = _import_fix_jira(["CBS-2249", "--arg", "ci_rounds=1"], cwd=tmp_path)
    names = _collect_top_names(mod.config.items)
    assert "fix ci 1" in names and "wait for checks 2" not in names


@pytest.mark.parametrize(
    "argv",
    [["CBS-2249"], ["CBS-2249", "auto"], ["CBS-2249", "--arg", "plan_gate=off"]],
    ids=["interactive", "auto", "plan_gate_off"],
)
def test_every_stage_name_is_unique(tmp_path, argv):
    """Results and the checkpoint are keyed by bare stage name, and `norn run`
    never runs build_graph's duplicate check."""
    mod = _import_fix_jira(argv, cwd=tmp_path)
    names = _all_stage_names(mod.config.items)
    loops = [i.name for i in mod.config.items if isinstance(i, Loop)]
    everything = names + loops
    duplicates = sorted({n for n in everything if everything.count(n) > 1})
    assert duplicates == []


def test_loop_counts_differ_only_in_auto(tmp_path):
    assert _find_loop(_import_default(tmp_path).config.items, "resolve open questions").max_retries == 1
    assert _find_loop(_import_auto(tmp_path).config.items, "resolve open questions").max_retries == 3


def test_auto_fills_blanks_and_requires_every_fix(tmp_path):
    interactive = _import_default(tmp_path)
    assert _find_any(interactive.config.items, "expand recommended answers").impl.fill_blanks is False
    assert _find_stage(interactive.config.items, "fixes recorded").impl.auto is False
    auto = _import_auto(tmp_path)
    assert _find_any(auto.config.items, "expand recommended answers").impl.fill_blanks is True
    assert _find_stage(auto.config.items, "fixes recorded").impl.auto is True


# ---------------------------------------------------------------------------
# Import-time arguments
# ---------------------------------------------------------------------------


def test_stop_raises_with_the_v2_message(tmp_path):
    with pytest.raises(ValueError) as exc:
        _import_fix_jira(["CBS-2249", "stop"], cwd=tmp_path)
    assert str(exc.value) == (
        "fix_jira_issue_v3 always pauses at the reviewed plan; `stop` is a v2 positional. "
        "Use `auto` to run hands-off or --arg plan_gate=off to skip the plan pause."
    )


def test_capitalised_auto_raises(tmp_path):
    with pytest.raises(ValueError, match="unrecognised argument 'Auto'"):
        _import_fix_jira(["CBS-2249", "Auto"], cwd=tmp_path)


def test_unknown_positional_raises(tmp_path):
    with pytest.raises(ValueError, match="unrecognised argument 'later'"):
        _import_fix_jira(["CBS-2249", "later"], cwd=tmp_path)


def test_auto_sets_auto(tmp_path):
    assert _import_auto(tmp_path).AUTO is True


def test_auto_accepts_a_browse_url(tmp_path):
    mod = _import_fix_jira(["https://jira.example.com/browse/CBS-2249", "auto"], cwd=tmp_path)
    assert mod.ISSUE_KEY == "CBS-2249" and mod.AUTO is True


def test_knob_defaults(tmp_path):
    mod = _import_default(tmp_path)
    assert mod.BUDGET == 75.0
    assert (mod.MODEL, mod.REVIEW_MODEL, mod.FIX_MODEL) == ("deep", "deep", "deep")
    assert mod.CODEX_LABEL == "review"
    assert mod.TRIAGE_MODEL == "standard"
    assert mod.TEST_CMD is None
    assert (mod.FIX_RETRIES, mod.CI_ROUNDS, mod.CI_TIMEOUT_MINUTES) == (3, 2, 60)
    assert mod.PLAN_GATE is True
    assert (mod.BASE, mod.OVERWRITE_REMOTE) == ("", "")
    assert mod.JIRA_BROWSE is None
    assert mod.BRANCH == "CBS-2249"
    assert mod.config.budgets[0].max_cost_usd == 75.0
    assert mod.config.env_vars == {"GH_PROMPT_DISABLED": "1"}


def test_knobs_reach_the_stages(tmp_path):
    mod = _import_fix_jira(
        [
            "CBS-2249",
            "--arg", "fix_retries=5",
            "--arg", "ci_timeout_minutes=30",
            "--arg", "review_model=standard",
            "--arg", "fix_model=light",
            "--arg", "test_cmd=make test",
        ],
        cwd=tmp_path,
    )
    items = mod.config.items
    assert _find_loop(items, "fix code review").max_retries == 5
    assert _find_any(items, "run claude code review").impl.model == "standard"
    assert _find_any(items, "fix review findings").impl.model == "light"
    assert _find_any(items, "triage findings").impl.model == "standard"
    assert _find_stage(items, "resolve test command").impl.test_cmd_arg == "make test"
    assert _find_stage(items, "fix ci 1").impl._ci_timeout_minutes == 30


def test_jira_browse_from_arg_and_env(tmp_path, monkeypatch):
    mod = _import_fix_jira(
        ["CBS-2249", "--arg", "jira_browse=https://jira.example.com/browse/"], cwd=tmp_path
    )
    assert mod.JIRA_BROWSE == "https://jira.example.com/browse/"
    monkeypatch.setenv("JIRA_BROWSE_BASE", "https://env.example.com/browse/")
    assert _import_default(tmp_path).JIRA_BROWSE == "https://env.example.com/browse/"


@pytest.mark.parametrize(
    "arg, match",
    [
        ("budget=lots", "budget"),
        ("fix_retries=0", "fix_retries"),
        ("fix_retries=two", "fix_retries"),
        ("ci_rounds=0", "ci_rounds"),
        ("ci_timeout_minutes=-5", "ci_timeout_minutes"),
        ("plan_gate=maybe", "plan_gate"),
        ("jira_browse=https://jira.example.com/", "jira_browse"),
        ("test_cmd=", "test_cmd"),
    ],
)
def test_bad_knob_values_raise(tmp_path, arg, match):
    with pytest.raises(ValueError, match=match):
        _import_fix_jira(["CBS-2249", "--arg", arg], cwd=tmp_path)


def test_bad_jira_browse_env_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("JIRA_BROWSE_BASE", "https://jira.example.com/")
    with pytest.raises(ValueError, match="/browse/"):
        _import_default(tmp_path)


@pytest.mark.parametrize("knob", ["model", "review_model", "fix_model", "codex_label"])
def test_unknown_labels_raise(tmp_path, knob):
    with pytest.raises(ModelLabelError):
        _import_fix_jira(["CBS-2249", "--arg", f"{knob}=nonexistent"], cwd=tmp_path)


# ---------------------------------------------------------------------------
# Gate twins and the PR phase
# ---------------------------------------------------------------------------

_DECISION_TWINS = [
    ("plan approved", "plan approval recorded"),
    ("code review decided", "code review dispositions recorded"),
    ("ship decided", "ship decision recorded"),
]


@pytest.mark.parametrize("make", [_import_default, _import_auto], ids=["interactive", "auto"])
def test_each_gate_has_a_soft_and_a_hard_twin(tmp_path, make):
    items = make(tmp_path).config.items
    # Gate 1 is a loop; its soft side is the loop's exhaust prompt.
    questions = _find_loop(items, "resolve open questions")
    assert questions.on_exhaust is OnFailure.ASK_USER
    assert _find_stage(items, "open questions resolved").on_failure is OnFailure.FAIL
    for soft, hard in _DECISION_TWINS:
        assert soft != hard
        assert _find_stage(items, soft).on_failure is OnFailure.ASK_USER
        hard_stage = _find_stage(items, hard)
        assert hard_stage.on_failure is OnFailure.FAIL
        assert hard_stage.always_run, f"{hard} must re-validate the file on every resume"
        names = _collect_top_names(items)
        assert names.index(soft) < names.index(hard)


def test_every_loop_has_a_hard_twin_after_it(tmp_path):
    names = _collect_top_names(_import_default(tmp_path).config.items)
    twins = {
        "write brief": "brief written",
        "resolve open questions": "open questions resolved",
        "complete plan sections": "plan sections present",
        "review plan": "review shape ok",
        "apply review": "dispositions recorded",
        "split plan": "step files written",
        "codex code review": "codex code review written",
        "claude code review": "claude code review written",
        "triage code review": "code review shape ok",
        "fix code review": "review fixes green",
        "plan commits": "commit plan valid",
        "summarize pr": "pr summary valid",
    }
    for loop, hard in twins.items():
        assert names.index(hard) == names.index(loop) + 1, (loop, hard)


def test_pr_phase_stages_are_always_run(tmp_path):
    items = _import_default(tmp_path).config.items
    names = _collect_top_names(items)
    assert names[names.index("check pr provenance"):] == _PR_PHASE
    for name in _PR_PHASE:
        assert _find_stage(items, name).always_run, name


def test_guards_before_the_pr_phase_are_always_run(tmp_path):
    items = _import_default(tmp_path).config.items
    launch = _find_stage(items, "assert launch tree")
    assert isinstance(launch.impl, AssertLaunchTree)
    assert launch.always_run and launch.on_failure is OnFailure.FAIL
    identity = _find_stage(items, "check run identity")
    assert identity.always_run and identity.on_failure is OnFailure.FAIL
    always = [
        n for n in _all_stage_names(items)
        if n not in _PR_PHASE and _find_any(items, n).always_run
    ]
    assert always == [
        "assert launch tree",
        "check run identity",
        "plan approval recorded",
        "code review dispositions recorded",
        "ship decision recorded",
    ]


def test_register_child_state_covers_both_children(tmp_path):
    mod = _import_default(tmp_path)
    impl = _find_stage(mod.config.items, "register child state").impl
    assert impl._state_keys == ["implement_features_v3", "check_and_fix_ci_v2"]
    assert impl._project_dir == str(tmp_path)


# ---------------------------------------------------------------------------
# Child command and the implement stage
# ---------------------------------------------------------------------------


def test_child_command(tmp_path):
    mod = _import_default(tmp_path)
    assert "-m norn run implement_features_v3" in mod.CHILD
    assert mod.STEPS_DIR in mod.CHILD
    assert "--non-interactive" in mod.CHILD
    assert "--arg budget" not in mod.CHILD


def test_implement_stage_wiring(tmp_path):
    mod = _import_default(tmp_path)
    items = mod.config.items
    stage = _find_stage(items, "implement")
    assert isinstance(stage.impl, RunChildPipelineV3)
    assert stage.on_failure is OnFailure.ASK_USER
    assert stage.impl.timeout is None
    assert stage.impl._base_cmd == mod.CHILD
    assert stage.impl._pipeline is mod.config
    assert stage.impl._runs_dir == tmp_path / "implement_features_v3.runs"
    assert str(stage.impl._rewritten_marker) == mod.V3.rewritten
    names = _collect_top_names(items)
    assert names.index("resolve test command") < names.index("implement")


def _ctx_with(result_name: str, success: bool) -> PipelineContext:
    ctx = PipelineContext()
    ctx.results[result_name] = StageResult(name=result_name, success=success)
    return ctx


def test_diagnose_and_check_implementation_split_on_the_child_result(tmp_path):
    items = _import_default(tmp_path).config.items
    diagnose = _find_stage(items, "diagnose implementation")
    check = _find_stage(items, "check implementation")
    assert diagnose.on_failure is OnFailure.FAIL and check.on_failure is OnFailure.FAIL
    assert diagnose.when(_ctx_with("implement", False))
    assert not diagnose.when(_ctx_with("implement", True))
    assert check.when(_ctx_with("implement", True))
    assert not check.when(_ctx_with("implement", False))
    assert "--state-key implement_features_v3" in diagnose.impl.cmd
    assert '"$LINE1" != "VERDICT: PASS"' in check.impl.cmd


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def test_preflight_checks_gh_and_a_symbolic_head(tmp_path):
    cmd = _find_stage(_import_default(tmp_path).config.items, "preflight environment").impl.cmd
    assert "command -v gh" in cmd
    assert "gh auth status" in cmd
    assert "symbolic-ref" in cmd
    assert ".permissions.push" in cmd
    assert "tmp/jira/CBS-2249" in cmd
    assert "fix_jira_issue_v3.checkpoint" in cmd


def test_preflight_leaves_the_child_checkpoint_to_register_child_state(tmp_path):
    cmd = _find_stage(_import_default(tmp_path).config.items, "preflight environment").impl.cmd
    assert "implement_features_v3" not in cmd


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


def _preflight_repo(tmp_path: Path, *, detached: bool = False) -> Path:
    repo = _repo_on_branch(tmp_path, "CBS-2249")
    _git(repo, "remote", "add", "origin", "https://github.com/acme/widgets.git")
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "exclude").write_text("tmp/\nfix_jira_issue_v3.checkpoint\n")
    if detached:
        _git(repo, "checkout", "-q", "--detach")
    return repo


def _run_preflight(mod, repo: Path, tmp_path: Path, push: str = "true") -> subprocess.CompletedProcess:
    gh = (
        'case "$1" in\n'
        "  auth) exit 0;;\n"
        "  repo) echo acme/widgets;;\n"
        f"  api) echo {push};;\n"
        "  *) exit 9;;\n"
        "esac"
    )
    bin_dir = _fake_bin(tmp_path, "gh", gh)
    for tool in ("curl", "jq", "codex"):
        _fake_bin(tmp_path, tool, "exit 0")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "JIRA_AUTH": "x",
        "JIRA_BASE": "https://jira.example.com/",
    }
    cmd = _find_stage(mod.config.items, "preflight environment").impl.cmd
    return subprocess.run(cmd, shell=True, cwd=repo, capture_output=True, text=True, env=env)


def test_preflight_passes_in_a_ready_repo(tmp_path):
    repo = _preflight_repo(tmp_path)
    mod = _import_fix_jira(["CBS-2249"], cwd=repo)
    r = _run_preflight(mod, repo, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr


def test_preflight_refuses_a_detached_head(tmp_path):
    repo = _preflight_repo(tmp_path, detached=True)
    mod = _import_fix_jira(["CBS-2249"], cwd=repo)
    r = _run_preflight(mod, repo, tmp_path)
    assert r.returncode == 1
    assert "HEAD is detached" in r.stdout


def test_preflight_refuses_without_push_rights(tmp_path):
    repo = _preflight_repo(tmp_path)
    mod = _import_fix_jira(["CBS-2249"], cwd=repo)
    r = _run_preflight(mod, repo, tmp_path, push="false")
    assert r.returncode == 1
    assert "no push permission on acme/widgets" in r.stdout


# ---------------------------------------------------------------------------
# Prepare branch (real git)
# ---------------------------------------------------------------------------


def _repo_on_branch(tmp_path, branch: str) -> Path:
    """Init a repo with one commit, checked out on *branch*."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("# test\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "initial")
    if branch != "main":
        _git(repo, "switch", "-q", "-c", branch)
    return repo


def _run_prepare_branch(mod, repo: Path) -> subprocess.CompletedProcess:
    cmd = _find_stage(mod.config.items, "prepare branch").impl.cmd
    return subprocess.run(cmd, shell=True, cwd=repo, capture_output=True, text=True)


def test_prepare_branch_writes_a_missing_base_sha(tmp_path):
    repo = _repo_on_branch(tmp_path, "CBS-2249")
    mod = _import_fix_jira(["CBS-2249"], cwd=repo)

    r = _run_prepare_branch(mod, repo)

    assert r.returncode == 0, r.stderr
    assert Path(mod.BASE_FILE).read_text().strip() == _git(repo, "rev-parse", "HEAD")


def test_prepare_branch_keeps_an_existing_base_sha(tmp_path):
    repo = _repo_on_branch(tmp_path, "CBS-2249")
    first = _git(repo, "rev-parse", "HEAD")
    mod = _import_fix_jira(["CBS-2249"], cwd=repo)
    assert _run_prepare_branch(mod, repo).returncode == 0
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "step one")

    r = _run_prepare_branch(mod, repo)

    assert r.returncode == 0, r.stderr
    assert Path(mod.BASE_FILE).read_text().strip() == first
    assert _git(repo, "rev-parse", "HEAD") != first


def test_prepare_branch_leaves_main_for_the_issue_branch(tmp_path):
    repo = _repo_on_branch(tmp_path, "main")
    mod = _import_fix_jira(["CBS-2249"], cwd=repo)

    r = _run_prepare_branch(mod, repo)

    assert r.returncode == 0, r.stderr
    assert _git(repo, "branch", "--show-current") == "CBS-2249"


def test_prepare_branch_stays_on_a_branch_naming_the_issue(tmp_path):
    repo = _repo_on_branch(tmp_path, "feature/cbs-2249-sorting")
    mod = _import_fix_jira(["CBS-2249"], cwd=repo)

    r = _run_prepare_branch(mod, repo)

    assert r.returncode == 0, r.stderr
    assert _git(repo, "branch", "--show-current") == "feature/cbs-2249-sorting"


# ---------------------------------------------------------------------------
# Generate stages
# ---------------------------------------------------------------------------

# The stages whose agent writes code, commit text or PR text.
_WRITES_CODE_OR_PR_TEXT = ["fix review findings", "write commit plan", "write pr summary"]


@pytest.mark.parametrize("make", [_import_default, _import_auto], ids=["interactive", "auto"])
def test_generate_stages_have_no_pinned_cwd(tmp_path, make):
    generates = list(_collect_generates(make(tmp_path).config.items))
    assert generates, "expected at least one Generate stage"
    for name, gen in generates:
        assert gen.cwd is None, name


def test_prompts_that_write_code_or_pr_text_carry_the_comment_rule(tmp_path):
    generates = dict(_collect_generates(_import_default(tmp_path).config.items))
    for name in _WRITES_CODE_OR_PR_TEXT:
        assert COMMENT_RULE in generates[name].prompt, name


def test_every_generate_carries_the_subagent_env(tmp_path):
    subagent = resolve_label("claude-code", "standard").model
    generates = list(_collect_generates(_import_auto(tmp_path).config.items))
    assert len(generates) >= 10
    for name, gen in generates:
        env = gen.env or {}
        assert env.get("CLAUDE_CODE_SUBAGENT_MODEL") == subagent, name
        assert env.get("CLAUDE_CODE_SUBAGENT_MODEL_FORCE") == "1", name


def test_generate_labels_resolve(tmp_path):
    from norn.model_labels import labels_for

    known = set(labels_for("claude-code"))
    for name, gen in _collect_generates(_import_default(tmp_path).config.items):
        assert gen.model in known, name


# ---------------------------------------------------------------------------
# Graph, metadata, catalog
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("make", [_import_default, _import_auto], ids=["interactive", "auto"])
def test_graph_builds(tmp_path, make):
    """Sibling node ids must be unique, or the TUI cannot launch the run."""
    build_graph(make(tmp_path).config)


def test_metadata(tmp_path):
    mod = _import_default(tmp_path)
    assert list(mod.metadata["args"].keys()) == ["args"]
    assert "auto" in mod.metadata["args"]["args"]
    assert mod.metadata["env_vars"] == ["ANTHROPIC_API_KEY", "JIRA_AUTH", "JIRA_BASE"]


def test_catalog_lists_v3_beside_its_original():
    infos = {info.name: info for info in list_pipelines()}
    assert "fix_jira_issue_v3" in infos
    assert "fix_jira_issue_v2" in infos
    info = infos["fix_jira_issue_v3"]
    assert info.short.startswith("Fix a Jira issue")
    assert list(info.args) == ["args"]
    for marker in (
        "CBS-1234 auto --non-interactive",
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
    ):
        assert marker in info.long, marker


def test_every_copy_has_its_original():
    from tests.test_catalog import test_every_copy_has_its_original as check

    check()


@pytest.mark.asyncio
async def test_missing_commit_skill_imports_and_fails_at_run_time(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    launch = tmp_path / "launch"
    launch.mkdir()
    monkeypatch.setenv("HOME", str(home))

    mod = _import_fix_jira(["CBS-2249"], cwd=launch)

    assert mod.COMMIT_SKILL is None
    stage = _find_stage(mod.config.items, "check commit skill")
    result = await stage.impl.run(PipelineContext())
    assert not result.success
    assert "split-commit-plan" in result.error.splitlines()[0]
    assert str(home / ".claude" / "skills" / "split-commit-plan") in result.error


# ---------------------------------------------------------------------------
# Resume flows (mocked agents, real git, notify patched by _quiet)
# ---------------------------------------------------------------------------

KEY = "CBS-1"

_DECIDED_REVIEW = """\
# Code review — CBS-1 (aaaaaaa..bbbbbbb)

STATUS: DECIDED
<!-- Set each Disposition to fix, skip or follow-up, then set the STATUS line to DECIDED. -->

## C1. Reset swaps the shared value without a lock [major]
Where: a.txt:1
Problem: readers can see a half-written value.
Fix: take the lock in reset.
Source: claude, codex
Disposition: fix

## C2. Rename the loop variable [nit]
Where: a.txt:1
Problem: the name hides a field.
Source: codex
Disposition: skip (auto: nit)
"""

_SHIP = """\
# Ship — CBS-1

STATUS: DECIDED
COMMITS: keep
TITLE: CBS-1: fix the shared value reset
"""


def _commit(repo: Path, name: str, text: str, message: str) -> str:
    (repo / name).write_text(text)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


class _Run:
    """A launch repo on branch CBS-1 with a base commit and one step commit."""

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
        (self.repo / ".git" / "info").mkdir(exist_ok=True)
        (self.repo / ".git" / "info" / "exclude").write_text("tmp/\n")
        self.base = _commit(self.repo, "README.md", "# test\n", "initial")
        _git(self.repo, "switch", "-q", "-c", KEY)
        self.step = _commit(self.repo, "a.txt", "one\n", "refactor: step-01 — add a")

        # No split-commit-plan skill anywhere: `check commit skill` stops the run.
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        self.state = tmp_path / "state"
        self.state.mkdir()
        self.config_path = str(self.state / "fix_jira_issue_v3")
        monkeypatch.setenv("NORN_STATE_KEY", self.config_path)

    def load(self, key: str = KEY):
        mod = _import_fix_jira([key], cwd=self.repo)
        self.mod = mod
        return mod

    def write_artifacts(self, mod) -> None:
        """The files the cached stages before gate 3 would have left."""
        v3 = mod.V3
        Path(v3.work).mkdir(parents=True, exist_ok=True)
        Path(mod.BASE_FILE).write_text(f"{self.base}\n")
        Path(v3.approval).write_text(f"# Plan approval — {KEY}\n\nDECISION: approve\n")
        Path(v3.code_review).write_text(_DECIDED_REVIEW)
        Path(v3.test_cmd_sh).write_text(f"set -e\necho run >> {shlex.quote(str(self.tmp / 'count'))}\n")
        Path(v3.test_timeout).write_text("60\n")

    def checkpoint(self, mod, *, through: str | None = None, before: str | None = None,
                   identity_key: str = KEY) -> None:
        """Save a checkpoint holding every stage before *before* (or through *through*)."""
        names = _all_stage_names(mod.config.items)
        stop = names.index(before) if before else names.index(through) + 1
        # A real run never checkpoints a stage its `when` skipped.
        done = [n for n in names[:stop] if n != "diagnose implementation"]
        outputs: dict = {n: None for n in done}
        outputs["record run identity"] = {"issue": identity_key, "branch": KEY}
        save_checkpoint(self.config_path, "fix_jira_issue_v3", None, done, outputs)

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


def _mock_agents(monkeypatch, run: _Run) -> list[str]:
    """Every Generate is mocked; the fix agent fixes C1 in a new file."""
    calls: list[str] = []

    async def fake_run(self, ctx, **kwargs):
        calls.append(self.prompt)
        (run.repo / "fixed.txt").write_text("locked\n")
        Path(run.mod.V3.review_fixes).write_text("- C1: fixed — reset takes the lock in fixed.txt\n")
        return StageResult(name="", success=True)

    monkeypatch.setattr(Generate, "run", fake_run)
    return calls


@pytest.mark.asyncio
async def test_resume_at_gate_3_runs_only_the_guards_then_the_decided_review(tmp_path, monkeypatch):
    run = _Run(tmp_path, monkeypatch)
    mod = run.load()
    run.write_artifacts(mod)
    run.checkpoint(mod, before="code review decided")
    calls = _mock_agents(monkeypatch, run)

    started, error = await run.resume(mod)

    assert started == [
        # The always-run guards before gate 3, and nothing else that is cached:
        "assert launch tree",
        "check run identity",
        "plan approval recorded",
        # Gate 3, as the human decided it, and what follows:
        "code review decided",
        "code review dispositions recorded",
        "snapshot before fix",
        "compress review tests",
        "fix review findings",
        "snapshot after fix",
        "run review tests",
        "review fixes green",
        "fixes recorded",
        "commit review fixes",
        "worktree clean",
        "write review followups",
        "record rewrite range",
        "check commit skill",
    ]
    # The run ends at the missing skill, after gate 3 and the fix commit.
    assert isinstance(error, PipelineError) and error.stage_name == "check commit skill"
    assert len(calls) == 1 and "fixed — <what you changed>" in calls[0]
    assert _git(run.repo, "log", "-1", "--format=%s") == f"{KEY}: address code review findings"
    assert _git(run.repo, "show", "--name-only", "--format=", "HEAD") == "fixed.txt"
    assert (tmp_path / "count").read_text().splitlines() == ["run"]  # the tests ran once
    assert Path(mod.V3.review_nits).read_text() == "- C2 Rename the loop variable (a.txt:1)\n"
    assert Path(mod.V3.rewrite_range).is_file()


@pytest.mark.asyncio
async def test_resume_with_another_issue_key_fails_at_check_run_identity(tmp_path, monkeypatch):
    run = _Run(tmp_path, monkeypatch)
    first = run.load(KEY)
    run.write_artifacts(first)
    run.checkpoint(first, before="code review decided")
    calls = _mock_agents(monkeypatch, run)

    other = run.load("CBS-2")
    started, error = await run.resume(other)

    assert started == ["assert launch tree", "check run identity"]
    assert isinstance(error, PipelineError) and error.stage_name == "check run identity"
    assert error.result.error.splitlines()[0] == "checkpoint belongs to CBS-1, not CBS-2"
    assert calls == []


@pytest.mark.asyncio
async def test_resume_after_an_unshipped_commit_fails_before_the_push(tmp_path, monkeypatch):
    run = _Run(tmp_path, monkeypatch)
    mod = run.load()
    run.write_artifacts(mod)
    Path(mod.V3.ship).write_text(_SHIP)
    shipped = run.step
    Path(mod.V3.shipped_heads).write_text(f"{shipped}\n")
    pr = pr_paths(mod.V3)
    Path(pr.root).mkdir(parents=True)
    halt(pr, "checks_red", "a check failed")
    extra = _commit(run.repo, "b.txt", "two\n", f"{KEY}: fix by hand after the halt")
    run.checkpoint(mod, through="record shipped head")

    calls = _mock_agents(monkeypatch, run)
    halting: list[str] = []

    async def no_halting_command(self, ctx, **kwargs):
        halting.append(self.cmd)
        return StageResult(name="", success=False, error="a halting command ran")

    monkeypatch.setattr(HaltingCommand, "run", no_halting_command)
    bin_dir, gh_calls = install_fake_gh(tmp_path, {(): [(0, "", "")]})
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    started, error = await run.resume(mod)

    assert started == [
        "assert launch tree",
        "check run identity",
        "plan approval recorded",
        "code review dispositions recorded",
        "ship decision recorded",
        "check pr provenance",
    ]
    assert isinstance(error, PipelineError) and error.stage_name == "check pr provenance"
    text = error.result.error
    assert text.splitlines()[0] == f"HEAD {extra[:7]} was not shipped by this run"
    assert f"git reset --hard {shipped}" in text
    assert f"norn run fix_jira_issue_v3 {KEY} --resume" in text
    # Nothing was pushed, and the halted PR root was left as it was.
    assert "push" not in started and halting == []
    assert Path(gh_calls).read_text() == ""
    assert Path(pr.halt).exists()
    assert calls == []
    assert "push" not in load_checkpoint(run.config_path).completed_stages

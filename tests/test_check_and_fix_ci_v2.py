"""Tests for norn.pipelines.check_and_fix_ci_v2.

Step 04 adds the first assertion: check_and_fix_ci.detect_test_cmd is the
same function object as norn.pipelines._test_cmd.detect_test_cmd, not a
copy. Step 12 adds the remaining reload-based tests: comma-separated
``--arg workflow=``, the comment-style rule in both fix prompts, and the
attribution guard in both commit stages.
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys

import pytest

from norn.dsl import Loop, Parallel, Stage
from norn.pipelines._comment_style import COMMENT_RULE
from tests.merge_fakes import make_repo_with_remote


def _walk_stages(items):
    """Yield every Stage in *items*, recursing into Loop and Parallel bodies."""
    for item in items:
        if isinstance(item, Stage):
            yield item
        elif isinstance(item, (Loop, Parallel)):
            yield from _walk_stages(item.stages)


def _find_stage(items, name):
    for stage in _walk_stages(items):
        if stage.name == name:
            return stage
    raise KeyError(name)


def _import_check_and_fix_ci(argv_tail: list[str] = (), cwd=None):
    """Import (or reload) check_and_fix_ci with patched argv and cwd.

    Mirrors the merge_branch reload idiom (tests/test_merge_branch.py:
    _import_merge_branch): the module reads both ``sys.argv`` (for
    ``--arg`` flags) and ``os.getcwd()`` (for ``PROJECT_DIR``, which also
    feeds ``detect_test_cmd`` at import time) so both must be patched
    before the (re)import. ``_ensure_githubkit()`` is a no-op here because
    githubkit is already in the dev dependency group.
    """
    saved_argv = list(sys.argv)
    saved_cwd = os.getcwd()
    try:
        sys.argv = [saved_argv[0], *argv_tail]
        if cwd is not None:
            os.chdir(str(cwd))
        from norn.pipelines import check_and_fix_ci_v2 as check_and_fix_ci

        return importlib.reload(check_and_fix_ci)
    finally:
        sys.argv = saved_argv
        os.chdir(saved_cwd)


def test_detect_test_cmd_is_the_shared_function(tmp_path) -> None:
    mod = _import_check_and_fix_ci(cwd=tmp_path)

    from norn.pipelines import _test_cmd

    assert mod.detect_test_cmd is _test_cmd.detect_test_cmd


def test_check_and_fix_ci_still_imports(tmp_path) -> None:
    mod = _import_check_and_fix_ci(cwd=tmp_path)

    assert mod.config is not None


# ---------------------------------------------------------------------------
# WORKFLOWS: comma-separated --arg workflow=
# ---------------------------------------------------------------------------


def test_workflows_defaults_to_ci_yml(tmp_path) -> None:
    mod = _import_check_and_fix_ci(cwd=tmp_path)

    assert mod.WORKFLOWS == ["ci.yml"]


def test_workflows_splits_comma_separated_arg(tmp_path) -> None:
    mod = _import_check_and_fix_ci(["--arg", "workflow=a.yml,b.yml"], cwd=tmp_path)

    assert mod.WORKFLOWS == ["a.yml", "b.yml"]


def test_workflows_drops_blank_entries_and_strips_whitespace(tmp_path) -> None:
    mod = _import_check_and_fix_ci(["--arg", "workflow= a.yml , ,b.yml"], cwd=tmp_path)

    assert mod.WORKFLOWS == ["a.yml", "b.yml"]


def test_ci_timeout_defaults_to_twenty_minutes(tmp_path) -> None:
    mod = _import_check_and_fix_ci(cwd=tmp_path)

    assert _find_stage(mod.pipeline.items, "check ci").impl.ci_kwargs["timeout_minutes"] == 20


def test_ci_timeout_arg_sets_the_poll_limit(tmp_path) -> None:
    mod = _import_check_and_fix_ci(["--arg", "ci_timeout_minutes=60"], cwd=tmp_path)
    try:
        assert _find_stage(mod.pipeline.items, "check ci").impl.ci_kwargs["timeout_minutes"] == 60
    finally:
        _import_check_and_fix_ci(cwd=tmp_path)


@pytest.mark.parametrize("value", ["0", "-5", "1.5", "soon"])
def test_bogus_ci_timeout_fails_at_import(tmp_path, value) -> None:
    try:
        with pytest.raises(ValueError, match="ci_timeout_minutes"):
            _import_check_and_fix_ci(["--arg", f"ci_timeout_minutes={value}"], cwd=tmp_path)
    finally:
        _import_check_and_fix_ci(cwd=tmp_path)


@pytest.mark.asyncio
async def test_ci_timeout_stops_the_run_with_its_own_error(tmp_path, monkeypatch) -> None:
    """A run still going at the deadline is not a failure to hand to the fix loop."""
    from norn.models import PipelineContext, StageResult
    from norn.runner import PipelineError

    mod = _import_check_and_fix_ci(cwd=tmp_path)

    class _TimedOut:
        def __init__(self, **_kwargs) -> None:
            pass

        async def run(self, ctx, **_kwargs):
            return StageResult(
                name="", success=False,
                error="CI timed out after 20 minutes. Last status: in_progress",
                output={
                    "run_id": 7, "name": "test (postgres)", "status": "in_progress",
                    "conclusion": None, "url": "https://example.test/runs/7",
                    "logs": "", "timed_out": True,
                },
            )

    monkeypatch.setattr(mod, "CheckCI", _TimedOut)

    with pytest.raises(PipelineError) as raised:
        await mod.CheckCIWithLogs(workflows=["test-pg.yml"]).run(PipelineContext())

    error = raised.value.result.error
    assert "timed out after 20 minutes" in error
    assert "https://example.test/runs/7" in error
    assert "--arg ci_timeout_minutes" in error


# ---------------------------------------------------------------------------
# COMMENT_RULE in both fix prompts
# ---------------------------------------------------------------------------


def test_fix_local_prompt_contains_comment_rule(tmp_path) -> None:
    mod = _import_check_and_fix_ci(cwd=tmp_path)

    fix_local = _find_stage(mod.pipeline.items, "fix local")
    assert COMMENT_RULE in fix_local.impl.prompt


def test_fix_ci_prompt_contains_comment_rule(tmp_path) -> None:
    mod = _import_check_and_fix_ci(cwd=tmp_path)

    fix_ci = _find_stage(mod.pipeline.items, "fix ci")
    assert COMMENT_RULE in fix_ci.impl.prompt


# ---------------------------------------------------------------------------
# Commit stages: fixed subjects + attribution_guard_sh
# ---------------------------------------------------------------------------


def test_commit_local_fixes_keeps_fixed_subject_and_has_guard(tmp_path) -> None:
    mod = _import_check_and_fix_ci(cwd=tmp_path)

    stage = _find_stage(mod.pipeline.items, "commit local fixes")
    cmd = stage.impl.cmd
    assert 'git commit -m "fix: auto-fix from check_and_fix_ci local loop"' in cmd
    assert "refusing to commit" in cmd
    # No messages_range for this stage — the unpushed range can hold the
    # human's own commits.
    assert "@{u}..HEAD" not in cmd
    # The guard runs after staging and before the commit.
    assert cmd.index("git add -A") < cmd.index("refusing to commit") < cmd.index("git commit")


def test_commit_push_ci_fix_keeps_fixed_subject_and_has_guard(tmp_path) -> None:
    mod = _import_check_and_fix_ci(cwd=tmp_path)

    stage = _find_stage(mod.pipeline.items, "commit push ci fix")
    cmd = stage.impl.cmd
    assert 'git commit -m "fix(ci): auto-fix from check_and_fix_ci"' in cmd
    assert "refusing to commit" in cmd
    assert "@{u}..HEAD" in cmd
    assert cmd.index("git add -A") < cmd.index("refusing to commit") < cmd.index("git commit")


def test_commit_push_ci_fix_blocks_attribution_line(tmp_path) -> None:
    repo = make_repo_with_remote(tmp_path)
    repo.repo.git.checkout("-b", "feature-x")
    repo.repo.git.push("origin", "feature-x", "--set-upstream")

    with open(repo.path + "/tainted.txt", "w") as f:
        f.write("code\n# Generated with [Claude Code]\n")
    repo.repo.index.add(["tainted.txt"])

    before_head = repo.repo.head.commit.hexsha
    before_remote = repo.repo.git.rev_parse("origin/feature-x")

    mod = _import_check_and_fix_ci(cwd=repo.path)
    stage = _find_stage(mod.pipeline.items, "commit push ci fix")
    cmd = stage.impl.cmd

    result = subprocess.run(
        ["sh", "-c", cmd], cwd=repo.path, capture_output=True, text=True
    )

    assert result.returncode == 1
    assert "refusing to commit" in result.stderr

    after_head = repo.repo.git.rev_parse("HEAD")
    assert after_head == before_head

    after_remote = repo.repo.git.rev_parse("origin/feature-x")
    assert after_remote == before_remote


def test_commit_push_ci_fix_commits_and_pushes_a_clean_fix(tmp_path) -> None:
    """The fix's new files go in the commit too, or CI gets code that references them."""
    repo = make_repo_with_remote(tmp_path)
    repo.repo.git.checkout("-b", "feature-x")
    repo.repo.git.push("origin", "feature-x", "--set-upstream")
    with open(repo.path + "/README.md", "a") as f:
        f.write("an ordinary fix\n")
    with open(repo.path + "/Helper.java", "w") as f:
        f.write("class Helper {}\n")

    mod = _import_check_and_fix_ci(cwd=repo.path)
    cmd = _find_stage(mod.pipeline.items, "commit push ci fix").impl.cmd
    result = subprocess.run(["sh", "-c", cmd], cwd=repo.path, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    head = repo.repo.head.commit
    assert head.message.strip() == "fix(ci): auto-fix from check_and_fix_ci"
    assert set(head.stats.files) == {"README.md", "Helper.java"}
    assert repo.repo.git.rev_parse("origin/feature-x") == head.hexsha


@pytest.mark.asyncio
async def test_fix_that_only_adds_a_file_counts_as_a_change(tmp_path) -> None:
    from norn.models import PipelineContext

    repo = make_repo_with_remote(tmp_path)
    mod = _import_check_and_fix_ci(cwd=repo.path)
    with open(repo.path + "/Helper.java", "w") as f:
        f.write("class Helper {}\n")

    result = await mod.AssertFixProducedDiff(project_dir=repo.path).run(PipelineContext())

    assert result.success


@pytest.mark.asyncio
async def test_fix_with_no_changes_stops_the_run(tmp_path) -> None:
    from norn.models import PipelineContext
    from norn.runner import PipelineError

    repo = make_repo_with_remote(tmp_path)
    mod = _import_check_and_fix_ci(cwd=repo.path)

    with pytest.raises(PipelineError, match="produced no edits"):
        await mod.AssertFixProducedDiff(project_dir=repo.path).run(PipelineContext())


# ---------------------------------------------------------------------------
# Learning proposal (step 16)
# ---------------------------------------------------------------------------

import shutil
from pathlib import Path

import pytest

from norn.dsl import OnFailure, Pipeline
from norn.models import PipelineContext, StageResult
from norn.pipelines._learnings import LearnPaths
from norn.runner import run_pipeline
from norn.stages.base import BaseStage
from norn.stages.run_command import RunCommand

LEARN_STAGES = [
    "collect learning evidence",
    "learn",
    "learn postcondition",
    "decide learning",
    "publish learning proposal",
    "learning summary",
]


def _top_level_names(mod) -> list[str]:
    return [item.name for item in mod.pipeline.items if isinstance(item, Stage)]


def _isolated(mod, tmp_path):
    """Point the module's work root at *tmp_path* so a test never touches the shared temp dir."""
    work = Path(tmp_path) / "work-root"
    work.mkdir()
    mod.WORK_ROOT = work
    mod.START_SHA = work / "start.sha"
    mod.ATTEMPTS_MD = work / "attempts.md"
    mod.LEARN_EVIDENCE = work / "learn-evidence.md"
    mod.LEARN_PATHS = LearnPaths(work)
    return work


def test_learn_stage_list_order_and_gating(tmp_path) -> None:
    mod = _import_check_and_fix_ci(cwd=tmp_path)
    names = _top_level_names(mod)

    i = names.index("push all")
    assert names[i:] == ["push all", *LEARN_STAGES, "assert nothing unpushed"]
    assert names.index("prepare learning scratch") == names.index("check clean worktree") + 1

    paths = mod.LEARN_PATHS
    ctx = PipelineContext()
    assert _find_stage(mod.pipeline.items, "collect learning evidence").when is None
    assert _find_stage(mod.pipeline.items, "learning summary").when is None
    assert _find_stage(mod.pipeline.items, "assert nothing unpushed").when is None
    baseline = ("learn", "learn postcondition", "decide learning")
    publish = _find_stage(mod.pipeline.items, "publish learning proposal")
    # The work root is keyed by the (unique) launch directory, so touching it is safe.
    paths.root.mkdir(parents=True, exist_ok=True)
    try:
        for name in baseline:
            assert _find_stage(mod.pipeline.items, name).when(ctx) is False, name
        assert publish.when(ctx) is False
        paths.pre_head.write_text("sha\n")
        for name in baseline:
            assert _find_stage(mod.pipeline.items, name).when(ctx) is True, name
        assert publish.when(ctx) is False
        paths.marker_ready.write_text("target\n")
        assert publish.when(ctx) is True
    finally:
        shutil.rmtree(paths.root, ignore_errors=True)

    post = _find_stage(mod.pipeline.items, "learn postcondition")
    assert post.on_failure == OnFailure.FAIL
    assert _find_stage(mod.pipeline.items, "assert nothing unpushed").on_failure == OnFailure.FAIL
    learn = _find_stage(mod.pipeline.items, "learn")
    assert learn.timeout is None
    assert learn.impl.timeout == 300
    assert learn.impl.allowed_tools == ["Write"]
    assert learn.impl.permission_mode == "acceptEdits"
    assert learn.impl.max_turns == 4


def test_recorders_sit_inside_the_loops(tmp_path) -> None:
    mod = _import_check_and_fix_ci(cwd=tmp_path)
    local = next(i for i in mod.pipeline.items if isinstance(i, Loop) and i.name == "local tests")
    ci = next(i for i in mod.pipeline.items if isinstance(i, Loop) and i.name == "wait ci")
    lnames = [s.name for s in local.stages]
    cnames = [s.name for s in ci.stages]
    assert lnames.index("record local attempt") == lnames.index("compress local tests") + 1
    assert cnames.index("record ci attempt") == cnames.index("isolate failing step") + 1


def test_learn_off_keeps_only_the_unpushed_assertion(tmp_path) -> None:
    mod = _import_check_and_fix_ci(["--arg", "learn=off"], cwd=tmp_path)
    try:
        names = _top_level_names(mod)
        assert names[names.index("push all"):] == ["push all", "assert nothing unpushed"]
        all_names = [s.name for s in _walk_stages(mod.pipeline.items)]
        for gone in (*LEARN_STAGES, "prepare learning scratch", "record local attempt", "record ci attempt"):
            assert gone not in all_names
    finally:
        _import_check_and_fix_ci(cwd=tmp_path)


@pytest.mark.parametrize("arg", ["learn=maybe", "learn_model=gpt-9", "learn_model=claude-sonnet-4"])
def test_bogus_learn_knobs_fail_at_import(tmp_path, arg) -> None:
    try:
        with pytest.raises(ValueError):
            _import_check_and_fix_ci(["--arg", arg], cwd=tmp_path)
    finally:
        _import_check_and_fix_ci(cwd=tmp_path)


def test_learn_model_label_is_applied(tmp_path) -> None:
    mod = _import_check_and_fix_ci(["--arg", "learn_model=standard"], cwd=tmp_path)
    try:
        assert _find_stage(mod.pipeline.items, "learn").impl.model == "standard"
    finally:
        _import_check_and_fix_ci(cwd=tmp_path)
    mod = _import_check_and_fix_ci(cwd=tmp_path)
    assert _find_stage(mod.pipeline.items, "learn").impl.model == "light"


def test_no_stage_after_wait_ci_commits_stages_or_pushes(tmp_path) -> None:
    mod = _import_check_and_fix_ci(cwd=tmp_path)
    names = _top_level_names(mod)
    for name in names[names.index("push all") + 1:]:
        impl = _find_stage(mod.pipeline.items, name).impl
        cmd = getattr(impl, "cmd", "")
        for verb in ("git add", "git commit", "git push"):
            assert verb not in cmd, (name, verb)


# -- the recorders through a real loop ---------------------------------------


class _FailTimes(BaseStage):
    needs_agent = False

    def __init__(self, fails: int, output: str) -> None:
        self.fails = fails
        self.output = output
        self.calls = 0

    async def run(self, ctx, **kwargs):
        self.calls += 1
        if self.calls <= self.fails:
            return StageResult(name="", success=False, error="red", output=self.output)
        return StageResult(name="", success=True, output="green")


class _Echo(BaseStage):
    needs_agent = False

    def __init__(self, fmt: str) -> None:
        self.fmt = fmt
        self.n = 0

    async def run(self, ctx, **kwargs):
        self.n += 1
        return StageResult(name="", success=True, output=self.fmt.format(n=self.n))


@pytest.mark.asyncio
async def test_record_local_attempt_through_a_real_loop(tmp_path) -> None:
    mod = _import_check_and_fix_ci(cwd=tmp_path)
    ledger = tmp_path / "attempts.md"
    pipeline = Pipeline("t").loop(
        "local tests",
        max_retries=3,
        stages=[
            Stage("compress local tests", _Echo("compressed-{n}"), when=stage_failed_("run local tests")),
            Stage(
                "record local attempt",
                mod.RecordAttempt(kind="local", source_stage="compress local tests", ledger=ledger),
                when=stage_failed_("run local tests"),
            ),
            Stage("run local tests", _FailTimes(2, "raw")),
        ],
    )
    await run_pipeline(pipeline)
    text = ledger.read_text()
    assert "local attempt 1" in text and "local attempt 2" in text
    assert text.index("local attempt 1") < text.index("local attempt 2")
    assert "local attempt 3" not in text
    assert text.index("compressed-1") < text.index("compressed-2")


@pytest.mark.asyncio
async def test_record_ci_attempt_through_a_real_loop(tmp_path) -> None:
    mod = _import_check_and_fix_ci(cwd=tmp_path)
    ledger = tmp_path / "attempts.md"
    pipeline = Pipeline("t").loop(
        "wait ci",
        max_retries=3,
        stages=[
            Stage("isolate failing step", _Echo("isolated-{n}"), when=stage_failed_("check ci")),
            Stage(
                "record ci attempt",
                mod.RecordAttempt(
                    kind="ci",
                    source_stage="isolate failing step",
                    ledger=ledger,
                    workflow_stage="check ci",
                ),
                when=stage_failed_("check ci"),
            ),
            Stage("check ci", _FailTimes(2, "Failing workflow: build.yml\nRun URL: u\n\nlogs")),
        ],
    )
    await run_pipeline(pipeline)
    text = ledger.read_text()
    assert text.index("ci attempt 1") < text.index("ci attempt 2")
    assert "ci attempt 3" not in text
    assert "Workflow: build.yml" in text
    assert "isolated-1" in text and "isolated-2" in text


def stage_failed_(name):
    from norn.dsl import stage_failed

    return stage_failed(name)


@pytest.mark.asyncio
async def test_record_attempt_requires_the_attempt_kwarg(tmp_path) -> None:
    mod = _import_check_and_fix_ci(cwd=tmp_path)
    rec = mod.RecordAttempt(kind="local", source_stage="x", ledger=tmp_path / "a.md")
    assert rec.emits_events is True
    with pytest.raises(TypeError):
        await rec.run(PipelineContext())


@pytest.mark.asyncio
async def test_record_attempt_caps_the_excerpt(tmp_path) -> None:
    mod = _import_check_and_fix_ci(cwd=tmp_path)
    ledger = tmp_path / "a.md"
    ctx = PipelineContext()
    ctx.results["src"] = StageResult(
        name="src", success=True, output="\n".join(f"line{i}" for i in range(200))
    )
    rec = mod.RecordAttempt(kind="local", source_stage="src", ledger=ledger)
    await rec.run(ctx, node_id="n", attempt=2)
    text = ledger.read_text()
    assert "line79" in text and "line80" not in text
    assert "local attempt 1" in text


# -- collect / decide / publish on temp repos --------------------------------


def _ready_repo(tmp_path):
    repo = make_repo_with_remote(tmp_path)
    repo.repo.git.checkout("-b", "feature-x")
    repo.repo.git.push("origin", "feature-x", "--set-upstream")
    return repo


def _fake_journal(tmp_path):
    class _J:
        dir = Path(tmp_path) / "run-dir"

    _J.dir.mkdir()
    return _J


async def _run_stage(mod, name, ctx):
    stage = _find_stage(mod.pipeline.items, name)
    return await stage.impl.run(ctx)


@pytest.mark.asyncio
async def test_collect_skips_without_fix_commits(tmp_path) -> None:
    repo = _ready_repo(tmp_path)
    mod = _import_check_and_fix_ci(cwd=repo.path)
    work = _isolated(mod, tmp_path)
    head = repo.repo.head.commit.hexsha
    (work / "start.sha").write_text(head + "\n")
    # A human commit after start is not a fix commit.
    (Path(repo.path) / "human.txt").write_text("x\n")
    repo.repo.index.add(["human.txt"])
    repo.repo.index.commit("human work")

    ctx = PipelineContext()
    ctx.journal = _fake_journal(tmp_path)
    paths = mod.LEARN_PATHS
    result = await mod.CollectLearningEvidence(paths=paths, name="collect learning evidence").run(ctx)

    assert result.success
    assert "CI was green without a fix in this run" in result.output
    assert paths.marker_skipped.exists()
    assert not paths.pre_head.exists()
    assert mod.file_exists(str(paths.pre_head))(ctx) is False
    summary = await mod.LearningSummary(paths=paths, name="learning summary").run(ctx)
    assert summary.output == "skipped: CI was green without a fix in this run"


@pytest.mark.asyncio
async def test_collect_skips_without_a_journal(tmp_path) -> None:
    repo = _ready_repo(tmp_path)
    mod = _import_check_and_fix_ci(cwd=repo.path)
    work = _isolated(mod, tmp_path)
    (work / "start.sha").write_text(repo.repo.head.commit.hexsha + "\n")
    (Path(repo.path) / "f.txt").write_text("x\n")
    repo.repo.index.add(["f.txt"])
    repo.repo.index.commit("fix(ci): auto-fix from check_and_fix_ci")

    result = await mod.CollectLearningEvidence(paths=mod.LEARN_PATHS, name="c").run(PipelineContext())
    assert result.output == "skipped: no journal to hold the proposal"
    assert not mod.LEARN_PATHS.pre_head.exists()


@pytest.mark.asyncio
async def test_collect_builds_evidence_and_baseline(tmp_path) -> None:
    repo = _ready_repo(tmp_path)
    mod = _import_check_and_fix_ci(cwd=repo.path)
    work = _isolated(mod, tmp_path)
    start = repo.repo.head.commit.hexsha
    (work / "start.sha").write_text(start + "\n")
    (work / "attempts.md").write_text("## ci attempt 1\n")
    (Path(repo.path) / "widget_parser.py").write_text("x = 1\n")
    repo.repo.index.add(["widget_parser.py"])
    repo.repo.index.commit("fix(ci): auto-fix from check_and_fix_ci")
    (Path(repo.path) / "other.txt").write_text("y\n")
    repo.repo.index.add(["other.txt"])
    repo.repo.index.commit("unrelated")

    ctx = PipelineContext()
    ctx.journal = _fake_journal(tmp_path)
    result = await mod.CollectLearningEvidence(paths=mod.LEARN_PATHS, name="c").run(ctx)

    assert result.success
    assert "## ci attempt 1" in result.output
    assert "widget_parser.py" in result.output
    assert "other.txt" not in result.output
    assert "store does not exist" in result.output
    assert mod.LEARN_PATHS.pre_head.read_text().strip() == repo.repo.head.commit.hexsha
    assert (work / "learn-evidence.md").read_text() == result.output


@pytest.mark.asyncio
async def test_collect_records_an_unusable_store_as_failed(tmp_path) -> None:
    repo = _ready_repo(tmp_path)
    cfg = Path(repo.path) / ".compound-engineering"
    cfg.mkdir()
    (cfg / "config.yaml").write_text("docs_root: ../outside\n")
    mod = _import_check_and_fix_ci(cwd=repo.path)
    work = _isolated(mod, tmp_path)
    (work / "start.sha").write_text(repo.repo.head.commit.hexsha + "\n")
    (Path(repo.path) / "f.txt").write_text("x\n")
    repo.repo.index.add(["f.txt"])
    repo.repo.index.commit("fix: auto-fix from check_and_fix_ci local loop")
    ctx = PipelineContext()
    ctx.journal = _fake_journal(tmp_path)

    result = await mod.CollectLearningEvidence(paths=mod.LEARN_PATHS, name="c").run(ctx)
    assert result.success
    assert result.output.startswith("failed: the learning store is unusable")
    assert mod.LEARN_PATHS.marker_failed.exists()
    assert not mod.LEARN_PATHS.pre_head.exists()


@pytest.mark.asyncio
async def test_collect_failure_becomes_a_marker_not_an_error(tmp_path) -> None:
    mod = _import_check_and_fix_ci(cwd=tmp_path)
    _isolated(mod, tmp_path)  # no start.sha
    result = await mod.CollectLearningEvidence(paths=mod.LEARN_PATHS, name="collect learning evidence").run(
        PipelineContext()
    )
    assert result.success
    assert result.output.startswith("failed: collect learning evidence:")
    assert mod.LEARN_PATHS.marker_failed.exists()


@pytest.mark.asyncio
async def test_publish_writes_under_the_journal_dir(tmp_path) -> None:
    mod = _import_check_and_fix_ci(cwd=tmp_path)
    _isolated(mod, tmp_path)
    paths = mod.LEARN_PATHS
    paths.scratch.mkdir(parents=True)
    paths.learning.write_text("---\ntitle: t\n---\n")
    paths.target.write_text("docs/solutions/best-practices/t.md\n")
    ctx = PipelineContext()
    ctx.journal = _fake_journal(tmp_path)

    result = await mod.PublishLearningProposal(paths=paths, name="publish learning proposal").run(ctx)

    assert result.output == f"proposed at {ctx.journal.dir / 'learning.md'}"
    assert (ctx.journal.dir / "learning.md").read_text() == "---\ntitle: t\n---\n"
    assert (ctx.journal.dir / "learning-target").read_text().strip() == "docs/solutions/best-practices/t.md"
    summary = await mod.LearningSummary(paths=paths, name="learning summary").run(ctx)
    assert summary.output.startswith(f"proposed at {ctx.journal.dir / 'learning.md'}")
    assert paths.outcome.exists()


@pytest.mark.asyncio
async def test_prepare_learning_scratch_empties_the_work_root(tmp_path) -> None:
    repo = _ready_repo(tmp_path)
    mod = _import_check_and_fix_ci(cwd=repo.path)
    work = _isolated(mod, tmp_path)
    (work / "stale.md").write_text("old\n")

    stage = _find_stage(mod.pipeline.items, "prepare learning scratch")
    stage.impl.work_root = work
    await stage.impl.run(PipelineContext())

    assert not (work / "stale.md").exists()
    assert (work / "start.sha").read_text().strip() == repo.repo.head.commit.hexsha


def test_work_root_is_outside_the_repo(tmp_path) -> None:
    import tempfile

    mod = _import_check_and_fix_ci(cwd=tmp_path)
    assert Path(tempfile.gettempdir()) in Path(mod.WORK_ROOT).parents
    assert Path(mod.WORK_ROOT).parent.parent.name == "norn-learn"


# -- learn postcondition and assert nothing unpushed -------------------------


def _sh(cmd: str, cwd: str):
    return subprocess.run(["sh", "-c", cmd], cwd=cwd, capture_output=True, text=True)


def test_learn_postcondition_shell(tmp_path) -> None:
    repo = _ready_repo(tmp_path)
    mod = _import_check_and_fix_ci(cwd=repo.path)
    paths = mod.LEARN_PATHS
    paths.root.mkdir(parents=True, exist_ok=True)
    try:
        paths.pre_head.write_text(repo.repo.head.commit.hexsha + "\n")
        cmd = _find_stage(mod.pipeline.items, "learn postcondition").impl.cmd

        assert _sh(cmd, repo.path).returncode == 0

        (Path(repo.path) / "dirty.txt").write_text("x\n")
        dirty = _sh(cmd, repo.path)
        assert dirty.returncode == 1 and "changed the working tree" in dirty.stderr
        (Path(repo.path) / "dirty.txt").unlink()

        (Path(repo.path) / "new.txt").write_text("x\n")
        repo.repo.index.add(["new.txt"])
        repo.repo.index.commit("moved")
        moved = _sh(cmd, repo.path)
        assert moved.returncode == 1 and "HEAD moved" in moved.stderr
    finally:
        shutil.rmtree(paths.root, ignore_errors=True)


def test_assert_nothing_unpushed_shell(tmp_path) -> None:
    repo = _ready_repo(tmp_path)
    mod = _import_check_and_fix_ci(cwd=repo.path)
    cmd = _find_stage(mod.pipeline.items, "assert nothing unpushed").impl.cmd

    assert _sh(cmd, repo.path).returncode == 0

    (Path(repo.path) / "a.txt").write_text("x\n")
    dirty = _sh(cmd, repo.path)
    assert dirty.returncode == 1 and "not clean" in dirty.stderr

    repo.repo.index.add(["a.txt"])
    repo.repo.index.commit("local only")
    unpushed = _sh(cmd, repo.path)
    assert unpushed.returncode == 1 and "1 unpushed commit" in unpushed.stderr

    repo.repo.git.push()
    assert _sh(cmd, repo.path).returncode == 0


def test_assert_nothing_unpushed_without_upstream(tmp_path) -> None:
    repo = make_repo_with_remote(tmp_path)
    repo.repo.git.checkout("-b", "no-upstream")
    mod = _import_check_and_fix_ci(cwd=repo.path)
    cmd = _find_stage(mod.pipeline.items, "assert nothing unpushed").impl.cmd

    result = _sh(cmd, repo.path)
    assert result.returncode == 1
    assert "no upstream" in result.stderr

"""Tests for the fix_jira_issue_v3 code-review builder (stages 35-51) and ResolveTestCmd."""
from __future__ import annotations

import json
import re
import shlex
import subprocess
from pathlib import Path

import pytest

from norn.checkpoint import load_checkpoint
from norn.dsl import ClearContext, Loop, OnFailure, Pipeline, Stage
from norn.model_labels import resolve_label
from norn.models import PipelineContext, StageResult
from norn.pipelines import _code_review_v3
from norn.pipelines._code_review_v3 import (
    BASE_PLACEHOLDER,
    COMPRESSED_LOG_PLACEHOLDER,
    CodexCodeReview,
    CommitReviewFixes,
    RecordReviewBase,
    ResolveTestCmd,
    ReviewFileGate,
    ReviewTreeGuard,
    RunReviewTests,
    WorktreeClean,
    WriteReviewFollowups,
    add_code_review_stages,
    add_review_fix_stages,
    claude_review_prompt,
    codex_review_cmd,
    codex_review_prompt,
    fix_prompt,
    read_review_range,
    render_test_script,
    triage_prompt,
)
from norn.pipelines._comment_style import COMMENT_RULE
from norn.pipelines._decisions_v3 import (
    AutoDecideCodeReview,
    CodeReviewGate,
    FixesRecordedGate,
    parse_code_review,
)
from norn.pipelines._fix_jira_v3 import V3Paths
from norn.runner import PipelineError, run_pipeline
from norn.stages.compress_test_log import CompressTestLog
from norn.stages.generate import Generate
from norn.stages.run_command import RunCommand

FIXTURES = Path(__file__).parent / "fixtures" / "fix_jira_v3"

KEY = "CBS-1"


def _ctx() -> PipelineContext:
    return PipelineContext()


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@x", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def paths(tmp_path: Path) -> V3Paths:
    return V3Paths(str(tmp_path / "art"), KEY)


# ---------------------------------------------------------------------------
# ResolveTestCmd
# ---------------------------------------------------------------------------


def _write_steps(root: Path, *, index: str | None = "", steps: dict[str, str]) -> Path:
    """Write ``index.md`` (front matter *index*; None: no file) and step files."""
    steps_dir = root / "steps"
    steps_dir.mkdir()
    if index is not None:
        fm = f"---\n{index}---\n" if index else ""
        (steps_dir / "index.md").write_text(f"{fm}# Shared context\n")
    for stem, fm in steps.items():
        head = f"---\n{fm}---\n" if fm else ""
        (steps_dir / f"{stem}.md").write_text(f"{head}# {stem}\n")
    return steps_dir


async def _resolve(steps_dir: Path, paths: V3Paths, arg: str | None = None) -> StageResult:
    return await ResolveTestCmd(steps_dir=str(steps_dir), test_cmd_arg=arg, paths=paths).run(_ctx())


def _script(paths: V3Paths) -> str:
    return Path(paths.test_cmd_sh).read_text()


def _timeout(paths: V3Paths) -> float:
    return float(Path(paths.test_timeout).read_text())


@pytest.mark.asyncio
async def test_arg_beats_final_test_cmd(tmp_path, paths):
    steps = _write_steps(
        tmp_path,
        index="final_test_cmd: uv run python -m pytest tests/ -q\n",
        steps={"step-01-a": "test_cmd: uv run python -m pytest tests/test_a.py -q\n"},
    )
    result = await _resolve(steps, paths, arg="make check")
    assert result.success, result.error
    assert _script(paths) == "set -e\nmake check\n"


@pytest.mark.asyncio
async def test_final_test_cmd_beats_the_aggregate(tmp_path, paths):
    steps = _write_steps(
        tmp_path,
        index="final_test_cmd: uv run python -m pytest tests/ -q\ntest_timeout: 900\n",
        steps={"step-01-a": "test_cmd: uv run python -m pytest tests/test_a.py -q\n"},
    )
    result = await _resolve(steps, paths)
    assert result.success, result.error
    assert _script(paths) == "set -e\nuv run python -m pytest tests/ -q\n"
    assert "===" not in _script(paths)
    assert _timeout(paths) == 900


@pytest.mark.asyncio
async def test_aggregate_holds_every_step_command_and_the_max_timeout(tmp_path, paths):
    steps = _write_steps(
        tmp_path,
        steps={
            "step-01-alpha": "test_cmd: uv run python -m pytest tests/test_a.py -q\ntest_timeout: 600\n",
            "step-02-beta": "test_cmd: uv run python -m pytest tests/test_b.py -q\ntest_timeout: 2400\n",
            "step-03-gamma": "test_cmd: uv run python -m pytest tests/test_a.py -q\n",
        },
    )
    result = await _resolve(steps, paths)
    assert result.success, result.error
    assert _script(paths) == (
        "set -e\n"
        'echo "=== step-01-alpha ===" && uv run python -m pytest tests/test_a.py -q && '
        'echo "=== step-02-beta ===" && uv run python -m pytest tests/test_b.py -q\n'
    )
    assert _timeout(paths) == 2400


@pytest.mark.asyncio
async def test_aggregate_timeout_has_a_floor(tmp_path, paths):
    steps = _write_steps(
        tmp_path,
        steps={"step-01-a": "test_cmd: uv run python -m pytest tests/test_a.py -q\ntest_timeout: 60\n"},
    )
    assert (await _resolve(steps, paths)).success
    assert _timeout(paths) == _code_review_v3.DEFAULT_TEST_TIMEOUT == 1800


@pytest.mark.asyncio
async def test_step_without_test_cmd_uses_the_index_one_and_bats_is_paired(tmp_path, paths):
    steps = _write_steps(
        tmp_path,
        index="test_cmd: uv run python -m pytest tests/ -q\n",
        steps={
            "step-01-a": "",
            "step-02-b": "bats_cmd: bats bats/x.bats\nbats_timeout: 3000\n",
        },
    )
    assert (await _resolve(steps, paths)).success
    assert _script(paths) == (
        "set -e\n"
        'echo "=== step-01-a ===" && uv run python -m pytest tests/ -q && '
        'echo "=== step-02-b ===" && echo "=== test_cmd ===" && uv run python -m pytest tests/ -q'
        ' && echo "=== bats_cmd ===" && bats bats/x.bats\n'
    )
    assert _timeout(paths) == 3000


@pytest.mark.asyncio
async def test_no_index_md_still_uses_the_step_commands(tmp_path, paths):
    steps = _write_steps(
        tmp_path,
        index=None,
        steps={"step-01-a": "test_cmd: uv run python -m pytest tests/test_a.py -q\n"},
    )
    result = await _resolve(steps, paths)
    assert result.success, result.error
    assert _script(paths) == (
        'set -e\necho "=== step-01-a ===" && uv run python -m pytest tests/test_a.py -q\n'
    )


@pytest.mark.asyncio
async def test_nothing_resolves_fails_naming_index_and_steps_dir(tmp_path, paths):
    steps = _write_steps(tmp_path, index=None, steps={})
    result = await _resolve(steps, paths)
    assert not result.success
    assert str(steps / "index.md") in result.error
    assert str(steps) in result.error
    assert not Path(paths.test_cmd_sh).exists()


@pytest.mark.asyncio
async def test_a_step_with_no_command_anywhere_fails(tmp_path, paths):
    steps = _write_steps(
        tmp_path,
        steps={"step-01-a": "test_cmd: uv run python -m pytest tests/test_a.py -q\n", "step-02-b": ""},
    )
    result = await _resolve(steps, paths)
    assert not result.success
    assert "step-02-b" in result.error
    assert "index.md" in result.error


def test_blank_arg_is_refused(paths):
    with pytest.raises(ValueError, match="test_cmd"):
        ResolveTestCmd(steps_dir="/x", test_cmd_arg="  ", paths=paths)


@pytest.mark.asyncio
async def test_set_e_header_fails_a_plain_multiline_command_on_its_first_line(tmp_path, paths):
    steps = _write_steps(
        tmp_path,
        index="final_test_cmd: |\n  false\n  echo second line passes\n",
        steps={},
    )
    assert (await _resolve(steps, paths)).success
    script = Path(paths.test_cmd_sh)
    assert script.read_text().splitlines()[0] == "set -e"
    assert subprocess.run(["sh", str(script)], capture_output=True).returncode != 0
    # Without the header the same body passes on its last line.
    body = "\n".join(script.read_text().splitlines()[1:])
    assert subprocess.run(["sh", "-c", body], capture_output=True).returncode == 0


# Synthetic copy of the CBS-2655 final_test_cmd shape (A17): `sh -ex -c` around
# STEP echoes, as a YAML block scalar.
_SH_EX_BLOCK = (
    "final_test_cmd: |\n"
    "  sh -ex -c '\n"
    '    echo "=== STEP 1: unit suite ==="\n'
    "    false\n"
    '    echo "=== STEP 2: e2e ==="\n'
    "    true\n"
    "  '\n"
    "test_timeout: 1800\n"
)
_SH_EX_CMD = (
    "sh -ex -c '\n"
    '  echo "=== STEP 1: unit suite ==="\n'
    "  false\n"
    '  echo "=== STEP 2: e2e ==="\n'
    "  true\n"
    "'"
)


@pytest.mark.asyncio
async def test_sh_ex_shape_is_preserved_byte_for_byte(tmp_path, paths):
    steps = _write_steps(tmp_path, index=_SH_EX_BLOCK, steps={})
    result = await _resolve(steps, paths)
    assert result.success, result.error
    assert Path(paths.test_cmd_sh).read_bytes() == f"set -e\n{_SH_EX_CMD}\n".encode()
    assert _timeout(paths) == 1800
    proc = subprocess.run(["sh", paths.test_cmd_sh], capture_output=True, text=True)
    assert proc.returncode != 0
    assert "STEP 2" not in proc.stdout


# ---------------------------------------------------------------------------
# Real git: RecordReviewBase and ReviewTreeGuard
# ---------------------------------------------------------------------------


class _Repo:
    def __init__(self, root: Path, paths: V3Paths) -> None:
        self.root = root
        self.paths = paths
        self.base_file = str(Path(paths.art_dir) / "base.sha")


@pytest.fixture
def repo(tmp_path: Path, paths: V3Paths) -> _Repo:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "a.txt").write_text("one\n")
    (root / "b.txt").write_text("bee\n")
    _git(root, "add", "a.txt", "b.txt")
    _git(root, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    (root / "b.txt").write_text("bee two\n")
    _git(root, "commit", "-q", "-am", "work")
    # Already dirty before the review: a modified tracked file and an untracked dir.
    (root / "a.txt").write_text("one\nmodified\n")
    (root / "out").mkdir()
    (root / "out" / "artefact.txt").write_text("x\n")
    Path(paths.art_dir).mkdir(parents=True)
    r = _Repo(root, paths)
    Path(r.base_file).write_text(base + "\n")
    return r


async def _record(repo: _Repo) -> StageResult:
    return await RecordReviewBase(
        paths=repo.paths, project_dir=str(repo.root), base_file=repo.base_file
    ).run(_ctx())


async def _guard(repo: _Repo) -> StageResult:
    return await ReviewTreeGuard(paths=repo.paths, project_dir=str(repo.root)).run(_ctx())


@pytest.mark.asyncio
async def test_record_review_base_clears_stale_outputs_and_snapshots(repo):
    p = repo.paths
    stale = [p.code_review, p.review_codex, p.review_claude, p.code_review_problems, p.review_fixes]
    Path(p.work).mkdir(parents=True)
    for path in stale:
        Path(path).write_text("stale\n")
    Path(p.fix_dir).mkdir()
    Path(p.fix_pre).write_text("{}")

    result = await _record(repo)
    assert result.success, result.error

    base = Path(repo.base_file).read_text().strip()
    head = _git(repo.root, "rev-parse", "HEAD")
    assert result.output == base
    for path in stale:
        assert not Path(path).exists(), path
    assert not Path(p.fix_dir).exists()
    assert Path(p.review_range).read_text() == f"base {base}\nhead {head}\n"
    assert read_review_range(p.review_range) == {"base": base, "head": head}
    snapshot = json.loads(Path(p.review_pre).read_text())
    assert set(snapshot) == {"a.txt", "out/artefact.txt"}


@pytest.mark.asyncio
async def test_record_review_base_fails_without_base_file(repo):
    Path(repo.base_file).unlink()
    result = await _record(repo)
    assert not result.success
    assert "base.sha" in result.error.splitlines()[0]


@pytest.mark.asyncio
async def test_tree_guard_passes_on_an_unchanged_tree(repo):
    assert (await _record(repo)).success
    result = await _guard(repo)
    assert result.success, result.error
    assert Path(repo.paths.review_post).exists()


@pytest.mark.asyncio
async def test_tree_guard_fails_when_a_modified_file_is_edited_again(repo):
    assert (await _record(repo)).success
    (repo.root / "a.txt").write_text("one\nmodified again\n")
    result = await _guard(repo)
    assert not result.success
    assert "a.txt" in result.error
    assert "restore them" in result.error.lower()
    assert "--resume" in result.error.splitlines()[-1]


@pytest.mark.asyncio
async def test_tree_guard_fails_on_a_new_file_in_an_untracked_dir(repo):
    assert (await _record(repo)).success
    (repo.root / "out" / "new.txt").write_text("y\n")
    result = await _guard(repo)
    assert not result.success
    assert "out/new.txt" in result.error


@pytest.mark.asyncio
async def test_tree_guard_fails_on_a_new_commit(repo):
    assert (await _record(repo)).success
    head = _git(repo.root, "rev-parse", "HEAD")
    _git(repo.root, "commit", "-q", "--allow-empty", "-m", "reviewer commit")
    result = await _guard(repo)
    assert not result.success
    assert f"HEAD moved from {head[:7]}" in result.error


# ---------------------------------------------------------------------------
# ReviewFileGate
# ---------------------------------------------------------------------------

_FINDING = (
    "## R1. Mapper reset races with getInstance [major]\n"
    "Where: src/main/java/x/MapperSingleton.java:74\n"
    "Problem: resetMapper swaps the instance without a lock.\n"
    "Fix: take the same lock in reset and getInstance.\n"
    "Confidence: 85\n"
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text, needle",
    [
        ("", "empty"),
        ("   \n\n", "empty"),
        (
            "I could not complete the review because the workspace is mounted read-only.\n",
            "No findings.",
        ),
        ("## R1 no tag\nWhere: a:1\nProblem: p\nFix: f\nConfidence: 90\n", "malformed"),
        (_FINDING.replace("Confidence: 85\n", ""), "Confidence"),
        (_FINDING.replace("Where:", "**Where:**"), "Where"),
    ],
)
async def test_review_file_gate_fails(tmp_path, text, needle):
    p = tmp_path / "review.md"
    p.write_text(text)
    result = await ReviewFileGate(path=str(p)).run(_ctx())
    assert not result.success
    assert needle in result.error


@pytest.mark.asyncio
async def test_review_file_gate_fails_on_a_missing_file(tmp_path):
    result = await ReviewFileGate(path=str(tmp_path / "nope.md")).run(_ctx())
    assert not result.success
    assert "not written" in result.error


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["No findings.\n", _FINDING, "# Review\n\n" + _FINDING + "\n" + _FINDING.replace("R1", "R2")])
async def test_review_file_gate_passes(tmp_path, text):
    p = tmp_path / "review.md"
    p.write_text(text)
    result = await ReviewFileGate(path=str(p)).run(_ctx())
    assert result.success, result.error


# ---------------------------------------------------------------------------
# Prompts and the codex command
# ---------------------------------------------------------------------------

_ANGLES = [
    "AGENTS.md",
    "CLAUDE.md",
    "Bugs in the changed lines",
    "Git history and blame",
    "Comments in the touched files",
    "Test runnability and isolation",
    "JUnit 5",
    "shared static state",
    "order dependence",
    "concurrency in test-only helpers",
    "time and precision",
]


def _prompts(paths: V3Paths) -> dict[str, str]:
    return {
        "codex": codex_review_prompt(repo="/repo", base="abc1234", out=paths.review_codex),
        "claude": claude_review_prompt(repo="/repo", base="abc1234", out=paths.review_claude),
    }


def test_codex_command_starts_with_rm_and_is_read_only(paths):
    cmd = codex_review_cmd(repo="/repo", out=paths.review_codex, base="abc1234", codex_label="review")
    resolved = resolve_label("codex", "review")
    assert cmd.startswith(f"rm -f {paths.review_codex} && codex exec ")
    assert "--sandbox read-only --skip-git-repo-check" in cmd
    assert f"-m {resolved.model} " in cmd
    assert f"-c 'model_reasoning_effort=\"{resolved.effort}\"'" in cmd
    assert f"-C /repo -o {paths.review_codex} " in cmd
    assert "git diff abc1234..HEAD" in cmd


@pytest.mark.parametrize("which", ["codex", "claude"])
def test_both_prompts_name_the_five_angles_and_the_rules(paths, which):
    prompt = _prompts(paths)[which]
    for angle in _ANGLES:
        assert angle in prompt, angle
    assert "do not run builds or tests" in prompt.lower()
    assert "review `git diff abc1234..HEAD`" in prompt
    out = paths.review_codex if which == "codex" else paths.review_claude
    assert f"write only {out}".lower() in prompt.lower()
    assert "## R<n>. <title> [critical|major|minor|nit]" in prompt
    for field in ("Where:", "Problem:", "Fix:", "Confidence:"):
        assert field in prompt
    assert "Confidence 80 or higher" in prompt
    assert "`No findings.`" in prompt


def test_codex_prompt_does_not_ask_codex_to_write(paths):
    prompt = _prompts(paths)["codex"]
    assert "final message" in prompt
    assert "Do not create or edit any file" in prompt


def test_claude_prompt_asks_for_parallel_subagents(paths):
    assert "parallel subagents, one per angle" in _prompts(paths)["claude"]


def test_triage_prompt_rules(paths):
    prompt = triage_prompt(paths=paths, steps_dir="/steps", key=KEY)
    assert f"# Code review — {KEY} (<base7>..<head7>)" in prompt
    assert "STATUS: PROPOSED" in prompt
    assert "Only nits may go under `## Omitted findings`" in prompt
    assert "There is no cap on them" in prompt
    assert "A non-nit must never be omitted" in prompt
    assert "fewer than 15 sections" in prompt
    assert "## Dropped as false positives" in prompt
    assert "STATUS: DECIDED" in prompt and "`No findings.`" in prompt
    assert paths.code_review_problems in prompt
    assert "fix exactly the problems listed there" in prompt
    for source in (paths.review_codex, paths.review_claude, "/steps/followups.md", "/steps/review.md"):
        assert source in prompt
    assert "## Unapplied review findings" in prompt
    assert "## Non-blocking" in prompt
    assert prompt.rstrip().endswith(COMMENT_RULE)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def _build(paths: V3Paths, *, auto: bool = False) -> Pipeline:
    return add_code_review_stages(
        Pipeline("test-code-review-v3"),
        paths=paths,
        project_dir="/repo",
        steps_dir="/steps",
        key=KEY,
        base_file=str(Path(paths.art_dir) / "base.sha"),
        codex_label="review",
        review_model="deep",
        triage_model="standard",
        fix_model="deep",
        fix_retries=3,
        auto=auto,
    )


def _item_names(pipeline: Pipeline) -> list[str]:
    names = []
    for item in pipeline.items:
        if isinstance(item, (Stage, Loop)):
            names.append(item.name)
        elif isinstance(item, ClearContext):
            names.append("ClearContext")
    return names


def _loop(pipeline: Pipeline, name: str) -> Loop:
    return next(i for i in pipeline.items if isinstance(i, Loop) and i.name == name)


def _stage(pipeline: Pipeline, name: str) -> Stage:
    return next(i for i in pipeline.items if isinstance(i, Stage) and i.name == name)


def _all_stages(pipeline: Pipeline) -> list[Stage]:
    out: list[Stage] = []
    for item in pipeline.items:
        out += item.stages if isinstance(item, Loop) else [item] if isinstance(item, Stage) else []
    return out


_HEAD = [
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
]
_TAIL = [
    "code review decided",
    "code review dispositions recorded",
    "fix code review",
    "review fixes green",
    "fixes recorded",
    "commit review fixes",
    "worktree clean",
    "write review followups",
]


def test_interactive_order(paths):
    assert _item_names(_build(paths)) == _HEAD + _TAIL


def test_auto_order(paths):
    assert _item_names(_build(paths, auto=True)) == _HEAD + ["auto decide code review"] + _TAIL


_FIX_BODY = [
    "snapshot before fix",
    "compress review tests",
    "fix review findings",
    "snapshot after fix",
    "run review tests",
]


def test_loop_bodies_and_policies(paths):
    p = _build(paths)
    bodies = {i.name: [s.name for s in i.stages] for i in p.items if isinstance(i, Loop)}
    assert bodies == {
        "codex code review": ["run codex code review", "check codex code review"],
        "claude code review": ["run claude code review", "check claude code review"],
        "triage code review": ["triage findings", "check code review"],
        "fix code review": _FIX_BODY,
    }
    for name in ("codex code review", "claude code review", "triage code review"):
        loop = _loop(p, name)
        assert loop.max_retries == 2
        assert loop.on_exhaust is OnFailure.ASK_USER
        assert loop.new_session is False
    fix = _loop(p, "fix code review")
    assert fix.max_retries == 3  # fix_retries
    assert fix.on_exhaust is OnFailure.ASK_USER
    assert fix.new_session is True


def test_each_reviewer_loop_has_its_hard_twin(paths):
    p = _build(paths)
    names = _item_names(p)
    for loop_name, check, twin, review in (
        ("codex code review", "check codex code review", "codex code review written", paths.review_codex),
        ("claude code review", "check claude code review", "claude code review written", paths.review_claude),
    ):
        assert names.index(twin) == names.index(loop_name) + 1
        in_loop = next(s for s in _loop(p, loop_name).stages if s.name == check)
        hard = _stage(p, twin)
        assert isinstance(in_loop.impl, ReviewFileGate) and in_loop.impl.path == review
        assert isinstance(hard.impl, ReviewFileGate) and hard.impl.path == review
        assert hard.on_failure is OnFailure.FAIL


def test_gate_three_policies(paths):
    p = _build(paths, auto=True)
    shape = _stage(p, "code review shape ok")
    auto = _stage(p, "auto decide code review")
    decided = _stage(p, "code review decided")
    recorded = _stage(p, "code review dispositions recorded")
    assert isinstance(shape.impl, CodeReviewGate) and not shape.impl.require_decided
    assert shape.on_failure is OnFailure.FAIL
    assert isinstance(auto.impl, AutoDecideCodeReview) and auto.on_failure is OnFailure.FAIL
    assert isinstance(decided.impl, CodeReviewGate) and decided.impl.require_decided
    assert decided.on_failure is OnFailure.ASK_USER and not decided.always_run
    assert isinstance(recorded.impl, CodeReviewGate) and recorded.impl.require_decided
    assert recorded.on_failure is OnFailure.FAIL and recorded.always_run is True
    check = next(s for s in _loop(p, "triage code review").stages if s.name == "check code review")
    assert check.impl.problems_file == paths.code_review_problems
    for stage in (shape, auto, decided, recorded):
        assert stage.impl.path == paths.code_review
    for name in ("record review base", "review left tree unchanged"):
        assert _stage(p, name).on_failure is OnFailure.FAIL
        assert not _stage(p, name).always_run


def test_reviewer_and_triage_generate_settings(paths):
    p = _build(paths)
    env = {
        "CLAUDE_CODE_SUBAGENT_MODEL": resolve_label("claude-code", "standard").model,
        "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1",
    }
    review = next(s for s in _loop(p, "claude code review").stages if s.name == "run claude code review").impl
    assert isinstance(review, Generate)
    assert review.model == "deep"
    assert review.allowed_tools == ["Read", "Grep", "Glob", "Bash", "Write"]
    assert review.permission_mode == "acceptEdits"
    assert review.setting_sources == ["project"]
    assert review.max_turns == 80
    assert review.env == env
    triage = next(s for s in _loop(p, "triage code review").stages if s.name == "triage findings").impl
    assert isinstance(triage, Generate)
    assert triage.model == "standard"
    assert "Bash" not in triage.allowed_tools
    assert triage.env == env
    gens = [s.name for s in _all_stages(p) if isinstance(s.impl, Generate)]
    assert gens == ["run claude code review", "triage findings", "fix review findings"]


def test_codex_stage_is_a_run_command_with_the_codex_timeout(paths):
    impl = next(s for s in _loop(_build(paths), "codex code review").stages if s.name == "run codex code review").impl
    assert isinstance(impl, RunCommand)
    assert impl.timeout == 1800
    assert impl.cmd.startswith(f"rm -f {paths.review_codex} && ")


@pytest.mark.asyncio
async def test_codex_stage_reads_the_base_when_it_runs(paths, monkeypatch):
    seen: list[str] = []

    async def fake_run(self, ctx, **kwargs):
        seen.append(self.cmd)
        return StageResult(name="", success=True)

    monkeypatch.setattr(RunCommand, "run", fake_run)
    stage = CodexCodeReview(paths=paths, repo="/repo", codex_label="review")
    assert not (await stage.run(_ctx())).success  # no review-range.txt yet
    Path(paths.work).mkdir(parents=True)
    Path(paths.review_range).write_text("base abc1234\nhead def5678\n")
    assert (await stage.run(_ctx())).success
    assert seen and "git diff abc1234..HEAD" in seen[0]
    assert seen[0].startswith("rm -f ")


def test_claude_prompt_placeholder_resolves_to_the_recorded_base(paths):
    review = next(
        s for s in _loop(_build(paths), "claude code review").stages if s.name == "run claude code review"
    ).impl
    ctx = _ctx()
    ctx.results["record review base"] = StageResult(name="record review base", success=True, output="abc1234")
    resolved = review._resolve(review.prompt, ctx)
    assert "review `git diff abc1234..HEAD`" in resolved
    assert "{" not in resolved and "}" not in resolved


def test_no_prompt_has_stray_braces(paths):
    for stage in _all_stages(_build(paths)):
        if isinstance(stage.impl, Generate):
            prompt = stage.impl.prompt.replace(BASE_PLACEHOLDER, "").replace(
                COMPRESSED_LOG_PLACEHOLDER, ""
            )
            assert "{" not in prompt and "}" not in prompt, stage.name


def test_no_prompt_names_a_model(paths):
    p = _build(paths)
    texts = [s.impl.prompt for s in _all_stages(p) if isinstance(s.impl, Generate)]
    texts += [codex_review_prompt(repo="/repo", base="abc1234", out=paths.review_codex)]
    texts += list(_prompts(paths).values())
    texts += [triage_prompt(paths=paths, steps_dir="/steps", key=KEY)]
    for text in texts:
        for family in ('"haiku"', '"sonnet"', '"opus"'):
            assert family not in text
        assert not re.search(r"claude-[a-z]+-[0-9]", text)


def test_triage_when_is_computed_from_files(paths):
    when = next(s for s in _loop(_build(paths), "triage code review").stages if s.name == "triage findings").when
    ctx = _ctx()  # empty results: a resumed process
    assert when(ctx) is True  # no gate-3 file
    Path(paths.work).mkdir(parents=True)
    Path(paths.code_review).write_text(
        f"# Code review — {KEY} (aaaaaaa..bbbbbbb)\n\nSTATUS: DECIDED\n\nNo findings.\n"
    )
    assert when(ctx) is False
    Path(paths.code_review_problems).write_text("no STATUS: line\n")
    assert when(ctx) is True


_HAS_FIX_STAGES = set(_FIX_BODY) | {"fixes recorded", "commit review fixes"}


def test_only_triage_findings_and_the_fix_stages_carry_a_when(paths):
    for stage in _all_stages(_build(paths, auto=True)):
        if stage.name != "triage findings" and stage.name not in _HAS_FIX_STAGES:
            assert stage.when is None, stage.name


def test_part_two_adder_is_called(paths, monkeypatch):
    calls: list[dict] = []

    def fake(pipeline, **kw):
        calls.append(kw)
        return pipeline

    monkeypatch.setattr(_code_review_v3, "add_review_fix_stages", fake)
    p = _build(paths, auto=True)
    assert len(calls) == 1
    assert calls[0]["fix_model"] == "deep" and calls[0]["fix_retries"] == 3 and calls[0]["auto"] is True
    assert isinstance(p, Pipeline)


# ---------------------------------------------------------------------------
# Part 2: builder (stages 46-51)
# ---------------------------------------------------------------------------

_ONE_FIX = (
    f"# Code review — {KEY} (aaaaaaa..bbbbbbb)\n\n"
    "STATUS: DECIDED\n\n"
    "## C1. Reset races with get [major]\n"
    "Where: src/x/Registry.java:41\n"
    "Problem: reset swaps the instance without a lock.\n"
    "Fix: take the same lock.\n"
    "Source: claude\n"
    "Disposition: fix\n"
)


def test_every_fix_stage_shares_one_has_fix_when(paths):
    p = _build(paths, auto=True)
    whens = {s.when for s in _loop(p, "fix code review").stages}
    assert len(whens) == 1 and None not in whens
    when = whens.pop()
    for name in ("fixes recorded", "commit review fixes"):
        assert _stage(p, name).when is when
    for name in ("review fixes green", "worktree clean", "write review followups"):
        assert _stage(p, name).when is None

    ctx = _ctx()  # empty results: a resumed process
    assert when(ctx) is False  # no gate-3 file
    Path(paths.work).mkdir(parents=True)
    Path(paths.code_review).write_text(_ONE_FIX.replace("Disposition: fix", "Disposition: skip"))
    assert when(ctx) is False
    Path(paths.code_review).write_text(_ONE_FIX)
    assert when(ctx) is True


@pytest.mark.parametrize("auto", [False, True])
def test_fix_stage_policies(paths, auto):
    p = _build(paths, auto=auto)
    body = {s.name: s for s in _loop(p, "fix code review").stages}
    run_tests = body["run review tests"].impl
    assert isinstance(run_tests, RunReviewTests) and run_tests.reuse_proof is False
    compress = body["compress review tests"].impl
    assert isinstance(compress, CompressTestLog) and compress.source_stage == "run review tests"
    before = body["snapshot before fix"].impl
    after = body["snapshot after fix"].impl
    assert isinstance(before, RunCommand) and isinstance(after, RunCommand)
    helper = _code_review_v3.SNAPSHOT_HELPER
    assert f"python3 {helper} snapshot --root /repo {paths.fix_pre}" in before.cmd
    assert f"python3 {helper} snapshot --root /repo {paths.fix_post}" in after.cmd
    assert f"diff {paths.fix_pre} {paths.fix_post} --root /repo >> {paths.owned}" in after.cmd

    green = _stage(p, "review fixes green")
    assert isinstance(green.impl, RunReviewTests) and green.impl.reuse_proof is True
    recorded = _stage(p, "fixes recorded")
    assert isinstance(recorded.impl, FixesRecordedGate)
    assert (recorded.impl.review, recorded.impl.fixes, recorded.impl.owned) == (
        paths.code_review,
        paths.review_fixes,
        paths.owned,
    )
    assert recorded.impl.auto is auto
    commit = _stage(p, "commit review fixes")
    assert isinstance(commit.impl, CommitReviewFixes) and commit.impl.key == KEY
    clean = _stage(p, "worktree clean")
    assert isinstance(clean.impl, WorktreeClean) and clean.on_failure is OnFailure.ASK_USER
    followups = _stage(p, "write review followups")
    assert isinstance(followups.impl, WriteReviewFollowups)
    for stage in (green, recorded, commit, followups):
        assert stage.on_failure is OnFailure.FAIL, stage.name
    for stage in (green, recorded, commit, clean, followups):
        assert not stage.always_run, stage.name


def test_fix_agent_settings_and_prompt(paths):
    fix = next(
        s for s in _loop(_build(paths), "fix code review").stages if s.name == "fix review findings"
    ).impl
    assert isinstance(fix, Generate)
    assert fix.model == "deep"
    assert fix.allowed_tools == ["Read", "Write", "Edit", "Glob", "Grep"]
    assert fix.permission_mode == "acceptEdits"
    assert fix.setting_sources == ["project"]
    assert fix.env == {
        "CLAUDE_CODE_SUBAGENT_MODEL": resolve_label("claude-code", "standard").model,
        "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1",
    }
    prompt = fix.prompt
    assert prompt == fix_prompt(paths=paths, project_dir="/repo")
    assert "Fix only the findings whose `Disposition:` is `fix`" in prompt
    assert "Run no git command" in prompt
    assert f"the pipeline runs\n  {paths.test_cmd_sh}" in prompt
    assert COMPRESSED_LOG_PLACEHOLDER in prompt
    assert f"The full log of that run is at {paths.test_log}" in prompt
    assert f"rewrite {paths.review_fixes}" in prompt
    assert "- C<n>: fixed — <what you changed>" in prompt
    assert "- C<n>: not fixed — <why not>" in prompt
    assert prompt.rstrip().endswith(COMMENT_RULE)

    ctx = _ctx()
    ctx.results["compress review tests"] = StageResult(
        name="compress review tests", success=True, output="FAILED tests/test_x.py::test_reset"
    )
    resolved = fix._resolve(fix.prompt, ctx)
    assert "FAILED tests/test_x.py::test_reset" in resolved
    assert "{" not in resolved and "}" not in resolved


# ---------------------------------------------------------------------------
# Part 2: real git
# ---------------------------------------------------------------------------


class _FixRepo:
    def __init__(self, root: Path, paths: V3Paths, hooks: Path, counter: Path) -> None:
        self.root = root
        self.paths = paths
        self.hooks = hooks
        self.counter = counter

    def test_cmd(self, body: str) -> None:
        Path(self.paths.test_cmd_sh).write_text(render_test_script(body))

    def count_cmd(self) -> str:
        """A command that records one line in the counter file per run."""
        return f"echo run >> {shlex.quote(str(self.counter))}"

    def runs(self) -> int:
        return len(self.counter.read_text().splitlines()) if self.counter.exists() else 0

    def owned(self) -> list[str]:
        return [p for p in Path(self.paths.owned).read_bytes().decode().split("\0") if p]

    def own(self, *names: str) -> None:
        Path(self.paths.owned).write_bytes(b"".join(n.encode() + b"\0" for n in names))

    def hook(self, name: str, body: str) -> None:
        hook = self.hooks / name
        hook.write_text("#!/bin/sh\n" + body)
        hook.chmod(0o755)

    def head(self) -> str:
        return _git(self.root, "rev-parse", "HEAD")

    def committed_files(self) -> list[str]:
        return sorted(_git(self.root, "show", "--name-only", "--format=", "HEAD").splitlines())


@pytest.fixture
def fix_repo(tmp_path: Path, paths: V3Paths) -> _FixRepo:
    root = tmp_path / "repo"
    root.mkdir()
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    _git(root, "init", "-q")
    # The stages run plain git, so the identity and hooks live in the repo config.
    for key, value in (
        ("user.name", "t"),
        ("user.email", "t@x"),
        ("commit.gpgsign", "false"),
        ("core.hooksPath", str(hooks)),
    ):
        _git(root, "config", key, value)
    (root / "a.txt").write_text("one\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "base")
    Path(paths.work).mkdir(parents=True)
    Path(paths.code_review).write_text(_ONE_FIX)
    Path(paths.test_timeout).write_text("60\n")
    repo = _FixRepo(root, paths, hooks, tmp_path / "count")
    repo.test_cmd(repo.count_cmd())
    return repo


def _fix_pipeline(repo: _FixRepo, *, auto: bool = False) -> Pipeline:
    return add_review_fix_stages(
        Pipeline("test-fix-v3"),
        paths=repo.paths,
        project_dir=str(repo.root),
        key=KEY,
        fix_model="deep",
        fix_retries=3,
        auto=auto,
    )


async def _run_items(pipeline: Pipeline, names: list[str]) -> PipelineContext:
    items = [i for i in pipeline.items if isinstance(i, (Stage, Loop)) and i.name in names]
    assert [i.name for i in items] == names
    return await run_pipeline(Pipeline("test-fix-v3", items=items))


def _fake_fix(monkeypatch, *actions) -> list[str]:
    """Mock the fix agent: call n runs ``actions[n - 1]``."""
    calls: list[str] = []

    async def fake_run(self, ctx, **kwargs):
        calls.append(self.prompt)
        actions[len(calls) - 1]()
        return StageResult(name="", success=True)

    monkeypatch.setattr(Generate, "run", fake_run)
    return calls


def _writes_new_java(repo: _FixRepo):
    def action() -> None:
        (repo.root / "new.java").write_text("class New {}\n")
        Path(repo.paths.review_fixes).write_text("- C1: fixed — added the lock in new.java\n")

    return action


# -- RunReviewTests ----------------------------------------------------------


@pytest.mark.asyncio
async def test_review_tests_read_the_timeout_when_they_run(fix_repo):
    stage = RunReviewTests(paths=fix_repo.paths, project_dir=str(fix_repo.root), reuse_proof=False)
    Path(fix_repo.paths.test_timeout).write_text("2\n")
    fix_repo.test_cmd("sleep 5")
    result = await stage.run(_ctx())
    assert not result.success
    assert result.output["returncode"] is None
    assert stage.timeout == 2
    assert result.error.splitlines()[0] == (
        f"review tests timed out after 2s — log: {fix_repo.paths.test_log}"
    )
    assert not Path(fix_repo.paths.test_proof).exists()


@pytest.mark.asyncio
async def test_proof_skips_a_proven_tree_and_reruns_on_any_change(fix_repo):
    repo = fix_repo
    (repo.root / "notes.txt").write_text("abc\n")  # untracked
    run = RunReviewTests(paths=repo.paths, project_dir=str(repo.root), reuse_proof=False)
    reuse = RunReviewTests(paths=repo.paths, project_dir=str(repo.root), reuse_proof=True)

    # No proof yet: the reusing stage runs the tests (A8).
    assert (await reuse.run(_ctx())).success
    assert repo.runs() == 1
    assert (await run.run(_ctx())).success
    assert repo.runs() == 2
    key, tree = Path(repo.paths.test_proof).read_text().splitlines()
    assert tree == _git(repo.root, "rev-parse", "HEAD^{tree}")

    result = await reuse.run(_ctx())
    assert result.success
    assert result.output == f"tests already passed on this tree ({key[:8]})"
    assert repo.runs() == 2

    (repo.root / "notes.txt").write_text("abd\n")  # one byte of an untracked file
    assert (await reuse.run(_ctx())).success
    assert repo.runs() == 3
    assert (await reuse.run(_ctx())).success
    assert repo.runs() == 3

    Path(repo.paths.test_cmd_sh).write_text(Path(repo.paths.test_cmd_sh).read_text() + "true\n")
    assert (await reuse.run(_ctx())).success
    assert repo.runs() == 4


@pytest.mark.asyncio
async def test_the_proof_covers_artefacts_the_run_creates(fix_repo):
    repo = fix_repo
    # A different artefact on every run: the snapshot is taken after the run.
    repo.test_cmd(f"{repo.count_cmd()} && mkdir -p out && echo $$ > out/stamp.txt")
    reuse = RunReviewTests(paths=repo.paths, project_dir=str(repo.root), reuse_proof=True)
    assert (await reuse.run(_ctx())).success
    assert (await reuse.run(_ctx())).success
    assert repo.runs() == 1


@pytest.mark.asyncio
async def test_failed_review_tests_report_the_log_tail(fix_repo):
    repo = fix_repo
    repo.test_cmd("for i in $(seq 1 50); do echo line $i; done; echo oops >&2; exit 3")
    result = await RunReviewTests(
        paths=repo.paths, project_dir=str(repo.root), reuse_proof=False
    ).run(_ctx())
    assert not result.success
    lines = result.error.splitlines()
    assert lines[0] == f"review tests failed (exit 3) — log: {repo.paths.test_log}"
    assert len(lines) == 1 + 40 + 1
    assert "line 12" not in lines and "line 13" in lines
    assert lines[-2] == "oops"
    assert "[r]etry" in lines[-1]
    log = Path(repo.paths.test_log).read_text()
    assert log.startswith("line 1\n") and log.endswith("--- stderr ---\noops\n")
    assert result.output["returncode"] == 3
    assert "line 1\n" in result.output["stdout"] and "line 50\n" in result.output["stdout"]
    assert not Path(repo.paths.test_proof).exists()


# -- The fix loop ------------------------------------------------------------


@pytest.mark.asyncio
async def test_fix_loop_is_not_paid_again_on_resume(fix_repo, tmp_path, monkeypatch):
    repo = fix_repo
    calls = _fake_fix(monkeypatch, _writes_new_java(repo))
    real_commit = CommitReviewFixes.run
    broken = {"on": True}

    async def flaky_commit(self, ctx, **kwargs):
        if broken["on"]:
            return StageResult(name="", success=False, error="commit broke")
        return await real_commit(self, ctx, **kwargs)

    monkeypatch.setattr(CommitReviewFixes, "run", flaky_commit)
    state = tmp_path / "state"
    state.mkdir()
    # Not a git repository: the runner registers no exclude lines anywhere.
    monkeypatch.chdir(state)
    config = str(state / "fix_loop.py")
    monkeypatch.setenv("NORN_STATE_KEY", config)
    pipeline = _fix_pipeline(repo)

    with pytest.raises(PipelineError):
        await run_pipeline(pipeline, config_path=config)
    assert len(calls) == 1 and repo.runs() == 1
    checkpoint = load_checkpoint(config)
    assert checkpoint is not None
    assert set(_FIX_BODY) <= set(checkpoint.completed_stages)
    assert "commit review fixes" not in checkpoint.completed_stages

    broken["on"] = False
    await run_pipeline(pipeline, config_path=config, resume_checkpoint=checkpoint)
    assert len(calls) == 1  # the fix agent ran once across both processes
    assert repo.runs() == 1
    assert _git(repo.root, "log", "-1", "--format=%s") == f"{KEY}: address code review findings"
    assert repo.committed_files() == ["new.java"]


@pytest.mark.asyncio
async def test_the_fix_owns_its_file_and_not_the_test_artefact(fix_repo, monkeypatch):
    repo = fix_repo
    _fake_fix(monkeypatch, _writes_new_java(repo))
    repo.test_cmd(f"{repo.count_cmd()} && mkdir -p out && echo x > out/artefact.txt")
    await _run_items(_fix_pipeline(repo), ["fix code review"])
    assert (repo.root / "out" / "artefact.txt").exists()
    assert repo.owned() == ["new.java"]


@pytest.mark.asyncio
async def test_a_file_created_then_deleted_is_dropped_from_the_commit(fix_repo, monkeypatch):
    repo = fix_repo

    def second() -> None:
        (repo.root / "F").unlink()

    def first() -> None:
        _writes_new_java(repo)()
        (repo.root / "F").write_text("temporary\n")

    calls = _fake_fix(monkeypatch, first, second)
    count = shlex.quote(str(repo.counter))
    repo.test_cmd(f"{repo.count_cmd()}\n[ $(wc -l < {count}) -ge 2 ]")  # fails on attempt 1
    ctx = await _run_items(
        _fix_pipeline(repo),
        ["fix code review", "review fixes green", "fixes recorded", "commit review fixes"],
    )
    assert len(calls) == 2
    assert repo.runs() == 2  # `review fixes green` reused the proof
    assert sorted(repo.owned()) == ["F", "new.java"]
    commit = ctx.results["commit review fixes"]
    assert commit.success, commit.error
    assert "dropped (gone and never tracked): F" in commit.output.splitlines()
    assert repo.committed_files() == ["new.java"]


@pytest.mark.asyncio
async def test_fixes_recorded_fails_a_fixed_claim_with_nothing_owned(fix_repo):
    stage = _stage(_fix_pipeline(fix_repo), "fixes recorded")
    Path(fix_repo.paths.review_fixes).write_text("- C1: fixed — added the lock\n")
    Path(fix_repo.paths.owned).write_bytes(b"")
    result = await stage.impl.run(_ctx())
    assert not result.success and "changed nothing" in result.error
    fix_repo.own("new.java")
    assert (await stage.impl.run(_ctx())).success


# -- CommitReviewFixes -------------------------------------------------------


async def _commit(repo: _FixRepo) -> StageResult:
    return await CommitReviewFixes(paths=repo.paths, project_dir=str(repo.root), key=KEY).run(_ctx())


@pytest.mark.asyncio
async def test_commit_takes_owned_files_and_leaves_the_rest(fix_repo):
    repo = fix_repo
    (repo.root / "new.java").write_text("class New {}\n")
    (repo.root / "a.txt").write_text("one\ntwo\n")
    (repo.root / "stray.txt").write_text("not owned\n")
    repo.own("new.java", "a.txt", "new.java")
    result = await _commit(repo)
    assert result.success, result.error
    assert result.output.splitlines()[0].startswith("committed ")
    assert _git(repo.root, "log", "-1", "--format=%B") == f"{KEY}: address code review findings"
    assert repo.committed_files() == ["a.txt", "new.java"]
    assert _git(repo.root, "status", "--porcelain") == "?? stray.txt"


@pytest.mark.asyncio
async def test_commit_with_only_dropped_paths_commits_nothing(fix_repo):
    repo = fix_repo
    head = repo.head()
    repo.own("gone.txt")
    result = await _commit(repo)
    assert result.success, result.error
    assert result.output.splitlines() == [
        "nothing to commit",
        "dropped (gone and never tracked): gone.txt",
    ]
    assert repo.head() == head


@pytest.mark.asyncio
async def test_commit_refuses_an_added_attribution_line(fix_repo):
    repo = fix_repo
    head = repo.head()
    (repo.root / "gen.py").write_text("x = 1\n# Generated with [Claude Code]\n")
    repo.own("gen.py")
    result = await _commit(repo)
    assert not result.success
    assert "# Generated with [Claude Code]" in result.error
    assert repo.head() == head


@pytest.mark.asyncio
async def test_commit_retries_once_after_an_auto_fixing_hook(fix_repo):
    repo = fix_repo
    repo.hook(
        "pre-commit",
        'marker="$(git rev-parse --git-dir)/hook-ran"\n'
        'if [ ! -f "$marker" ]; then\n'
        '  touch "$marker"\n'
        '  for f in $(git diff --cached --name-only); do printf "\\n" >> "$f"; done\n'
        "  exit 1\n"
        "fi\n",
    )
    (repo.root / "new.java").write_text("class New {}\n")
    repo.own("new.java")
    head = repo.head()
    result = await _commit(repo)
    assert result.success, result.error
    assert (repo.root / ".git" / "hook-ran").exists()
    assert _git(repo.root, "rev-parse", "HEAD~1") == head
    assert repo.committed_files() == ["new.java"]
    # The hook's rewrite went into the commit: nothing is left modified.
    assert _git(repo.root, "status", "--porcelain") == ""
    shown = subprocess.run(
        ["git", "show", "HEAD:new.java"], cwd=repo.root, capture_output=True, text=True, check=True
    ).stdout
    assert shown == "class New {}\n\n"


@pytest.mark.asyncio
async def test_commit_fails_when_a_hook_adds_attribution_to_the_message(fix_repo):
    repo = fix_repo
    repo.hook("prepare-commit-msg", 'echo "Co-Authored-By: X" >> "$1"\n')
    (repo.root / "new.java").write_text("class New {}\n")
    repo.own("new.java")
    result = await _commit(repo)
    assert not result.success
    assert "Co-Authored-By: X" in result.error
    assert result.error.splitlines()[-1].startswith("Run git commit --amend")


# -- WorktreeClean -----------------------------------------------------------


@pytest.mark.asyncio
async def test_worktree_clean_lists_leftovers_until_they_are_excluded(fix_repo):
    repo = fix_repo
    (repo.root / "out").mkdir()
    (repo.root / "out" / "artefact.txt").write_text("x\n")
    stage = WorktreeClean(project_dir=str(repo.root))
    result = await stage.run(_ctx())
    assert not result.success
    lines = result.error.splitlines()
    assert "  out/" in lines
    assert ".git/info/exclude" in lines[-1] and ".gitignore" in lines[-1]
    assert "[r]etry" in lines[-1] and "CI-fix child refuses a dirty tree" in lines[-1]

    exclude = repo.root / ".git" / "info" / "exclude"
    exclude.parent.mkdir(exist_ok=True)
    with exclude.open("a") as fh:
        fh.write("out/\n")
    assert (await stage.run(_ctx())).success


@pytest.mark.asyncio
async def test_worktree_clean_shows_five_paths_and_a_count(fix_repo):
    for n in range(7):
        (fix_repo.root / f"left{n}.txt").write_text("x\n")
    result = await WorktreeClean(project_dir=str(fix_repo.root)).run(_ctx())
    assert not result.success
    assert result.error.splitlines()[0].startswith("7 paths")
    assert "  … and 2 more" in result.error.splitlines()


# -- WriteReviewFollowups ----------------------------------------------------


@pytest.mark.asyncio
async def test_write_review_followups_from_a_decided_review(paths):
    fixture = (FIXTURES / "code_review_decided.md").read_text()
    review = parse_code_review(fixture)
    assert review.problems == [] and review.status == "DECIDED"
    assert len(review.omitted_nits) == 2
    Path(paths.work).mkdir(parents=True)
    Path(paths.code_review).write_text(fixture)
    Path(paths.review_fixes).write_text(
        "- C1: fixed — took the lock in reset\n- C2: not fixed — needs a design call\n"
    )
    result = await WriteReviewFollowups(paths=paths).run(_ctx())
    assert result.success, result.error
    assert Path(paths.review_followups).read_text() == (
        "- [ ] C2 Retry count is read twice per request "
        "(modules/core/src/main/java/x/Client.java:88)\n"
        "- [ ] C3 Timeout parsing duplicates the config loader "
        "(modules/core/src/main/java/x/Settings.java:12)\n"
    )
    assert Path(paths.review_nits).read_text() == (
        "- C5 Rename the loop variable (modules/core/src/main/java/x/Client.java:93)\n"
        "- Sort the imports in Client\n"
        "- Drop the unused constant in Settings\n"
    )


@pytest.mark.asyncio
async def test_write_review_followups_with_no_findings_writes_empty_files(paths):
    Path(paths.work).mkdir(parents=True)
    Path(paths.code_review).write_text(
        f"# Code review — {KEY} (aaaaaaa..bbbbbbb)\n\nSTATUS: DECIDED\n\nNo findings.\n"
    )
    assert (await WriteReviewFollowups(paths=paths).run(_ctx())).success
    assert Path(paths.review_followups).read_text() == ""
    assert Path(paths.review_nits).read_text() == ""


# ---------------------------------------------------------------------------
# Subject prefix (fix_issue_v1: the commit prefix differs from the file key)
# ---------------------------------------------------------------------------


def test_subject_prefix_reaches_the_fix_commit_and_not_the_triage_prompt(tmp_path):
    paths = V3Paths(str(tmp_path / "art"), "issue-123")
    p = add_code_review_stages(
        Pipeline("test-code-review-v3"),
        paths=paths,
        project_dir="/repo",
        steps_dir="/steps",
        key="issue-123",
        base_file=str(Path(paths.art_dir) / "base.sha"),
        codex_label="review",
        review_model="deep",
        triage_model="standard",
        fix_model="deep",
        fix_retries=3,
        auto=False,
        subject_prefix="#123",
    )
    commit = _stage(p, "commit review fixes").impl
    assert isinstance(commit, CommitReviewFixes)
    assert commit.key == "#123"
    assert commit.message == "#123: address code review findings\n"
    triage = next(
        s for s in _loop(p, "triage code review").stages if s.name == "triage findings"
    ).impl
    assert triage.prompt == triage_prompt(paths=paths, steps_dir="/steps", key="issue-123")


def test_fix_commit_uses_the_key_without_a_subject_prefix(paths):
    commit = _stage(_build(paths), "commit review fixes").impl
    assert commit.message == f"{KEY}: address code review findings\n"

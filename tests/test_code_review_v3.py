"""Tests for the fix_jira_issue_v3 code-review builder, part 1 (stages 35-45) and ResolveTestCmd."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from norn.dsl import ClearContext, Loop, OnFailure, Pipeline, Stage
from norn.model_labels import resolve_label
from norn.models import PipelineContext, StageResult
from norn.pipelines import _code_review_v3
from norn.pipelines._code_review_v3 import (
    BASE_PLACEHOLDER,
    CodexCodeReview,
    RecordReviewBase,
    ResolveTestCmd,
    ReviewFileGate,
    ReviewTreeGuard,
    add_code_review_stages,
    claude_review_prompt,
    codex_review_cmd,
    codex_review_prompt,
    read_review_range,
    triage_prompt,
)
from norn.pipelines._comment_style import COMMENT_RULE
from norn.pipelines._decisions_v3 import AutoDecideCodeReview, CodeReviewGate
from norn.pipelines._fix_jira_v3 import V3Paths
from norn.stages.generate import Generate
from norn.stages.run_command import RunCommand

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
_TAIL = ["code review decided", "code review dispositions recorded"]


def test_interactive_order(paths):
    assert _item_names(_build(paths)) == _HEAD + _TAIL


def test_auto_order(paths):
    assert _item_names(_build(paths, auto=True)) == _HEAD + ["auto decide code review"] + _TAIL


def test_loop_bodies_and_policies(paths):
    p = _build(paths)
    bodies = {i.name: [s.name for s in i.stages] for i in p.items if isinstance(i, Loop)}
    assert bodies == {
        "codex code review": ["run codex code review", "check codex code review"],
        "claude code review": ["run claude code review", "check claude code review"],
        "triage code review": ["triage findings", "check code review"],
    }
    for name in bodies:
        loop = _loop(p, name)
        assert loop.max_retries == 2
        assert loop.on_exhaust is OnFailure.ASK_USER


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
    assert gens == ["run claude code review", "triage findings"]


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
            prompt = stage.impl.prompt.replace(BASE_PLACEHOLDER, "")
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


def test_only_triage_findings_carries_a_when(paths):
    for stage in _all_stages(_build(paths, auto=True)):
        if stage.name != "triage findings":
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

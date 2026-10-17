"""Tests for the fix_jira_issue_v3 ship builder (stages 52-68): real git, mocked agents."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from norn.dsl import ClearContext, Loop, OnFailure, Pipeline, Stage
from norn.model_labels import resolve_label
from norn.models import PipelineContext, StageResult
from norn.pipelines import _ship_v3
from norn.pipelines._comment_style import COMMENT_RULE
from norn.pipelines._decisions_v3 import (
    AutoDecide,
    DecisionGate,
    ShipDecisionGate,
    WriteDecision,
)
from norn.pipelines._fix_jira_v3 import V3Paths, shorten_home
from norn.pipelines._plan_review_v2 import _TOOLS
from norn.pipelines._ship_v3 import (
    BODY_END,
    BODY_START,
    DETAILS_CLOSE,
    DETAILS_OPEN,
    HEAD_PREVIEW,
    START_PREVIEW,
    AssemblePrBody,
    CheckCommitMessages,
    CheckSkill,
    CommitPlanGenerate,
    PrBodyGate,
    PrSummaryGate,
    RecordRewriteRange,
    RecordShippedHead,
    RewriteTreeGate,
    ScriptCheck,
    WriteShipDecision,
    add_ship_stages,
    check_plan_argv,
    checklist_file,
    commit_plan_prompt,
    pr_body_problems,
    pr_summary_prompt,
    render_ship,
    resolve_commit_skill,
)
from norn.runner import run_pipeline
from norn.skills import Skill
from norn.stages.generate import Generate
from norn.stages.run_command import RunCommand

FIXTURES = Path(__file__).parent / "fixtures" / "fix_jira_v3"

KEY = "CBS-1"
EMAIL = "dev@example.com"
BROWSE = "https://jira.example.com/browse/"
ISSUE_MD = "# CBS-1: Split the slow supplier query\n\n| Field | Value |\n| --- | --- |\n"

DEFAULT_COMMITS = [
    ("refactor: step-01-a — A", {"modified.txt": "one\ntwo\n", "new.txt": "new\n"}),
    ("refactor: step-02-b — B", {"dir/x.txt": "x\n"}),
    ("CBS-1: address code review findings", {"new.txt": "new\nnewer\n"}),
]
STYLED_COMMITS = [
    ("CBS-1: change the greeting files", {"modified.txt": "one\ntwo\n", "new.txt": "new\n"}),
    ("CBS-1: add the dir file", {"dir/x.txt": "x\n"}),
]
FIX_CI_COMMIT = ("fix(ci): auto-fix from check_and_fix_ci", {"dir/x.txt": "y\n"})
GROUPS = [
    ("CBS-1: change the greeting files", ["modified.txt", "new.txt"]),
    ("CBS-1: add the dir file\n\nThe file lives under dir/.", ["dir/x.txt"]),
]

TEST_CMD = (
    "set -e\n"
    'echo "=== STEP 1: unit tests ==="\n'
    "uv run pytest tests/ -q\n"
    'echo "=== STEP 2: lint ==="\n'
    "./scripts/lint.sh\n"
    'echo "=== STEP 3: headless tests ==="\n'
    "mvn -B verify\n"
)
EXPECTED_CHECKLIST = [
    "- [ ] 1. Flyway patch must be applied to the database repo",
    "- [ ] 2. Deploy notes",
    "- [ ] 3. Post-deploy verification query",
    "- [ ] human C4 confirm the retention period with the product owner",
    "- [ ] human C5 decide whether the report needs a feature flag",
]
FLYWAY_BODY = "Add the patch `V12__split_index.sql` to the database repository before deploy."


def _ctx() -> PipelineContext:
    return PipelineContext()


# ---------------------------------------------------------------------------
# Real git
# ---------------------------------------------------------------------------


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


def plan_text(start: str, head: str, groups: list[tuple[str, list[str]]]) -> str:
    """A commit plan in the split-commit-plan skill's format, whole files only."""
    lines = [
        f"# Split plan for {start[:7]}..{head[:7]}",
        "",
        f"- **Start commit (inclusive):** `{start}`",
        f"- **HEAD at plan time:**        `{head}`",
        "- **Branch:**                   `CBS-1`",
        "",
        "---",
        "",
    ]
    for n, (message, files) in enumerate(groups, 1):
        lines += [
            f"## Group {n} — {message.splitlines()[0]}",
            "",
            "**Commit message:**",
            "",
            "```",
            *message.splitlines(),
            "```",
            "",
            "**Files (whole-file):**",
            *[f"- `{f}`" for f in files],
            "",
            "---",
            "",
        ]
    return "\n".join(lines)


def _plan_with_home_line() -> str:
    """The synthetic plan with a home-directory path inside its human section."""
    text = (FIXTURES / "plan_for_the_human.md").read_text()
    return text.replace(
        FLYWAY_BODY, f"{FLYWAY_BODY}\nThe script is at {Path.home()}/notes/flyway.md.", 1
    )


def _human_section(plan: str) -> str:
    return plan.split("## For the human (run report)", 1)[1].split("## Pre-plan", 1)[0]


@dataclass
class ShipEnv:
    root: Path
    paths: V3Paths
    base_file: Path
    base: str
    plan: Path
    steps: Path
    issue_md: Path

    def head(self) -> str:
        return _git(self.root, "rev-parse", "HEAD")

    def tree(self) -> str:
        return _git(self.root, "rev-parse", "HEAD^{tree}")

    def start(self) -> str:
        return _git(self.root, "rev-list", "--reverse", f"{self.base}..HEAD").split()[0]

    def subjects(self) -> list[str]:
        return _git(self.root, "log", "--reverse", "--format=%s", f"{self.base}..HEAD").splitlines()

    def write_plan(self, groups=GROUPS) -> None:
        Path(self.paths.commit_plan).write_text(plan_text(self.start(), self.head(), groups))

    def write_ship(self, commits: str, status: str = "DECIDED") -> None:
        Path(self.paths.ship).write_text(
            f"# Ship — {KEY}\n\nSTATUS: {status}\nCOMMITS: {commits}\n"
            f"TITLE: {KEY}: split the slow supplier query\n"
        )

    def write_body_inputs(self, *, nits: str = "", proof_tree: str | None = None) -> None:
        p = self.paths
        self.write_plan()
        Path(p.pr_title).write_text(f"{KEY}: split the slow supplier query\n")
        Path(p.pr_summary).write_text(
            "Splits the supplier lookup into two indexed queries.\n"
            "The OR join forced a full scan on every lookup.\n"
        )
        Path(p.test_proof).write_text(f"{'0' * 40}\n{proof_tree or self.tree()}\n")
        Path(p.test_cmd_sh).write_text(TEST_CMD)
        Path(p.review_followups).write_text(
            "- [ ] C3 Timeout parsing duplicates the config loader (x/Settings.java:12)\n"
        )
        Path(p.review_nits).write_text(nits)


def _make_env(tmp_path: Path, commits=DEFAULT_COMMITS) -> ShipEnv:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    for key, value in (
        ("user.email", EMAIL),
        ("user.name", "Dev Eloper"),
        ("commit.gpgsign", "false"),
        ("core.hooksPath", str(root / ".git" / "hooks")),
    ):
        _git(root, "config", key, value)
    base = _commit(root, "initial", {"README.md": "# readme\n", "modified.txt": "one\n"})
    _git(root, "switch", "-q", "-c", KEY)
    for message, files in commits:
        _commit(root, message, files)

    art = tmp_path / "art"
    paths = V3Paths(str(art), KEY)
    Path(paths.work).mkdir(parents=True)
    base_file = art / "base.sha"
    base_file.write_text(f"{base}\n")
    issue_md = art / "issue.md"
    issue_md.write_text(ISSUE_MD)

    plan_md = _plan_with_home_line()
    plan = tmp_path / "CBS-1-final-plan.md"
    plan.write_text(plan_md)
    steps = tmp_path / "steps"
    steps.mkdir()
    # The same section with different whitespace: it must be de-duplicated.
    section = _human_section(plan_md).replace("\n\n", "\n\n\n")
    (steps / "README.md").write_text(
        f"# Run report\n\n## For the human (run report)\n{section}\n## Other\n\nx\n"
    )
    (steps / "handoff.md").write_text("# Handoff\n\n## Result\n\ndone\n\n## For the human\n\nnothing\n")
    (steps / "followups.md").write_text((FIXTURES / "followups.md").read_text())
    return ShipEnv(root, paths, base_file, base, plan, steps, issue_md)


@pytest.fixture
def env(tmp_path: Path) -> ShipEnv:
    return _make_env(tmp_path)


async def _open(env: ShipEnv) -> StageResult:
    result = await RecordRewriteRange(
        paths=env.paths, project_dir=str(env.root), base_file=str(env.base_file), key=KEY
    ).run(_ctx())
    assert result.success, result.error
    return result


def _assemble(env: ShipEnv, *, jira_browse: str | None = BROWSE) -> AssemblePrBody:
    return AssemblePrBody(
        paths=env.paths,
        plan=str(env.plan),
        steps_dir=str(env.steps),
        issue_md=str(env.issue_md),
        key=KEY,
        jira_browse=jira_browse,
        project_dir=str(env.root),
    )


async def _body(env: ShipEnv, **kw) -> str:
    result = await _assemble(env, **kw).run(_ctx())
    assert result.success, result.error
    return Path(env.paths.pr_body).read_text()


def _section_lines(body: str, heading: str) -> list[str]:
    lines = body.splitlines()
    start = lines.index(heading)
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ") or lines[i] == BODY_END),
        len(lines),
    )
    return [ln for ln in lines[start + 1:end] if ln.strip()]


# ---------------------------------------------------------------------------
# AssemblePrBody
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_body_layout_from_the_fixtures(env):
    await _open(env)
    env.write_body_inputs()
    body = await _body(env)
    lines = body.splitlines()

    assert lines[0] == BODY_START
    assert lines[1] == f"Jira: [{KEY}]({BROWSE}{KEY}) — CBS-1: Split the slow supplier query"
    assert lines[2] == "Splits the supplier lookup into two indexed queries."
    assert body.endswith(f"{BODY_END}\n")

    assert _section_lines(body, "## Commits") == [
        "- CBS-1: change the greeting files (2 files)",
        "- CBS-1: add the dir file (1 files)",
    ]

    # Plan and README carry the same section: one copy only.
    assert body.count(FLYWAY_BODY) == 1
    human = lines[lines.index("## For the human") + 1:lines.index(DETAILS_OPEN)]
    assert [ln for ln in human if ln.strip()] == EXPECTED_CHECKLIST

    details = body.split(DETAILS_OPEN, 1)[1].split(DETAILS_CLOSE, 1)[0]
    assert str(Path.home()) not in details
    assert "~/notes/flyway.md" in details
    assert "### 4. Background" in details

    assert _section_lines(body, "## Follow-ups") == [
        "- [ ] C3 Timeout parsing duplicates the config loader (x/Settings.java:12)",
        "- [ ] minor C2 rename the helper for clarity",
        "- [ ] minor C3 add a log line when the cache is cold",
        "- [ ] nit: C6 trailing whitespace in the fixture",
    ]
    # The range holds two `refactor:` subjects.
    assert len(_section_lines(body, "## Reword at squash/merge")) == 2

    assert _section_lines(body, "## Testing") == [
        f"Local, passed on tree {env.tree()[:7]} (gate 3):",
        "- unit tests `uv run pytest tests/ -q`",
        "- lint",
        "- headless tests `mvn -B verify`",
    ]
    assert Path(checklist_file(env.paths)).read_text().splitlines() == EXPECTED_CHECKLIST


@pytest.mark.asyncio
async def test_jira_line_is_plain_without_a_browse_base(env):
    await _open(env)
    env.write_body_inputs()
    body = await _body(env, jira_browse=None)
    assert body.splitlines()[1] == f"Jira: {KEY} — CBS-1: Split the slow supplier query"
    assert "](" not in body.splitlines()[1]


@pytest.mark.asyncio
async def test_nits_come_from_review_nits_and_are_omitted_when_empty(env):
    await _open(env)
    env.write_body_inputs(nits="- C5 Rename the loop variable (x/Client.java:93)\n- Sort the imports\n")
    body = await _body(env)
    assert _section_lines(body, "## Nits (not fixed)") == [
        "- C5 Rename the loop variable (x/Client.java:93)",
        "- Sort the imports",
    ]
    Path(env.paths.review_nits).write_text("")
    assert "## Nits" not in await _body(env)


@pytest.mark.asyncio
async def test_reword_lists_a_ci_fix_commit_and_is_omitted_without_one(tmp_path):
    styled = _make_env(tmp_path / "styled", commits=STYLED_COMMITS)
    await _open(styled)
    styled.write_body_inputs()
    assert "## Reword" not in await _body(styled)

    fixed = _make_env(tmp_path / "fixed", commits=[*STYLED_COMMITS, FIX_CI_COMMIT])
    await _open(fixed)
    fixed.write_body_inputs()
    body = await _body(fixed)
    head = fixed.head()
    assert _section_lines(body, "## Reword at squash/merge") == [
        f"- {head[:7]} fix(ci): auto-fix from check_and_fix_ci"
    ]


@pytest.mark.asyncio
async def test_testing_line_does_not_claim_a_pre_fix_tree(env):
    await _open(env)
    # The proof records HEAD's tree from before the review-fix commit.
    pre_fix = _git(env.root, "rev-parse", "HEAD~1^{tree}")
    env.write_body_inputs(proof_tree=pre_fix)
    line = _section_lines(await _body(env), "## Testing")[0]
    assert line == (
        f"Local, passed on tree {pre_fix[:7]} plus the changes uncommitted at the time (gate 3):"
    )
    assert env.tree()[:7] not in line


@pytest.mark.asyncio
async def test_empty_sources_omit_for_the_human_and_details(env):
    await _open(env)
    env.write_body_inputs()
    env.plan.write_text("# Plan\n\n## Summary\n\nx\n")
    (env.steps / "README.md").unlink()
    (env.steps / "followups.md").write_text("# Follow-ups\n\n## Unapplied review findings\n\n- [ ] minor C2 x\n")
    body = await _body(env)
    assert "## For the human" not in body
    assert "<details>" not in body
    assert Path(checklist_file(env.paths)).read_text() == ""
    assert (await PrBodyGate(paths=env.paths, key=KEY).run(_ctx())).success


@pytest.mark.asyncio
async def test_huge_details_are_replaced_by_the_plan_path(env):
    await _open(env)
    env.write_body_inputs()
    huge = "x" * 79 + "\n"
    env.plan.write_text(
        "# Plan\n\n## For the human (run report)\n\n### 1. Huge\n\n" + huge * 900 + "\n## Other\n\ny\n"
    )
    (env.steps / "README.md").unlink()
    body = await _body(env)
    assert "<details>" not in body
    assert f"Full instructions: {shorten_home(str(env.plan))}" in body
    assert "- [ ] 1. Huge" in body
    assert len(body) < 60_000
    assert (await PrBodyGate(paths=env.paths, key=KEY).run(_ctx())).success


@pytest.mark.asyncio
async def test_body_still_over_the_limit_fails(env):
    await _open(env)
    env.write_body_inputs()
    Path(env.paths.review_nits).write_text(("- " + "n" * 98 + "\n") * 700)
    result = await _assemble(env).run(_ctx())
    assert not result.success
    assert "the limit is 60000" in result.error.splitlines()[0]
    assert not Path(env.paths.pr_body).exists()


@pytest.mark.asyncio
async def test_missing_test_proof_fails(env):
    await _open(env)
    env.write_body_inputs()
    Path(env.paths.test_proof).unlink()
    result = await _assemble(env).run(_ctx())
    assert not result.success
    assert "test proof is missing" in result.error.splitlines()[0]


# ---------------------------------------------------------------------------
# PrBodyGate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pr_body_gate_passes_then_fails_on_each_broken_rule(env):
    await _open(env)
    env.write_body_inputs()
    body = await _body(env)
    gate = PrBodyGate(paths=env.paths, key=KEY)
    assert (await gate.run(_ctx())).success
    body_path = Path(env.paths.pr_body)

    body_path.write_text(body.replace("## Commits\n", ""))
    result = await gate.run(_ctx())
    assert not result.success and "'## Commits' section is missing" in result.error

    body_path.write_text(body.replace("## Testing\n", "Claude-Session: abc\n\n## Testing\n"))
    result = await gate.run(_ctx())
    assert not result.success and "attribution line: 'Claude-Session: abc'" in result.error

    body_path.write_text(body.replace(BODY_END, ""))
    result = await gate.run(_ctx())
    assert not result.success and "end marker" in result.error


def test_pr_body_problems_rules():
    good = (
        f"{BODY_START}\nJira: {KEY} — x\nsummary\n\n## Commits\n- {KEY}: a (1 files)\n\n"
        f"## For the human\n- [x] 1. Do it\n\n{DETAILS_OPEN}\n\n### 1. Do it\n\n{DETAILS_CLOSE}\n\n"
        f"## Testing\nLocal\n{BODY_END}\n"
    )
    assert pr_body_problems(good, KEY, expect_human=True) == []
    assert any("sources have no items" in p for p in pr_body_problems(good, KEY, expect_human=False))
    no_human = re.sub(r"## For the human\n.*?</details>\n\n", "", good, flags=re.S)
    assert any("'## For the human' section is missing" in p for p in pr_body_problems(no_human, KEY, expect_human=True))
    no_box = good.replace("- [x] 1. Do it\n", "Do it.\n")
    assert any("no '- [ ]' checklist" in p for p in pr_body_problems(no_box, KEY, expect_human=True))
    no_jira = good.replace(f"Jira: {KEY} — x", "x")
    assert any("Jira:" in p for p in pr_body_problems(no_jira, KEY, expect_human=True))
    swapped = good.replace(BODY_START, "@@").replace(BODY_END, BODY_START).replace("@@", BODY_END)
    assert any("comes before" in p for p in pr_body_problems(swapped, KEY, expect_human=True))


# ---------------------------------------------------------------------------
# Gate-4 opener and the commit plan stages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_rewrite_range_clears_stale_outputs_and_records_the_range(env):
    p = env.paths
    stale = [
        p.commit_plan, p.ship, p.pr_body, checklist_file(p), p.pr_title, p.pr_summary,
        p.commit_plan_problems, p.pr_summary_problems, p.rewritten, p.shipped_heads,
    ]
    for name in stale:
        Path(name).write_text("stale\n")
    result = await _open(env)
    for name in stale:
        assert not Path(name).exists(), name
    assert Path(p.rewrite_range).read_text() == (
        f"base {env.base}\nstart {env.start()}\nhead {env.head()}\ntree {env.tree()}\n"
    )
    assert result.output == f"{env.start()[:7]}..{env.head()[:7]}: 3 commits"


@pytest.mark.asyncio
async def test_record_rewrite_range_fails_with_the_script_problems(tmp_path):
    env = _make_env(tmp_path, commits=[])
    result = await RecordRewriteRange(
        paths=env.paths, project_dir=str(env.root), base_file=str(env.base_file), key=KEY
    ).run(_ctx())
    assert not result.success
    assert "no commits in base..HEAD" in result.error
    assert not Path(env.paths.rewrite_range).exists()


def _builder(env: ShipEnv | None = None, *, paths: V3Paths | None = None, auto: bool = False,
             skill: Skill | None = Skill("split-commit-plan", "skill text")) -> Pipeline:
    paths = paths or env.paths
    return add_ship_stages(
        Pipeline("test-ship-v3"),
        paths=paths,
        project_dir=str(env.root) if env else "/repo",
        key=KEY,
        plan=str(env.plan) if env else "/plan.md",
        steps_dir=str(env.steps) if env else "/steps",
        issue_md=str(env.issue_md) if env else "/issue.md",
        base_file=str(env.base_file) if env else "/art/base.sha",
        skill=skill,
        skill_searched=["/a", "/b", "/c"],
        jira_browse=BROWSE,
        auto=auto,
    )


def _loop(pipeline: Pipeline, name: str) -> Loop:
    return next(i for i in pipeline.items if isinstance(i, Loop) and i.name == name)


def _stage(pipeline: Pipeline, name: str) -> Stage:
    return next(i for i in pipeline.items if isinstance(i, Stage) and i.name == name)


def _body_stage(pipeline: Pipeline, loop: str, name: str) -> Stage:
    return next(s for s in _loop(pipeline, loop).stages if s.name == name)


def _all_stages(pipeline: Pipeline) -> list[Stage]:
    out: list[Stage] = []
    for item in pipeline.items:
        out += item.stages if isinstance(item, Loop) else [item] if isinstance(item, Stage) else []
    return out


def test_write_commit_plan_when(tmp_path):
    paths = V3Paths(str(tmp_path / "art"), KEY)
    when = _body_stage(_builder(paths=paths), "plan commits", "write commit plan").when
    ctx = _ctx()  # empty results: a resumed process
    assert when(ctx) is True  # no plan yet
    Path(paths.work).mkdir(parents=True)
    Path(paths.commit_plan).write_text("plan\n")
    assert when(ctx) is False
    Path(paths.commit_plan_problems).write_text("group 1 has no files\n")
    assert when(ctx) is True


@pytest.mark.asyncio
async def test_script_check_writes_and_removes_the_problems_file(env):
    await _open(env)
    check = ScriptCheck(
        argv=check_plan_argv(paths=env.paths, project_dir=str(env.root), key=KEY),
        problems_file=env.paths.commit_plan_problems,
    )
    problems = Path(env.paths.commit_plan_problems)

    # Exit 2 (no plan file): a usage error, and no problems file for the agent.
    result = await check.run(_ctx())
    assert not result.success and "usage error" in result.error.splitlines()[0]
    assert not problems.exists()

    env.write_plan([("refactor: change things", ["modified.txt", "new.txt"]), GROUPS[1]])
    result = await check.run(_ctx())
    assert not result.success
    assert result.error.splitlines()[0] == "_commit_rewrite_v3.py check: 1 problems"
    assert "group 1: subject must start with 'CBS-1: '" in problems.read_text()

    env.write_plan()
    result = await check.run(_ctx())
    assert result.success, result.error
    assert not problems.exists()


@pytest.mark.asyncio
async def test_script_check_without_a_range_fails_cleanly(tmp_path):
    paths = V3Paths(str(tmp_path / "art"), KEY)
    check = ScriptCheck(argv=check_plan_argv(paths=paths, project_dir="/repo", key=KEY))
    result = await check.run(_ctx())
    assert not result.success
    assert result.error.splitlines()[0] == f"{paths.rewrite_range} is missing"


# ---------------------------------------------------------------------------
# Prompts and agent stages
# ---------------------------------------------------------------------------


def test_commit_plan_prompt_states_every_override(tmp_path):
    paths = V3Paths(str(tmp_path / "art"), KEY)
    start, head = "a" * 40, "b" * 40
    prompt = commit_plan_prompt(paths=paths, key=KEY, start=start, head=head, issue_md="/art/issue.md")
    for line in (
        "This is an unattended stage: skip the phase-1 chat and write the file now.",
        f"Write the plan to {paths.commit_plan}, not to `tmp/plans/…`.",
        f"`Start commit (inclusive)` is `{start}` and `HEAD at plan time` is `{head}`.",
        "Whole-file groups only",
        "Write no `**Hunks (partial-file):**` bullets.",
        f"Every subject is `{KEY}: <lowercase summary>`: at most 72 characters",
        "no trailing period",
        "Do not copy\n  the `refactor:` style of the commits in scope.",
        "`KEY type:` forms",
        "comma-joined keys",
        "exempt prefixes",
        "`#KEY` forms",
        "The body is optional and has at most 4 lines, not 6",
        "No footers or trailers.",
        "Pass no Jira hints and do not run `jira-fetch.sh`: the issue text is at\n  /art/issue.md.",
        "Read-only git only",
        f"If {paths.commit_plan_problems} exists, the plan failed its check: fix\n  exactly the problems listed there",
        "split-commit-plan skill in your system prompt",
    ):
        assert line in prompt, line
    assert prompt.rstrip().endswith(COMMENT_RULE)
    assert "{" not in prompt and "}" not in prompt


def _subagent_env() -> dict[str, str]:
    return {
        "CLAUDE_CODE_SUBAGENT_MODEL": resolve_label("claude-code", "standard").model,
        "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1",
    }


def test_agent_stage_settings(tmp_path):
    skill = Skill("split-commit-plan", "skill text")
    p = _builder(paths=V3Paths(str(tmp_path / "art"), KEY), skill=skill)
    plan = _body_stage(p, "plan commits", "write commit plan").impl
    assert isinstance(plan, CommitPlanGenerate) and isinstance(plan, Generate)
    assert plan.model == "standard"
    assert plan.skills == [skill]
    assert plan.allowed_tools == list(_TOOLS)
    assert plan.permission_mode == "acceptEdits"
    assert plan.setting_sources == ["project"]
    assert plan.env == _subagent_env()
    assert START_PREVIEW in plan.prompt and HEAD_PREVIEW in plan.prompt

    summary = _body_stage(p, "summarize pr", "write pr summary").impl
    assert isinstance(summary, Generate)
    assert summary.model == "light"
    assert "Bash" not in summary.allowed_tools
    assert summary.permission_mode == "acceptEdits"
    assert summary.setting_sources == ["project"]
    assert summary.env == _subagent_env()

    no_skill = _builder(paths=V3Paths(str(tmp_path / "art"), KEY), skill=None)
    assert _body_stage(no_skill, "plan commits", "write commit plan").impl.skills == []


def test_prompts_name_no_model_and_have_no_braces(tmp_path):
    p = _builder(paths=V3Paths(str(tmp_path / "art"), KEY))
    for stage in _all_stages(p):
        if isinstance(stage.impl, Generate):
            text = stage.impl.prompt
            assert "{" not in text and "}" not in text, stage.name
            for family in ('"haiku"', '"sonnet"', '"opus"'):
                assert family not in text
            assert not re.search(r"claude-[a-z]+-[0-9]", text)
            assert text.rstrip().endswith(COMMENT_RULE), stage.name


@pytest.mark.asyncio
async def test_commit_plan_stage_reads_the_range_when_it_runs(env, monkeypatch):
    seen: list[str] = []

    async def fake_run(self, ctx, **kwargs):
        seen.append(self.prompt)
        return StageResult(name="", success=True)

    monkeypatch.setattr(Generate, "run", fake_run)
    stage = CommitPlanGenerate(paths=env.paths, key=KEY, issue_md=str(env.issue_md), skill=None)
    result = await stage.run(_ctx())
    assert not result.success and "rewrite-range.txt is missing" in result.error
    assert seen == []
    await _open(env)
    assert (await stage.run(_ctx())).success
    assert f"`Start commit (inclusive)` is `{env.start()}`" in seen[0]
    assert f"`HEAD at plan time` is `{env.head()}`" in seen[0]


def test_pr_summary_prompt(tmp_path):
    paths = V3Paths(str(tmp_path / "art"), KEY)
    prompt = pr_summary_prompt(paths=paths, key=KEY, plan="/plan.md", issue_md="/issue.md")
    assert paths.pr_title in prompt and paths.pr_summary in prompt
    assert f"`{KEY}: <lowercase summary>`" in prompt
    assert "1 to 8 lines" in prompt
    assert paths.pr_summary_problems in prompt
    assert prompt.rstrip().endswith(COMMENT_RULE)


def test_write_pr_summary_when(tmp_path):
    paths = V3Paths(str(tmp_path / "art"), KEY)
    when = _body_stage(_builder(paths=paths), "summarize pr", "write pr summary").when
    ctx = _ctx()
    assert when(ctx) is True
    Path(paths.work).mkdir(parents=True)
    Path(paths.pr_title).write_text(f"{KEY}: x\n")
    assert when(ctx) is True  # summary still missing
    Path(paths.pr_summary).write_text("x\n")
    assert when(ctx) is False
    Path(paths.pr_summary_problems).write_text("title: …\n")
    assert when(ctx) is True


@pytest.mark.asyncio
async def test_pr_summary_gate(tmp_path):
    paths = V3Paths(str(tmp_path / "art"), KEY)
    Path(paths.work).mkdir(parents=True)
    gate = PrSummaryGate(paths=paths, key=KEY, problems_file=paths.pr_summary_problems)
    problems = Path(paths.pr_summary_problems)

    result = await gate.run(_ctx())
    assert not result.success and "pr-title.txt was not written" in result.error
    assert problems.exists()

    Path(paths.pr_title).write_text(f"{KEY}: Split the query.\n")
    Path(paths.pr_summary).write_text("line\n" * 9 + "Co-Authored-By: someone\n")
    result = await gate.run(_ctx())
    assert not result.success
    text = problems.read_text()
    assert "lower-case word" in text and "must not end with '.'" in text
    assert "10 non-blank lines" in text and "attribution line" in text

    Path(paths.pr_title).write_text(f"{KEY}: split the query\n")
    Path(paths.pr_summary).write_text("Splits the query in two.\n")
    assert (await gate.run(_ctx())).success
    assert not problems.exists()


# ---------------------------------------------------------------------------
# resolve_commit_skill / CheckSkill
# ---------------------------------------------------------------------------


def test_resolve_commit_skill_none_with_empty_dirs(tmp_path, monkeypatch):
    home, cwd = tmp_path / "home", tmp_path / "cwd"
    home.mkdir()
    cwd.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(cwd)
    skill, searched = resolve_commit_skill()
    assert skill is None
    assert searched == [
        str(cwd / "skills" / "split-commit-plan"),
        str(cwd / ".claude" / "skills" / "split-commit-plan"),
        str(home / ".claude" / "skills" / "split-commit-plan"),
    ]


def test_resolve_commit_skill_reads_both_files(tmp_path, monkeypatch):
    home, cwd = tmp_path / "home", tmp_path / "cwd"
    folder = cwd / ".claude" / "skills" / "split-commit-plan"
    folder.mkdir(parents=True)
    home.mkdir()
    (folder / "SKILL.md").write_text("# split-commit-plan\nplan body\n")
    (folder / "commit-message-style.md").write_text("# style\nsubject rules\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(cwd)
    skill, _searched = resolve_commit_skill()
    assert isinstance(skill, Skill)
    assert skill.name == "split-commit-plan"
    assert "plan body" in skill.content and "subject rules" in skill.content
    assert skill.content.index("plan body") < skill.content.index("subject rules")


@pytest.mark.asyncio
async def test_check_skill():
    result = await CheckSkill(skill=None, searched=["/x/skills/split-commit-plan", "/y"]).run(_ctx())
    assert not result.success
    assert "split-commit-plan skill was not found" in result.error.splitlines()[0]
    assert "searched: /x/skills/split-commit-plan" in result.error
    ok = await CheckSkill(skill=Skill("split-commit-plan", "x"), searched=[]).run(_ctx())
    assert ok.success


# ---------------------------------------------------------------------------
# Gate 4: decision, rewrite, messages, shipped head
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_ship_decision_then_auto_decide(env):
    await _open(env)
    env.write_body_inputs()
    p = _builder(env, auto=True)
    write = _stage(p, "write ship decision").impl
    assert isinstance(write, WriteShipDecision) and isinstance(write, WriteDecision)
    assert (await write.run(_ctx())).success
    text = Path(env.paths.ship).read_text()
    assert text == render_ship(
        key=KEY,
        paths=env.paths,
        summary=["1. CBS-1: change the greeting files (2 files)", "2. CBS-1: add the dir file (1 files)"],
        title=f"{KEY}: split the slow supplier query",
    )
    assert "STATUS: PROPOSED\nCOMMITS: rewrite\nTITLE: CBS-1: split the slow supplier query\n" in text
    assert f"PR body: {env.paths.pr_body} (edit freely)" in text

    decided = _stage(p, "ship decided").impl
    assert not (await decided.run(_ctx())).success
    assert (await _stage(p, "auto decide ship").impl.run(_ctx())).success
    assert "STATUS: DECIDED (auto)" in Path(env.paths.ship).read_text()
    assert (await decided.run(_ctx())).success
    assert (await _stage(p, "ship decision recorded").impl.run(_ctx())).success


@pytest.mark.asyncio
async def test_write_ship_decision_fails_without_a_title(env):
    await _open(env)
    env.write_body_inputs()
    Path(env.paths.pr_title).unlink()
    result = await _stage(_builder(env), "write ship decision").impl.run(_ctx())
    assert not result.success and "pr-title.txt is missing" in result.error


_REWRITE_STAGES = [
    "commit plan still valid",
    "rewrite commits",
    "rewrite matches tree",
    "check commit messages",
    "record shipped head",
]


async def _run_stages(env: ShipEnv, names: list[str]) -> PipelineContext:
    p = _builder(env)
    items = [i for i in p.items if isinstance(i, (Stage, Loop)) and i.name in names]
    assert [i.name for i in items] == names
    return await run_pipeline(Pipeline("test-ship-v3", items=items))


def _statuses(ctx: PipelineContext) -> dict[str, str]:
    return {entry.name: entry.status for entry in ctx.stage_log}


@pytest.mark.asyncio
async def test_keep_skips_the_rewrite_and_checks_attribution_only(env):
    await _open(env)
    env.write_plan()
    env.write_ship("keep")
    head = env.head()
    ctx = await _run_stages(env, _REWRITE_STAGES)
    statuses = _statuses(ctx)
    for name in ("commit plan still valid", "rewrite commits", "rewrite matches tree"):
        assert statuses[name] == "skipped_condition", name
    # The `refactor:` subjects would fail the full check.
    assert ctx.results["check commit messages"].output.startswith("COMMITS: keep")
    assert env.head() == head
    assert not Path(env.paths.rewritten).exists()
    assert Path(env.paths.shipped_heads).read_text() == f"{head}\n"


@pytest.mark.asyncio
async def test_keep_still_refuses_attribution(env):
    await _open(env)
    env.write_ship("keep")
    _commit(env.root, "refactor: step-03-c — C\n\nClaude-Session: abc", {"z.txt": "z\n"})
    stage = CheckCommitMessages(
        paths=env.paths, project_dir=str(env.root), base_file=str(env.base_file), key=KEY
    )
    result = await stage.run(_ctx())
    assert not result.success
    assert "attribution line: 'Claude-Session: abc'" in result.error


@pytest.mark.asyncio
async def test_rewrite_applies_the_plan_and_records_the_new_head(env):
    await _open(env)
    env.write_plan()
    env.write_ship("rewrite")
    old_head, tree = env.head(), env.tree()
    Path(env.paths.shipped_heads).write_text("0" * 40 + "\n")  # replaced, never appended to
    ctx = await _run_stages(env, _REWRITE_STAGES)
    statuses = _statuses(ctx)
    assert all(statuses[name] == "passed" for name in _REWRITE_STAGES), statuses
    assert Path(env.paths.rewritten).exists()
    new_head = env.head()
    assert new_head != old_head and env.tree() == tree
    assert env.subjects() == ["CBS-1: change the greeting files", "CBS-1: add the dir file"]
    assert Path(env.paths.shipped_heads).read_text() == f"{new_head}\n"
    assert ctx.results["check commit messages"].output.startswith("COMMITS: rewrite")


@pytest.mark.asyncio
async def test_rewrite_tree_gate_fails_on_another_tree(env):
    await _open(env)
    _commit(env.root, "CBS-1: more", {"extra.txt": "e\n"})
    result = await RewriteTreeGate(paths=env.paths, project_dir=str(env.root)).run(_ctx())
    assert not result.success and "is not the recorded tree" in result.error


@pytest.mark.asyncio
async def test_record_shipped_head_keep_with_a_new_commit_names_both_shas(env):
    await _open(env)
    env.write_ship("keep")
    recorded = env.head()
    new = _commit(env.root, "CBS-1: made at gate 4", {"extra.txt": "e\n"})
    result = await RecordShippedHead(paths=env.paths, project_dir=str(env.root)).run(_ctx())
    assert not result.success
    assert recorded[:7] in result.error.splitlines()[0]
    assert new[:7] in result.error.splitlines()[0]
    assert not Path(env.paths.shipped_heads).exists()


@pytest.mark.asyncio
async def test_record_shipped_head_refuses_an_untracked_file(env):
    await _open(env)
    env.write_ship("keep")
    (env.root / "stray.log").write_text("x\n")
    result = await RecordShippedHead(paths=env.paths, project_dir=str(env.root)).run(_ctx())
    assert not result.success
    assert "stray.log" in result.error
    assert not Path(env.paths.shipped_heads).exists()


@pytest.mark.asyncio
async def test_record_shipped_head_rewrite_needs_the_recorded_tree(env):
    await _open(env)
    env.write_ship("rewrite")
    _commit(env.root, "CBS-1: made at gate 4", {"extra.txt": "e\n"})
    result = await RecordShippedHead(paths=env.paths, project_dir=str(env.root)).run(_ctx())
    assert not result.success and "is not the recorded tree" in result.error


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def _item_names(pipeline: Pipeline) -> list[str]:
    names = []
    for item in pipeline.items:
        if isinstance(item, (Stage, Loop)):
            names.append(item.name)
        elif isinstance(item, ClearContext):
            names.append("ClearContext")
    return names


_HEAD = [
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
]
_TAIL = [
    "ship decided",
    "ship decision recorded",
    "commit plan still valid",
    "pr body still complete",
    "rewrite commits",
    "rewrite matches tree",
    "check commit messages",
    "record shipped head",
]


def test_interactive_order(tmp_path):
    assert _item_names(_builder(paths=V3Paths(str(tmp_path), KEY))) == _HEAD + _TAIL


def test_auto_order(tmp_path):
    p = _builder(paths=V3Paths(str(tmp_path), KEY), auto=True)
    assert _item_names(p) == _HEAD + ["auto decide ship"] + _TAIL


def test_stage_policies(tmp_path):
    paths = V3Paths(str(tmp_path / "art"), KEY)
    p = _builder(paths=paths, auto=True)
    bodies = {i.name: [s.name for s in i.stages] for i in p.items if isinstance(i, Loop)}
    assert bodies == {
        "plan commits": ["write commit plan", "check commit plan"],
        "summarize pr": ["write pr summary", "check pr summary"],
    }
    for name in bodies:
        loop = _loop(p, name)
        assert loop.max_retries == 2 and loop.on_exhaust is OnFailure.ASK_USER

    check = _body_stage(p, "plan commits", "check commit plan").impl
    assert isinstance(check, ScriptCheck) and check.problems_file == paths.commit_plan_problems
    summary_check = _body_stage(p, "summarize pr", "check pr summary").impl
    assert isinstance(summary_check, PrSummaryGate)
    assert summary_check.problems_file == paths.pr_summary_problems

    expected = {
        "record rewrite range": (RecordRewriteRange, OnFailure.FAIL),
        "check commit skill": (CheckSkill, OnFailure.FAIL),
        "commit plan valid": (ScriptCheck, OnFailure.FAIL),
        "pr summary valid": (PrSummaryGate, OnFailure.FAIL),
        "assemble pr body": (AssemblePrBody, OnFailure.FAIL),
        "write ship decision": (WriteDecision, OnFailure.FAIL),
        "auto decide ship": (AutoDecide, OnFailure.FAIL),
        "ship decided": (DecisionGate, OnFailure.ASK_USER),
        "ship decision recorded": (ShipDecisionGate, OnFailure.FAIL),
        "commit plan still valid": (ScriptCheck, OnFailure.FAIL),
        "pr body still complete": (PrBodyGate, OnFailure.FAIL),
        "rewrite commits": (RunCommand, OnFailure.ASK_USER),
        "rewrite matches tree": (RewriteTreeGate, OnFailure.FAIL),
        "check commit messages": (CheckCommitMessages, OnFailure.FAIL),
        "record shipped head": (RecordShippedHead, OnFailure.FAIL),
    }
    for name, (cls, policy) in expected.items():
        stage = _stage(p, name)
        assert isinstance(stage.impl, cls), name
        assert stage.on_failure is policy, name
        assert stage.always_run is (name == "ship decision recorded"), name
    assert _stage(p, "commit plan valid").impl.problems_file is None
    assert _stage(p, "auto decide ship").impl.field == "STATUS"
    assert _stage(p, "auto decide ship").impl.value == "DECIDED"

    rewrite = _stage(p, "rewrite commits").impl
    assert f"python3 {_ship_v3.REWRITE_SCRIPT} apply --plan {paths.commit_plan}" in rewrite.cmd
    assert rewrite.cmd.endswith(f"touch {paths.rewritten}")


def test_rewrite_stages_share_the_commits_when(tmp_path):
    paths = V3Paths(str(tmp_path / "art"), KEY)
    p = _builder(paths=paths, auto=True)
    gated = ("commit plan still valid", "rewrite commits", "rewrite matches tree")
    whens = {_stage(p, name).when for name in gated}
    assert len(whens) == 1 and None not in whens
    when = whens.pop()
    for stage in p.items:
        if isinstance(stage, Stage) and stage.name not in gated:
            assert stage.when is None, stage.name

    ctx = _ctx()
    assert when(ctx) is False  # no ship file
    Path(paths.work).mkdir(parents=True)
    Path(paths.ship).write_text("STATUS: DECIDED\nCOMMITS: keep\n")
    assert when(ctx) is False
    Path(paths.ship).write_text("STATUS: DECIDED\nCOMMITS: Rewrite\n")
    assert when(ctx) is True

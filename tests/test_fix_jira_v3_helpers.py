"""Tests for norn/pipelines/_fix_jira_v3.py — paths, run mode, house style, run identity."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from norn.alerts import AlertManager
from norn.models import PipelineContext, StageResult
from norn.pipelines import _fix_jira_v3 as v3
from norn.pipelines._fix_jira_v3 import (
    CheckRunIdentity,
    RecordRunIdentity,
    RegisterChildState,
    V3Paths,
    attribution_lines,
    followup_items,
    human_checklist,
    human_followup_items,
    human_sections,
    notify,
    on_off_knob,
    positive_int_knob,
    run_mode,
    shorten_home,
    subject_problems,
)
from norn.pipelines._fix_jira_v3 import test_steps as extract_test_steps

FIXTURES = Path(__file__).parent / "fixtures" / "fix_jira_v3"
RUN = ["run", "fix_jira_issue_v3"]


# ---------------------------------------------------------------------------
# attribution and house style
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Co-Authored-By: Jane <j@x>",
        "Claude-Session: abc",
        "\U0001F916 Generated with [Claude Code](https://claude.com/claude-code)",
    ],
)
def test_strict_ere_matches(text):
    assert attribution_lines(text) == [text]


@pytest.mark.parametrize(
    "text",
    ["claude_code.py", "generated with jOOQ", "# co-authored by the team"],
)
def test_strict_ere_ignores(text):
    assert attribution_lines(text) == []


def test_subject_accepted():
    msg = "CBS-2655: split the slow supplier-reference OR into two queries"
    assert subject_problems(msg, "CBS-2655") == []


def test_subject_with_short_body_accepted():
    msg = "CBS-1: do it\n\nline one\nline two"
    assert subject_problems(msg, "CBS-1") == []


@pytest.mark.parametrize(
    "msg, fragment",
    [
        ("refactor: step-01-x — Step 1", "must start with 'CBS-2655: '"),
        ("CBS-2655: Split it", "must start with 'CBS-2655: '"),
        ("CBS-2655: done.", "must not end with '.'"),
        ("CBS-2655: " + "a" * 63, "73 characters"),
        ("CBS-2655: do it\n\n1\n2\n3\n4\n5", "5 non-blank lines"),
        ("CBS-2655: do it\nbody right away", "line 2 must be blank"),
        ("CBS-2655: do it\n\nCo-Authored-By: Jane <j@x>", "attribution line"),
    ],
)
def test_subject_rejected(msg, fragment):
    problems = subject_problems(msg, "CBS-2655")
    assert len(problems) == 1, problems
    assert fragment in problems[0]


# ---------------------------------------------------------------------------
# markdown extraction
# ---------------------------------------------------------------------------


def test_human_sections_fixture():
    sections = human_sections((FIXTURES / "plan_for_the_human.md").read_text())
    assert len(sections) == 1
    assert "## not a heading" in sections[0]
    assert "Pre-plan acceptance" not in sections[0]
    items = human_checklist(sections)
    assert items == [
        "- [ ] 1. Flyway patch must be applied to the database repo",
        "- [ ] 2. Deploy notes",
        "- [ ] 3. Post-deploy verification query",
    ]


def test_human_sections_nothing():
    assert human_sections("## For the human\n\nnothing") == []
    assert human_sections("## For the human\n\nN/A.\n\n## Next\n\nx") == []


def test_human_checklist_without_level3_uses_first_line():
    sections = human_sections("## For the human\n\n\nApply the patch by hand.\nThen restart.\n")
    assert human_checklist(sections) == ["- [ ] Apply the patch by hand."]


def test_followup_items_split():
    md = (FIXTURES / "followups.md").read_text()
    human = human_followup_items(md)
    other = followup_items(md)
    assert len(human) == 2 and all(i.startswith("- [ ] human") for i in human)
    assert len(other) == 3
    assert not any("not part of the section" in i for i in human + other)


def test_shorten_home():
    home = str(Path.home())
    assert shorten_home(f"{home}/projects/x") == "~/projects/x"
    assert shorten_home("/opt/x") == "/opt/x"


def test_test_steps_step_markers():
    script = "\n".join(
        [
            'echo "=== STEP 1: unit tests ==="',
            "uv run pytest tests/ -q",
            'echo "=== STEP 2: lint ==="',
            "./scripts/lint.sh",
            'echo "=== STEP 3: headless tests ==="',
            "mvn -B -ntp -pl modules/core-ejb3 verify -P headless-tests -DskipITs",
        ]
    )
    labels = extract_test_steps(script)
    assert len(labels) == 3
    assert labels[0] == "unit tests `uv run pytest tests/ -q`"
    assert labels[1] == "lint"
    assert labels[2].endswith(
        "`mvn -B -ntp -pl modules/core-ejb3 verify -P headless-tests -DskipITs`"
    )


def test_test_steps_aggregate_markers():
    script = (
        'echo "=== step-01-x ===" && echo "=== test_cmd ===" && pytest a && '
        'echo "=== step-02-y ===" && pytest b'
    )
    assert extract_test_steps(script) == ["step-01-x", "step-02-y"]


def test_test_steps_without_markers():
    assert extract_test_steps("set -e\nmvn -B verify\nnpm test\n") == ["mvn -B verify", "npm test"]
    assert extract_test_steps("a\n\nb\nc\nd\ne\nf\ng\n") == ["a", "b", "c", "d", "e"]


# ---------------------------------------------------------------------------
# run mode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        [*RUN, "CBS-1", "auto"],
        ["CBS-1", "auto"],
        [*RUN, "https://x.atlassian.net/browse/CBS-1", "auto"],
    ],
)
def test_run_mode_auto(argv):
    assert run_mode(argv) == "auto"


@pytest.mark.parametrize(
    "argv",
    [
        [*RUN, "CBS-1"],
        [*RUN, "CBS-1", "--arg", "x=auto"],
        [*RUN, "CBS-1", "--quota", "codexbar"],
        [*RUN, "CBS-1", "--require-quota", "20"],
        [*RUN, "CBS-1", "--require-quota", "20:weekly", "--non-interactive"],
        [*RUN, "CBS-1", "--budget", "5"],
        ["run", "norn/pipelines/fix_jira_issue_v3.py", "CBS-1"],
    ],
)
def test_run_mode_interactive(argv):
    assert run_mode(argv) == "interactive"


def test_run_mode_require_quota_without_value_keeps_next_positional():
    assert run_mode([*RUN, "--require-quota", "auto", "CBS-1"]) == "auto"


@pytest.mark.parametrize("token", ["Auto", "autonomous"])
def test_run_mode_rejects_unknown_positional(token):
    with pytest.raises(ValueError, match=token):
        run_mode([*RUN, "CBS-1", token])


# ---------------------------------------------------------------------------
# paths and knobs
# ---------------------------------------------------------------------------


def test_paths_are_absolute_and_under_art_dir(tmp_path):
    art = str(tmp_path / "tmp" / "jira" / "CBS-1")
    paths = V3Paths(art, "CBS-1")
    props = [n for n, v in vars(V3Paths).items() if isinstance(v, property)]
    assert len(props) > 25
    for name in props:
        value = getattr(paths, name)
        assert Path(value).is_absolute(), name
        assert value == art or value.startswith(art + "/"), name
    assert paths.approval.endswith("CBS-1-plan-approval.md")
    assert paths.fix_pre.endswith("/v3/fix/pre.json")
    assert paths.pr_state == f"{art}/v3/pr"


@pytest.mark.parametrize("raw", ["0", "-1", "x"])
def test_positive_int_knob_rejects(raw):
    with pytest.raises(ValueError, match="--arg rounds"):
        positive_int_knob({"rounds": raw}, "rounds", 2)


def test_positive_int_knob_default_and_value():
    assert positive_int_knob({}, "rounds", 2) == 2
    assert positive_int_knob({"rounds": "4"}, "rounds", 2) == 4


def test_on_off_knob():
    with pytest.raises(ValueError, match="--arg learn"):
        on_off_knob({"learn": "maybe"}, "learn", True)
    assert on_off_knob({}, "learn", True) is True
    assert on_off_knob({"learn": "off"}, "learn", True) is False


# ---------------------------------------------------------------------------
# git helpers
# ---------------------------------------------------------------------------


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@x", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _commit(repo, name):
    (repo / name).write_text(name)
    _git(repo, "add", name)
    _git(repo, "commit", "-m", f"add {name}")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "develop", str(origin)], check=True,
                   capture_output=True)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "develop")
    _git(work, "remote", "add", "origin", str(origin))
    _commit(work, "a.txt")
    _git(work, "push", "origin", "develop")
    _git(work, "switch", "-c", "feat/x")
    return work


# ---------------------------------------------------------------------------
# RegisterChildState
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_child_state(repo):
    stage = RegisterChildState(str(repo), ["implement_features_v3", "check_and_fix_ci_v2"])
    ctx = PipelineContext()
    result = await stage.run(ctx)
    assert result.success
    text = (repo / ".git" / "info" / "exclude").read_text()
    for line in (
        "/implement_features_v3.checkpoint",
        "/implement_features_v3.runs/",
        "/check_and_fix_ci_v2.checkpoint",
        "/check_and_fix_ci_v2.runs/",
    ):
        assert line in text.splitlines()
    assert "implement_features_v3.checkpoint" in result.output
    again = await stage.run(ctx)
    assert again.success and again.output == ""
    assert (repo / ".git" / "info" / "exclude").read_text() == text


# ---------------------------------------------------------------------------
# run identity
# ---------------------------------------------------------------------------


def _ctx(identity):
    ctx = PipelineContext()
    ctx.results["record run identity"] = StageResult(
        name="record run identity", success=True, output=identity
    )
    return ctx


def _identity_stage(repo, key="CBS-1"):
    base = repo / "base.sha"
    return CheckRunIdentity(key, str(repo), str(base)), base


@pytest.mark.asyncio
async def test_record_run_identity(repo):
    result = await RecordRunIdentity("CBS-1", str(repo)).run(PipelineContext())
    assert result.output == {"issue": "CBS-1", "branch": "feat/x"}


@pytest.mark.asyncio
async def test_record_run_identity_detached(repo):
    _git(repo, "switch", "--detach")
    result = await RecordRunIdentity("CBS-1", str(repo)).run(PipelineContext())
    assert not result.success and "detached" in result.error.splitlines()[0]


@pytest.mark.asyncio
async def test_identity_fresh_run_passes(repo):
    stage, base = _identity_stage(repo)
    base.write_text(_git(repo, "rev-parse", "HEAD"))
    ident = (await RecordRunIdentity("CBS-1", str(repo)).run(PipelineContext())).output
    assert (await stage.run(_ctx(ident))).success


@pytest.mark.asyncio
async def test_identity_other_issue_fails(repo):
    stage, base = _identity_stage(repo)
    base.write_text(_git(repo, "rev-parse", "HEAD"))
    result = await stage.run(_ctx({"issue": "CBS-9", "branch": "feat/x"}))
    assert not result.success
    assert "CBS-9" in result.error.splitlines()[0]
    assert "without --resume" in result.error


@pytest.mark.asyncio
async def test_identity_missing_record_fails(repo):
    stage, base = _identity_stage(repo)
    base.write_text(_git(repo, "rev-parse", "HEAD"))
    result = await stage.run(PipelineContext())
    assert not result.success


@pytest.mark.asyncio
async def test_identity_branch_moved_fails(repo):
    stage, base = _identity_stage(repo)
    base.write_text(_git(repo, "rev-parse", "HEAD"))
    _git(repo, "switch", "-c", "other")
    result = await stage.run(_ctx({"issue": "CBS-1", "branch": "feat/x"}))
    assert not result.success
    assert "other" in result.error and "feat/x" in result.error


@pytest.mark.asyncio
async def test_identity_base_not_ancestor_fails(repo):
    stage, base = _identity_stage(repo)
    _git(repo, "switch", "-c", "side")
    side = _commit(repo, "side.txt")
    _git(repo, "switch", "feat/x")
    base.write_text(side)
    result = await stage.run(_ctx({"issue": "CBS-1", "branch": "feat/x"}))
    assert not result.success
    assert "not an ancestor" in result.error


@pytest.mark.asyncio
async def test_identity_stale_base_fails(repo):
    stage, base = _identity_stage(repo)
    base.write_text(_git(repo, "rev-parse", "HEAD"))
    _commit(repo, "b.txt")
    _git(repo, "push", "origin", "feat/x:develop")
    result = await stage.run(_ctx({"issue": "CBS-1", "branch": "feat/x"}))
    assert not result.success
    first = result.error.splitlines()[0]
    assert "base.sha is stale" in first and "origin/develop" in first
    assert "tmp/jira/CBS-1/base.sha" in result.error


@pytest.mark.asyncio
async def test_identity_pushed_branch_passes(repo):
    stage, base = _identity_stage(repo)
    base.write_text(_git(repo, "rev-parse", "HEAD"))
    _commit(repo, "b.txt")
    _git(repo, "push", "origin", "feat/x")
    result = await stage.run(_ctx({"issue": "CBS-1", "branch": "feat/x"}))
    assert result.success, result.error


# ---------------------------------------------------------------------------
# notify
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_uses_pipeline_name(monkeypatch):
    sent = []

    async def fake_fire(self, msg):
        sent.append(msg)

    monkeypatch.setattr(AlertManager, "fire", fake_fire)
    await notify("PR ready", "CBS-1 is ready")
    assert len(sent) == 1
    assert sent[0].pipeline_name == "fix_jira_issue_v3"
    assert sent[0].title == "PR ready"
    assert "CBS-1 is ready" in sent[0].body
    assert v3.PIPELINE == "fix_jira_issue_v3"


# ---------------------------------------------------------------------------
# fix_issue_v1 additions: pipeline name, run kind, base hint, prefixes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_accepts_another_pipeline_name(monkeypatch):
    sent = []

    async def fake_fire(self, msg):
        sent.append(msg)

    monkeypatch.setattr(AlertManager, "fire", fake_fire)
    await notify("PR ready", "x", pipeline="fix_issue_v1")
    assert len(sent) == 1
    assert sent[0].pipeline_name == "fix_issue_v1"


@pytest.mark.asyncio
async def test_record_run_identity_kind(repo):
    result = await RecordRunIdentity("acme/app#123", str(repo), kind="github").run(PipelineContext())
    assert result.output == {"issue": "acme/app#123", "branch": "feat/x", "kind": "github"}


@pytest.mark.asyncio
async def test_record_run_identity_without_kind_has_two_keys(repo):
    result = await RecordRunIdentity("CBS-1", str(repo)).run(PipelineContext())
    assert set(result.output) == {"issue", "branch"}


@pytest.mark.asyncio
async def test_identity_stale_base_names_the_base_hint(repo):
    base = repo / "base.sha"
    hint = "/abs/tmp/github/123/base.sha"
    stage = CheckRunIdentity("CBS-1", str(repo), str(base), base_hint=hint)
    base.write_text(_git(repo, "rev-parse", "HEAD"))
    _commit(repo, "b.txt")
    _git(repo, "push", "origin", "feat/x:develop")
    result = await stage.run(_ctx({"issue": "CBS-1", "branch": "feat/x"}))
    assert not result.success
    assert result.error.splitlines()[-1] == f"Delete {hint} and start from a fresh branch."
    assert "tmp/jira" not in result.error


@pytest.mark.asyncio
async def test_identity_stale_base_without_hint_keeps_the_jira_text(repo):
    stage, base = _identity_stage(repo)
    base.write_text(_git(repo, "rev-parse", "HEAD"))
    _commit(repo, "b.txt")
    _git(repo, "push", "origin", "feat/x:develop")
    result = await stage.run(_ctx({"issue": "CBS-1", "branch": "feat/x"}))
    assert result.error.splitlines()[-1] == (
        "Delete tmp/jira/CBS-1/base.sha and start from a fresh branch."
    )


def test_subject_problems_with_a_same_repo_prefix():
    assert v3.subject_problems("#123: add x", "#123") == []
    for bad in ("#1234: add x", "#123:add x", "#123: Add x", "123: add x"):
        assert v3.subject_problems(bad, "#123"), bad


def test_subject_problems_with_an_owner_repo_prefix():
    key = "acme/my.repo#5"
    assert v3.subject_problems(f"{key}: add x", key) == []
    assert v3.subject_problems("acme/myXrepo#5: add x", key)
    assert v3.subject_problems(f"{key}: " + "a" * (73 - len(key) - 2), key)

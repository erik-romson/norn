"""Tests for norn/pipelines/_commit_rewrite_v3.py (real git in tmp_path)."""
from __future__ import annotations

import ast
import json
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from norn.pipelines import _commit_rewrite_v3 as rewrite
from norn.pipelines import _fix_jira_v3

SCRIPT = Path(__file__).resolve().parents[1] / "norn" / "pipelines" / "_commit_rewrite_v3.py"
KEY = "CBS-1"
EMAIL = "dev@example.com"

GROUP_1 = "CBS-1: change the greeting files"
GROUP_2 = "CBS-1: add the dir files\n\nThe two files live under dir/."
FILES_1 = ["modified.txt", "new.txt", "deleted.txt"]
FILES_2 = ["dir/ø.txt", "dir/[id].tsx"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str, author: str | None = None) -> str:
    cmd = ["git", *args]
    if author is not None:
        cmd = ["git", *args[:1], f"--author={author}", *args[1:]]
    return subprocess.run(
        cmd, cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, cwd=cwd,
    )


def _commit(repo: Path, message: str, author: str | None = None) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message, author=author)
    return _git(repo, "rev-parse", "HEAD")


@dataclass
class Fixture:
    repo: Path
    base: str
    head: str
    start: str
    tree: str
    plan: Path

    def args(self, cmd: str, *, plan: Path | None = None, expect_head: str | None = None) -> list[str]:
        return [
            cmd,
            "--plan", str(plan or self.plan),
            "--repo", str(self.repo),
            "--base", self.base,
            "--key", KEY,
            "--expect-head", expect_head or self.head,
        ]

    def commits(self) -> list[str]:
        return _git(self.repo, "rev-list", "--reverse", f"{self.base}..HEAD").split()


def _init(tmp_path: Path) -> tuple[Path, str]:
    """A repo with a base commit on main and the branch CBS-1 checked out."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", EMAIL)
    _git(repo, "config", "user.name", "Dev Eloper")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "core.hooksPath", str(repo / ".git" / "hooks"))
    (repo / "README.md").write_text("# readme\n")
    (repo / "modified.txt").write_text("one\n")
    (repo / "deleted.txt").write_text("going away\n")
    base = _commit(repo, "initial")
    _git(repo, "switch", "-q", "-c", "CBS-1")
    return repo, base


def plan_text(
    start: str,
    head: str,
    groups: list[tuple[str, list[str]]],
    hunks: dict[int, list[str]] | None = None,
) -> str:
    """A plan in the split-commit-plan skill's format."""
    hunks = hunks or {}
    lines = [
        f"# Split plan for {start[:7]}..{head[:7]}",
        "",
        f"- **Start commit (inclusive):** `{start}`",
        f"- **HEAD at plan time:**        `{head}`",
        f"- **Base parent:**              `{start[:7]}^  = (parent)`",
        "- **Branch:**                   `CBS-1`",
        "",
        "## Proposed commits (in order)",
        "",
        "| # | Jira | Subject | Files | Hunks |",
        "|---|------|---------|-------|-------|",
        "| 1 | CBS-1 | change the greeting files | 3 | 7 |",
        "",
        "---",
        "",
    ]
    for n, (message, files) in enumerate(groups, 1):
        lines += [
            f"## Group {n} — {message.splitlines()[0]}",
            "",
            "**Guidelines cited:** #2 (Jira key)",
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
            "**Hunks (partial-file):**",
            *hunks.get(n, []),
            "",
            "---",
            "",
        ]
    return "\n".join(lines)


def _fixture(tmp_path: Path, groups: list[tuple[str, list[str]]] | None = None) -> Fixture:
    """Three commits touching five files: modified, new, deleted, dir/ø.txt, dir/[id].tsx."""
    repo, base = _init(tmp_path)
    (repo / "modified.txt").write_text("one\ntwo\n")
    (repo / "new.txt").write_text("new\n")
    _commit(repo, "refactor: step-01-a — A")
    (repo / "deleted.txt").unlink()
    (repo / "dir").mkdir()
    (repo / "dir" / "ø.txt").write_text("ø\n")
    _commit(repo, "refactor: step-02-b — B")
    (repo / "dir" / "[id].tsx").write_text("export default 1\n")
    (repo / "new.txt").write_text("new\nnewer\n")
    head = _commit(repo, "refactor: step-03-c — C")
    start = _git(repo, "rev-list", "--reverse", f"{base}..HEAD").split()[0]
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    plan = tmp_path / "plan.md"
    plan.write_text(plan_text(start, head, groups or [(GROUP_1, FILES_1), (GROUP_2, FILES_2)]))
    return Fixture(repo=repo, base=base, head=head, start=start, tree=tree, plan=plan)


def _write_plan(fx: Fixture, name: str, text: str) -> Path:
    path = fx.repo.parent / name
    path.write_text(text)
    return path


def _hook(repo: Path, name: str, body: str) -> Path:
    path = repo / ".git" / "hooks" / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _backup_refs(repo: Path) -> list[str]:
    return _git(repo, "for-each-ref", "--format=%(refname)", "refs/backup/").split()


def _backup_branches(repo: Path) -> list[str]:
    return _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/backup/").split()


# ---------------------------------------------------------------------------
# range
# ---------------------------------------------------------------------------


def test_range_prints_start_head_tree_and_count(tmp_path):
    fx = _fixture(tmp_path)
    r = _run("range", "--repo", str(fx.repo), "--base", fx.base, "--key", KEY)
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(r.stdout) == {
        "start": fx.start, "head": fx.head, "tree": fx.tree, "count": 3,
    }


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


def test_apply_regroups_the_range_into_the_plan_commits(tmp_path):
    fx = _fixture(tmp_path)

    r = _run(*fx.args("check"))
    assert r.returncode == 0, r.stdout + r.stderr

    r = _run(*fx.args("apply"))
    assert r.returncode == 0, r.stdout + r.stderr

    commits = fx.commits()
    assert len(commits) == 2
    messages = [_git(fx.repo, "log", "-1", "--format=%B", sha) for sha in commits]
    assert messages == [GROUP_1, GROUP_2]
    assert _git(fx.repo, "show", "--name-only", "--format=", commits[0]).splitlines() == sorted(
        FILES_1
    )
    assert _git(fx.repo, "rev-parse", "HEAD^{tree}") == fx.tree

    refs = _backup_refs(fx.repo)
    assert len(refs) == 1
    assert refs[0].startswith(f"refs/backup/split-{fx.start[:7]}-{fx.head[:7]}-")
    assert _git(fx.repo, "rev-parse", f"{refs[0]}^{{tree}}") == fx.tree
    branches = _backup_branches(fx.repo)
    assert len(branches) == 1
    assert re.fullmatch(r"backup/CBS-1-\d{8}", branches[0])
    assert _git(fx.repo, "rev-parse", branches[0]) == fx.head
    assert f"backup branch: {branches[0]}" in r.stdout
    assert f"backup ref: {refs[0]}" in r.stdout
    assert _git(fx.repo, "status", "--porcelain", "--untracked-files=all") == ""


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def test_check_names_a_file_left_out(tmp_path):
    fx = _fixture(tmp_path, [(GROUP_1, FILES_1), (GROUP_2, ["dir/ø.txt"])])
    r = _run(*fx.args("check"))
    assert r.returncode == 1
    assert "`dir/[id].tsx` is in no group" in r.stdout


def test_check_names_a_file_listed_twice(tmp_path):
    fx = _fixture(tmp_path, [(GROUP_1, FILES_1), (GROUP_2, [*FILES_2, "new.txt"])])
    r = _run(*fx.args("check"))
    assert r.returncode == 1
    assert "`new.txt` is listed more than once (groups 1, 2)" in r.stdout


def test_check_names_a_listed_file_outside_the_diff(tmp_path):
    fx = _fixture(tmp_path, [(GROUP_1, [*FILES_1, "README.md"]), (GROUP_2, FILES_2)])
    r = _run(*fx.args("check"))
    assert r.returncode == 1
    assert "lists `README.md`, which base..HEAD does not change" in r.stdout


def test_check_refuses_a_hunk_bullet(tmp_path):
    fx = _fixture(tmp_path)
    plan = _write_plan(
        fx,
        "hunks.md",
        plan_text(
            fx.start,
            fx.head,
            [(GROUP_1, FILES_1), (GROUP_2, FILES_2)],
            hunks={2: ["- `new.txt` @@ -1,0 +2,1 @@"]},
        ),
    )
    r = _run(*fx.args("check", plan=plan))
    assert r.returncode == 1
    assert "group 2: hunk bullets are refused" in r.stdout


def test_check_ignores_the_proposed_commits_table_hunk_column(tmp_path):
    # The table's Hunks column holds a count; only bullets under the label count.
    fx = _fixture(tmp_path)
    assert "| 7 |" in fx.plan.read_text()
    assert _run(*fx.args("check")).returncode == 0


def test_check_refuses_a_start_commit_one_commit_late(tmp_path):
    fx = _fixture(tmp_path)
    second = fx.commits()[1]
    plan = _write_plan(
        fx, "late.md", plan_text(second, fx.head, [(GROUP_1, FILES_1), (GROUP_2, FILES_2)])
    )
    r = _run(*fx.args("check", plan=plan))
    assert r.returncode == 1
    assert f"the plan's Start commit is {second}" in r.stdout


def test_check_refuses_a_plan_head_other_than_expect_head(tmp_path):
    fx = _fixture(tmp_path)
    other = fx.commits()[1]
    plan = _write_plan(
        fx, "head.md", plan_text(fx.start, other, [(GROUP_1, FILES_1), (GROUP_2, FILES_2)])
    )
    r = _run(*fx.args("check", plan=plan))
    assert r.returncode == 1
    assert f"the plan's HEAD at plan time is {other}" in r.stdout


def test_check_refuses_when_head_moved_after_the_plan(tmp_path):
    fx = _fixture(tmp_path)
    (fx.repo / "late.txt").write_text("late\n")
    _commit(fx.repo, "CBS-1: add a late file")
    r = _run(*fx.args("check"))
    assert r.returncode == 1
    assert r.stdout.splitlines()[0].startswith("HEAD moved after the plan")


def test_check_refuses_a_merge_in_the_range(tmp_path):
    fx = _fixture(tmp_path)
    _git(fx.repo, "switch", "-q", "-c", "side", fx.base)
    (fx.repo / "side.txt").write_text("side\n")
    _commit(fx.repo, "side work")
    _git(fx.repo, "switch", "-q", "CBS-1")
    _git(fx.repo, "merge", "-q", "--no-ff", "--no-edit", "side")
    head = _git(fx.repo, "rev-parse", "HEAD")
    r = _run(*fx.args("check", expect_head=head))
    assert r.returncode == 1
    assert "rebase onto base.sha to remove merge commits, then --resume" in r.stdout


def test_check_refuses_a_net_zero_range(tmp_path):
    repo, base = _init(tmp_path)
    (repo / "modified.txt").write_text("changed\n")
    start = _commit(repo, "refactor: step-01-a — A")
    (repo / "extra.txt").write_text("extra\n")
    _commit(repo, "refactor: step-02-b — B")
    (repo / "modified.txt").write_text("one\n")
    (repo / "extra.txt").unlink()
    head = _commit(repo, "refactor: step-03-c — C")
    plan = tmp_path / "plan.md"
    plan.write_text(plan_text(start, head, [(GROUP_1, ["modified.txt"])]))
    r = _run(
        "check", "--plan", str(plan), "--repo", str(repo), "--base", base,
        "--key", KEY, "--expect-head", head,
    )
    assert r.returncode == 1
    assert "nothing to ship" in r.stdout


def test_check_refuses_a_commit_by_another_author(tmp_path):
    fx = _fixture(tmp_path)
    (fx.repo / "new.txt").write_text("someone else\n")
    other = _commit(fx.repo, "refactor: step-04-d — D", author="Other <other@example.com>")
    r = _run(*fx.args("check", expect_head=other))
    assert r.returncode == 1
    assert f"the rewrite would re-author {other[:7]} by other@example.com" in r.stdout


def test_check_refuses_a_detached_head(tmp_path):
    fx = _fixture(tmp_path)
    _git(fx.repo, "switch", "-q", "--detach", "HEAD")
    r = _run(*fx.args("check"))
    assert r.returncode == 1
    assert "HEAD is detached" in r.stdout


def test_check_refuses_a_root_start_commit_as_a_start_mismatch(tmp_path):
    fx = _fixture(tmp_path)
    root = _git(fx.repo, "rev-list", "--max-parents=0", "HEAD")
    plan = _write_plan(
        fx, "root.md", plan_text(root, fx.head, [(GROUP_1, FILES_1), (GROUP_2, FILES_2)])
    )
    r = _run(*fx.args("check", plan=plan))
    assert r.returncode == 1
    assert f"the plan's Start commit is {root}, but the first commit of base..HEAD is {fx.start}" in r.stdout


def test_apply_works_from_a_subdirectory(tmp_path):
    fx = _fixture(tmp_path)
    r = _run(*fx.args("check"), cwd=fx.repo / "dir")
    assert r.returncode == 0, r.stdout + r.stderr
    r = _run(*fx.args("apply"), cwd=fx.repo / "dir")
    assert r.returncode == 0, r.stdout + r.stderr
    assert len(fx.commits()) == 2
    assert _git(fx.repo, "rev-parse", "HEAD^{tree}") == fx.tree


def test_apply_restores_a_file_the_range_added_and_now_ignores(tmp_path):
    repo, base = _init(tmp_path)
    (repo / "out.log").write_text("generated\n")
    start = _commit(repo, "refactor: step-01-a — A")
    (repo / ".gitignore").write_text("*.log\n")
    head = _commit(repo, "refactor: step-02-b — B")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    plan = tmp_path / "plan.md"
    plan.write_text(
        plan_text(start, head, [(GROUP_1, [".gitignore"]), ("CBS-1: add the log", ["out.log"])])
    )
    r = _run(
        "apply", "--plan", str(plan), "--repo", str(repo), "--base", base,
        "--key", KEY, "--expect-head", head,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert _git(repo, "rev-parse", "HEAD^{tree}") == tree
    assert _git(repo, "ls-files", "out.log") == "out.log"


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


def test_a_commit_msg_hook_failure_stops_apply_and_the_restore_line_recovers(tmp_path):
    fx = _fixture(tmp_path)
    hook = _hook(
        fx.repo,
        "commit-msg",
        'if grep -q "add the dir files" "$1"; then echo "commit-msg: rejected" >&2; exit 1; fi\n'
        "exit 0\n",
    )

    r = _run(*fx.args("apply"))
    assert r.returncode == 1
    first = r.stdout.splitlines()[0]
    assert first.startswith("restore: git reset --hard refs/backup/split-")
    ref = first.split()[-1]
    assert "commit-msg: rejected" in r.stdout
    assert _git(fx.repo, "rev-parse", ref) == fx.head

    r = _run(*fx.args("check"))
    assert r.returncode == 1
    assert r.stdout.splitlines() == [
        f"a previous apply stopped midway; restore: git reset --hard {ref}"
    ]

    _git(fx.repo, "reset", "-q", "--hard", ref)
    hook.unlink()
    r = _run(*fx.args("apply"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert len(fx.commits()) == 2
    assert _git(fx.repo, "rev-parse", "HEAD^{tree}") == fx.tree
    assert len(_backup_branches(fx.repo)) == 2


def test_a_pre_commit_hook_that_rewrites_a_staged_file_is_tree_drift(tmp_path):
    fx = _fixture(tmp_path)
    _hook(
        fx.repo,
        "pre-commit",
        "if git diff --cached --name-only | grep -qx modified.txt; then\n"
        "  echo reformatted >> modified.txt\n"
        "  git add modified.txt\n"
        "fi\n",
    )
    r = _run(*fx.args("apply"))
    assert r.returncode == 1
    lines = r.stdout.splitlines()
    assert lines[0].startswith("restore: git reset --hard refs/backup/split-")
    assert "differs from the backup tree" in lines[1]
    assert "modified.txt" in r.stdout


def test_a_pre_commit_hook_that_dirties_an_unstaged_tracked_file_fails_step_6(tmp_path):
    fx = _fixture(tmp_path)
    _hook(fx.repo, "pre-commit", "echo touched >> README.md\nexit 0\n")
    r = _run(*fx.args("apply"))
    assert r.returncode == 1
    lines = r.stdout.splitlines()
    assert lines[0].startswith("restore: git reset --hard refs/backup/split-")
    assert lines[1:] == ["a hook changed files outside the commits:", "  README.md"]
    # The committed tree still matches: only the work tree is dirty.
    assert _git(fx.repo, "rev-parse", "HEAD^{tree}") == fx.tree


def test_a_post_commit_hook_that_leaves_an_untracked_file_fails_step_6(tmp_path):
    fx = _fixture(tmp_path)
    _hook(fx.repo, "post-commit", "echo ran >> hook.log\nexit 0\n")
    r = _run(*fx.args("apply"))
    assert r.returncode == 1
    lines = r.stdout.splitlines()
    assert lines[0].startswith("restore: git reset --hard refs/backup/split-")
    assert lines[1] == "a hook changed files outside the commits:"
    assert lines[2].startswith("untracked")
    assert lines[3:] == ["  hook.log"]


def test_apply_refuses_an_untracked_file_before_any_backup(tmp_path):
    fx = _fixture(tmp_path)
    (fx.repo / "stray.txt").write_text("stray\n")
    r = _run(*fx.args("apply"))
    assert r.returncode == 1
    assert "commit, ignore or delete them, then [r]etry" in r.stdout
    assert "  stray.txt" in r.stdout.splitlines()
    assert _backup_branches(fx.repo) == []
    assert _backup_refs(fx.repo) == []
    assert _git(fx.repo, "rev-parse", "HEAD") == fx.head


# ---------------------------------------------------------------------------
# messages and summary
# ---------------------------------------------------------------------------


def test_messages_flags_attribution_and_attribution_only_ignores_subjects(tmp_path):
    fx = _fixture(tmp_path)
    (fx.repo / "new.txt").write_text("session\n")
    tagged = _commit(fx.repo, "CBS-1: tidy the new file\n\nClaude-Session: x")
    rng = f"{fx.base}..HEAD"

    r = _run("messages", "--repo", str(fx.repo), "--key", KEY, "--range", rng)
    assert r.returncode == 1
    assert f"{tagged[:7]} attribution line: 'Claude-Session: x'" in r.stdout.splitlines()
    assert "refactor: step-01-a" in r.stdout

    r = _run("messages", "--repo", str(fx.repo), "--key", KEY, "--range", rng, "--attribution-only")
    assert r.returncode == 1
    assert r.stdout.splitlines() == [f"{tagged[:7]} attribution line: 'Claude-Session: x'"]


def test_messages_attribution_only_passes_refactor_subjects(tmp_path):
    fx = _fixture(tmp_path)
    r = _run(
        "messages", "--repo", str(fx.repo), "--key", KEY,
        "--range", f"{fx.base}..HEAD", "--attribution-only",
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout == ""


def test_summary_prints_one_line_per_group(tmp_path):
    fx = _fixture(tmp_path)
    r = _run("summary", "--plan", str(fx.plan))
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.splitlines() == [
        "1. CBS-1: change the greeting files (3 files)",
        "2. CBS-1: add the dir files (2 files)",
    ]


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------


def test_usage_errors_exit_2(tmp_path):
    fx = _fixture(tmp_path)
    common = ["--repo", str(fx.repo), "--base", fx.base, "--key", KEY]
    cases = [
        [],
        ["bogus"],
        ["check", *common, "--expect-head", fx.head],  # no --plan
        ["apply", "--plan", str(fx.plan), *common],  # no --expect-head
        ["check", "--plan", str(fx.plan), *common, "--expect-head", fx.head[:7]],
        ["summary", "--plan", str(tmp_path / "missing.md")],
        ["range", "--repo", str(fx.repo / "dir"), "--base", fx.base, "--key", KEY],
        ["messages", "--repo", str(fx.repo), "--key", KEY, "--range", "HEAD"],
    ]
    for case in cases:
        r = _run(*case)
        assert r.returncode == 2, (case, r.stdout, r.stderr)


# ---------------------------------------------------------------------------
# Guard: the script's local copy of the house style agrees with _fix_jira_v3
# ---------------------------------------------------------------------------

_MESSAGES = [
    "CBS-1: add the export",
    "CBS-1: Add the export",
    "refactor: step-01-a — A",
    "CBS-1: add the export.",
    "CBS-1: " + "x" * 70,
    "CBS-1: add the export\nbody on line two",
    "CBS-1: add the export\n\none\ntwo\nthree\nfour\nfive",
    "CBS-1: add the export\n\nCo-Authored-By: Someone <s@example.com>",
    "CBS-1: add the export\n\nClaude-Session: x",
    "CBS-1: add the export\n\n\U0001F916 Generated with [Claude Code](https://example.com)",
    "",
    "CBS-12: add the export",
]


def test_local_subject_rules_agree_with_fix_jira_v3():
    assert len(_MESSAGES) == 12
    assert rewrite.STRICT_ATTRIBUTION_ERE == _fix_jira_v3.STRICT_ATTRIBUTION_ERE
    for message in _MESSAGES:
        assert rewrite.subject_problems(message, KEY) == _fix_jira_v3.subject_problems(
            message, KEY
        ), message
        assert rewrite.attribution_lines(message) == _fix_jira_v3.attribution_lines(message), message
    # The table exercises both outcomes, not just agreement on clean input.
    assert rewrite.subject_problems(_MESSAGES[0], KEY) == []
    assert all(rewrite.subject_problems(m, KEY) for m in _MESSAGES[1:])


def test_the_script_imports_nothing_from_norn():
    tree = ast.parse(SCRIPT.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("norn"), node.module
        elif isinstance(node, ast.Import):
            assert not any(alias.name.startswith("norn") for alias in node.names)


_GITHUB_CASES = [
    ("#123", "#123: add x"),
    ("#123", "#1234: add x"),
    ("#123", "#123:add x"),
    ("#123", "#123: Add x"),
    ("#123", "123: add x"),
    ("acme/my.repo#5", "acme/my.repo#5: add x"),
    ("acme/my.repo#5", "acme/myXrepo#5: add x"),
    ("acme/my.repo#5", "acme/my.repo#5: " + "a" * (73 - len("acme/my.repo#5: "))),
]


def test_local_subject_rules_agree_for_github_prefixes():
    for prefix, message in _GITHUB_CASES:
        assert rewrite.subject_problems(message, prefix) == _fix_jira_v3.subject_problems(
            message, prefix
        ), message
    assert len(_GITHUB_CASES[-1][1]) == 73
    for prefix, message in (_GITHUB_CASES[0], _GITHUB_CASES[5]):
        assert rewrite.subject_problems(message, prefix) == []
    for prefix, message in _GITHUB_CASES[1:5] + _GITHUB_CASES[6:]:
        assert rewrite.subject_problems(message, prefix), message

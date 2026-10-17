"""Tests for norn/pipelines/_comment_style.py."""
from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

import pytest

from tests.merge_fakes import make_repo_with_remote

from norn.pipelines._comment_style import (
    ATTRIBUTION_ERE,
    attribution_guard_sh,
)

POSITIVE_CASES = [
    "Co-Authored-By: Claude <noreply@anthropic.com>",
    "🤖 Generated with [Claude Code](https://claude.com/claude-code)",
]

NEGATIVE_CASES = [
    "from norn.agents.claude_code import X",
    "the Claude provider",
    "anthropic_api_key",
]


# ---------------------------------------------------------------------------
# ATTRIBUTION_ERE — Python re
# ---------------------------------------------------------------------------


def test_ere_matches_positive_cases_in_python():
    compiled = re.compile(ATTRIBUTION_ERE, re.I)
    for text in POSITIVE_CASES:
        assert compiled.search(text), text


def test_ere_does_not_match_negative_cases_in_python():
    compiled = re.compile(ATTRIBUTION_ERE, re.I)
    for text in NEGATIVE_CASES:
        assert not compiled.search(text), text


# ---------------------------------------------------------------------------
# ATTRIBUTION_ERE — grep -iE agrees with Python's re
# ---------------------------------------------------------------------------


def _grep_matches(text: str) -> bool:
    result = subprocess.run(
        ["sh", "-c", f"grep -iE {shlex.quote(ATTRIBUTION_ERE)}"],
        input=text,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def test_grep_and_python_re_agree_on_every_case():
    compiled = re.compile(ATTRIBUTION_ERE, re.I)
    for text in POSITIVE_CASES + NEGATIVE_CASES:
        assert _grep_matches(text) == bool(compiled.search(text)), text


# ---------------------------------------------------------------------------
# attribution_guard_sh() — staged diff
# ---------------------------------------------------------------------------


def _init_repo(tmp_path: Path) -> Path:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "erik@zenior.no"], cwd=repo_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "Erik"], cwd=repo_path, check=True)
    (repo_path / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial commit"], cwd=repo_path, check=True, capture_output=True
    )
    return repo_path


def test_guard_exits_1_on_staged_attribution_line(tmp_path):
    repo_path = _init_repo(tmp_path)
    (repo_path / "README.md").write_text(
        "hello\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n"
    )
    subprocess.run(["git", "add", "README.md"], cwd=repo_path, check=True)

    result = subprocess.run(
        ["sh", "-c", attribution_guard_sh()], cwd=repo_path, capture_output=True, text=True
    )
    assert result.returncode == 1
    assert "ERROR: assistant attribution; refusing to commit" in result.stderr
    assert "Co-Authored-By: Claude" in result.stderr


def test_guard_exits_0_on_clean_staged_diff(tmp_path):
    repo_path = _init_repo(tmp_path)
    (repo_path / "README.md").write_text("hello\n\nan ordinary change\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo_path, check=True)

    result = subprocess.run(
        ["sh", "-c", attribution_guard_sh()], cwd=repo_path, capture_output=True, text=True
    )
    assert result.returncode == 0


@pytest.mark.parametrize("messages_range", [None, "HEAD~1..HEAD"])
def test_passing_guard_continues_an_and_chain(tmp_path, messages_range):
    """Callers write `<guard> && git commit`; a passing guard must reach the commit."""
    repo_path = _init_repo(tmp_path)
    (repo_path / "README.md").write_text("hello\n\nan ordinary change\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo_path, check=True)
    subprocess.run(["git", "commit", "-qm", "ordinary"], cwd=repo_path, check=True)

    cmd = f"{attribution_guard_sh(messages_range=messages_range)} && echo chained"
    result = subprocess.run(["sh", "-c", cmd], cwd=repo_path, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "chained"


# ---------------------------------------------------------------------------
# attribution_guard_sh(messages_range=...) — commit message range
# ---------------------------------------------------------------------------


def test_guard_checks_messages_range_and_ignores_pushed_commits(tmp_path):
    remote = make_repo_with_remote(tmp_path)
    repo = remote.repo
    repo_path = Path(remote.path)
    repo.git.branch("--set-upstream-to=origin/main", "main")

    (repo_path / "note.txt").write_text("note\n")
    repo.index.add(["note.txt"])
    repo.index.commit(
        "add note\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n"
    )

    guard = attribution_guard_sh(messages_range="@{u}..HEAD")

    result = subprocess.run(
        ["sh", "-c", guard], cwd=repo_path, capture_output=True, text=True
    )
    assert result.returncode == 1
    assert "ERROR: assistant attribution; refusing to commit" in result.stderr

    repo.remotes.origin.push("main")

    result_after_push = subprocess.run(
        ["sh", "-c", guard], cwd=repo_path, capture_output=True, text=True
    )
    assert result_after_push.returncode == 0

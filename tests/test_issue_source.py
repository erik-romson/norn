"""Tests for norn/pipelines/_issue_source.py -- the per-source values of fix_issue_v1."""
from __future__ import annotations

import dataclasses
import hashlib
import importlib
import json
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from norn.pipelines import _github_issue, _issue_source
from norn.pipelines._issue_source import IssueSource, resolve_issue

from tests.merge_fakes import install_fake_gh

ORIGIN = "https://github.com/acme/app.git"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path) -> str:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "remote", "add", "origin", ORIGIN)
    return os.path.realpath(root)


def _resolve(argv, repo, args=None, environ=None):
    return resolve_issue(list(argv), repo, args or {}, environ if environ is not None else {})


def _src(token, repo, **kw) -> IssueSource:
    return _resolve([token], repo, **kw)[0]


# ---------------------------------------------------------------------------
# The per-source table
# ---------------------------------------------------------------------------

SAME_REPO_FORMS = [
    "123",
    "#123",
    "acme/app#123",
    "https://github.com/acme/app/issues/123#issuecomment-1",
]


@pytest.mark.parametrize("token", SAME_REPO_FORMS)
def test_github_same_repo_column(repo, token):
    s = _src(token, repo)
    d = Path(repo) / "tmp" / "github" / "123"
    assert (s.kind, s.display, s.identity) == ("github", "#123", "acme/app#123")
    assert (s.file_key, s.subject_prefix, s.resume_arg) == ("issue-123", "#123", "123")
    assert s.art.dir == f"{d}/"
    assert s.art.issue_json == f"{d}/issue.json"
    assert s.art.issue_md == f"{d}/issue.md"
    assert s.art.attachments == f"{d}/attachments"
    assert s.art.preplan == f"{d}/issue-123-preplan.md"
    assert s.default_branch == "issue-123"
    assert s.branch_reuse is False
    assert s.env_check_sh == ""
    assert s.issue_label == "GitHub"
    assert s.jira_browse is None
    assert s.issue_line("acme/app#123: Fix it") == "Closes #123 — Fix it"
    assert s.issue_line("Fix it") == "Closes #123 — Fix it"
    assert s.issue_line_ok("Closes #123 — Fix it")
    assert s.issue_line_ok("Closes #123")
    assert not s.issue_line_ok("Closes #1234 — x")
    assert not s.issue_line_ok("Closes #12")
    assert s.notes_path == s.art.issue_md
    assert s.extra_issue_files == (f"{d}/fetch-notes.json",)


def test_same_repo_forms_are_equal(repo):
    sources = [_src(t, repo) for t in SAME_REPO_FORMS]
    for other in sources[1:]:
        assert other == sources[0]
        for f in dataclasses.fields(IssueSource):
            if not callable(getattr(sources[0], f.name)):
                assert getattr(other, f.name) == getattr(sources[0], f.name)


@pytest.mark.parametrize("token", ["other/lib#45", "https://github.com/other/lib/issues/45"])
def test_github_other_repo_column(repo, token):
    s = _src(token, repo)
    h8 = hashlib.sha256(b"other/lib#45").hexdigest()[:8]
    assert h8 == "2b9ddc19"
    key = f"issue-other-lib-45-{h8}"
    d = Path(repo) / "tmp" / "github" / f"other-lib-45-{h8}"
    assert (s.kind, s.display, s.identity) == ("github", "other/lib#45", "other/lib#45")
    assert (s.file_key, s.subject_prefix) == (key, "other/lib#45")
    assert s.resume_arg == "https://github.com/other/lib/issues/45"
    assert s.art.dir == f"{d}/"
    assert s.art.preplan == f"{d}/{key}-preplan.md"
    assert s.default_branch == key
    assert s.branch_reuse is False
    assert s.issue_line("other/lib#45: T") == "Closes other/lib#45 — T"
    assert s.issue_line_ok("Closes other/lib#45 — T")
    assert not s.issue_line_ok("Closes #45 — T")
    assert s.jira_browse is None
    assert s.notes_path == s.art.issue_md
    assert s.extra_issue_files == (f"{d}/fetch-notes.json",)


def test_slug_collisions_are_distinct(repo):
    a = _src("a-b/c#1", repo)
    b = _src("a/b-c#1", repo)
    assert a.art.dir != b.art.dir
    assert a.art.preplan != b.art.preplan
    assert a.file_key != b.file_key
    assert a.default_branch != b.default_branch
    assert a.file_key.startswith("issue-a-b-c-1-")
    assert b.file_key.startswith("issue-a-b-c-1-")


def test_jira_column(tmp_path, monkeypatch):
    marker = tmp_path / "git-called"
    shim = tmp_path / "shim"
    shim.mkdir()
    git = shim / "git"
    git.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 1\n")
    git.chmod(git.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{shim}:{os.environ['PATH']}")
    project = str(tmp_path / "proj")
    for token in ("CBS-1234", "https://x.atlassian.net/browse/CBS-1234"):
        s, auto = resolve_issue([token], project, {}, {})
        assert auto is False
        assert (s.kind, s.display, s.identity, s.file_key) == ("jira",) + ("CBS-1234",) * 3
        assert (s.subject_prefix, s.resume_arg, s.default_branch) == ("CBS-1234",) * 3
        assert s.branch_reuse is True
        assert s.art.dir == f"{project}/tmp/jira/CBS-1234/"
        assert s.art.preplan == f"{project}/tmp/jira/CBS-1234/CBS-1234-preplan.md"
        assert s.issue_label == "Jira"
        assert s.issue_check_sh == ""
        assert s.extra_issue_files == ()
        assert s.issue_line is None and s.issue_line_ok is None
        assert s.fetch_notes is None and s.fetch_checklist is None and s.notes_path is None
        assert s.jira_browse is None
        assert s.env_check_sh.endswith(" && ")
    assert not marker.exists()


# ---------------------------------------------------------------------------
# Parity with fix_jira_issue_v3
# ---------------------------------------------------------------------------


# Copied from tests/test_fix_jira_issue_pipeline_v3.py:54-77 (that file is
# hash-pinned alongside the frozen v3 pipeline; do not import from it).
def _import_fix_jira(argv_tail, cwd=None):
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


def test_jira_parity_with_v3(tmp_path, monkeypatch):
    monkeypatch.delenv("JIRA_BROWSE_BASE", raising=False)
    v3 = _import_fix_jira(["CBS-1234"], cwd=tmp_path)
    s, _ = resolve_issue(["CBS-1234"], v3.PROJECT_DIR, {}, {})
    assert v3._preflight_cmd.startswith(s.env_check_sh)
    assert s.fetch_cmd == v3._fetch_cmd
    assert s.art == v3.ART


# ---------------------------------------------------------------------------
# Argument forms
# ---------------------------------------------------------------------------


def test_auto_flag(repo):
    assert _resolve(["auto", "123"], repo)[1] is True
    assert _resolve(["123", "auto"], repo)[1] is True
    assert _resolve(["123"], repo)[1] is False
    with pytest.raises(ValueError, match="'Auto'"):
        _resolve(["123", "Auto"], repo)


def test_auto_arg_is_the_same_as_the_positional(repo):
    assert _resolve(["123"], repo, {"auto": "on"})[1] is True
    assert _resolve(["123"], repo, {"auto": "off"})[1] is False
    assert _resolve(["123", "auto"], repo, {"auto": "off"})[1] is True
    with pytest.raises(ValueError, match="--arg auto: must be 'on' or 'off'"):
        _resolve(["123"], repo, {"auto": "yes"})


def test_pipeline_tokens(repo):
    assert _resolve(["run", "fix_issue_v1", "123"], repo)[0].display == "#123"
    s, auto = _resolve(["run", "/x/fix_issue_v1.py", "123", "auto", "--resume"], repo)
    assert s.display == "#123" and auto is True
    with pytest.raises(ValueError, match="unrecognised argument 'foo'"):
        _resolve(["foo", "123"], repo)


def test_stop_is_refused(repo):
    with pytest.raises(ValueError, match="always pauses at the reviewed plan"):
        _resolve(["123", "stop"], repo)


@pytest.mark.parametrize("argv", [[], ["-5"], ["auto"]])
def test_no_reference(repo, argv):
    with pytest.raises(ValueError, match="needs one issue reference") as e:
        _resolve(argv, repo)
    assert "'#123'" in str(e.value)


@pytest.mark.parametrize(
    "argv", [["123", "124"], ["123", "123"], ["123", "acme/app#123"], ["CBS-1", "123"]]
)
def test_several_references(repo, argv):
    with pytest.raises(ValueError, match="found several issue references"):
        _resolve(argv, repo)


def test_require_quota_positions(repo):
    assert _resolve(["123", "--require-quota"], repo)[0].display == "#123"
    with pytest.raises(ValueError, match="needs one issue reference"):
        _resolve(["--require-quota", "123"], repo)


def test_pull_url_is_refused(repo):
    with pytest.raises(ValueError, match="fix_issue_v1"):
        _src("https://github.com/acme/app/pull/5", repo)


# ---------------------------------------------------------------------------
# Host, jira_browse, prefix length
# ---------------------------------------------------------------------------


def test_other_host_is_refused(repo):
    with pytest.raises(ValueError, match="GH_HOST"):
        _src("https://github.com/acme/app/issues/1", repo, environ={"GH_HOST": "ghe.corp"})


def test_no_hostname_flag(repo):
    for token in ("123", "other/lib#45"):
        s = _src(token, repo)
        assert "--hostname" not in s.issue_check_sh
        assert "--hostname" not in s.fetch_cmd


def test_github_ignores_jira_browse_env(repo):
    s = _src("123", repo, environ={"JIRA_BROWSE_BASE": "garbage"})
    assert s.jira_browse is None


def test_github_jira_browse_arg_is_an_error(repo):
    with pytest.raises(ValueError, match="Jira issues only"):
        _src("123", repo, args={"jira_browse": "https://x/browse/"})


def test_jira_browse_rules(tmp_path):
    project = str(tmp_path)
    s = _src("CBS-1", project, environ={"JIRA_BROWSE_BASE": "https://x/browse/"})
    assert s.jira_browse == "https://x/browse/"
    with pytest.raises(ValueError, match=r"must end in /browse/, got 'bad'"):
        _src("CBS-1", project, args={"jira_browse": "bad"})
    s = _src(
        "CBS-1", project,
        args={"jira_browse": ""}, environ={"JIRA_BROWSE_BASE": "https://x/browse/"},
    )
    assert s.jira_browse is None


def test_prefix_length(repo):
    owner = "a" * 30
    ok = f"{owner}/{'b' * 27}#1"
    too_long = f"{owner}/{'b' * 28}#1"
    assert len(ok) == 60 and len(too_long) == 61
    assert _src(ok, repo).subject_prefix == ok
    with pytest.raises(ValueError, match="72"):
        _src(too_long, repo)


# ---------------------------------------------------------------------------
# Generated shell fragments
# ---------------------------------------------------------------------------


def _run_check(s: IssueSource, repo: str, bin_dir: str):
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    return subprocess.run(
        ["sh", "-c", "true" + s.issue_check_sh], cwd=repo, env=env, capture_output=True, text=True
    )


def test_issue_check_pull_request(repo, tmp_path):
    bin_dir, _ = install_fake_gh(tmp_path, {("api",): [(0, "true\n", "")]})
    r = _run_check(_src("123", repo), repo, bin_dir)
    assert r.returncode == 1
    assert "is a pull request" in r.stdout


def test_issue_check_gh_failure(repo, tmp_path):
    bin_dir, _ = install_fake_gh(tmp_path, {("api",): [(1, "", "boom")]})
    r = _run_check(_src("123", repo), repo, bin_dir)
    assert r.returncode == 1
    assert "not found, or gh has no access" in r.stdout


def test_issue_check_passes(repo, tmp_path):
    bin_dir, calls = install_fake_gh(tmp_path, {("api",): [(0, "false\n", "")]})
    r = _run_check(_src("123", repo), repo, bin_dir)
    assert r.returncode == 0, r.stdout
    argv = json.loads(Path(calls).read_text().splitlines()[0])
    assert argv == ["api", "repos/acme/app/issues/123", "--jq", ".pull_request != null"]


def test_commit_cleanup_strip(repo, tmp_path):
    _git(Path(repo), "config", "commit.cleanup", "strip")
    bin_dir, _ = install_fake_gh(tmp_path, {("api",): [(0, "false\n", "")]})
    r = _run_check(_src("123", repo), repo, bin_dir)
    assert r.returncode == 1
    assert "commit.cleanup" in r.stdout
    assert _run_check(_src("other/lib#45", repo), repo, bin_dir).returncode == 0


def test_fragments_are_valid_sh(repo):
    for token in ("123", "other/lib#45", "octo_cat/my.app#7"):
        s = _src(token, repo)
        for fragment in ("true" + s.issue_check_sh, s.fetch_cmd):
            r = subprocess.run(["sh", "-n", "-c", fragment], capture_output=True, text=True)
            assert r.returncode == 0, r.stderr


def test_hostile_names_arrive_as_one_word(repo, tmp_path):
    bin_dir, calls = install_fake_gh(tmp_path, {("api",): [(0, "false\n", "")]})
    s = _src("octo_cat/my.app#7", repo)
    r = _run_check(s, repo, bin_dir)
    assert r.returncode == 0, r.stdout
    argv = json.loads(Path(calls).read_text().splitlines()[0])
    assert argv[1] == "repos/octo_cat/my.app/issues/7"


def test_fetch_cmd_shape(repo):
    s = _src("other/lib#45", repo)
    tail = f"-I -m norn.pipelines._github_issue notes {shlex.quote(s.art.dir)}"
    assert s.fetch_cmd.endswith(tail)
    first, _, second = s.fetch_cmd.partition(" && ")
    assert second.startswith(shlex.quote(sys.executable) + " ")
    assert first.startswith("bash ")
    assert "_github_fetch.sh" in first
    assert "other/lib 45 " in first
    j = _src("CBS-1234", repo)
    assert j.fetch_cmd.startswith("bash ") and "_jira_fetch.sh CBS-1234 " in j.fetch_cmd


# ---------------------------------------------------------------------------
# Fetch-note fields
# ---------------------------------------------------------------------------


def test_fetch_note_readers(repo):
    s = _src("123", repo)
    Path(s.art.dir).mkdir(parents=True)
    _github_issue.write_fetch_notes(
        s.art.dir, [_github_issue.FetchNote("full note text", "pr safe text")]
    )
    assert s.fetch_notes() == ["full note text"]
    assert s.fetch_checklist() == ["- [ ] fetch note: pr safe text"]
    assert s.notes_path == s.art.issue_md
    assert s.extra_issue_files[0].endswith("fetch-notes.json")


def test_module_import_has_no_side_effects():
    assert _issue_source.PIPELINE == "fix_issue_v1"

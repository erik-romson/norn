"""Tests for norn/pipelines/_github_issue.py -- references, origin, artifact paths."""
from __future__ import annotations

import re
import stat
import subprocess
from pathlib import Path

import pytest

from norn.pipelines import _github_issue as gi
from norn.pipelines._github_issue import (
    GithubIssueRef,
    artifact_paths,
    launch_repo,
    parse_github_ref,
    resolve_github_ref,
    ssh_hostname,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _repo(tmp_path: Path, origin: str | None) -> str:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    if origin is not None:
        _git(tmp_path, "remote", "add", "origin", origin)
    return str(tmp_path)


class FakeSsh:
    """Records calls; maps gh-work to github.com and every other alias to itself."""

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self.calls: list[str] = []
        self.mapping = mapping if mapping is not None else {"gh-work": "github.com"}

    def __call__(self, alias: str) -> str:
        self.calls.append(alias)
        return self.mapping.get(alias, alias)


# ---------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    "token, expected",
    [
        ("123", (None, None, None, 123)),
        ("#123", (None, None, None, 123)),
        ("acme/app#123", (None, "acme", "app", 123)),
        ("Acme/App#123", (None, "Acme", "App", 123)),
        ("octo_cat/my.app#7", (None, "octo_cat", "my.app", 7)),
        ("https://github.com/acme/app/issues/5", ("github.com", "acme", "app", 5)),
        ("https://github.com/acme/app/issues/5/", ("github.com", "acme", "app", 5)),
        ("https://github.com/acme/app/issues/5?a=1&b=2", ("github.com", "acme", "app", 5)),
        ("https://github.com/acme/app/issues/5#issuecomment-9", ("github.com", "acme", "app", 5)),
        ("HTTPS://GITHUB.COM/ACME/APP/ISSUES/5", ("github.com", "ACME", "APP", 5)),
        ("https://www.github.com/acme/app/issues/5", ("github.com", "acme", "app", 5)),
        ("https://github.com:443/acme/app/issues/5", ("github.com", "acme", "app", 5)),
        ("https://github.com/acme/browse/issues/5", ("github.com", "acme", "browse", 5)),
        ("https://ghe.corp/a/b/issues/1", ("ghe.corp", "a", "b", 1)),
    ],
)
def test_parse_accepts(token, expected):
    assert parse_github_ref(token) == expected


@pytest.mark.parametrize(
    "token",
    [
        "0",
        "0123",
        "١٢٣",
        "acme/app#0",
        "a;b/app#1",
        "acme/app#x",
        "https://github.com/acme/app/pull/7",
        "https://github.com/acme/app/pulls/7",
        "https://github.com/acme/app/discussions/3",
        "https://github.com/acme/app/issues/5/events",
        "https://github.com/acme/app/issues/new",
        "https://github.com/acme/app/issues/0123",
        "https://api.github.com/repos/a/b/issues/1",
        "https://user:pw@github.com/a/b/issues/1",
    ],
)
def test_parse_refuses(token):
    with pytest.raises(ValueError):
        parse_github_ref(token)


def test_userinfo_not_echoed():
    with pytest.raises(ValueError) as exc:
        parse_github_ref("https://user:pw@github.com/a/b/issues/1")
    assert "pw" not in str(exc.value)


def test_pull_url_message_names_kind_and_pipeline():
    with pytest.raises(ValueError, match="pull.*fix_issue_v1"):
        parse_github_ref("https://github.com/acme/app/pull/7")


@pytest.mark.parametrize("token", ["CBS-1234", "https://x.atlassian.net/browse/CBS-1", "auto"])
def test_parse_not_github(token):
    assert parse_github_ref(token) is None


def test_names_validated():
    assert parse_github_ref("octo_cat/app#1") == (None, "octo_cat", "app", 1)
    for token in ["a;b/app#1", "$(x)/app#1", "a b/app#1", "/app#1", "acme/.#1", "acme/..#1", "acme/a..b#1"]:
        with pytest.raises(ValueError):
            parse_github_ref(token)


# ----------------------------------------------------------------- origin


@pytest.mark.parametrize(
    "origin",
    [
        "https://github.com/o/r",
        "https://github.com/o/r.git",
        "https://github.com/o/r/",
        "https://github.com/o/r.git/",
    ],
)
def test_launch_repo_https(tmp_path, origin):
    ssh = FakeSsh()
    assert launch_repo(_repo(tmp_path, origin), ssh_host=ssh) == ("github.com", "o", "r")
    assert ssh.calls == []


@pytest.mark.parametrize("origin", ["git@github.com:o/r.git", "ssh://git@github.com:22/o/r.git"])
def test_launch_repo_ssh(tmp_path, origin):
    ssh = FakeSsh()
    assert launch_repo(_repo(tmp_path, origin), ssh_host=ssh) == ("github.com", "o", "r")
    assert ssh.calls == ["github.com"]


def test_launch_repo_ssh_alias(tmp_path):
    ssh = FakeSsh()
    assert launch_repo(_repo(tmp_path, "git@gh-work:o/r.git"), ssh_host=ssh) == ("github.com", "o", "r")
    assert ssh.calls == ["gh-work"]


def test_launch_repo_git_scheme_does_not_call_ssh(tmp_path):
    ssh = FakeSsh()
    assert launch_repo(_repo(tmp_path, "git://github.com/o/r.git"), ssh_host=ssh) == ("github.com", "o", "r")
    assert ssh.calls == []


def test_launch_repo_ssh_error_propagates(tmp_path):
    def boom(alias: str) -> str:
        raise ValueError("no host")

    with pytest.raises(ValueError, match="no host"):
        launch_repo(_repo(tmp_path, "git@gh-work:o/r.git"), ssh_host=boom)


def test_launch_repo_strips_userinfo_and_never_shows_it(tmp_path):
    project = _repo(tmp_path, "https://x-access-token:SECRET@github.com/o/r.git")
    assert launch_repo(project, ssh_host=FakeSsh()) == ("github.com", "o", "r")
    with pytest.raises(ValueError) as exc:
        resolve_github_ref("123", project, gh_host="ghe.corp", ssh_host=FakeSsh())
    assert "SECRET" not in str(exc.value)


@pytest.mark.parametrize(
    "origin",
    ["git@gitlab.com:group/sub/r.git", "/srv/git/r.git", "file:///srv/r.git", None],
)
def test_launch_repo_none(tmp_path, origin):
    ssh = FakeSsh()
    assert launch_repo(_repo(tmp_path, origin), ssh_host=ssh) is None
    assert ssh.calls == []


def test_launch_repo_not_a_repository(tmp_path):
    ssh = FakeSsh()
    assert launch_repo(str(tmp_path), ssh_host=ssh) is None
    assert ssh.calls == []


# ------------------------------------------------------------ ssh_hostname


def _fake_ssh(tmp_path: Path, monkeypatch, body: str) -> Path:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    log = tmp_path / "ssh-args.txt"
    script = bin_dir / "ssh"
    script.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n{body}\n')
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bin_dir))
    return log


@pytest.mark.parametrize(
    "printed, expected",
    [
        ("hostname GitHub.com", "github.com"),
        ("hostname ssh.github.com", "github.com"),
        ("hostname ghe.corp", "ghe.corp"),
    ],
)
def test_ssh_hostname(tmp_path, monkeypatch, printed, expected):
    log = _fake_ssh(tmp_path, monkeypatch, f"echo user git\necho {printed}")
    assert ssh_hostname("gh-work") == expected
    assert log.read_text().strip() == "-G gh-work"


def test_ssh_hostname_failures(tmp_path, monkeypatch):
    _fake_ssh(tmp_path, monkeypatch, "exit 255")
    with pytest.raises(ValueError, match=r"gh-work.*ssh -G"):
        ssh_hostname("gh-work")


def test_ssh_hostname_no_hostname_line(tmp_path, monkeypatch):
    _fake_ssh(tmp_path, monkeypatch, "echo user git")
    with pytest.raises(ValueError, match=r"gh-work.*ssh -G"):
        ssh_hostname("gh-work")


def test_ssh_hostname_missing_ssh(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(ValueError, match="ssh -G"):
        ssh_hostname("gh-work")


def test_ssh_hostname_rejects_option_alias(tmp_path, monkeypatch):
    log = _fake_ssh(tmp_path, monkeypatch, "echo hostname evil")
    with pytest.raises(ValueError):
        ssh_hostname("-oProxyCommand=x")
    assert not log.exists()


# ------------------------------------------------------------- resolution


def test_resolve_bare_number_same_repo(tmp_path):
    project = _repo(tmp_path, "https://github.com/Acme/App.git")
    ref = resolve_github_ref("123", project, gh_host=None, ssh_host=FakeSsh())
    assert ref == GithubIssueRef("github.com", "acme", "app", 123, True)
    assert ref.slug == "123"
    assert ref.url == "https://github.com/acme/app/issues/123"
    assert ref.canonical == "acme/app#123"


@pytest.mark.parametrize("token", ["acme/app#123", "https://github.com/ACME/app/issues/123"])
def test_resolve_names_launch_repo(tmp_path, token):
    project = _repo(tmp_path, "https://github.com/Acme/App.git")
    ref = resolve_github_ref(token, project, gh_host=None, ssh_host=FakeSsh())
    assert ref.same_repo is True
    assert ref.short_ref == "#123"
    assert ref.slug == "123"


@pytest.mark.parametrize("token", ["other/lib#45", "https://github.com/other/lib/issues/45"])
def test_resolve_other_repo(tmp_path, token):
    project = _repo(tmp_path, "https://github.com/acme/app.git")
    ref = resolve_github_ref(token, project, gh_host=None, ssh_host=FakeSsh())
    assert ref.same_repo is False
    assert ref.slug == "other-lib-45-2b9ddc19"
    assert ref.short_ref == "other/lib#45"
    assert ref.url == "https://github.com/other/lib/issues/45"


def test_resolve_no_origin(tmp_path):
    project = _repo(tmp_path, None)
    with pytest.raises(ValueError, match="pass owner/repo#N or the issue URL"):
        resolve_github_ref("123", project, gh_host=None, ssh_host=FakeSsh())
    ref = resolve_github_ref("other/lib#45", project, gh_host=None, ssh_host=FakeSsh())
    assert ref.same_repo is False


def test_resolve_unknown_token(tmp_path):
    with pytest.raises(ValueError, match="not a GitHub issue reference"):
        resolve_github_ref("CBS-1234", _repo(tmp_path, None), gh_host=None, ssh_host=FakeSsh())


def test_resolve_origin_on_other_host(tmp_path):
    project = _repo(tmp_path, "https://ghe.corp/acme/app.git")
    with pytest.raises(ValueError) as exc:
        resolve_github_ref("123", project, gh_host=None, ssh_host=FakeSsh())
    assert "ghe.corp" in str(exc.value) and "GH_HOST=ghe.corp" in str(exc.value)
    ref = resolve_github_ref("123", project, gh_host="ghe.corp", ssh_host=FakeSsh())
    assert ref.url == "https://ghe.corp/acme/app/issues/123"


def test_resolve_url_on_other_host(tmp_path):
    project = _repo(tmp_path, None)
    with pytest.raises(ValueError) as exc:
        resolve_github_ref("https://ghe.corp/a/b/issues/1", project, gh_host=None, ssh_host=FakeSsh())
    assert "ghe.corp" in str(exc.value) and "github.com" in str(exc.value)


def test_resolve_ssh_alias_origin_matches_url_host(tmp_path):
    project = _repo(tmp_path, "git@gh-work:acme/app.git")
    ref = resolve_github_ref("https://github.com/acme/app/issues/1", project, gh_host=None, ssh_host=FakeSsh())
    assert ref.same_repo is True


@pytest.mark.parametrize("token", ["123", "acme/app#123", "other/lib#45"])
def test_resolve_ssh_origin_on_other_host(tmp_path, token):
    project = _repo(tmp_path, "git@github.com:acme/app.git")
    with pytest.raises(ValueError) as exc:
        resolve_github_ref(token, project, gh_host="ghe.corp", ssh_host=FakeSsh())
    message = str(exc.value)
    assert "github.com" in message
    assert "GH_HOST=github.com" in message
    assert "ssh -G github.com" in message


def test_resolve_ssh_alias_unmapped(tmp_path):
    project = _repo(tmp_path, "git@gh-work:acme/app.git")
    with pytest.raises(ValueError, match="gh-work"):
        resolve_github_ref("123", project, gh_host=None, ssh_host=FakeSsh({}))


def test_slug_hash_known_value(tmp_path):
    project = _repo(tmp_path, None)
    ref = resolve_github_ref("https://github.com/acme-corp/api/issues/1", project, gh_host=None, ssh_host=FakeSsh())
    assert ref.slug == "acme-corp-api-1-16bd2cef"


def test_slug_collisions_and_case(tmp_path):
    project = _repo(tmp_path, None)
    a = resolve_github_ref("a-b/c#1", project, gh_host=None, ssh_host=FakeSsh())
    b = resolve_github_ref("a/b-c#1", project, gh_host=None, ssh_host=FakeSsh())
    assert a.slug == "a-b-c-1-15fddf9f"
    assert b.slug == "a-b-c-1-9ed34121"
    assert artifact_paths(project, a).dir != artifact_paths(project, b).dir
    assert artifact_paths(project, a).preplan != artifact_paths(project, b).preplan
    upper = resolve_github_ref("Acme/App#1", project, gh_host=None, ssh_host=FakeSsh())
    lower = resolve_github_ref("acme/app#1", project, gh_host=None, ssh_host=FakeSsh())
    assert upper.slug == lower.slug


def test_slugs_are_safe_branch_names(tmp_path):
    same = _repo(tmp_path, "https://github.com/acme/app.git")
    tokens = [
        "123",
        "#123",
        "acme/app#123",
        "Acme/App#123",
        "octo_cat/my.app#7",
        "other/lib#45",
        "https://github.com/acme-corp/api/issues/1",
        "https://www.github.com/acme/app/issues/5",
        "acme/.github#5",
        "acme/x.lock#5",
    ]
    for token in tokens:
        ref = resolve_github_ref(token, same, gh_host=None, ssh_host=FakeSsh())
        assert re.fullmatch(r"[a-z0-9._-]+", ref.slug), ref.slug
        assert ".." not in ref.slug
        subprocess.run(
            ["git", "check-ref-format", "--branch", f"issue-{ref.slug}"], check=True, capture_output=True
        )


# ------------------------------------------------------------------ paths


def test_artifact_paths(tmp_path):
    project = str(tmp_path)
    same = GithubIssueRef("github.com", "acme", "app", 123, True)
    paths = artifact_paths(project, same)
    base = f"{project}/tmp/github/123"
    assert paths.dir == base + "/"
    assert paths.issue_json == base + "/issue.json"
    assert paths.issue_md == base + "/issue.md"
    assert paths.attachments == base + "/attachments"
    assert paths.preplan == base + "/issue-123-preplan.md"

    other = GithubIssueRef("github.com", "other", "lib", 45, False)
    paths = artifact_paths(project, other)
    assert paths.dir == f"{project}/tmp/github/other-lib-45-2b9ddc19/"
    assert paths.preplan.endswith("issue-other-lib-45-2b9ddc19-preplan.md")


def test_import_runs_nothing():
    assert callable(gi.parse_github_ref)

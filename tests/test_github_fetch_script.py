"""Tests for norn/pipelines/_github_fetch.sh.

Every test runs the real script end to end against the committed shims in
tests/fixtures/fix_github_issue/: bin/gh (REST, GraphQL and ``auth token``),
bin/curl (attachment downloads) and, for the signed-URL window test,
clock/date. No network call is made.

jq is a required tool of the script, so a missing jq fails the test with a clear
message instead of skipping it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import NamedTuple

import pytest

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "norn" / "pipelines" / "_github_fetch.sh"
FIXTURES = Path(__file__).parent / "fixtures" / "fix_github_issue"
BIN_DIR = FIXTURES / "bin"
CLOCK_DIR = FIXTURES / "clock"

UUID_1 = "11111111-1111-4111-8111-111111111111"
UUID_3 = "33333333-3333-4333-8333-333333333333"
UUID_CODE = "c0de0000-2222-4222-8222-222222222222"
UUID_STALL = "57a11000-5555-4555-8555-555555555555"
SIGNED_1_NAME = f"400000001-{UUID_1}.png"
ASSET_1 = f"https://github.com/user-attachments/assets/{UUID_1}"
ASSET_3 = f"https://github.com/user-attachments/assets/{UUID_3}"
ASSET_CODE = f"https://github.com/user-attachments/assets/{UUID_CODE}"
PDF = "https://github.com/user-attachments/files/424242/report.pdf"
SIGNED_PREFIX = "https://private-user-images.githubusercontent.com/"

MANIFEST_HEADER = ["index", "path", "source_url", "content_type", "bytes", "status"]
SCRIPT_OUTPUTS = ("issue.md", "issue.md.tmp", "issue.json", "fetch-notes.json", "raw", "attachments")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class Fetch(NamedTuple):
    proc: subprocess.CompletedProcess[str]
    out: Path
    logs: Path

    @property
    def gh_log(self) -> str:
        path = self.logs / "gh.log"
        return path.read_text() if path.exists() else ""

    @property
    def curl_log(self) -> str:
        path = self.logs / "curl.log"
        return path.read_text() if path.exists() else ""

    @property
    def curl_events(self) -> list[str]:
        """START/END lines of the curl shim log, in order (ARGV lines dropped)."""
        return [line for line in self.curl_log.splitlines() if not line.startswith("ARGV ")]

    @property
    def curl_starts(self) -> list[tuple[str, str, str]]:
        """``(AUTH|NOAUTH, max-time, url)`` for every curl call, in start order."""
        starts = []
        for line in self.curl_events:
            if line.startswith("START "):
                _, auth, max_time, url = line.split(" ", 3)
                starts.append((auth, max_time, url))
        return starts

    @property
    def summary(self) -> str:
        lines = self.proc.stdout.strip().splitlines()
        return lines[-1] if lines else ""


def _require_jq() -> None:
    if shutil.which("jq") is None:
        pytest.fail(
            "jq is not on PATH, and _github_fetch.sh requires it "
            "(install jq, e.g. `brew install jq` or `apt-get install jq`)"
        )


def _invoke(
    tmp_path: Path,
    args: list[str],
    *,
    out: Path,
    shell: str = "bash",
    env: dict[str, str] | None = None,
    clock: bool = False,
) -> Fetch:
    """Run the script with *args*, the fixture shims first on PATH and fresh log files."""
    _require_jq()
    logs = Path(tempfile.mkdtemp(dir=tmp_path, prefix="logs-"))
    path = [str(BIN_DIR), os.environ.get("PATH", "")]
    if clock:
        path.insert(0, str(CLOCK_DIR))
    run_env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"GH_SHIM_FAIL", "GH_HOST", "DATE_SHIM_STATE", "GH_SHIM_LOG", "CURL_SHIM_LOG"}
    }
    run_env.update(
        PATH=os.pathsep.join(path),
        GH_SHIM_LOG=str(logs / "gh.log"),
        CURL_SHIM_LOG=str(logs / "curl.log"),
    )
    if clock:
        run_env["DATE_SHIM_STATE"] = str(logs / "date.state")
    if env:
        run_env.update(env)
    proc = subprocess.run(
        [shell, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=run_env,
        timeout=120,
    )
    return Fetch(proc, out, logs)


def _fetch(
    tmp_path: Path,
    number: int | str,
    *,
    repo: str = "acme/app",
    out: Path | None = None,
    shell: str = "bash",
    env: dict[str, str] | None = None,
    clock: bool = False,
) -> Fetch:
    out = out if out is not None else tmp_path / "out"
    return _invoke(tmp_path, [repo, str(number), str(out)], out=out, shell=shell, env=env, clock=clock)


def _ok(fetch: Fetch) -> Fetch:
    assert fetch.proc.returncode == 0, (
        f"_github_fetch.sh exited {fetch.proc.returncode}\n"
        f"stdout: {fetch.proc.stdout}\nstderr: {fetch.proc.stderr}"
    )
    return fetch


def _manifest(out: Path) -> list[list[str]]:
    lines = (out / "attachments" / "manifest.tsv").read_text().splitlines()
    assert lines[0].split("\t") == MANIFEST_HEADER
    rows = [line.split("\t") for line in lines[1:]]
    assert all(len(row) == len(MANIFEST_HEADER) for row in rows), rows
    return rows


def _md_attachment_rows(md: str) -> list[list[str]]:
    """The rows of the issue.md Attachments table, as lists of stripped cells."""
    section = md.split("\n## Attachments (", 1)[1].split("\n## ", 1)[0]
    table = [line for line in section.splitlines() if line.startswith("| ")]
    assert table[0] == "| # | local path | source URL | content type | bytes | status |"
    return [[cell.strip() for cell in line.strip().strip("|").split(" | ")] for line in table[2:]]


def _attachment_files(out: Path) -> list[str]:
    return sorted(p.name for p in (out / "attachments").iterdir())


def _assert_no_script_outputs(out: Path) -> None:
    for name in SCRIPT_OUTPUTS:
        assert not (out / name).exists(), f"{name} was left behind by a failed fetch"


# ---------------------------------------------------------------------------
# 1. The #123 render
# ---------------------------------------------------------------------------


def test_issue_123_renders_the_artifact_contract(tmp_path: Path) -> None:
    f = _ok(_fetch(tmp_path, 123))
    md = (f.out / "issue.md").read_text()

    assert md.startswith("# acme/app#123: Crash on save (fixes #99) cc @alice\n")
    headings = [
        "| Field | Value |",
        "\n## Description\n",
        "\n## Comments (3)\n",
        "\n## Attachments (4)\n",
        "\n## Linked issues and pull requests\n",
        "\n## Sub-issues\n",
    ]
    positions = [md.index(h) for h in headings]
    assert positions == sorted(positions)

    rows = _md_attachment_rows(md)
    assert [r[0] for r in rows] == ["01", "02", "03", "-"]
    assert [r[5] for r in rows] == ["downloaded", "downloaded", "downloaded", "not rendered by GitHub"]
    assert rows[3][1:3] == ["--", ASSET_CODE]

    assert _attachment_files(f.out) == sorted(
        [f"01_{SIGNED_1_NAME}", "02_report.pdf", f"03_{UUID_3}.png", "manifest.tsv"]
    )

    manifest = _manifest(f.out)
    assert len(manifest) == 4
    assert manifest[0][:4] == ["01", f"attachments/01_{SIGNED_1_NAME}", ASSET_1, "image/png"]
    assert manifest[1][:4] == ["02", "attachments/02_report.pdf", PDF, "application/pdf"]
    assert manifest[2][:4] == ["03", f"attachments/03_{UUID_3}.png", ASSET_3, "image/png"]
    assert manifest[3] == ["-", "", ASSET_CODE, "", "", "not rendered by GitHub"]
    for row in manifest[:3]:
        assert int(row[4]) == (f.out / row[1]).stat().st_size

    manifest_text = (f.out / "attachments" / "manifest.tsv").read_text()
    assert "jwt=" not in md
    assert "jwt=" not in manifest_text
    assert "i.imgur.com" not in f.curl_log
    assert "triage-secret" not in md
    assert "| Author | olivia (OWNER) |" in md
    assert "### mia (MEMBER) -- 2026-10-01T10:00:00Z" in md
    assert "### drive-by (NONE) -- 2026-10-01T11:00:00Z" in md
    assert "(first " not in md
    assert "| Type | Bug |" in md
    assert "| Parent | acme/app#100: Editor stability (OPEN) |" in md
    assert md.count("acme/app#130 Fix crash on save (OPEN, PullRequest)") == 1
    assert "- other/lib#140 Export dialog freezes (CLOSED, Issue)" in md
    assert "- acme/app#124 Save handler null check (OPEN)" in md
    assert "- acme/app#125 Autosave race (CLOSED)" in md
    assert not (f.out / "raw" / "downloads").exists()

    doc = json.loads((f.out / "issue.json").read_text())
    assert sorted(doc) == ["comments", "issue", "relations", "repo"]
    assert doc["repo"] == "acme/app"
    assert len(doc["comments"]) == 3
    assert doc["relations"]["parent"]["number"] == 100

    assert "3 attachments downloaded, 1 not downloaded" in f.proc.stdout
    assert f.summary == (
        "fetched acme/app#123: open, 3 comments, 3 attachments downloaded, "
        "1 not downloaded, 1 linked PRs (1 open)"
    )


# ---------------------------------------------------------------------------
# 2. Token routing
# ---------------------------------------------------------------------------


def test_token_goes_only_to_github_com(tmp_path: Path) -> None:
    f = _ok(_fetch(tmp_path, 123))
    starts = f.curl_starts
    assert len(starts) == 3
    for auth, _, url in starts:
        expected = "AUTH" if url.startswith("https://github.com/") else "NOAUTH"
        assert auth == expected, (auth, url)
    assert any(url.startswith(SIGNED_PREFIX) and auth == "NOAUTH" for auth, _, url in starts)
    assert any(auth == "AUTH" for auth, _, _ in starts)
    assert "auth token" in f.gh_log
    assert "test-token" not in f.gh_log
    assert "test-token" not in f.curl_log


# ---------------------------------------------------------------------------
# 3. Owner flags
# ---------------------------------------------------------------------------


def test_graphql_owner_and_repo_are_string_fields(tmp_path: Path) -> None:
    f = _ok(_fetch(tmp_path, 123))
    assert "-f o=acme" in f.gh_log
    assert "-f r=app" in f.gh_log
    assert "-F n=123" in f.gh_log
    assert "--hostname" not in f.gh_log

    numeric = _ok(_fetch(tmp_path, 11, repo="1234/app", out=tmp_path / "numeric"))
    assert "-f o=1234" in numeric.gh_log
    assert "--hostname" not in numeric.gh_log
    assert (numeric.out / "issue.md").read_text().startswith("# 1234/app#11: ")


# ---------------------------------------------------------------------------
# 4-5. Pull request and moved issue
# ---------------------------------------------------------------------------


def test_pull_request_number_is_refused(tmp_path: Path) -> None:
    f = _fetch(tmp_path, 7)
    assert f.proc.returncode == 1
    assert "is a pull request" in f.proc.stderr
    assert "graphql" not in f.gh_log
    _assert_no_script_outputs(f.out)


def test_moved_issue_is_refused(tmp_path: Path) -> None:
    f = _fetch(tmp_path, 8)
    assert f.proc.returncode == 1
    assert "moved to acme/other" in f.proc.stderr
    _assert_no_script_outputs(f.out)


# ---------------------------------------------------------------------------
# 6. Dead attachments
# ---------------------------------------------------------------------------


def test_dead_attachments_are_recorded_and_the_fetch_continues(tmp_path: Path) -> None:
    f = _ok(_fetch(tmp_path, 9))
    statuses = ["HTTP 404", "HTML page", "curl exit 28"]
    manifest = _manifest(f.out)
    assert [row[5] for row in manifest] == statuses
    assert [row[1] for row in manifest] == ["", "", ""]
    md = (f.out / "issue.md").read_text()
    assert [row[5] for row in _md_attachment_rows(md)] == statuses
    assert _attachment_files(f.out) == ["manifest.tsv"]
    assert "0 attachments downloaded, 3 not downloaded" in f.summary


# ---------------------------------------------------------------------------
# 7. Empty issue
# ---------------------------------------------------------------------------


def test_empty_issue(tmp_path: Path) -> None:
    f = _ok(_fetch(tmp_path, 11))
    md = (f.out / "issue.md").read_text()
    assert "\n## Comments (0)\n" in md
    assert "\n## Attachments (0)\n" in md
    description = md.split("\n## Description", 1)[1].split("\n## Comments (0)", 1)[0]
    assert description.strip() == ""
    assert _manifest(f.out) == []


# ---------------------------------------------------------------------------
# 8. Enterprise host
# ---------------------------------------------------------------------------


def test_enterprise_host_skips_the_attachment_pass(tmp_path: Path) -> None:
    f = _ok(_fetch(tmp_path, 12))
    assert f.curl_log == ""
    assert _manifest(f.out) == []
    assert _attachment_files(f.out) == ["manifest.tsv"]
    assert "\n## Attachments (0)\n" in (f.out / "issue.md").read_text()


# ---------------------------------------------------------------------------
# 9. Rate limit after the downloads
# ---------------------------------------------------------------------------


def test_relations_failure_removes_every_output(tmp_path: Path) -> None:
    f = _fetch(tmp_path, 123, env={"GH_SHIM_FAIL": "graphql"})
    assert f.proc.returncode == 1
    assert "relations query failed" in f.proc.stderr
    assert "API rate limit exceeded" in f.proc.stderr
    # The failure comes after the downloads, so files had been kept.
    assert len(f.curl_starts) == 3
    _assert_no_script_outputs(f.out)


# ---------------------------------------------------------------------------
# 10. Retry clean-up
# ---------------------------------------------------------------------------


def test_failed_refetch_removes_script_outputs_and_keeps_other_files(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _ok(_fetch(tmp_path, 123, out=out))
    (out / "fetch-notes.json").write_text('{"notes": []}\n')
    (out / "base.sha").write_text("0123456789abcdef\n")
    (out / "plan-approval.md").write_text("DECISION: approve\n")

    f = _fetch(tmp_path, 123, out=out, env={"GH_SHIM_FAIL": "comments"})
    assert f.proc.returncode == 1
    assert "API rate limit exceeded" in f.proc.stderr
    _assert_no_script_outputs(out)
    assert (out / "base.sha").read_text() == "0123456789abcdef\n"
    assert (out / "plan-approval.md").read_text() == "DECISION: approve\n"


# ---------------------------------------------------------------------------
# 11. Arguments
# ---------------------------------------------------------------------------


def test_bad_arguments_fail_before_any_gh_call_and_remove_nothing(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _ok(_fetch(tmp_path, 123, out=out))
    md_before = (out / "issue.md").read_text()
    files_before = _attachment_files(out)

    no_args = _invoke(tmp_path, [], out=out)
    assert no_args.proc.returncode != 0
    assert "Usage" in no_args.proc.stderr
    assert no_args.gh_log == ""

    for repo, number in [
        ("acme/app", "0"),
        ("a;b/app", "1"),
        ("acme/app", "0123"),
        ("acme/app", "12a"),
        ("acme", "1"),
        ("acme/..", "1"),
        ("acme/a..b", "1"),
    ]:
        f = _fetch(tmp_path, number, repo=repo, out=out)
        assert f.proc.returncode != 0, (repo, number)
        assert f.gh_log == "", (repo, number)
        assert f.curl_log == "", (repo, number)

    assert (out / "issue.md").read_text() == md_before
    assert _attachment_files(out) == files_before


# ---------------------------------------------------------------------------
# 12. Signed URLs start first and together
# ---------------------------------------------------------------------------


def test_signed_urls_start_first_and_together(tmp_path: Path) -> None:
    f = _ok(_fetch(tmp_path, 13))
    events = f.curl_events

    def index(kind: str, needle: str) -> int:
        matches = [i for i, line in enumerate(events) if line.startswith(kind + " ") and needle in line]
        assert len(matches) == 1, (kind, needle, events)
        return matches[0]

    hang_start = index("START", "/hang")
    stall_start = index("START", UUID_STALL)
    one_start = index("START", UUID_1)
    stall_end = index("END", UUID_STALL)
    assert stall_start < hang_start
    assert one_start < hang_start
    assert one_start < stall_end

    max_times = {url: max_time for _, max_time, url in f.curl_starts}
    signed = [url for url in max_times if url.startswith(SIGNED_PREFIX)]
    assert len(signed) == 2
    assert all(max_times[url] == "240" for url in signed)
    assert [max_times[url] for url in max_times if url.endswith("/hang")] == ["600"]

    manifest = _manifest(f.out)
    assert [(row[0], row[5]) for row in manifest] == [
        ("01", "curl exit 28"),
        ("02", "curl exit 28"),
        ("03", "downloaded"),
    ]
    assert manifest[0][2].endswith("/hang")
    assert UUID_STALL in manifest[1][2]
    assert manifest[2][2] == ASSET_1
    assert manifest[2][1] == f"attachments/03_{SIGNED_1_NAME}"
    assert (f.out / manifest[2][1]).is_file()
    assert _attachment_files(f.out) == sorted([f"03_{SIGNED_1_NAME}", "manifest.tsv"])
    assert not (f.out / "raw" / "downloads").exists()


# ---------------------------------------------------------------------------
# 13. Signed window passed
# ---------------------------------------------------------------------------


def test_signed_urls_are_not_started_after_the_window(tmp_path: Path) -> None:
    f = _ok(_fetch(tmp_path, 123, clock=True))
    assert (f.logs / "date.state").exists(), "the clock shim was not used"
    assert "private-user-images" not in f.curl_log

    expired = "signed URL expired before download"
    manifest = _manifest(f.out)
    assert manifest[0] == ["01", "", ASSET_1, "", "", expired]
    assert [row[5] for row in manifest[1:3]] == ["downloaded", "downloaded"]
    md_rows = _md_attachment_rows((f.out / "issue.md").read_text())
    assert md_rows[0][0] == "01"
    assert md_rows[0][5] == expired
    assert _attachment_files(f.out) == sorted(["02_report.pdf", f"03_{UUID_3}.png", "manifest.tsv"])
    assert "2 attachments downloaded, 2 not downloaded" in f.summary


# ---------------------------------------------------------------------------
# bash 3.2: the same fetch under /bin/bash
# ---------------------------------------------------------------------------


def test_bin_bash_produces_the_same_artifacts(tmp_path: Path) -> None:
    a = _ok(_fetch(tmp_path, 123, out=tmp_path / "bash"))
    b = _ok(_fetch(tmp_path, 123, out=tmp_path / "bin-bash", shell="/bin/bash"))
    assert (b.out / "issue.md").read_text() == (a.out / "issue.md").read_text()
    assert _manifest(b.out) == _manifest(a.out)
    assert _attachment_files(b.out) == _attachment_files(a.out)
    assert b.summary == a.summary

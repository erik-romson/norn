"""Tests for the fetch-notes half of norn/pipelines/_github_issue.py."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from norn.pipelines import _github_issue as gi

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "norn" / "pipelines" / "_github_fetch.sh"
FIXTURES = Path(__file__).parent / "fixtures" / "fix_github_issue"

HEADER = "index\tpath\tsource_url\tcontent_type\tbytes\tstatus\n"
GOOD_TITLE = "Plain title"


def _write(
    out: Path,
    *,
    title: str = GOOD_TITLE,
    body: str = "text",
    user_assoc: str = "OWNER",
    comments: list[dict] | None = None,
    state: str = "open",
    state_reason: str | None = None,
    html_url: str = "https://github.com/acme/app/issues/5",
    relations: dict | None = None,
    rows: list[list[str]] | None = None,
) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "attachments").mkdir(exist_ok=True)
    issue = {
        "title": title,
        "body": body,
        "state": state,
        "state_reason": state_reason,
        "html_url": html_url,
        "user": {"login": "olivia"},
        "author_association": user_assoc,
    }
    doc = {"issue": issue, "comments": comments or [], "relations": relations, "repo": "acme/app"}
    (out / "issue.json").write_text(json.dumps(doc))
    manifest = HEADER + "".join("\t".join(r) + "\n" for r in rows or [])
    (out / "attachments" / "manifest.tsv").write_text(manifest)
    (out / "issue.md").write_text(f"# acme/app#5: {title}\n\n| Field | Value |\n\n## Description\n\ntext\n\n## Comments (0)\n")


def _comment(login: str, assoc: str, body: str = "hi") -> dict:
    return {"user": {"login": login}, "author_association": assoc, "body": body}


def _texts(out: Path) -> list[str]:
    return [n.text for n in gi.fetch_notes(str(out))]


def _pr(number: int, title: str, state: str, typename: str = "PullRequest") -> dict:
    return {
        "__typename": typename,
        "number": number,
        "title": title,
        "state": state,
        "repository": {"nameWithOwner": "acme/app"},
    }


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Crash on save (fixes #99)", "fixes #99"),
        ("Resolves: acme/lib#5", "Resolves: acme/lib#5"),
    ],
)
def test_closing_keyword_title(tmp_path, title, expected):
    _write(tmp_path, title=title)
    notes = _texts(tmp_path)
    assert len(notes) == 1
    assert "closing keyword" in notes[0] and expected in notes[0]


def test_keyword_without_reference_is_no_note(tmp_path):
    _write(tmp_path, title="fix the #1 bug")
    assert _texts(tmp_path) == []


def test_mention_title(tmp_path):
    _write(tmp_path, title="cc @alice")
    notes = _texts(tmp_path)
    assert len(notes) == 1 and "a mention" in notes[0] and "@alice" in notes[0]


@pytest.mark.parametrize("title", ["mail a@b.com", "use `@x` here"])
def test_non_mentions(tmp_path, title):
    _write(tmp_path, title=title)
    assert _texts(tmp_path) == []


def test_attribution_title(tmp_path):
    _write(tmp_path, title="🤖 weekly report")
    notes = _texts(tmp_path)
    assert len(notes) == 1 and "pr body still complete" in notes[0]


def test_write_access_authors_give_no_note(tmp_path):
    _write(tmp_path, comments=[_comment("m", "MEMBER"), _comment("c", "COLLABORATOR")])
    assert _texts(tmp_path) == []


def test_outside_authors_one_note(tmp_path):
    _write(
        tmp_path,
        comments=[_comment("m", "MEMBER"), _comment("dan", "NONE"), _comment("eve", "CONTRIBUTOR")],
    )
    notes = _texts(tmp_path)
    assert len(notes) == 1
    assert "dan (NONE, comment 2)" in notes[0] and "eve (CONTRIBUTOR, comment 3)" in notes[0]
    assert notes[0].startswith("written by people without write access to acme/app: ")


def test_hidden_html_comments_counted(tmp_path):
    _write(
        tmp_path,
        body="a <!-- one --> b <!--\ntwo\n-->",
        comments=[_comment("m", "MEMBER", "x <!-- three -->"), {**_comment("m", "MEMBER"), "body": None}],
    )
    assert _texts(tmp_path) == ["3 hidden HTML comment(s) removed from issue.md (kept in raw/)"]


def test_closed_issue_note(tmp_path):
    _write(tmp_path, state="closed", state_reason="completed")
    assert _texts(tmp_path) == ["issue is closed (completed)"]
    _write(tmp_path, state="closed")
    assert _texts(tmp_path) == ["issue is closed (no reason given)"]


def test_only_open_linked_pr_noted(tmp_path):
    relations = {
        "closedByPullRequestsReferences": {"nodes": [_pr(7, "Open one", "OPEN")]},
        "timelineItems": {
            "nodes": [{"source": None}, {"source": _pr(7, "Open one", "OPEN")}, {"source": _pr(8, "Done", "MERGED")}]
        },
    }
    _write(tmp_path, relations=relations)
    assert _texts(tmp_path) == ["an open pull request already references this issue: acme/app#7 Open one"]


def _row(index: str, status: str, url: str = "https://x/y") -> list[str]:
    return [index, "", url, "", "", status]


def test_attachment_notes_without_retry(tmp_path):
    _write(tmp_path, rows=[_row("1", "HTTP 404"), _row("-", "not rendered by GitHub"), _row("2", "downloaded")])
    assert _texts(tmp_path) == [
        "attachment 1 not downloaded (HTTP 404): https://x/y",
        "attachment - not downloaded (not rendered by GitHub): https://x/y",
    ]


@pytest.mark.parametrize("status", ["curl exit 28", "signed URL expired before download", "HTTP 503"])
def test_retryable_attachment_gets_retry_note(tmp_path, status):
    _write(tmp_path, rows=[_row("1", status)])
    notes = _texts(tmp_path)
    assert notes == [
        f"attachment 1 not downloaded ({status}): https://x/y",
        "retry the fetch stage to try those downloads again",
    ]


def test_other_host_note(tmp_path):
    _write(tmp_path, html_url="https://ghe.corp/acme/app/issues/5")
    assert _texts(tmp_path) == [
        "attachments are downloaded only from github.com; files pasted on ghe.corp were not fetched"
    ]


def test_no_notes_files(tmp_path, capsys):
    _write(tmp_path)
    assert gi.main(["notes", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == "notes: none"
    assert json.loads((tmp_path / gi.FETCH_NOTES_FILE).read_text()) == {"notes": []}
    md = (tmp_path / "issue.md").read_text()
    assert "## Fetch notes\n\nnone\n\n## Description" in md
    assert gi.read_fetch_notes(str(tmp_path)) == []


def test_main_twice_leaves_one_section(tmp_path):
    _write(tmp_path, title="cc @alice")
    assert gi.main(["notes", str(tmp_path)]) == 0
    first = (tmp_path / "issue.md").read_text()
    assert gi.main(["notes", str(tmp_path)]) == 0
    second = (tmp_path / "issue.md").read_text()
    assert first == second
    assert second.count("## Fetch notes") == 1
    assert not (tmp_path / "issue.md.tmp").exists()


def test_missing_description_heading_fails(tmp_path):
    _write(tmp_path)
    (tmp_path / "issue.md").write_text("# t\n\nno sections\n")
    with pytest.raises(ValueError, match="issue.md"):
        gi.main(["notes", str(tmp_path)])


def test_read_fetch_notes_errors(tmp_path):
    with pytest.raises(ValueError, match=re.escape(str(tmp_path / gi.FETCH_NOTES_FILE))):
        gi.read_fetch_notes(str(tmp_path))
    (tmp_path / gi.FETCH_NOTES_FILE).write_text('{"notes": 3}')
    with pytest.raises(ValueError, match=re.escape(str(tmp_path / gi.FETCH_NOTES_FILE))):
        gi.read_fetch_notes(str(tmp_path))


def test_main_usage():
    assert gi.main([]) == 2
    assert gi.main(["other", "x"]) == 2


def test_pr_safe_text(tmp_path):
    relations = {"closedByPullRequestsReferences": {"nodes": [_pr(130, "fixes #7 @bob", "OPEN")]}}
    _write(
        tmp_path,
        title="Crash (fixes #99) cc @alice 🤖",
        comments=[_comment("carol", "NONE")],
        relations=relations,
    )
    assert gi.main(["notes", str(tmp_path)]) == 0
    items = gi.pr_checklist_items(str(tmp_path))
    assert len(items) == 5
    for line in items:
        assert line.startswith("- [ ] fetch note: ")
        for bad in ("#99", "#7", "@alice", "@bob", "🤖", "fixes", "fixes #7 @bob"):
            assert bad not in line
    joined = "\n".join(items)
    assert re.search(r"(?<!`)carol", joined) is None and "`carol`" in joined
    assert "`acme/app#130`" in joined and re.search(r"(?<!`)acme/app#", joined) is None
    texts = gi.note_texts(str(tmp_path))
    assert any("fixes #99" in t for t in texts) and any("@alice" in t for t in texts)


def test_end_to_end_from_fetch_script(tmp_path):
    out = tmp_path / "out"
    env = {**os.environ, "PATH": os.pathsep.join([str(FIXTURES / "bin"), os.environ.get("PATH", "")])}
    for k in ("GH_SHIM_FAIL", "GH_HOST"):
        env.pop(k, None)
    fetch = subprocess.run(
        ["bash", str(SCRIPT), "acme/app", "123", str(out)], capture_output=True, text=True, env=env, timeout=120
    )
    assert fetch.returncode == 0, fetch.stderr
    proc = subprocess.run(
        [sys.executable, "-I", "-m", "norn.pipelines._github_issue", "notes", str(out)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    md = (out / "issue.md").read_text()
    assert md.index("## Fetch notes") < md.index("## Description")
    section = md.split("## Fetch notes\n", 1)[1].split("\n## ", 1)[0]
    bullets = [line[2:] for line in section.splitlines() if line.startswith("- ")]
    printed = [line[len("NOTE: "):] for line in proc.stdout.splitlines() if line.startswith("NOTE: ")]
    assert bullets == printed == [n.text for n in gi.read_fetch_notes(str(out))]
    joined = "\n".join(bullets)
    assert "not rendered by GitHub" in joined
    assert "c0de0000-2222-4222-8222-222222222222" in joined
    assert "drive-by (NONE, comment 2)" in joined
    assert "1 hidden HTML comment(s)" in joined
    assert "an open pull request already references this issue: acme/app#130" in joined
    assert "fixes #99" in joined and "@alice" in joined

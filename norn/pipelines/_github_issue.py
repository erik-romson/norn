"""Private helper for fix_issue_v1 -- the offline half of the GitHub issue source.

Parses a GitHub issue reference, reads the launch repo's ``owner/repo`` and host
from ``origin`` (resolving SSH aliases with ``ssh -G``, which reads the ssh
config and opens no connection), enforces one ``gh`` host per run, and builds
the artifact paths under ``tmp/github/<slug>/``.

Importing this module runs nothing: no subprocess, no argv parsing.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, NamedTuple
from urllib.parse import urlsplit

from norn.pipelines import _fix_jira_v3
from norn.pipelines._jira_issue import ArtifactPaths

DEFAULT_HOST = "github.com"

_NUMBER_RE = re.compile(r"[1-9][0-9]*", re.ASCII)
_ANY_DIGITS_RE = re.compile(r"#?\d+")  # unicode digits too: refused, not "not GitHub"
_BARE_RE = re.compile(r"#?([1-9][0-9]*)", re.ASCII)
_OWNER_RE = re.compile(r"[A-Za-z0-9_-]+", re.ASCII)
_REPO_RE = re.compile(r"[A-Za-z0-9._-]+", re.ASCII)
_ALIAS_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*", re.ASCII)
_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")
_SCP_RE = re.compile(r"(?:[^@/:]+@)?([^/:]+):(?!/)(.+)")
_NOT_ISSUE_KINDS = {"pull", "pulls", "discussions"}


class GithubIssueRef(NamedTuple):
    host: str
    owner: str
    repo: str
    number: int
    same_repo: bool

    @property
    def canonical(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"

    @property
    def slug(self) -> str:
        if self.same_repo:
            return str(self.number)
        digest = hashlib.sha256(self.canonical.encode()).hexdigest()[:8]
        return f"{self.owner}-{self.repo}-{self.number}-{digest}"

    @property
    def short_ref(self) -> str:
        return f"#{self.number}" if self.same_repo else self.canonical

    @property
    def url(self) -> str:
        return f"https://{self.host}/{self.owner}/{self.repo}/issues/{self.number}"


def _check_owner(owner: str, token: str) -> None:
    if not _OWNER_RE.fullmatch(owner):
        raise ValueError(f"fix_issue_v1: {token!r} has an invalid owner name {owner!r}")


def _check_repo(repo: str, token: str) -> None:
    if not _REPO_RE.fullmatch(repo) or repo in {".", ".."} or ".." in repo:
        raise ValueError(f"fix_issue_v1: {token!r} has an invalid repository name {repo!r}")


def _check_number(text: str, token: str) -> int:
    if not _NUMBER_RE.fullmatch(text):
        raise ValueError(f"fix_issue_v1: {token!r} has an invalid issue number {text!r}")
    return int(text)


def parse_github_ref(token: str) -> tuple[str | None, str | None, str | None, int] | None:
    """Return ``(host, owner, repo, number)`` for a GitHub issue reference, else ``None``.

    ``None`` means the token is not a GitHub reference (a Jira key or URL, ``auto``,
    ...). A token that is one but is malformed raises ``ValueError``.
    """
    if _ANY_DIGITS_RE.fullmatch(token):
        m = _BARE_RE.fullmatch(token)
        if not m:
            raise ValueError(f"fix_issue_v1: {token!r} is not a valid issue number")
        return (None, None, None, int(m.group(1)))

    if _URL_RE.match(token):
        return _parse_url(token)

    if "/" in token and "#" in token:
        left, _, num = token.partition("#")
        owner, _, repo = left.partition("/")
        _check_owner(owner, token)
        _check_repo(repo, token)
        return (None, owner, repo, _check_number(num, token))
    return None


def _parse_url(token: str) -> tuple[str | None, str | None, str | None, int] | None:
    parts = urlsplit(token)
    if parts.scheme.lower() not in ("http", "https"):
        return None
    if "@" in parts.netloc:
        raise ValueError("fix_issue_v1: issue URL must not contain credentials (userinfo)")
    host = (parts.hostname or "").lower()
    if host == "api.github.com":
        raise ValueError("fix_issue_v1: api.github.com is the API host; use the issue's web URL")
    port = parts.port
    if host.startswith("www."):
        host = host[len("www."):]
    if port not in (None, 443):
        host = f"{host}:{port}"

    segments = parts.path.split("/")
    if segments and segments[0] == "":
        segments = segments[1:]
    if segments and segments[-1] == "":
        segments = segments[:-1]
    if len(segments) < 3:
        return None
    owner, repo, kind = segments[0], segments[1], segments[2].lower()
    if kind in _NOT_ISSUE_KINDS:
        raise ValueError(f"fix_issue_v1: {token!r} is a {kind} URL, not an issue; fix_issue_v1 takes issues")
    if kind != "issues":
        return None
    _check_owner(owner, token)
    _check_repo(repo, token)
    if len(segments) != 4:
        raise ValueError(f"fix_issue_v1: {token!r} is not an issue URL of the form <host>/<owner>/<repo>/issues/<n>")
    return (host, owner, repo, _check_number(segments[3], token))


def ssh_hostname(alias: str) -> str:
    """Return the real host behind an SSH alias, via ``ssh -G`` (no connection is opened)."""
    if not _ALIAS_RE.fullmatch(alias):
        raise ValueError(f"fix_issue_v1: origin's SSH host {alias!r} is not a plain host name or alias")

    def refuse(reason: str) -> ValueError:
        return ValueError(
            f"fix_issue_v1: cannot tell which host origin's SSH alias {alias} is on: "
            f"ssh -G {alias} {reason}; fix the ssh config or set origin to an https URL"
        )

    try:
        proc = subprocess.run(
            ["ssh", "-G", alias],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=10,
        )
    except OSError as exc:
        raise refuse(f"could not run ({exc})") from exc
    except subprocess.TimeoutExpired as exc:
        raise refuse("timed out") from exc
    if proc.returncode != 0:
        raise refuse(f"exited {proc.returncode}")
    for line in proc.stdout.splitlines():
        fields = line.split(None, 1)
        if len(fields) == 2 and fields[0].lower() == "hostname":
            host = fields[1].strip().lower()
            return DEFAULT_HOST if host == "ssh.github.com" else host
    raise refuse("printed no hostname")


class _Origin(NamedTuple):
    host: str
    owner: str
    repo: str
    ssh_alias: str | None
    display: str  # origin without userinfo, safe to show


def _split_repo_path(path: str) -> tuple[str, str] | None:
    path = path.lstrip("/")
    if path.endswith("/"):
        path = path[:-1]
    if path.endswith(".git"):
        path = path[: -len(".git")]
    segments = path.split("/")
    if len(segments) != 2 or not all(segments):
        return None
    if not _OWNER_RE.fullmatch(segments[0]) or not _REPO_RE.fullmatch(segments[1]):
        return None
    return segments[0], segments[1]


def _read_origin(project_dir: str, ssh_host: Callable[[str], str]) -> _Origin | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_dir), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise ValueError(f"cannot run git: {exc}") from exc
    if proc.returncode != 0:
        return None
    url = proc.stdout.strip()
    if not url:
        return None

    if _URL_RE.match(url):
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https", "git", "ssh"):
            return None
        repo_path = _split_repo_path(parts.path)
        if repo_path is None or not parts.hostname:
            return None
        written = parts.hostname.lower()
        if scheme == "ssh":
            return _Origin(ssh_host(written).lower(), *repo_path, written, f"{scheme}://{written}/{repo_path[0]}/{repo_path[1]}")
        return _Origin(written, *repo_path, None, f"{scheme}://{written}/{repo_path[0]}/{repo_path[1]}")

    m = _SCP_RE.fullmatch(url)
    if not m:
        return None
    written = m.group(1).lower()
    repo_path = _split_repo_path(m.group(2))
    if repo_path is None:
        return None
    return _Origin(ssh_host(written).lower(), *repo_path, written, f"{written}:{repo_path[0]}/{repo_path[1]}")


def launch_repo(
    project_dir: str,
    *,
    ssh_host: Callable[[str], str] = ssh_hostname,
) -> tuple[str, str, str] | None:
    """Return ``(host, owner, repo)`` of ``origin`` in *project_dir*, or ``None``."""
    origin = _read_origin(project_dir, ssh_host)
    if origin is None:
        return None
    return origin.host, origin.owner, origin.repo


def resolve_github_ref(
    token: str,
    project_dir: str,
    *,
    gh_host: str | None,
    ssh_host: Callable[[str], str] = ssh_hostname,
) -> GithubIssueRef:
    """Resolve *token* to a :class:`GithubIssueRef` for the run's single ``gh`` host."""
    parsed = parse_github_ref(token)
    if parsed is None:
        raise ValueError(f"fix_issue_v1: {token!r} is not a GitHub issue reference")
    url_host, owner, repo, number = parsed
    run_host = (gh_host or DEFAULT_HOST).lower()

    if url_host is not None and url_host != run_host:
        raise ValueError(
            f"fix_issue_v1: {token} is on {url_host}, but gh uses {run_host} for this run; "
            f"set GH_HOST={url_host} to use that host"
        )

    origin = _read_origin(project_dir, ssh_host)
    if origin is not None and origin.host != run_host:
        resolved = f" (resolved with ssh -G {origin.ssh_alias})" if origin.ssh_alias else ""
        raise ValueError(
            f"fix_issue_v1: origin {origin.display}{resolved} is on {origin.host}, "
            f"but gh uses {run_host} for this run; set GH_HOST={origin.host} to use that host"
        )

    if owner is None or repo is None:
        if origin is None:
            raise ValueError(
                f"fix_issue_v1: cannot read owner/repo from origin in {project_dir}; "
                "pass owner/repo#N or the issue URL"
            )
        return GithubIssueRef(run_host, origin.owner.lower(), origin.repo.lower(), number, True)

    owner, repo = owner.lower(), repo.lower()
    same = origin is not None and (origin.owner.lower(), origin.repo.lower()) == (owner, repo)
    return GithubIssueRef(run_host, owner, repo, number, same)


def artifact_paths(project_dir: str, ref: GithubIssueRef) -> ArtifactPaths:
    """Return the artifact paths for *ref* under ``<project_dir>/tmp/github/<slug>/``."""
    base = Path(project_dir) / "tmp" / "github" / ref.slug
    return ArtifactPaths(
        dir=str(base) + "/",
        issue_json=str(base / "issue.json"),
        issue_md=str(base / "issue.md"),
        attachments=str(base / "attachments"),
        preplan=str(base / f"issue-{ref.slug}-preplan.md"),
    )


# --- Fetch notes ---------------------------------------------------------------

FETCH_NOTES_FILE = "fetch-notes.json"

_WRITE_ACCESS = {"OWNER", "MEMBER", "COLLABORATOR"}
_HTML_COMMENT_RE = re.compile(r"<!--[\s\S]*?-->")
_CLOSING_RE = re.compile(
    r"\b(close[sd]?|fix(e[sd])?|resolve[sd]?)\b:?\s+([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#[0-9]+",
    re.IGNORECASE,
)
_MENTION_RE = re.compile(r"(?<![\w`])@[A-Za-z0-9][A-Za-z0-9-]*")
_WHAT_CLOSING = "a closing keyword, which also closes that issue when the PR merges"
_WHAT_MENTION = "a mention, which notifies that user when the draft PR opens"
_WHAT_ATTRIBUTION = (
    "text the attribution check rejects; pr body still complete will fail until the line is edited at gate 4"
)
_RETRY_NOTE = "retry the fetch stage to try those downloads again"
_RETRYABLE_RE = re.compile(r"curl exit \d+|HTTP 5\d\d")
_EXPIRED = "signed URL expired before download"


class FetchNote(NamedTuple):
    text: str  # full note: issue.md, stage output, run summary
    pr_text: str  # PR-safe form: no third-party free text


def _same(text: str) -> FetchNote:
    return FetchNote(text, text)


def _tick(value: str) -> str:
    return f"`{value}`"


def _read_manifest(path: Path) -> list[list[str]]:
    lines = path.read_text(encoding="utf-8").split("\n")
    rows = []
    for line in lines[1:]:  # line 0 is the header
        if line.strip("\r"):
            rows.append(line.rstrip("\r").split("\t"))
    for row in rows:
        if len(row) != 6:
            raise ValueError(f"{path}: expected 6 tab-separated columns, got {len(row)}: {row!r}")
    return rows


def _linked_open_prs(relations: dict) -> list[dict]:
    nodes = [n for n in (relations.get("closedByPullRequestsReferences") or {}).get("nodes") or [] if n]
    for item in (relations.get("timelineItems") or {}).get("nodes") or []:
        source = (item or {}).get("source")
        if source and source.get("__typename") == "PullRequest":
            nodes.append(source)
    seen: set[str] = set()
    out = []
    for node in nodes:
        ref = f"{(node.get('repository') or {}).get('nameWithOwner') or '?'}#{node.get('number')}"
        if ref in seen:
            continue
        seen.add(ref)
        if node.get("state") == "OPEN":
            out.append({"ref": ref, "title": node.get("title") or ""})
    return out


def fetch_notes(out_dir: str) -> list[FetchNote]:
    """Compute the fetch notes for the finished fetch in *out_dir*."""
    base = Path(out_dir)
    doc = json.loads((base / "issue.json").read_text(encoding="utf-8"))
    issue = doc["issue"]
    comments = doc.get("comments") or []
    relations = doc.get("relations") or {}
    repo = doc["repo"]
    notes: list[FetchNote] = []

    if issue.get("state") == "closed":
        notes.append(_same(f"issue is closed ({issue.get('state_reason') or 'no reason given'})"))

    for pr in _linked_open_prs(relations):
        prefix = "an open pull request already references this issue: "
        notes.append(FetchNote(f"{prefix}{pr['ref']} {pr['title']}", f"{prefix}{_tick(pr['ref'])}"))

    rows = _read_manifest(base / "attachments" / "manifest.tsv")
    statuses = []
    for index, _path, source_url, _ctype, _bytes, status in rows:
        if status == "downloaded":
            continue
        statuses.append(status)
        head = f"attachment {index} not downloaded ({status}): "
        notes.append(FetchNote(head + source_url, head + _tick(source_url)))
    if any(_RETRYABLE_RE.fullmatch(s) or s == _EXPIRED for s in statuses):
        notes.append(_same(_RETRY_NOTE))

    html_url = issue.get("html_url") or ""
    host = (urlsplit(html_url).hostname or "").lower()
    if host != DEFAULT_HOST:
        notes.append(
            _same(f"attachments are downloaded only from github.com; files pasted on {host} were not fetched")
        )

    writers = [(issue, "issue")] + [(c, f"comment {i}") for i, c in enumerate(comments, 1)]
    outsiders = [
        (((w.get("user") or {}).get("login") or "--"), w.get("author_association") or "--", where)
        for w, where in writers
        if w.get("author_association") not in _WRITE_ACCESS
    ]
    if outsiders:
        head = f"written by people without write access to {repo}: "
        notes.append(
            FetchNote(
                head + ", ".join(f"{login} ({assoc}, {where})" for login, assoc, where in outsiders),
                head + ", ".join(f"{_tick(login)} ({assoc}, {where})" for login, assoc, where in outsiders),
            )
        )

    hidden = sum(len(_HTML_COMMENT_RE.findall(w.get("body") or "")) for w, _ in writers)
    if hidden:
        notes.append(_same(f"{hidden} hidden HTML comment(s) removed from issue.md (kept in raw/)"))

    title = issue.get("title") or ""
    head = "the title goes on the PR body's Closes line and contains "
    pr_head = "the issue title on the Closes line contains "
    for regex, what in ((_CLOSING_RE, _WHAT_CLOSING), (_MENTION_RE, _WHAT_MENTION)):
        for m in regex.finditer(title):
            notes.append(FetchNote(f"{head}{what}: {m.group(0)}", f"{pr_head}{what}"))
    if _fix_jira_v3.attribution_lines(title):
        notes.append(FetchNote(f"{head}{_WHAT_ATTRIBUTION}: {title}", f"{pr_head}{_WHAT_ATTRIBUTION}"))
    return notes


def write_fetch_notes(out_dir: str, notes: list[FetchNote]) -> None:
    path = Path(out_dir) / FETCH_NOTES_FILE
    tmp = path.with_name(path.name + ".tmp")
    payload = {"notes": [{"text": n.text, "pr_text": n.pr_text} for n in notes]}
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_fetch_notes(out_dir: str) -> list[FetchNote]:
    path = Path(out_dir) / FETCH_NOTES_FILE
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read fetch notes {path}: {exc}") from exc
    items = doc.get("notes") if isinstance(doc, dict) else None
    if not isinstance(items, list):
        raise ValueError(f"{path}: expected an object with a 'notes' list")
    notes = []
    for item in items:
        if not (
            isinstance(item, dict) and isinstance(item.get("text"), str) and isinstance(item.get("pr_text"), str)
        ):
            raise ValueError(f"{path}: every note needs string 'text' and 'pr_text': {item!r}")
        notes.append(FetchNote(item["text"], item["pr_text"]))
    return notes


def note_texts(out_dir: str) -> list[str]:
    return [n.text for n in read_fetch_notes(out_dir)]


def pr_checklist_items(out_dir: str) -> list[str]:
    return [f"- [ ] fetch note: {n.pr_text}" for n in read_fetch_notes(out_dir)]


def add_notes_to_issue_md(out_dir: str, notes: list[FetchNote]) -> None:
    """Put a ``## Fetch notes`` section directly before ``## Description`` in issue.md."""
    path = Path(out_dir) / "issue.md"
    lines = path.read_text(encoding="utf-8").split("\n")
    if "## Fetch notes" in lines:
        start = lines.index("## Fetch notes")
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
        del lines[start:end]
    if "## Description" not in lines:
        raise ValueError(f"{path}: no '## Description' heading to put the fetch notes before")
    at = lines.index("## Description")
    bullets = [f"- {n.text}" for n in notes] or ["none"]
    lines[at:at] = ["## Fetch notes", "", *bullets, ""]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    os.replace(tmp, path)


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "notes":
        print("Usage: python -I -m norn.pipelines._github_issue notes <OUT_DIR>", file=sys.stderr)
        return 2
    out_dir = argv[1]
    notes = fetch_notes(out_dir)
    write_fetch_notes(out_dir, notes)
    add_notes_to_issue_md(out_dir, notes)
    if notes:
        for note in notes:
            print(f"NOTE: {note.text}")
    else:
        print("notes: none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

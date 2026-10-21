"""Private helper for fix_issue_v1 -- the one home of the per-source values.

``resolve_issue`` walks the pipeline's argv, decides whether the single issue
reference is Jira or GitHub, and returns a frozen :class:`IssueSource` with every
per-source value the pipeline needs: paths, subject prefix, fetch command,
preflight fragments, PR-body line and fetch-note readers. For Jira it reproduces
``fix_jira_issue_v3`` exactly; for GitHub it builds the values from
``_github_issue``.

Importing this module runs nothing: no subprocess, no argv parsing. All work
happens inside ``resolve_issue``.
"""
from __future__ import annotations

import functools
import os
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from norn.pipelines import _fix_jira_v3, _github_issue, _jira_issue
from norn.pipelines._jira_issue import ArtifactPaths

PIPELINE = "fix_issue_v1"

_PIPELINE_DIR = Path(__file__).parent
_JIRA_FETCH = str(_PIPELINE_DIR / "_jira_fetch.sh")
_GITHUB_FETCH = str(_PIPELINE_DIR / "_github_fetch.sh")

_MAX_PREFIX = 60
_SUBJECT_LIMIT = 72

# Copied from fix_jira_issue_v3.py:204-208 (cannot be imported: import-time argv parsing)
_ENV_SOURCES_HINT = (
    "norn reads it from the environment, ~/.norn/env or .norn.env in the launch "
    "directory (KEY=value lines). The env block of .claude/settings.local.json is "
    "applied only by Claude Code, not to norn."
)

# Copied from fix_jira_issue_v3.py:216-218 (cannot be imported: import-time argv parsing)
_JIRA_ENV_CHECK_SH = (
    f'[ -n "$JIRA_AUTH" ] || {{ echo "ERROR: JIRA_AUTH is empty. {_ENV_SOURCES_HINT}"; exit 1; }} && '
    f'[ -n "$JIRA_BASE" ] || {{ echo "ERROR: JIRA_BASE is empty. {_ENV_SOURCES_HINT}"; exit 1; }} && '
    'case "$JIRA_BASE" in */) ;; *) echo "ERROR: JIRA_BASE must end with /"; exit 1;; esac && '
)


@dataclass(frozen=True)
class IssueSource:
    kind: str
    display: str
    identity: str
    file_key: str
    subject_prefix: str
    resume_arg: str
    default_branch: str
    env_check_sh: str
    issue_check_sh: str
    fetch_cmd: str
    issue_label: str
    branch_reuse: bool
    art: ArtifactPaths
    jira_browse: str | None
    issue_line: Callable[[str], str] | None = field(compare=False)
    issue_line_ok: Callable[[str], bool] | None = field(compare=False)
    extra_issue_files: tuple[str, ...]
    fetch_notes: Callable[[], list[str]] | None = field(compare=False)
    fetch_checklist: Callable[[], list[str]] | None = field(compare=False)
    notes_path: str | None


def _is_pipeline_ref(token: str) -> bool:
    return token == PIPELINE or token.endswith(f"{PIPELINE}.py")


def resolve_issue(
    argv: list[str],
    project_dir: str,
    args: Mapping[str, str],
    environ: Mapping[str, str],
) -> tuple[IssueSource, bool]:
    """Return the :class:`IssueSource` for *argv* and the ``auto`` flag."""
    auto = False
    refs: list[tuple[str, str]] = []  # (kind, token)
    for index, token in enumerate(_fix_jira_v3.positional_tokens(argv)):
        if token == "auto":
            auto = True
        elif token == "run" and index == 0:
            continue
        elif _is_pipeline_ref(token):
            continue
        elif token == "stop":
            raise ValueError(
                "fix_issue_v1 always pauses at the reviewed plan; `stop` is a "
                "fix_jira_issue_v2 positional. Use `auto` to run hands-off or "
                "--arg plan_gate=off to skip the plan pause."
            )
        elif _github_issue.parse_github_ref(token) is not None:
            refs.append(("github", token))
        elif _jira_issue._KEY_RE.match(token):
            refs.append(("jira", token))
        elif m := _jira_issue._BROWSE_RE.search(token):
            refs.append(("jira", m.group(1)))
        else:
            raise ValueError(
                f"fix_issue_v1: unrecognised argument {token!r}. Accepted: a Jira key "
                "(CBS-1234), a Jira /browse/ URL, a GitHub issue number (123, or '#123' "
                "in quotes), owner/repo#123, a GitHub issue URL, and the literal 'auto' "
                "(lower-case)."
            )

    # `--arg auto=on` means the same as the positional: it is what the norn ui checkbox sends.
    auto = auto or _fix_jira_v3.on_off_knob(dict(args), "auto", False)

    if not refs:
        raise ValueError(
            "fix_issue_v1 needs one issue reference: a Jira key (CBS-1234), a /browse/ "
            "URL, a GitHub issue number (123), owner/repo#123 or a GitHub issue URL. "
            "An unquoted #123 is a shell comment: write 123 or '#123'."
        )
    if len(refs) > 1:
        raise ValueError(
            "fix_issue_v1 found several issue references and cannot choose: "
            + ", ".join(token for _, token in refs)
        )

    kind, token = refs[0]
    if kind == "jira":
        source = _jira_source(token, project_dir, args, environ)
    else:
        source = _github_source(token, project_dir, args, environ)
    if len(source.subject_prefix) > _MAX_PREFIX:
        raise ValueError(
            f"fix_issue_v1: subject prefix {source.subject_prefix!r} is "
            f"{len(source.subject_prefix)} characters; more than {_MAX_PREFIX} leaves "
            f"too little of the {_SUBJECT_LIMIT}-character commit subject limit"
        )
    return source, auto


def _jira_source(
    key: str, project_dir: str, args: Mapping[str, str], environ: Mapping[str, str]
) -> IssueSource:
    # Copied from fix_jira_issue_v3.py:154-162 (cannot be imported: import-time argv parsing)
    # An explicit --arg wins over the env var; an empty value means "no link".
    raw_browse = (
        args["jira_browse"] if "jira_browse" in args else environ.get("JIRA_BROWSE_BASE", "")
    )
    jira_browse = raw_browse or None
    if jira_browse is not None and not jira_browse.endswith("/browse/"):
        raise ValueError(
            f"--arg jira_browse (or JIRA_BROWSE_BASE): must end in /browse/, got {jira_browse!r}"
        )

    art = _jira_issue.artifact_paths(project_dir, key)
    # Copied from fix_jira_issue_v3.py:296-298 (cannot be imported: import-time argv parsing)
    fetch_cmd = f"bash {shlex.quote(_JIRA_FETCH)} {shlex.quote(key)} {shlex.quote(art.dir)}"
    return IssueSource(
        kind="jira",
        display=key,
        identity=key,
        file_key=key,
        subject_prefix=key,
        resume_arg=key,
        default_branch=key,
        env_check_sh=_JIRA_ENV_CHECK_SH,
        issue_check_sh="",
        fetch_cmd=fetch_cmd,
        issue_label="Jira",
        branch_reuse=True,
        art=art,
        jira_browse=jira_browse,
        issue_line=None,
        issue_line_ok=None,
        extra_issue_files=(),
        fetch_notes=None,
        fetch_checklist=None,
        notes_path=None,
    )


def _github_source(
    token: str, project_dir: str, args: Mapping[str, str], environ: Mapping[str, str]
) -> IssueSource:
    ref = _github_issue.resolve_github_ref(token, project_dir, gh_host=environ.get("GH_HOST"))
    if "jira_browse" in args:
        raise ValueError("--arg jira_browse applies to Jira issues only")

    q = shlex.quote
    art = _github_issue.artifact_paths(project_dir, ref)
    file_key = f"issue-{ref.slug}"
    short_ref = ref.short_ref
    repo_path = f"{ref.owner}/{ref.repo}"
    heading_prefix = f"{ref.canonical}: "

    fetch_cmd = (
        f"bash {q(_GITHUB_FETCH)} {q(repo_path)} {ref.number} {q(art.dir)} && "
        f"{q(sys.executable)} -I -m norn.pipelines._github_issue notes {q(art.dir)}"
    )

    not_found = f"ERROR: cannot read {ref.canonical} (not found, or gh has no access)"
    is_pr = f"ERROR: {short_ref} is a pull request, not an issue"
    issue_check_sh = (
        f" && ISPR=$(gh api {q(f'repos/{repo_path}/issues/{ref.number}')} "
        f"--jq '.pull_request != null') || {{ echo {q(not_found)}; exit 1; }}"
        f" && [ \"$ISPR\" = false ] || {{ echo {q(is_pr)}; exit 1; }}"
    )
    if short_ref.startswith("#"):
        strip = (
            f"ERROR: git config commit.cleanup=strip would drop '{short_ref}:' commit "
            "subjects; set it to whitespace for this repo"
        )
        issue_check_sh += (
            f" && [ \"$(git config --get commit.cleanup)\" != strip ] || "
            f"{{ echo {q(strip)}; exit 1; }}"
        )

    closes_re = re.compile(rf"^Closes {re.escape(short_ref)}(\s|$)")

    return IssueSource(
        kind="github",
        display=short_ref,
        identity=ref.canonical,
        file_key=file_key,
        subject_prefix=short_ref,
        resume_arg=str(ref.number) if ref.same_repo else ref.url,
        default_branch=file_key,
        env_check_sh="",
        issue_check_sh=issue_check_sh,
        fetch_cmd=fetch_cmd,
        issue_label="GitHub",
        branch_reuse=False,
        art=art,
        jira_browse=None,
        issue_line=lambda summary: f"Closes {short_ref} — {summary.removeprefix(heading_prefix)}",
        issue_line_ok=lambda first: closes_re.match(first) is not None,
        extra_issue_files=(os.path.join(art.dir, _github_issue.FETCH_NOTES_FILE),),
        fetch_notes=functools.partial(_github_issue.note_texts, art.dir),
        fetch_checklist=functools.partial(_github_issue.pr_checklist_items, art.dir),
        notes_path=art.issue_md,
    )

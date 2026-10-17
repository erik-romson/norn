"""Validate and apply a whole-file commit regrouping for fix_jira_issue_v3.

Gate 4 of ``fix_jira_issue_v3`` asks an agent for a commit plan in the
``split-commit-plan`` skill's format, then regroups ``base..HEAD`` into the
plan's commits. This script is the deterministic half: it reads the plan,
checks it against git, and performs the rewrite.

Subcommands
-----------
``range    --repo R --base B --key K``
    Print ``{"start", "head", "tree", "count"}`` as JSON for ``base..HEAD``.
``check    --plan P --repo R --base B --key K --expect-head H``
    Validate the plan against the range and the house style.
``apply    --plan P --repo R --base B --key K --expect-head H``
    ``check``, then back up HEAD, soft-reset to ``base`` and commit each group.
``messages --repo R --key K --range A..B [--attribution-only]``
    Check the messages of the commits in a range.
``summary  --plan P``
    Print ``<n>. <subject> (<k> files)`` per group.

Exit codes: 0 ok, 1 problems (printed one per line on stdout), 2 usage error
(printed on stderr).

Every git call runs as ``git -C <toplevel>`` with an argv list (never a shell)
and its return code is checked, so the caller's cwd does not matter. Paths are
read with ``-z`` and passed with ``--literal-pathspecs``.

``apply`` never rolls back. Every failure after the backup is made prints
``restore: git reset --hard <ref>`` as its first line.

This script is **stdlib-only** and must not import anything from ``norn``. It
is invoked as ``python3 <abs path> <subcommand> ...``.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# House style — local copies
# ---------------------------------------------------------------------------
# Copied from norn/pipelines/_fix_jira_v3.py (STRICT_ATTRIBUTION_ERE,
# attribution_lines, subject_problems): this script is stdlib-only and may not
# import norn. tests/test_commit_rewrite_v3.py::test_local_subject_rules_agree_with_fix_jira_v3
# keeps the two copies in step, so change both together.

STRICT_ATTRIBUTION_ERE = (
    r"^\s*(co-authored-by|claude-session)\s*:|generated (with|by) \[?claude|\U0001F916"
)
_STRICT_ATTRIBUTION_RE = re.compile(STRICT_ATTRIBUTION_ERE, re.I | re.M)


def attribution_lines(text: str) -> list[str]:
    """Return the lines of *text* that carry attribution."""
    return [line for line in text.splitlines() if _STRICT_ATTRIBUTION_RE.search(line)]


def subject_problems(message: str, key: str) -> list[str]:
    """Return one string per house-style problem in commit *message*."""
    problems: list[str] = []
    lines = message.splitlines()
    subject = lines[0] if lines else ""
    if not re.match(rf"^{re.escape(key)}: [a-z0-9]", subject):
        problems.append(
            f"subject must start with '{key}: ' followed by a lower-case word, got {subject!r}"
        )
    if len(subject) > 72:
        problems.append(f"subject is {len(subject)} characters; the limit is 72")
    if subject.endswith("."):
        problems.append("subject must not end with '.'")
    if len(lines) > 1:
        if lines[1].strip():
            problems.append("line 2 must be blank when there is a body")
        body = [ln for ln in lines[1:] if ln.strip()]
        if len(body) > 4:
            problems.append(f"body has {len(body)} non-blank lines; the limit is 4")
    for line in attribution_lines(message):
        problems.append(f"attribution line: {line.strip()!r}")
    return problems


# ---------------------------------------------------------------------------
# Errors and git
# ---------------------------------------------------------------------------


class UsageError(Exception):
    """A command-line problem: exit 2."""


class GitError(Exception):
    """A git call exited with a code its caller did not expect."""

    def __init__(self, argv: list[str], returncode: int, stdout: bytes, stderr: bytes) -> None:
        super().__init__(f"git {' '.join(argv[:6])} failed (exit {returncode})")
        self.argv = argv
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def output_lines(self) -> list[str]:
        """The command's own output, indented, blank lines dropped."""
        out: list[str] = []
        for stream in (self.stdout, self.stderr):
            for line in _decode(stream).splitlines():
                if line.strip():
                    out.append(f"  {line.rstrip()}")
        return out

    def lines(self) -> list[str]:
        shown = " ".join(self.argv[:6]) + (" …" if len(self.argv) > 6 else "")
        return [f"git {shown} failed (exit {self.returncode})", *self.output_lines()]


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="surrogateescape")


def _git(root: str, *args: str, ok: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[bytes]:
    """Run ``git -C <root> <args>``; raise ``GitError`` on an exit code not in *ok*."""
    proc = subprocess.run(["git", "-C", root, *args], capture_output=True)
    if proc.returncode not in ok:
        raise GitError(list(args), proc.returncode, proc.stdout, proc.stderr)
    return proc


def _text(proc: subprocess.CompletedProcess[bytes]) -> str:
    return _decode(proc.stdout).strip()


def _nul_list(data: bytes) -> list[str]:
    return [_decode(part) for part in data.split(b"\0") if part]


def _rev(root: str, spec: str) -> str | None:
    """Full sha of *spec*, or ``None`` when it does not resolve."""
    proc = _git(root, "rev-parse", "--verify", "--quiet", spec, ok=(0, 1))
    return _text(proc) if proc.returncode == 0 else None


def _status(root: str) -> list[tuple[str, str]]:
    """``(XY, path)`` per entry of ``git status``, untracked files included."""
    data = _git(root, "status", "--porcelain", "-z", "--untracked-files=all").stdout
    fields = data.split(b"\0")
    entries: list[tuple[str, str]] = []
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        xy = entry[:2].decode("ascii", errors="replace")
        entries.append((xy, _decode(entry[3:])))
        # A rename or copy record is followed by its original path.
        if xy[0] in "RC" or xy[1] in "RC":
            i += 1
    return entries


def toplevel(repo: str) -> str:
    """Return the top level of *repo*; ``UsageError`` unless *repo* is that top level."""
    proc = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--show-toplevel"], capture_output=True
    )
    if proc.returncode != 0:
        detail = _decode(proc.stderr).strip().splitlines()
        raise UsageError(
            f"--repo {repo}: not a git work tree" + (f" ({detail[0]})" if detail else "")
        )
    top = _text(proc)
    if os.path.realpath(repo) != os.path.realpath(top):
        raise UsageError(f"--repo {repo} is not the top level of its repository ({top})")
    return top


# ---------------------------------------------------------------------------
# Range
# ---------------------------------------------------------------------------


@dataclass
class Range:
    base: str
    head: str
    start: str
    tree: str
    commits: list[str]


def range_problems(root: str, base_arg: str) -> tuple[list[str], str | None, Range | None]:
    """Problems with ``base..HEAD``, the resolved base, and the range when it has commits."""
    problems: list[str] = []
    if _git(root, "symbolic-ref", "-q", "HEAD", ok=(0, 1)).returncode == 1:
        problems.append("HEAD is detached; switch to the branch the run is on")
    head = _rev(root, "HEAD^{commit}")
    if head is None:
        problems.append("HEAD has no commit")
        return problems, None, None
    base = _rev(root, f"{base_arg}^{{commit}}")
    if base is None:
        problems.append(f"base {base_arg} is not a commit")
        return problems, None, None
    anc = _git(root, "merge-base", "--is-ancestor", base, head, ok=(0, 1))
    if anc.returncode == 1:
        problems.append(f"base {base[:7]} is not an ancestor of HEAD {head[:7]}")
        return problems, base, None
    commits = _text(_git(root, "rev-list", "--reverse", f"{base}..{head}")).split()
    if not commits:
        problems.append(f"no commits in base..HEAD ({base[:7]}..{head[:7]})")
        return problems, base, None

    merges = _text(_git(root, "rev-list", "--merges", f"{base}..{head}")).split()
    if merges:
        problems.append(
            f"merge commits in base..HEAD ({', '.join(m[:7] for m in merges)}): "
            "rebase onto base.sha to remove merge commits, then --resume"
        )

    tree = _text(_git(root, "rev-parse", f"{head}^{{tree}}"))
    if _text(_git(root, "rev-parse", f"{base}^{{tree}}")) == tree:
        problems.append(
            f"nothing to ship: base..HEAD ({base[:7]}..{head[:7]}) has no net change"
        )

    email_proc = _git(root, "config", "user.email", ok=(0, 1))
    if email_proc.returncode == 1:
        problems.append("git config user.email is not set; the rewrite needs it to author commits")
    else:
        email = _text(email_proc)
        authors = _text(
            _git(
                root,
                "rev-list",
                "--no-commit-header",
                "--format=%H %ae",
                f"{base}..{head}",
            )
        )
        for line in authors.splitlines():
            sha, _, author = line.partition(" ")
            if author != email:
                problems.append(f"the rewrite would re-author {sha[:7]} by {author}")

    return problems, base, Range(base=base, head=head, start=commits[0], tree=tree, commits=commits)


# ---------------------------------------------------------------------------
# Plan parser
# ---------------------------------------------------------------------------

_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_GROUP_RE = re.compile(r"^##\s+Group\s+(\d+)\s*[—–-]\s*(.*?)\s*$")
_SECTION_END_RE = re.compile(r"^#{1,2}\s")
_START_RE = re.compile(r"^\s*[-*+]\s+\*\*Start commit \(inclusive\):\*\*\s*`([^`]*)`", re.I)
_HEAD_RE = re.compile(r"^\s*[-*+]\s+\*\*HEAD at plan time:\*\*\s*`([^`]*)`", re.I)
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*?)\s*$")
_PATH_RE = re.compile(r"^`([^`]+)`")
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")

_MESSAGE_LABEL = "**Commit message:**"
_FILES_LABEL = "**Files (whole-file):**"
_HUNKS_LABEL = "**Hunks (partial-file):**"


@dataclass
class _Fence:
    start: int
    end: int
    info: str
    body: list[str]


@dataclass
class Group:
    number: int
    title: str
    message: str = ""
    files: list[str] = field(default_factory=list)
    hunks: list[str] = field(default_factory=list)
    bad_bullets: list[str] = field(default_factory=list)

    @property
    def subject(self) -> str:
        return self.message.splitlines()[0] if self.message else ""


@dataclass
class Plan:
    start: str | None
    head: str | None
    groups: list[Group]
    problems: list[str]


def _closes(line: str, marker: str) -> bool:
    stripped = line.strip()
    indent = len(line) - len(line.lstrip(" "))
    return indent <= 3 and len(stripped) >= len(marker) and set(stripped) == {marker[0]}


def _fences(lines: list[str]) -> tuple[list[_Fence], list[bool], list[str]]:
    """Fenced blocks, an in-fence flag per line, and unclosed-fence problems."""
    blocks: list[_Fence] = []
    inside = [False] * len(lines)
    problems: list[str] = []
    i = 0
    while i < len(lines):
        m = _FENCE_OPEN_RE.match(lines[i])
        if not m:
            i += 1
            continue
        marker, info = m.group(1), m.group(2).strip()
        j = i + 1
        while j < len(lines) and not _closes(lines[j], marker):
            j += 1
        if j >= len(lines):
            problems.append(f"line {i + 1}: code fence is never closed")
            for k in range(i, len(lines)):
                inside[k] = True
            break
        for k in range(i, j + 1):
            inside[k] = True
        blocks.append(_Fence(start=i, end=j, info=info, body=lines[i + 1:j]))
        i = j + 1
    return blocks, inside, problems


def _trim_blank(lines: list[str]) -> list[str]:
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def _label_index(lines: list[str], inside: list[bool], lo: int, hi: int, label: str) -> int | None:
    for k in range(lo, hi):
        if not inside[k] and lines[k].strip().startswith(label):
            return k
    return None


def _bullets_after(lines: list[str], inside: list[bool], label: int, hi: int) -> list[str]:
    """Bullet texts directly under the line *label*, blank lines skipped."""
    found: list[str] = []
    for k in range(label + 1, hi):
        if inside[k]:
            break
        if not lines[k].strip():
            continue
        m = _BULLET_RE.match(lines[k])
        if not m:
            break
        found.append(m.group(1))
    return found


def parse_plan(text: str) -> Plan:
    """Parse a ``split-commit-plan`` file. The ``## Proposed commits`` table is ignored."""
    lines = text.splitlines()
    blocks, inside, problems = _fences(lines)

    start = head = None
    for k, line in enumerate(lines):
        if inside[k]:
            continue
        if start is None and (m := _START_RE.match(line)):
            start = m.group(1).strip()
        if head is None and (m := _HEAD_RE.match(line)):
            head = m.group(1).strip()

    heads = [k for k, line in enumerate(lines) if not inside[k] and _GROUP_RE.match(line)]
    groups: list[Group] = []
    for h in heads:
        m = _GROUP_RE.match(lines[h])
        assert m is not None
        end = next(
            (k for k in range(h + 1, len(lines)) if not inside[k] and _SECTION_END_RE.match(lines[k])),
            len(lines),
        )
        group = Group(number=int(m.group(1)), title=m.group(2))

        label = _label_index(lines, inside, h + 1, end, _MESSAGE_LABEL)
        if label is not None:
            block = next(
                (b for b in blocks if label < b.start < end and b.info == ""), None
            )
            if block is not None:
                group.message = "\n".join(_trim_blank(block.body))

        label = _label_index(lines, inside, h + 1, end, _FILES_LABEL)
        if label is not None:
            for bullet in _bullets_after(lines, inside, label, end):
                pm = _PATH_RE.match(bullet)
                if pm:
                    group.files.append(pm.group(1))
                else:
                    group.bad_bullets.append(bullet)

        label = _label_index(lines, inside, h + 1, end, _HUNKS_LABEL)
        if label is not None:
            group.hunks = _bullets_after(lines, inside, label, end)

        groups.append(group)

    if not groups:
        problems.append("the plan has no '## Group <n> — <title>' sections")
    return Plan(start=start, head=head, groups=groups, problems=problems)


def read_plan(path: str) -> Plan:
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise UsageError(f"cannot read --plan {path}: {exc}") from exc
    return parse_plan(text)


def group_problems(plan: Plan, key: str) -> list[str]:
    """Problems of the plan's own shape and of each group's message."""
    problems = list(plan.problems)
    for g in plan.groups:
        if not g.files:
            problems.append(f"group {g.number} has no files under {_FILES_LABEL}")
        for bullet in g.bad_bullets:
            problems.append(f"group {g.number}: file bullet without a backticked path: {bullet}")
        for bullet in g.hunks:
            problems.append(
                f"group {g.number}: hunk bullets are refused (assign whole files only): {bullet}"
            )
        if not g.message:
            problems.append(
                f"group {g.number} has no commit message (a plain ``` block after {_MESSAGE_LABEL})"
            )
        else:
            problems += [f"group {g.number}: {p}" for p in subject_problems(g.message, key)]
    return problems


def coverage_problems(root: str, rng: Range, plan: Plan) -> list[str]:
    """Every changed path in exactly one group, and no group path outside the diff."""
    changed = _nul_list(
        _git(root, "diff", "-z", "--name-only", "--no-renames", rng.base, rng.head).stdout
    )
    changed_set = set(changed)
    listed: dict[str, list[int]] = {}
    for g in plan.groups:
        for path in g.files:
            listed.setdefault(path, []).append(g.number)
    problems: list[str] = []
    for path in changed:
        if path not in listed:
            problems.append(f"`{path}` is in no group")
    for path, numbers in listed.items():
        if len(numbers) > 1:
            where = ", ".join(str(n) for n in numbers)
            problems.append(f"`{path}` is listed more than once (groups {where})")
        if path not in changed_set:
            problems.append(
                f"group {numbers[0]} lists `{path}`, which base..HEAD does not change"
            )
    return problems


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    problems: list[str]
    rng: Range | None
    plan: Plan
    midway: bool = False


def _midway_ref(root: str, base: str | None, expect_head: str) -> str | None:
    """The newest backup ref an earlier ``apply`` of this range left, if any."""
    if base is None or _rev(root, f"{expect_head}^{{commit}}") is None:
        return None
    original = _text(_git(root, "rev-list", "--reverse", f"{base}..{expect_head}")).split()
    if not original:
        return None
    pattern = f"refs/backup/split-{original[0][:7]}-{expect_head[:7]}-*"
    refs = sorted(_text(_git(root, "for-each-ref", "--format=%(refname)", pattern)).split())
    return refs[-1] if refs else None


def run_check(root: str, plan_path: str, base_arg: str, key: str, expect_head: str) -> CheckResult:
    plan = read_plan(plan_path)
    problems, base, rng = range_problems(root, base_arg)

    head = _rev(root, "HEAD^{commit}")
    if head is not None and head != expect_head:
        ref = _midway_ref(root, base, expect_head)
        if ref is not None:
            # Everything else would only describe the half-rewritten HEAD.
            return CheckResult(
                problems=[f"a previous apply stopped midway; restore: git reset --hard {ref}"],
                rng=rng,
                plan=plan,
                midway=True,
            )
        problems.insert(
            0,
            f"HEAD moved after the plan: HEAD is {head[:7]}, the plan was made at "
            f"{expect_head[:7]}; undo the new commit, or start the ship phase over "
            "from a fresh run",
        )

    if plan.start is None:
        problems.append("the plan has no '**Start commit (inclusive):**' line")
    elif rng is not None and plan.start.lower() != rng.start:
        problems.append(
            f"the plan's Start commit is {plan.start}, but the first commit of "
            f"base..HEAD is {rng.start}"
        )
    if plan.head is None:
        problems.append("the plan has no '**HEAD at plan time:**' line")
    elif plan.head.lower() != expect_head:
        problems.append(
            f"the plan's HEAD at plan time is {plan.head}, but --expect-head is {expect_head}"
        )

    problems += group_problems(plan, key)
    if rng is not None:
        problems += coverage_problems(root, rng, plan)
    return CheckResult(problems=problems, rng=rng, plan=plan)


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


def _commit_group(root: str, group: Group) -> list[str]:
    """Stage and commit one group; problem lines, empty on success."""
    _git(root, "--literal-pathspecs", "add", "-f", "-A", "--", *group.files)
    staged = _git(root, "diff", "--cached", "--quiet", ok=(0, 1))
    if staged.returncode == 0:
        return [f"group {group.number} staged nothing"]
    fd, msg_path = tempfile.mkstemp(prefix="norn-commit-msg-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(group.message + "\n")
        try:
            _git(root, "commit", "-F", msg_path)
        except GitError as exc:
            return [
                f"group {group.number}: git commit failed (exit {exc.returncode}):",
                *exc.output_lines(),
            ]
    finally:
        os.unlink(msg_path)
    return []


def _rewrite(root: str, rng: Range, plan: Plan, ref: str) -> list[str]:
    """Steps (3)–(6) of ``apply``; problem lines, empty on success."""
    _git(root, "reset", "--soft", rng.base)
    _git(root, "reset", "-q")
    for group in plan.groups:
        problems = _commit_group(root, group)
        if problems:
            return problems

    backup_tree = _text(_git(root, "rev-parse", f"{ref}^{{tree}}"))
    new_tree = _text(_git(root, "rev-parse", "HEAD^{tree}"))
    if backup_tree != new_tree:
        stat = _decode(_git(root, "diff", "--stat", ref, "HEAD").stdout).splitlines()
        return [
            f"the rewritten tree {new_tree[:7]} differs from the backup tree {backup_tree[:7]}:",
            *(f"  {line.rstrip()}" for line in stat if line.strip()),
        ]

    dirty = _status(root)
    if dirty:
        tracked = [path for xy, path in dirty if xy != "??"]
        untracked = [path for xy, path in dirty if xy == "??"]
        problems = ["a hook changed files outside the commits:"]
        problems += [f"  {path}" for path in tracked]
        if untracked:
            problems.append(
                "untracked (git reset --hard does not remove these; delete them by hand):"
            )
            problems += [f"  {path}" for path in untracked]
        return problems
    return []


def _backup(root: str, rng: Range) -> tuple[str, str]:
    """Step (2): create the backup branch and ref at HEAD."""
    now = datetime.datetime.now()
    branch = _text(_git(root, "symbolic-ref", "--short", "HEAD"))
    name = f"backup/{branch}-{now:%Y%m%d}"
    if _rev(root, f"refs/heads/{name}") is not None:
        name = f"{name}-{now:%H%M%S}"
    _git(root, "branch", name)
    ref = f"refs/backup/split-{rng.start[:7]}-{rng.head[:7]}-{now:%Y%m%d%H%M%S}"
    _git(root, "update-ref", ref, "HEAD")
    return name, ref


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def _emit(lines: list[str]) -> None:
    out = sys.stdout.buffer
    for line in lines:
        out.write(line.encode("utf-8", errors="surrogateescape") + b"\n")
    out.flush()


def _report(problems: list[str]) -> int:
    if problems:
        _emit(problems)
        return 1
    return 0


def cmd_range(args: argparse.Namespace) -> int:
    root = toplevel(args.repo)
    problems, _base, rng = range_problems(root, args.base)
    if problems or rng is None:
        return _report(problems)
    _emit(
        [json.dumps({"start": rng.start, "head": rng.head, "tree": rng.tree, "count": len(rng.commits)})]
    )
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    root = toplevel(args.repo)
    return _report(run_check(root, args.plan, args.base, args.key, args.expect_head).problems)


def cmd_apply(args: argparse.Namespace) -> int:
    root = toplevel(args.repo)
    result = run_check(root, args.plan, args.base, args.key, args.expect_head)
    problems = list(result.problems)
    if not result.midway:
        dirty = _status(root)
        if dirty:
            problems.append(
                "the working tree is not clean; commit, ignore or delete them, then [r]etry:"
            )
            problems += [f"  {path}" for _xy, path in dirty]
    if problems or result.rng is None:
        return _report(problems)
    rng = result.rng

    name, ref = _backup(root, rng)
    missing = [made for made in (f"refs/heads/{name}", ref) if _rev(root, made) != rng.head]
    if missing:
        return _report(
            [f"backup {made} does not point at HEAD {rng.head[:7]}; nothing was rewritten" for made in missing]
        )
    restore = f"restore: git reset --hard {ref}"
    try:
        problems = _rewrite(root, rng, result.plan, ref)
    except GitError as exc:
        problems = exc.lines()
    except OSError as exc:
        problems = [f"rewrite stopped: {exc}"]
    if problems:
        _emit([restore, *problems])
        return 1
    _emit([f"backup branch: {name}", f"backup ref: {ref}"])
    return 0


def cmd_messages(args: argparse.Namespace) -> int:
    root = toplevel(args.repo)
    left, sep, right = args.range.partition("..")
    if not sep or not left or not right or right.startswith("."):
        raise UsageError(f"--range must be A..B, got {args.range!r}")
    commits = _text(_git(root, "rev-list", "--reverse", args.range)).split()
    problems: list[str] = []
    for sha in commits:
        message = _decode(
            _git(
                root, "rev-list", "--no-commit-header", "--format=%B", "--max-count=1", sha
            ).stdout
        ).rstrip("\n")
        if args.attribution_only:
            found = [f"attribution line: {line.strip()!r}" for line in attribution_lines(message)]
        else:
            found = subject_problems(message, args.key)
        problems += [f"{sha[:7]} {p}" for p in found]
    return _report(problems)


def cmd_summary(args: argparse.Namespace) -> int:
    plan = read_plan(args.plan)
    problems = list(plan.problems)
    problems += [f"group {g.number} has no commit message" for g in plan.groups if not g.message]
    if problems:
        return _report(problems)
    _emit(
        [
            f"{n}. {g.subject} ({len(dict.fromkeys(g.files))} files)"
            for n, g in enumerate(plan.groups, 1)
        ]
    )
    return 0


def _full_sha(value: str) -> str:
    value = value.strip().lower()
    if not _FULL_SHA_RE.match(value):
        raise argparse.ArgumentTypeError(f"must be a full commit sha, got {value!r}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="_commit_rewrite_v3.py",
        description="Validate and apply a whole-file commit regrouping of base..HEAD.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("range", help="print {start, head, tree, count} of base..HEAD")
    p.add_argument("--repo", required=True)
    p.add_argument("--base", required=True)
    p.add_argument("--key", required=True)
    p.set_defaults(func=cmd_range)

    for name, func, text in (
        ("check", cmd_check, "validate a commit plan"),
        ("apply", cmd_apply, "validate, back up and rewrite base..HEAD"),
    ):
        p = sub.add_parser(name, help=text)
        p.add_argument("--plan", required=True)
        p.add_argument("--repo", required=True)
        p.add_argument("--base", required=True)
        p.add_argument("--key", required=True)
        p.add_argument("--expect-head", required=True, type=_full_sha)
        p.set_defaults(func=func)

    p = sub.add_parser("messages", help="check the commit messages of a range")
    p.add_argument("--repo", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--range", required=True)
    p.add_argument("--attribution-only", action="store_true")
    p.set_defaults(func=cmd_messages)

    p = sub.add_parser("summary", help="print one line per group")
    p.add_argument("--plan", required=True)
    p.set_defaults(func=cmd_summary)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except UsageError as exc:
        print(f"usage error: {exc}", file=sys.stderr)
        return 2
    except GitError as exc:
        return _report(exc.lines())


if __name__ == "__main__":
    raise SystemExit(main())

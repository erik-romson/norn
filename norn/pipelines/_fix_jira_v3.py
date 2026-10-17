"""Private helper for fix_jira_issue_v3 — paths, run mode, house style, run identity."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from norn.alerts import AlertEvent, AlertManager, AlertMessage, MacOSChannel
from norn.models import PipelineContext, StageResult
from norn.pipelines._jira_issue import _KEY_RE, _VALUE_FLAGS
from norn.pipelines._plan_gates_v2 import format_gate_error
from norn.runner import resolve_run_path
from norn.stages.base import BaseStage
from norn.state import register_git_exclude

PIPELINE = "fix_jira_issue_v3"


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class V3Paths:
    """Absolute artifact paths for one issue.

    Human-facing files live in ``art_dir``; scratch lives under ``work``
    (``<art_dir>/v3``).
    """

    art_dir: str
    key: str

    def _art(self, name: str) -> str:
        return str(Path(self.art_dir) / name)

    def _work(self, name: str) -> str:
        return str(Path(self.work) / name)

    # -- human-facing files -------------------------------------------------

    @property
    def approval(self) -> str:
        return self._art(f"{self.key}-plan-approval.md")

    @property
    def code_review(self) -> str:
        return self._art(f"{self.key}-code-review.md")

    @property
    def commit_plan(self) -> str:
        return self._art(f"{self.key}-commit-plan.md")

    @property
    def pr_body(self) -> str:
        return self._art(f"{self.key}-pr-body.md")

    @property
    def ship(self) -> str:
        return self._art(f"{self.key}-ship.md")

    # -- scratch ------------------------------------------------------------

    @property
    def work(self) -> str:
        return self._art("v3")

    @property
    def review_range(self) -> str:
        return self._work("review-range.txt")

    @property
    def review_pre(self) -> str:
        return self._work("review-pre.json")

    @property
    def review_post(self) -> str:
        return self._work("review-post.json")

    @property
    def review_codex(self) -> str:
        return self._work("review-codex.md")

    @property
    def review_claude(self) -> str:
        return self._work("review-claude.md")

    @property
    def code_review_problems(self) -> str:
        return self._work("code-review-problems.txt")

    @property
    def review_fixes(self) -> str:
        return self._work("code-review-fixes.md")

    @property
    def review_followups(self) -> str:
        return self._work("review-followups.md")

    @property
    def review_nits(self) -> str:
        return self._work("review-nits.md")

    @property
    def fix_dir(self) -> str:
        return self._work("fix")

    @property
    def fix_pre(self) -> str:
        return self._work("fix/pre.json")

    @property
    def fix_post(self) -> str:
        return self._work("fix/post.json")

    @property
    def owned(self) -> str:
        return self._work("owned.nul")

    @property
    def test_cmd_sh(self) -> str:
        return self._work("test-cmd.sh")

    @property
    def test_timeout(self) -> str:
        return self._work("test-timeout")

    @property
    def test_proof(self) -> str:
        return self._work("test-proof")

    @property
    def test_log(self) -> str:
        return self._work("review-tests.log")

    @property
    def rewrite_range(self) -> str:
        return self._work("rewrite-range.txt")

    @property
    def rewritten(self) -> str:
        return self._work("rewritten")

    @property
    def commit_plan_problems(self) -> str:
        return self._work("commit-plan-problems.txt")

    @property
    def shipped_heads(self) -> str:
        return self._work("shipped-heads")

    @property
    def pr_title(self) -> str:
        return self._work("pr-title.txt")

    @property
    def pr_summary(self) -> str:
        return self._work("pr-summary.md")

    @property
    def pr_summary_problems(self) -> str:
        return self._work("pr-summary-problems.txt")

    @property
    def pr_state(self) -> str:
        """The ``MergePaths`` root of the PR phase."""
        return self._work("pr")


# ---------------------------------------------------------------------------
# Argv: run mode
# ---------------------------------------------------------------------------

_QUOTA_WINDOW_RE = re.compile(r"^\d+(\.\d+)?(:[a-z]+)?$")


def positional_tokens(argv: list[str]) -> list[str]:
    """Return the tokens of *argv* that are neither flags nor flag values."""
    out: list[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token.startswith("-"):
            if "=" in token:
                i += 1
            elif token in _VALUE_FLAGS or token == "--quota":
                i += 2
            elif (
                token == "--require-quota"
                and i + 1 < len(argv)
                and _QUOTA_WINDOW_RE.match(argv[i + 1])
            ):
                # The value of --require-quota is optional (norn/cli.py).
                i += 2
            else:
                i += 1
            continue
        out.append(token)
        i += 1
    return out


def _is_pipeline_ref(token: str) -> bool:
    return token == PIPELINE or token.endswith(f"{PIPELINE}.py")


def run_mode(argv: list[str]) -> Literal["interactive", "auto"]:
    """Return ``"auto"`` when the positional ``auto`` is present.

    Raises ``ValueError`` for any positional that is not a Jira key, a browse
    URL, ``auto``, ``run`` (first positional only) or this pipeline's name or
    file path.
    """
    mode: Literal["interactive", "auto"] = "interactive"
    for index, token in enumerate(positional_tokens(argv)):
        if token == "auto":
            mode = "auto"
        elif _KEY_RE.match(token) or "/browse/" in token:
            continue
        elif token == "run" and index == 0:
            continue
        elif _is_pipeline_ref(token):
            continue
        else:
            raise ValueError(
                f"unrecognised argument {token!r}. Accepted: a Jira key "
                f"(e.g. CBS-1234), a /browse/ URL, or the literal 'auto' "
                f"(lower-case) after the key."
            )
    return mode


# ---------------------------------------------------------------------------
# Attribution and house style
# ---------------------------------------------------------------------------

# Scope rule (the one home): this strict ERE applies to commit messages and PR
# text. Added code lines keep the narrower ``_comment_style.ATTRIBUTION_ERE``:
# a code line such as ``Co-Authored-By: Jane`` in a test fixture is not
# attribution.
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
# Markdown extraction
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
# Copied from _plan_gates_v2.py:300-333 (cannot be imported: _FENCE_RE there is
# private to a gate module that the frozen v2 helpers own).
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_EMPTY_BODIES = {"nothing", "none", "n/a"}


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """Return ``(line index, level, text)`` of every heading outside a fence."""
    found: list[tuple[int, int, str]] = []
    in_fence = False
    for i, line in enumerate(lines):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING_RE.match(line)
        if m:
            found.append((i, len(m.group(1)), m.group(2)))
    return found


def _section_bodies(md: str, accept) -> list[str]:
    """Bodies of level-2 headings whose text satisfies *accept*."""
    lines = md.splitlines()
    heads = _headings(lines)
    bodies: list[str] = []
    for pos, (start, level, title) in enumerate(heads):
        if level != 2 or not accept(title):
            continue
        end = len(lines)
        for nxt_i, nxt_level, _t in heads[pos + 1:]:
            if nxt_level <= 2:
                end = nxt_i
                break
        bodies.append("\n".join(lines[start + 1:end]).strip("\n"))
    return bodies


def human_sections(md: str) -> list[str]:
    """Bodies of every ``## For the human…`` section, empty ones dropped."""
    bodies = _section_bodies(md, lambda t: t.lower().startswith("for the human"))
    return [b for b in bodies if b.strip().rstrip(".").lower() not in _EMPTY_BODIES and b.strip()]


def _unapplied_items(followups_md: str) -> list[str]:
    bodies = _section_bodies(
        followups_md, lambda t: t.lower().startswith("unapplied review findings")
    )
    items: list[str] = []
    for body in bodies:
        for line in body.splitlines():
            if re.match(r"^\s*- \[ \]", line):
                items.append(line.strip())
    return items


def _is_human_item(line: str) -> bool:
    return re.match(r"^- \[ \]\s+human\b", line, re.I) is not None


def human_followup_items(followups_md: str) -> list[str]:
    """The ``- [ ] human …`` lines of ``## Unapplied review findings``."""
    return [i for i in _unapplied_items(followups_md) if _is_human_item(i)]


def followup_items(followups_md: str) -> list[str]:
    """The other ``- [ ]`` lines of ``## Unapplied review findings``."""
    return [i for i in _unapplied_items(followups_md) if not _is_human_item(i)]


def human_checklist(sections: list[str]) -> list[str]:
    """One ``- [ ] <heading>`` per level-3 heading of the human sections."""
    items: list[str] = []
    for section in sections:
        lines = section.splitlines()
        level3 = [t for _i, level, t in _headings(lines) if level == 3]
        if not level3:
            first = next((ln.strip() for ln in lines if ln.strip()), "")
            if first:
                items.append(f"- [ ] {first}")
            continue
        for title in level3:
            low = re.sub(r"^\d+[.)]\s*", "", title.lower())
            if low.startswith("background") or low.startswith("follow-ups"):
                continue
            items.append(f"- [ ] {title}")
    return items


def shorten_home(text: str) -> str:
    """Replace the user's home directory with ``~``."""
    return text.replace(str(Path.home()), "~")


_STEP_ECHO_RE = re.compile(r'echo\s+"=== STEP \d+:\s*(.+?)\s*==="')
_STEM_ECHO_RE = re.compile(r'echo\s+"=== ([\w.\-]+) ==="')
_NON_STEM_MARKERS = {"test_cmd", "bats_cmd"}
_BUILD_TOOLS = ("mvn", "./mvnw", "gradle", "./gradlew", "npm", "uv", "pytest")


def _is_build_line(line: str) -> bool:
    first = line.strip().split(None, 1)[0] if line.strip() else ""
    return first in _BUILD_TOOLS


def test_steps(script: str) -> list[str]:
    """Human-readable labels for the steps of a test script."""
    lines = script.splitlines()
    labels: list[str] = []
    for i, line in enumerate(lines):
        # (position in the line, label): a one-line aggregate holds several markers.
        found: list[tuple[int, str]] = [(m.start(), m.group(1)) for m in _STEP_ECHO_RE.finditer(line)]
        found += [
            (m.start(), m.group(1))
            for m in _STEM_ECHO_RE.finditer(line)
            if m.group(1) not in _NON_STEM_MARKERS
        ]
        for _pos, label in sorted(found):
            nxt = next((ln for ln in lines[i + 1:] if ln.strip()), "")
            if nxt and _is_build_line(nxt) and len(found) == 1:
                label = f"{label} `{nxt.strip()}`"
            labels.append(label)
    if labels:
        return labels
    commands = [ln.strip() for ln in lines if ln.strip() and _is_build_line(ln)]
    if commands:
        return commands
    return [ln for ln in lines if ln.strip()][:5]


# Pytest must not collect the helper above as a test when this module is imported
# into a test namespace.
test_steps.__test__ = False  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


# Copied from _merge_state.py:1653-1672 (cannot be imported: its _Notice hard-codes
# the pipeline name merge_branch).
class _Notice(AlertMessage):
    """An ``AlertMessage`` whose ``title`` is an explicit heading."""

    def __init__(self, heading: str, detail: str) -> None:
        super().__init__(event=AlertEvent.COMPLETE, pipeline_name=PIPELINE, detail=detail)
        self._heading = heading

    @property
    def title(self) -> str:
        return self._heading


async def notify(title: str, detail: str) -> None:
    """Fire an in-run notification branded ``fix_jira_issue_v3``."""
    manager = AlertManager(channels=[MacOSChannel()])
    await manager.fire(_Notice(title, detail))


# ---------------------------------------------------------------------------
# Knobs
# ---------------------------------------------------------------------------


def positive_int_knob(args: dict[str, str], name: str, default: int) -> int:
    raw = str(args.get(name, default)).strip()
    if not raw.isdigit() or int(raw) < 1:
        raise ValueError(f"--arg {name}: must be a positive whole number, got {raw!r}")
    return int(raw)


def on_off_knob(args: dict[str, str], name: str, default: bool) -> bool:
    raw = str(args.get(name, "on" if default else "off")).strip()
    if raw not in ("on", "off"):
        raise ValueError(f"--arg {name}: must be 'on' or 'off', got {raw!r}")
    return raw == "on"


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------


def _git(project_dir: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=project_dir, capture_output=True, text=True, check=False
    )


class RegisterChildState(BaseStage):
    """Keep the child runs' state files out of the target repository."""

    needs_agent = False

    def __init__(self, project_dir: str, state_keys: list[str]) -> None:
        self._project_dir = project_dir
        self._state_keys = state_keys

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        added: list[str] = []
        for key in self._state_keys:
            # An absolute key resolves the same way whatever the process cwd is.
            added += register_git_exclude(str(Path(self._project_dir) / key), self._project_dir)
        return StageResult(name="", success=True, output="\n".join(added))


class RecordRunIdentity(BaseStage):
    """Record the issue and the branch this run belongs to."""

    needs_agent = False

    def __init__(self, key: str, project_dir: str) -> None:
        self._key = key
        self._project_dir = project_dir

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        proc = _git(self._project_dir, "symbolic-ref", "--short", "HEAD")
        if proc.returncode != 0:
            return StageResult(
                name="",
                success=False,
                error=format_gate_error(
                    "HEAD is detached; the run needs a branch",
                    [proc.stderr.strip()],
                    "Switch to the issue branch, then rerun.",
                ),
            )
        return StageResult(
            name="",
            success=True,
            output={"issue": self._key, "branch": proc.stdout.strip()},
        )


class CheckRunIdentity(BaseStage):
    """Fail when the checkpoint, branch or ``base.sha`` do not fit this run."""

    needs_agent = False

    def __init__(
        self,
        key: str,
        project_dir: str,
        base_file: str,
        record_stage: str = "record run identity",
    ) -> None:
        self._key = key
        self._project_dir = project_dir
        self._base_file = base_file
        self._record_stage = record_stage

    def _fail(self, summary: str, items: list[str], action: str) -> StageResult:
        return StageResult(
            name="", success=False, error=format_gate_error(summary, items, action)
        )

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        record = ctx.results.get(self._record_stage)
        identity = record.output if record is not None else None
        if not isinstance(identity, dict) or "issue" not in identity or "branch" not in identity:
            return self._fail(
                f"no run identity recorded by '{self._record_stage}'",
                [],
                "Run without --resume to start fresh.",
            )
        if identity["issue"] != self._key:
            return self._fail(
                f"checkpoint belongs to {identity['issue']}, not {self._key}",
                [],
                f"Resume that issue ({identity['issue']}), or run without --resume.",
            )

        proc = _git(self._project_dir, "symbolic-ref", "--short", "HEAD")
        if proc.returncode != 0:
            return self._fail(
                "HEAD is detached; the run needs a branch",
                [proc.stderr.strip()],
                f"Switch back to {identity['branch']}, then --resume.",
            )
        branch = proc.stdout.strip()
        if branch != identity["branch"]:
            return self._fail(
                f"on branch {branch}, but this run started on {identity['branch']}",
                [],
                f"Run: git switch {identity['branch']}, then --resume.",
            )

        base_path = resolve_run_path(ctx, self._base_file)
        if not base_path.is_file():
            return self._fail(
                f"{base_path} is missing",
                [],
                "Run without --resume to start fresh.",
            )
        base = base_path.read_text().strip()

        anc = _git(self._project_dir, "merge-base", "--is-ancestor", base, "HEAD")
        if anc.returncode != 0:
            return self._fail(
                f"base.sha {base[:7]} is not an ancestor of HEAD",
                [anc.stderr.strip()] if anc.stderr.strip() else [],
                f"Delete {base_path} and start from a fresh branch.",
            )

        rng = _git(self._project_dir, "rev-list", "--reverse", f"{base}..HEAD")
        if rng.returncode != 0:
            return self._fail(
                f"cannot list {base[:7]}..HEAD",
                [rng.stderr.strip()],
                "Fix the repository state, then --resume.",
            )
        commits = rng.stdout.split()
        if commits:
            first = commits[0]
            contains = _git(self._project_dir, "branch", "-r", "--contains", first)
            if contains.returncode != 0:
                return self._fail(
                    f"cannot list remote branches containing {first[:7]}",
                    [contains.stderr.strip()],
                    "Fix the repository state, then --resume.",
                )
            allowed = {f"origin/{branch}", "origin/HEAD"}
            for raw in contains.stdout.splitlines():
                ref = raw.strip()
                # "origin/HEAD -> origin/develop" is a symbolic ref, not a branch.
                if not ref or "->" in ref or ref in allowed:
                    continue
                return self._fail(
                    f"base.sha is stale: {ref} already contains {first[:7]}",
                    [],
                    f"Delete tmp/jira/{self._key}/base.sha and start from a fresh branch.",
                )
        return StageResult(name="", success=True)

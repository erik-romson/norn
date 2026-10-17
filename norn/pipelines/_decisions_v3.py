"""Private gate-file parsers, writers and gate stages for fix_jira_issue_v3 — not a pipeline.

Four files, each edited by the human (formats: see the plan's "Gate-file
contracts"):

1. ``<KEY>-plan-questions.md``  — open questions with recommended answers;
2. ``<KEY>-plan-approval.md``   — ``DECISION:`` line;
3. ``<KEY>-code-review.md``     — ``C<n>`` findings with dispositions;
4. ``<KEY>-ship.md``            — ``STATUS``/``COMMITS``/``TITLE``.

Field parsing is line-anchored (``^FIELD:``) and case-insensitive; HTML
comments are never fields.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, NamedTuple

from norn.models import PipelineContext, StageResult
from norn.pipelines._fix_jira_v3 import subject_problems
from norn.pipelines._plan_gates_v2 import (
    OpenQuestionsGate,
    format_gate_error,
    parse_questions,
)
from norn.runner import resolve_run_path
from norn.stages.base import BaseStage

# ---------------------------------------------------------------------------
# Shared text helpers
# ---------------------------------------------------------------------------

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _mask_comments(text: str) -> str:
    """Blank out HTML comments, keeping every offset and newline in place."""
    return _COMMENT_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)


def _field_match(text: str, field: str) -> re.Match[str] | None:
    masked = _mask_comments(text)
    return re.search(
        rf"^{re.escape(field)}:[ \t]*(.*?)[ \t]*$", masked, re.IGNORECASE | re.MULTILINE
    )


def read_field(path: str | Path, field: str, *, first_token: bool = True) -> str | None:
    """Return the value of ``FIELD:`` in *path*, or None.

    A missing file or a missing field returns None; other I/O errors propagate.
    With *first_token* the value is the first whitespace-separated token,
    otherwise the whole rest of the line.  Safe inside a ``when`` lambda.
    """
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return None
    m = _field_match(text, field)
    if m is None:
        return None
    value = m.group(1)
    if first_token:
        parts = value.split()
        return parts[0] if parts else ""
    return value


def _is_undecided(value: str | None) -> bool:
    return value is not None and value.lower() in ("pending", "proposed")


def _fail(error: str, output: str = "") -> StageResult:
    return StageResult(name="", success=False, error=error, output=output)


def _ok(output: str = "") -> StageResult:
    return StageResult(name="", success=True, output=output)


# ---------------------------------------------------------------------------
# Gate 1 — questions with recommended answers
# ---------------------------------------------------------------------------

_Q_HEADING_RE = re.compile(r"^##\s+(Q\d+)\b(.*)$", re.MULTILINE)
_REC_RE = re.compile(r"^[ \t]*\*\*Recommended answer:\*\*[ \t]*(.*?)[ \t]*$", re.IGNORECASE | re.MULTILINE)
_ANSWER_MARK = "**Answer:**"
_NONE_RE = re.compile(r"none\s*(?:[—–-]+\s*(.*))?", re.IGNORECASE | re.DOTALL)

RECOMMENDED_SUFFIX = " _(recommended answer)_"
AUTO_RECOMMENDED_SUFFIX = " _(auto: recommended answer)_"


def _q_sections(text: str) -> list[tuple[str, str]]:
    """Return ``(qid, body)`` per ``## Q<n>`` section."""
    matches = list(_Q_HEADING_RE.finditer(text))
    out: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out.append((m.group(1), text[m.end():end]))
    return out


def parse_recommendations(text: str) -> dict[str, str | None]:
    """Map qid to its recommended answer; ``None`` for ``none — …``.

    A question with no ``**Recommended answer:**`` line is absent from the map.
    """
    recs: dict[str, str | None] = {}
    for qid, body in _q_sections(text):
        m = _REC_RE.search(body)
        if m is None:
            continue
        value = m.group(1).strip()
        if _NONE_RE.fullmatch(value):
            recs[qid] = None
        else:
            recs[qid] = value
    return recs


def recommendation_problems(text: str) -> list[str]:
    """Return one problem line per malformed recommendation."""
    problems: list[str] = []
    for qid, body in _q_sections(text):
        m = _REC_RE.search(body)
        if m is None:
            problems.append(f"{qid}: no **Recommended answer:** line")
            continue
        answer_idx = body.find(_ANSWER_MARK)
        if answer_idx >= 0 and m.start() > answer_idx:
            problems.append(
                f"{qid}: **Recommended answer:** is below **Answer:**; move it above"
            )
        value = m.group(1).strip()
        if not value:
            problems.append(f"{qid}: **Recommended answer:** is empty")
            continue
        none = _NONE_RE.fullmatch(value)
        if none and not (none.group(1) or "").strip():
            problems.append(
                f"{qid}: 'none' needs a reason after a dash (none — <why only a human can decide>)"
            )
    return problems


def questions_need_revision(path: str | Path) -> bool:
    """True when the plan must absorb the human's answers (the ``when`` of ``revise plan``).

    The file would fail ``OpenQuestionsGate`` and either at least one question
    is answered or there are none.  A missing file is False.
    """
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return False
    status, questions = parse_questions(text)
    would_fail = status != "READY" or any(not q.answered for q in questions)
    if not would_fail:
        return False
    return not questions or any(q.answered for q in questions)


class QuestionsGate(BaseStage):
    """``OpenQuestionsGate`` plus recommendation checks and no literal ``recommended`` answer."""

    needs_agent = False

    def __init__(self, *, path: str) -> None:
        self.path = path

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        base = await OpenQuestionsGate(path=self.path).run(ctx)
        if not base.success:
            return base
        resolved = resolve_run_path(ctx, self.path)
        text = resolved.read_text()
        problems = recommendation_problems(text)
        if problems:
            return _fail(
                format_gate_error(
                    f"{len(problems)} recommendation problems in {resolved}",
                    problems,
                    "Fix the **Recommended answer:** lines (above **Answer:**), then [r]etry.",
                )
            )
        literal = [qid for qid, body in _q_sections(text) if _literal_recommended(body)]
        if literal:
            return _fail(
                format_gate_error(
                    f"{len(literal)} answers are still the literal 'recommended' in {resolved}",
                    literal,
                    "[r]etry the loop so the plan absorbs it; [c]ontinue skips that.",
                )
            )
        return _ok()


def _literal_recommended(body: str) -> bool:
    idx = body.find(_ANSWER_MARK)
    if idx < 0:
        return False
    return body[idx + len(_ANSWER_MARK):].strip().lower() == "recommended"


class ExpandRecommendedAnswers(BaseStage):
    """Replace ``recommended`` answers (and, with *fill_blanks*, blanks) by the recommendation.

    Writes every expansion it can first, then fails listing the questions it
    could not expand because their recommendation is ``none`` or missing.
    """

    needs_agent = False

    def __init__(self, *, path: str, fill_blanks: bool) -> None:
        self.path = path
        self.fill_blanks = fill_blanks

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        resolved = resolve_run_path(ctx, self.path)
        if not resolved.exists():
            return _fail(f"plan questions file not written: {resolved}")
        text = resolved.read_text()
        recs = parse_recommendations(text)

        matches = list(_Q_HEADING_RE.finditer(text))
        expanded: list[str] = []
        unresolved: list[str] = []
        # Rebuild back to front so earlier offsets stay valid.
        for i in range(len(matches) - 1, -1, -1):
            m = matches[i]
            qid = m.group(1)
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[m.end():end]
            idx = body.find(_ANSWER_MARK)
            if idx < 0:
                continue
            head = body[: idx + len(_ANSWER_MARK)]
            after = body[idx + len(_ANSWER_MARK):]
            stripped = after.strip()
            if stripped.lower() == "recommended":
                suffix = RECOMMENDED_SUFFIX
                trailing = after[len(after.rstrip()):]
            elif not stripped and self.fill_blanks:
                suffix = AUTO_RECOMMENDED_SUFFIX
                trailing = after.lstrip(" \t")
            else:
                continue
            rec = recs.get(qid)
            if not rec:
                unresolved.append(qid)
                continue
            expanded.append(qid)
            text = text[: m.end()] + head + f" {rec}{suffix}{trailing}" + text[end:]
        if expanded:
            resolved.write_text(text)
        expanded.reverse()
        unresolved.reverse()
        if unresolved:
            return _fail(
                format_gate_error(
                    f"{len(unresolved)} questions have no recommended answer to use in {resolved}",
                    [f"{qid}: recommendation is none or missing" for qid in unresolved],
                    "Answer them under **Answer:** then choose [r]etry.",
                ),
                output="\n".join(expanded),
            )
        return _ok("\n".join(expanded))


# ---------------------------------------------------------------------------
# Generic writers and gates (gates 2 and 4)
# ---------------------------------------------------------------------------


class WriteDecision(BaseStage):
    """Always write a fresh decision file from ``render(ctx)`` (a stale file never survives)."""

    needs_agent = False

    def __init__(self, *, path: str, render: Callable[[PipelineContext], str]) -> None:
        self.path = path
        self.render = render

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        resolved = resolve_run_path(ctx, self.path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(self.render(ctx))
        return _ok(str(resolved))


class AutoDecide(BaseStage):
    """Set *field* to ``<value> (<note>)`` when the file is still undecided.

    Undecided means the field's first token is ``pending`` or ``PROPOSED``; a
    human decision is never overwritten.
    """

    needs_agent = False

    def __init__(self, *, path: str, field: str, value: str, note: str = "auto") -> None:
        self.path = path
        self.field = field
        self.value = value
        self.note = note

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        resolved = resolve_run_path(ctx, self.path)
        if not resolved.exists():
            return _fail(f"decision file not written: {resolved}")
        text = resolved.read_text()
        m = _field_match(text, self.field)
        if m is None:
            return _fail(f"{resolved}: no {self.field}: line to decide")
        parts = m.group(1).split()
        if not _is_undecided(parts[0] if parts else ""):
            return _ok("left alone")
        new = f"{self.field}: {self.value} ({self.note})"
        # _field_match works on the comment-masked text; offsets match the original.
        start = m.start()
        line_end = text.find("\n", start)
        line_end = len(text) if line_end < 0 else line_end
        resolved.write_text(text[:start] + new + text[line_end:])
        return _ok(new)


class DecisionGate(BaseStage):
    """Fail unless *field*'s first token is one of *accept* (case-insensitive)."""

    needs_agent = False

    def __init__(self, *, path: str, field: str, accept: frozenset[str], action: str) -> None:
        self.path = path
        self.field = field
        self.accept = frozenset(a.lower() for a in accept)
        self.action = action

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        resolved = resolve_run_path(ctx, self.path)
        value = read_field(resolved, self.field)
        if value is not None and value.lower() in self.accept:
            return _ok(value)
        shown = "missing" if value is None else value
        return _fail(f"{resolved}: {self.field} is {shown}. {self.action}")


class ShipDecisionGate(BaseStage):
    """Gate 4: STATUS DECIDED, COMMITS rewrite|keep, TITLE passing the house style."""

    needs_agent = False

    def __init__(self, *, path: str, key: str) -> None:
        self.path = path
        self.key = key

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        resolved = resolve_run_path(ctx, self.path)
        if not resolved.exists():
            return _fail(f"ship decision file not written: {resolved}")
        problems: list[str] = []
        status = read_field(resolved, "STATUS")
        if status is None or status.upper() != "DECIDED":
            problems.append(f"STATUS is {status or 'missing'}; set it to DECIDED")
        commits = read_field(resolved, "COMMITS")
        if commits is None or commits.lower() not in ("rewrite", "keep"):
            problems.append(f"COMMITS is {commits or 'missing'}; it must be rewrite or keep")
        title = read_field(resolved, "TITLE", first_token=False)
        if not title:
            problems.append("TITLE is missing or empty")
        else:
            problems.extend(f"TITLE: {p}" for p in subject_problems(title, self.key))
        if problems:
            return _fail(
                format_gate_error(
                    f"{len(problems)} problems in {resolved}",
                    problems,
                    "Fix them in that file, then choose [r]etry.",
                )
            )
        return _ok()


# ---------------------------------------------------------------------------
# Gate 3 — code review findings
# ---------------------------------------------------------------------------

DISPOSITIONS = ("fix", "skip", "follow-up")
MAX_FINDING_LINES = 8

_C_LOOSE_RE = re.compile(r"^#{1,6}[ \t]*C\d+\b")
_C_HEADING_RE = re.compile(r"^## C(\d+)\. (\S.*?)\s*$")
_SEVERITY_RE = re.compile(r"^(.*?)\s*\[(critical|major|minor|nit)\]$", re.IGNORECASE)
_L2_RE = re.compile(r"^##(?!#)")
_BULLET_RE = re.compile(r"^\s*[-*]\s+(\S.*?)\s*$")
_TAG_RE = re.compile(r"\s*\[([^\]]*)\]\s*$")
_OMITTED_RE = re.compile(r"^##\s+Omitted findings\s*$", re.IGNORECASE)


class Finding(NamedTuple):
    cid: str
    title: str
    severity: str  # critical | major | minor | nit, or "" when untagged
    disposition: str  # first token, lower-case; "" when missing
    lines: list[str]


class CodeReview(NamedTuple):
    status: str | None
    findings: list[Finding]
    omitted_nits: list[str]
    problems: list[str]


def _field_in(lines: list[str], field: str) -> str | None:
    pat = re.compile(rf"^{re.escape(field)}:[ \t]*(.*?)[ \t]*$", re.IGNORECASE)
    for line in lines:
        m = pat.match(line)
        if m:
            return m.group(1)
    return None


def parse_code_review(text: str) -> CodeReview:
    """Parse a gate-3 file into status, findings, omitted nits and shape problems."""
    lines = _mask_comments(text).splitlines()
    problems: list[str] = []

    status: str | None = None
    for line in lines:
        m = re.match(r"^STATUS:\s*(\S+)", line, re.IGNORECASE)
        if m:
            status = m.group(1).upper()
            break
    if status is None:
        problems.append("no STATUS: line")
    elif status not in ("PROPOSED", "DECIDED"):
        problems.append(f"STATUS is {status}; it must be PROPOSED or DECIDED")

    # Pass 1: section boundaries.  A finding ends at the next level-2 heading.
    sections: list[tuple[str, int, int]] = []  # (kind, first line, end line)
    starts = [i for i, ln in enumerate(lines) if _L2_RE.match(ln)]
    for pos, i in enumerate(starts):
        end = starts[pos + 1] if pos + 1 < len(starts) else len(lines)
        kind = "finding" if _C_HEADING_RE.match(lines[i]) else (
            "omitted" if _OMITTED_RE.match(lines[i]) else "other"
        )
        sections.append((kind, i, end))

    for i, line in enumerate(lines):
        if _C_LOOSE_RE.match(line) and not _C_HEADING_RE.match(line):
            problems.append(
                f"line {i + 1}: malformed finding heading {line.strip()!r}; "
                "use '## C<n>. <title> [critical|major|minor|nit]'"
            )

    findings: list[Finding] = []
    omitted: list[str] = []
    seen: set[str] = set()
    for kind, first, end in sections:
        if kind == "finding":
            m = _C_HEADING_RE.match(lines[first])
            assert m is not None
            cid = f"C{m.group(1)}"
            raw_title = m.group(2)
            sm = _SEVERITY_RE.match(raw_title)
            title, severity = (sm.group(1), sm.group(2).lower()) if sm else (raw_title, "")
            body = lines[first:end]
            while len(body) > 1 and not body[-1].strip():
                body.pop()
            if cid in seen:
                problems.append(f"{cid}: duplicate finding")
            seen.add(cid)
            if len(body) > MAX_FINDING_LINES:
                problems.append(
                    f"{cid}: section is {len(body)} lines; the limit is "
                    f"{MAX_FINDING_LINES} including the heading"
                )
            for field in ("Where", "Problem"):
                if not (_field_in(body, field) or "").strip():
                    problems.append(f"{cid}: missing {field}:")
            raw = _field_in(body, "Disposition")
            token = raw.split()[0].lower() if raw and raw.split() else ""
            if raw is None or not token:
                problems.append(f"{cid}: missing Disposition: (fix, skip or follow-up)")
                token = ""
            elif token not in DISPOSITIONS:
                problems.append(
                    f"{cid}: Disposition must be fix, skip or follow-up, got {token!r}"
                )
                token = ""
            findings.append(Finding(cid, title, severity, token, body))
        elif kind == "omitted":
            for line in lines[first + 1:end]:
                if not line.strip():
                    continue
                b = _BULLET_RE.match(line)
                tag = _TAG_RE.search(b.group(1)) if b else None
                if b and tag and tag.group(1).lower() == "nit":
                    omitted.append(b.group(1))
                    continue
                shown = line.strip()
                if b:
                    shown = _TAG_RE.sub("", b.group(1)) if tag else b.group(1)
                problems.append(
                    f"only nits may be omitted; give '{shown}' its own C<n> section"
                )

    if not findings and not any(ln.strip().lower() == "no findings." for ln in lines):
        problems.append("no findings and no 'No findings.' line")

    return CodeReview(status, findings, omitted, problems)


def has_fix(path: str | Path) -> bool:
    """True when the gate-3 file has a finding with ``Disposition: fix`` (missing file: False)."""
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return False
    return any(f.disposition == "fix" for f in parse_code_review(text).findings)


class CodeReviewGate(BaseStage):
    """Shape check (and, with *require_decided*, STATUS DECIDED) for the gate-3 file."""

    needs_agent = False

    def __init__(
        self, *, path: str, require_decided: bool = False, problems_file: str | None = None
    ) -> None:
        self.path = path
        self.require_decided = require_decided
        self.problems_file = problems_file

    def _record(self, ctx: PipelineContext, problems: list[str]) -> None:
        if self.problems_file is None:
            return
        pf = resolve_run_path(ctx, self.problems_file)
        if problems:
            pf.parent.mkdir(parents=True, exist_ok=True)
            pf.write_text("\n".join(problems) + "\n")
        else:
            pf.unlink(missing_ok=True)

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        resolved = resolve_run_path(ctx, self.path)
        if not resolved.exists():
            problems = [f"code review file not written: {resolved}"]
            self._record(ctx, problems)
            return _fail(
                format_gate_error(problems[0], [], "Write the file, then choose [r]etry."),
                output="\n".join(problems),
            )
        review = parse_code_review(resolved.read_text())
        problems = list(review.problems)
        if self.require_decided and review.status != "DECIDED":
            problems.append(
                f"STATUS is {review.status or 'missing'}; set each Disposition, "
                "then set the STATUS line to DECIDED"
            )
        self._record(ctx, problems)
        if problems:
            return _fail(
                format_gate_error(
                    f"{len(problems)} problems in {resolved}",
                    problems,
                    "Fix them in that file, then choose [r]etry.",
                ),
                output="\n".join(problems),
            )
        return _ok()


class AutoDecideCodeReview(BaseStage):
    """Auto mode: ``fix`` every non-nit finding, ``skip (auto: nit)`` every nit (D5, A6).

    Acts only on a ``PROPOSED`` file.  Sections outside the ``C<n>`` findings
    (dropped false positives, omitted nits) are never touched.
    """

    needs_agent = False

    def __init__(self, *, path: str) -> None:
        self.path = path

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        resolved = resolve_run_path(ctx, self.path)
        if not resolved.exists():
            return _fail(f"code review file not written: {resolved}")
        text = resolved.read_text()
        status = read_field(resolved, "STATUS")
        if status is None or status.upper() != "PROPOSED":
            return _ok("left alone")

        masked = _mask_comments(text).split("\n")
        out = text.split("\n")
        current: str | None = None  # severity of the C-section we are in
        in_c = False
        for i, line in enumerate(masked):
            if _L2_RE.match(line):
                m = _C_HEADING_RE.match(line)
                in_c = m is not None
                if m:
                    sm = _SEVERITY_RE.match(m.group(2))
                    current = sm.group(2).lower() if sm else ""
                continue
            if in_c and re.match(r"^Disposition:", line, re.IGNORECASE):
                out[i] = "Disposition: skip (auto: nit)" if current == "nit" else "Disposition: fix"
            elif re.match(r"^STATUS:", line, re.IGNORECASE) and not in_c:
                out[i] = "STATUS: DECIDED (auto)"
        resolved.write_text("\n".join(out))
        return _ok("decided")


_FIXED_LINE_RE = re.compile(r"^\s*[-*]\s*(C\d+):\s*(not fixed|fixed)\b", re.IGNORECASE | re.MULTILINE)


class FixesRecordedGate(BaseStage):
    """Every ``fix`` finding has a ``fixed``/``not fixed`` line, and ``fixed`` means a change."""

    needs_agent = False

    def __init__(self, *, review: str, fixes: str, owned: str, auto: bool) -> None:
        self.review = review
        self.fixes = fixes
        self.owned = owned
        self.auto = auto

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        review_path = resolve_run_path(ctx, self.review)
        fixes_path = resolve_run_path(ctx, self.fixes)
        owned_path = resolve_run_path(ctx, self.owned)
        if not review_path.exists():
            return _fail(f"code review file not written: {review_path}")
        wanted = [
            f.cid for f in parse_code_review(review_path.read_text()).findings
            if f.disposition == "fix"
        ]
        text = fixes_path.read_text() if fixes_path.exists() else ""
        recorded: dict[str, str] = {}
        for m in _FIXED_LINE_RE.finditer(text):
            recorded.setdefault(m.group(1).upper(), m.group(2).lower())
        owned_empty = not owned_path.exists() or not owned_path.read_text().strip()

        problems: list[str] = []
        for cid in wanted:
            state = recorded.get(cid)
            if state is None:
                problems.append(f"{cid}: no '- {cid}: fixed — …' or 'not fixed — …' line")
            elif state == "not fixed" and self.auto:
                problems.append(f"{cid}: not fixed, and auto mode needs every finding fixed")
        if owned_empty:
            for cid, state in recorded.items():
                if state == "fixed":
                    problems.append(f"{cid}: claims fixed, changed nothing")
        if problems:
            return _fail(
                format_gate_error(
                    f"{len(problems)} problems with the fix record {fixes_path}",
                    problems,
                    "Record each as `- C<n>: fixed — why` or `not fixed — why`, then [r]etry.",
                ),
                output="\n".join(problems),
            )
        return _ok()

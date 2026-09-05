"""Private gate stages for plan_with_review — not a pipeline."""
from __future__ import annotations

import re
from typing import Any, NamedTuple

from norn.models import PipelineContext, StageResult
from norn.runner import resolve_run_path
from norn.stages.base import BaseStage


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class Question(NamedTuple):
    """A single Q<n> section parsed from a questions file."""

    qid: str
    heading: str
    answered: bool


# ---------------------------------------------------------------------------
# Parsers (module-level, tested directly)
# ---------------------------------------------------------------------------


def parse_questions(text: str) -> tuple[str | None, list[Question]]:
    """Parse a questions markdown file into (status, questions).

    Returns the STATUS value (uppercased) or None when absent, and a list of
    Question entries — one per ``## Q<n>`` section, with ``answered`` True
    when the ``**Answer:**`` slot has any non-whitespace content.
    """
    # Status: first matching line
    status_match = re.search(r"^STATUS:\s*(\S+)", text, re.MULTILINE)
    status = status_match.group(1).upper() if status_match else None

    # Questions: split on ## Q<n> headings
    heading_re = re.compile(r"^##\s+(Q\d+)\b(.*)$", re.MULTILINE)
    matches = list(heading_re.finditer(text))

    questions: list[Question] = []
    for i, m in enumerate(matches):
        qid = m.group(1)
        heading = (m.group(1) + m.group(2)).strip()

        section_start = m.end()
        section_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        section_text = text[section_start:section_end]

        answer_idx = section_text.find("**Answer:**")
        if answer_idx >= 0:
            after_answer = section_text[answer_idx + len("**Answer:**"):]
            answered = bool(after_answer.strip())
        else:
            answered = False

        questions.append(Question(qid=qid, heading=heading, answered=answered))

    return status, questions


def unaddressed_findings(review_text: str, response_text: str) -> list[str]:
    """Return finding IDs from *review_text* that have no disposition in *response_text*.

    Finding IDs are ``### F<n>`` headings, deduplicated in order.  A finding is
    addressed when a response line contains the bare ID (digit-boundary
    guarded) followed by ``accepted``, ``rejected``, or ``deferred``
    (case-insensitive).
    """
    finding_re = re.compile(r"^###\s+F(\d+)\b", re.MULTILINE)
    seen: set[str] = set()
    findings: list[str] = []
    for m in finding_re.finditer(review_text):
        fid = f"F{m.group(1)}"
        if fid not in seen:
            seen.add(fid)
            findings.append(fid)

    unaddressed: list[str] = []
    response_lines = response_text.splitlines()
    for fid in findings:
        # digit-boundary guard so F1 is not satisfied by a line about F10
        pattern = re.compile(
            rf"{re.escape(fid)}(?!\d).*\b(accepted|rejected|deferred)\b",
            re.IGNORECASE,
        )
        if not any(pattern.search(line) for line in response_lines):
            unaddressed.append(fid)

    return unaddressed


# ---------------------------------------------------------------------------
# Error-string builder
# ---------------------------------------------------------------------------


def format_gate_error(summary: str, items: list[str], action: str) -> str:
    """Build an ≤8-line gate error message.

    Line 1 is *summary*.  Up to five *items* follow, each indented two spaces.
    When more than five items are supplied a ``… and <k> more`` line is added
    after the fifth.  The final line is *action*.
    """
    lines = [summary]
    displayed = items[:5]
    remaining = len(items) - 5
    for item in displayed:
        lines.append(f"  {item}")
    if remaining > 0:
        lines.append(f"  \u2026 and {remaining} more")
    lines.append(action)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Gate stages
# ---------------------------------------------------------------------------


class OpenQuestionsGate(BaseStage):
    """Fail when the questions file still has unanswered questions.

    Rules (in order):
    1. File missing.
    2. No STATUS: line.
    3. STATUS: NEEDS_INPUT — always fail (even with zero blank questions).
    4. STATUS: READY with any blank answer slot — fail.
    5. STATUS: READY with all questions answered — pass.
    6. STATUS: READY with zero Q<n> sections — pass (vacuously).
    7. Any other STATUS value — fail.
    """

    needs_agent = False

    def __init__(self, *, path: str) -> None:
        self.path = path

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        resolved = resolve_run_path(ctx, self.path)
        path_str = str(resolved)

        # Rule 1: file missing
        if not resolved.exists():
            return StageResult(
                name="",
                success=False,
                error=f"plan questions file not written: {path_str}",
            )

        text = resolved.read_text()
        status, questions = parse_questions(text)

        # Rule 2: no STATUS line
        if status is None:
            return StageResult(
                name="",
                success=False,
                error=f"questions file has no STATUS: line: {path_str}",
            )

        if status == "NEEDS_INPUT":
            # Rule 3: fail regardless; items are blank-answer headings. With no
            # blanks the counted summary would read "0 open questions need
            # answers", which tells the human nothing about why the run stopped,
            # so each no-blank case says what is actually wrong instead.
            blank = [q.heading for q in questions if not q.answered]
            action = "Answer them under **Answer:** then choose [r]etry."
            if blank:
                summary = f"{len(blank)} open questions need answers in {path_str}"
            elif not questions:
                summary = f"draft still needs input but lists no questions in {path_str}"
                action = "The draft must list its questions or set STATUS: READY — choose [r]etry."
            else:
                summary = (
                    f"draft still needs input though all {len(questions)} questions "
                    f"are answered in {path_str}"
                )
                action = "Choose [r]etry so the plan is revised and the status updated."
            return StageResult(
                name="",
                success=False,
                error=format_gate_error(summary, blank, action),
            )

        if status == "READY":
            # Rule 4: any blank slot
            blank = [q.heading for q in questions if not q.answered]
            if blank:
                n = len(blank)
                summary = f"{n} open questions need answers in {path_str}"
                action = "Answer them under **Answer:** then choose [r]etry."
                return StageResult(
                    name="",
                    success=False,
                    error=format_gate_error(summary, blank, action),
                )
            # Rules 5 and 6: all answered, or vacuously zero questions
            return StageResult(name="", success=True)

        # Rule 7: unrecognised status
        return StageResult(
            name="",
            success=False,
            error=f"questions file has unrecognised STATUS: {status}: {path_str}",
        )


class ReviewDispositionGate(BaseStage):
    """Fail when any codex review finding lacks a disposition in the response.

    Rules (in order):
    1. Review file missing.
    2. Review has no F<n> headings — pass (vacuously).
    3. Response file missing while findings exist.
    4. Any finding without a disposition — fail.
    5. Every finding dispositioned — pass.
    """

    needs_agent = False

    def __init__(self, *, review: str, response: str) -> None:
        self.review = review
        self.response = response

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        review_path = resolve_run_path(ctx, self.review)
        response_path = resolve_run_path(ctx, self.response)
        review_str = str(review_path)
        response_str = str(response_path)

        # Rule 1: review file missing
        if not review_path.exists():
            return StageResult(
                name="",
                success=False,
                error=f"codex review file not written: {review_str}",
            )

        review_text = review_path.read_text()

        # Collect findings
        finding_re = re.compile(r"^###\s+F(\d+)\b", re.MULTILINE)
        seen: set[str] = set()
        findings: list[str] = []
        for m in finding_re.finditer(review_text):
            fid = f"F{m.group(1)}"
            if fid not in seen:
                seen.add(fid)
                findings.append(fid)

        # Rule 2: no findings — pass
        if not findings:
            return StageResult(name="", success=True)

        # Rule 3: response file missing
        if not response_path.exists():
            action = "Add `- F<n>: accepted|rejected|deferred \u2014 why` for each, then [r]etry."
            summary = f"review response file not written: {response_str}"
            return StageResult(
                name="",
                success=False,
                error=format_gate_error(summary, findings, action),
            )

        response_text = response_path.read_text()
        unaddressed = unaddressed_findings(review_text, response_text)

        # Rule 4: unaddressed findings
        if unaddressed:
            summary = f"{len(unaddressed)} findings need dispositions in {response_str}"
            action = "Add `- F<n>: accepted|rejected|deferred \u2014 why` for each, then [r]etry."
            return StageResult(
                name="",
                success=False,
                error=format_gate_error(summary, unaddressed, action),
            )

        # Rule 5: all dispositioned
        return StageResult(name="", success=True)


# Values that satisfy the YAML key but never catch a regression.
_PLACEHOLDER_TEST_CMDS = {"true", "false", ":", "/bin/true"}


def step_test_cmd(text: str) -> str | None:
    """Return the ``test_cmd`` declared in a step file's YAML front-matter.

    Returns None when the file has no front-matter, no ``test_cmd`` key, or a
    blank value. Block scalars (``test_cmd: |``) are handled by the YAML parser.
    """
    import yaml

    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not m:
        return None
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("test_cmd")
    if isinstance(value, bool):
        # YAML reads a bare `test_cmd: true` as a boolean, not a command.
        return str(value).lower()
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


class StepFilesGate(BaseStage):
    """Fail when the split-plan output is missing or a step has no real ``test_cmd``.

    Rules (in order):
    1. Steps directory missing.
    2. ``index.md`` missing.
    3. No ``step-*.md`` files.
    4. Any step file without a ``test_cmd`` front-matter value, or with a
       placeholder one (``true``, ``:``) — fail.
    5. Every step file carries a real ``test_cmd`` — pass.
    """

    needs_agent = False

    def __init__(self, *, steps_dir: str) -> None:
        self.steps_dir = steps_dir

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        resolved = resolve_run_path(ctx, self.steps_dir)
        dir_str = str(resolved)
        action = (
            "Split the plan into index.md + step-NN-name.md files, each with a "
            "real `test_cmd:`, then choose [r]etry."
        )

        # Rule 1: directory missing
        if not resolved.is_dir():
            return StageResult(
                name="",
                success=False,
                error=f"step files directory not written: {dir_str}",
            )

        # Rule 2: index.md missing
        if not (resolved / "index.md").exists():
            return StageResult(
                name="",
                success=False,
                error=f"step files are missing their shared context: {dir_str}/index.md",
            )

        # Rule 3: no step files
        step_files = sorted(resolved.glob("step-*.md"))
        if not step_files:
            return StageResult(
                name="",
                success=False,
                error=format_gate_error(
                    f"no step-*.md files in {dir_str}", [], action
                ),
            )

        # Rule 4: every step file needs a real test_cmd
        bad: list[str] = []
        for step in step_files:
            cmd = step_test_cmd(step.read_text())
            if cmd is None:
                bad.append(f"{step.name}: no test_cmd")
            elif cmd in _PLACEHOLDER_TEST_CMDS:
                bad.append(f"{step.name}: placeholder test_cmd {cmd!r}")
        if bad:
            summary = f"{len(bad)} of {len(step_files)} step files lack a real test_cmd in {dir_str}"
            return StageResult(
                name="",
                success=False,
                error=format_gate_error(summary, bad, action),
            )

        # Rule 5: all good
        return StageResult(name="", success=True)

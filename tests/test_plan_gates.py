"""Tests for _plan_gates: the gate stages (open questions, review dispositions,
step files) and their parsers.

All tests are offline — no agent calls, no subprocesses, no network.
"""
from __future__ import annotations

import pytest

from norn.models import PipelineContext
from norn.pipelines._plan_gates import (
    OpenQuestionsGate,
    ReviewDispositionGate,
    StepFilesGate,
    format_gate_error,
    parse_questions,
    step_test_cmd,
    unaddressed_findings,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(tmp_path=None) -> PipelineContext:
    ctx = PipelineContext()
    if tmp_path is not None:
        ctx.working_dir = str(tmp_path)
    return ctx


# ---------------------------------------------------------------------------
# parse_questions — parser unit tests
# ---------------------------------------------------------------------------

NEEDS_INPUT_SAMPLE = """\
STATUS: NEEDS_INPUT

## Q1. Which merge strategy should the worktree use on success?

Some context about the question.

**Answer:**

## Q2. Is `norn history` in scope for this change?

More context.

**Answer:** Yes, it should be in scope.
"""

NO_QUESTIONS_SAMPLE = """\
STATUS: READY

No open questions at this time.
"""

NO_STATUS_SAMPLE = """\
## Q1. What is the approach?

**Answer:** Use the existing pattern.
"""


def test_parse_questions_needs_input_sample():
    status, questions = parse_questions(NEEDS_INPUT_SAMPLE)
    assert status == "NEEDS_INPUT"
    assert len(questions) == 2
    q1, q2 = questions
    assert q1.qid == "Q1"
    assert not q1.answered  # blank answer
    assert q2.qid == "Q2"
    assert q2.answered  # has content


def test_parse_questions_no_questions():
    status, questions = parse_questions(NO_QUESTIONS_SAMPLE)
    assert status == "READY"
    assert questions == []


def test_parse_questions_no_status_line():
    status, questions = parse_questions(NO_STATUS_SAMPLE)
    assert status is None
    assert len(questions) == 1
    assert questions[0].answered


def test_parse_questions_whitespace_only_answer_is_blank():
    text = "STATUS: NEEDS_INPUT\n\n## Q1. Question?\n\n**Answer:**    \n\n"
    _, questions = parse_questions(text)
    assert len(questions) == 1
    assert not questions[0].answered


def test_parse_questions_multiline_answer_is_answered():
    text = (
        "STATUS: READY\n\n"
        "## Q1. Question?\n\n"
        "**Answer:**\nLine one.\nLine two.\n"
    )
    _, questions = parse_questions(text)
    assert len(questions) == 1
    assert questions[0].answered


def test_parse_questions_status_is_uppercased():
    text = "STATUS: ready\n"
    status, _ = parse_questions(text)
    assert status == "READY"


def test_parse_questions_heading_strips_leading_hash_space():
    text = "STATUS: READY\n\n## Q3. The title — with a dash.\n\n**Answer:** done\n"
    _, questions = parse_questions(text)
    assert questions[0].heading == "Q3. The title \u2014 with a dash."


# ---------------------------------------------------------------------------
# unaddressed_findings — parser unit tests
# ---------------------------------------------------------------------------

REVIEW_F1_F2 = """\
## Review

### F1 Use consistent naming

Some details.

### F2 Add type annotations

More details.
"""

RESPONSE_F1_ONLY = """\
- F1: accepted — naming is fine as-is
"""

RESPONSE_F1_AND_F2 = """\
- F1: accepted — naming is fine
- F2: rejected — type annotations are optional here
"""

REVIEW_NO_FINDINGS = """\
## Overall

Looks good. No findings.
"""

REVIEW_F1_AND_F10 = """\
### F1 Short title

### F10 Another finding
"""


def test_unaddressed_findings_one_missing():
    result = unaddressed_findings(REVIEW_F1_F2, RESPONSE_F1_ONLY)
    assert result == ["F2"]


def test_unaddressed_findings_all_covered():
    result = unaddressed_findings(REVIEW_F1_F2, RESPONSE_F1_AND_F2)
    assert result == []


def test_unaddressed_findings_no_findings_in_review():
    result = unaddressed_findings(REVIEW_NO_FINDINGS, "anything")
    assert result == []


def test_unaddressed_findings_f10_does_not_satisfy_f1():
    """A disposition line for F10 must not count as covering F1."""
    response = "- F10: accepted — not related to F1\n"
    result = unaddressed_findings(REVIEW_F1_AND_F10, response)
    # F1 is unaddressed; F10 is addressed
    assert "F1" in result
    assert "F10" not in result


def test_unaddressed_findings_case_insensitive_disposition():
    response = "- F1: Accepted — caps test\n- F2: DEFERRED — also caps\n"
    result = unaddressed_findings(REVIEW_F1_F2, response)
    assert result == []


def test_unaddressed_findings_deduplicates_findings():
    review = "### F1 First\n\n### F1 Duplicate heading\n\n### F2 Second\n"
    response = "- F1: accepted\n- F2: rejected\n"
    result = unaddressed_findings(review, response)
    assert result == []


# ---------------------------------------------------------------------------
# format_gate_error
# ---------------------------------------------------------------------------


def test_format_gate_error_no_items():
    msg = format_gate_error("summary", [], "action")
    lines = msg.splitlines()
    assert lines[0] == "summary"
    assert lines[-1] == "action"
    assert len(lines) <= 8


def test_format_gate_error_five_items():
    items = [f"item{i}" for i in range(5)]
    msg = format_gate_error("summary", items, "action")
    lines = msg.splitlines()
    assert lines[0] == "summary"
    assert lines[-1] == "action"
    assert len(lines) == 7  # 1 + 5 + 1
    assert "\u2026" not in msg


def test_format_gate_error_truncates_at_five_with_more_line():
    items = [f"item{i}" for i in range(9)]
    msg = format_gate_error("summary", items, "action")
    lines = msg.splitlines()
    assert len(lines) == 8
    assert lines[0] == "summary"
    assert "\u2026 and 4 more" in lines[6]
    assert lines[-1] == "action"


def test_format_gate_error_items_are_indented():
    msg = format_gate_error("s", ["a", "b"], "act")
    lines = msg.splitlines()
    assert lines[1] == "  a"
    assert lines[2] == "  b"


# ---------------------------------------------------------------------------
# OpenQuestionsGate — one test per rule
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_questions_gate_missing_file(tmp_path):
    """Rule 1: file does not exist → fail."""
    stage = OpenQuestionsGate(path=str(tmp_path / "questions.md"))
    result = await stage.run(_ctx())
    assert not result.success
    assert "plan questions file not written" in result.error
    assert result.name == ""


@pytest.mark.asyncio
async def test_open_questions_gate_no_status_line(tmp_path):
    """Rule 2: no STATUS: line → fail."""
    f = tmp_path / "q.md"
    f.write_text("## Q1. Question?\n\n**Answer:** done\n")
    stage = OpenQuestionsGate(path=str(f))
    result = await stage.run(_ctx())
    assert not result.success
    assert "no STATUS: line" in result.error


@pytest.mark.asyncio
async def test_open_questions_gate_needs_input_with_blanks(tmp_path):
    """Rule 3: NEEDS_INPUT with blank questions → fail, listing headings."""
    f = tmp_path / "q.md"
    f.write_text(NEEDS_INPUT_SAMPLE)
    stage = OpenQuestionsGate(path=str(f))
    result = await stage.run(_ctx())
    assert not result.success
    assert "1 open questions" in result.error
    assert "Q1" in result.error


@pytest.mark.asyncio
async def test_open_questions_gate_needs_input_zero_questions(tmp_path):
    """Rule 3: NEEDS_INPUT with zero questions → must still fail.

    The error must say the draft lists no questions rather than counting the
    blanks: a "0 open questions need answers" summary reads as though nothing
    is wrong, leaving the human with no idea why the run stopped.
    """
    f = tmp_path / "q.md"
    f.write_text("STATUS: NEEDS_INPUT\n\nNo questions listed.\n")
    stage = OpenQuestionsGate(path=str(f))
    result = await stage.run(_ctx())
    assert not result.success
    assert "0 open questions" not in result.error
    assert "lists no questions" in result.error
    assert "STATUS: READY" in result.error  # names the way out


@pytest.mark.asyncio
async def test_open_questions_gate_needs_input_all_answered(tmp_path):
    """Rule 3: NEEDS_INPUT with every slot filled → fail, and say why.

    The agent owns ``STATUS:``; a file that still claims NEEDS_INPUT after the
    human answered everything means the revise stage has not caught up. That is
    the other path to a zero blank count, so it needs its own message too.
    """
    text = (
        "STATUS: NEEDS_INPUT\n\n"
        "## Q1. Answered?\n\n**Answer:** yes\n\n"
        "## Q2. Also answered?\n\n**Answer:** also yes\n"
    )
    f = tmp_path / "q.md"
    f.write_text(text)
    stage = OpenQuestionsGate(path=str(f))
    result = await stage.run(_ctx())
    assert not result.success
    assert "0 open questions" not in result.error
    assert "all 2 questions" in result.error


@pytest.mark.asyncio
async def test_open_questions_gate_ready_with_blank_slot(tmp_path):
    """Rule 4: READY but one answer is blank → fail."""
    text = (
        "STATUS: READY\n\n"
        "## Q1. Answered?\n\n**Answer:** yes\n\n"
        "## Q2. Unanswered?\n\n**Answer:**\n"
    )
    f = tmp_path / "q.md"
    f.write_text(text)
    stage = OpenQuestionsGate(path=str(f))
    result = await stage.run(_ctx())
    assert not result.success
    assert "Q2" in result.error


@pytest.mark.asyncio
async def test_open_questions_gate_ready_fully_answered(tmp_path):
    """Rule 5: READY with all questions answered → pass."""
    text = (
        "STATUS: READY\n\n"
        "## Q1. Question one?\n\n**Answer:** answer one\n\n"
        "## Q2. Question two?\n\n**Answer:** answer two\n"
    )
    f = tmp_path / "q.md"
    f.write_text(text)
    stage = OpenQuestionsGate(path=str(f))
    result = await stage.run(_ctx())
    assert result.success


@pytest.mark.asyncio
async def test_open_questions_gate_ready_zero_questions(tmp_path):
    """Rule 6: READY with zero Q<n> sections → pass (vacuously)."""
    f = tmp_path / "q.md"
    f.write_text(NO_QUESTIONS_SAMPLE)
    stage = OpenQuestionsGate(path=str(f))
    result = await stage.run(_ctx())
    assert result.success


@pytest.mark.asyncio
async def test_open_questions_gate_unrecognised_status(tmp_path):
    """Rule 7: unrecognised STATUS value → fail, naming the value."""
    f = tmp_path / "q.md"
    f.write_text("STATUS: PENDING\n\n## Q1. Q?\n\n**Answer:** done\n")
    stage = OpenQuestionsGate(path=str(f))
    result = await stage.run(_ctx())
    assert not result.success
    assert "PENDING" in result.error


# ---------------------------------------------------------------------------
# OpenQuestionsGate — resolve_run_path integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_questions_gate_resolves_relative_path(tmp_path):
    """A relative path= finds the file under ctx.working_dir."""
    (tmp_path / "q.md").write_text(NO_QUESTIONS_SAMPLE)
    stage = OpenQuestionsGate(path="q.md")
    result = await stage.run(_ctx(tmp_path))
    assert result.success


@pytest.mark.asyncio
async def test_open_questions_gate_relative_missing_under_working_dir(tmp_path):
    """A relative path= that does not exist under working_dir → fail."""
    stage = OpenQuestionsGate(path="nope.md")
    result = await stage.run(_ctx(tmp_path))
    assert not result.success
    assert "plan questions file not written" in result.error


# ---------------------------------------------------------------------------
# ReviewDispositionGate — one test per rule
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_disposition_gate_missing_review(tmp_path):
    """Rule 1: review file missing → fail."""
    stage = ReviewDispositionGate(
        review=str(tmp_path / "review.md"),
        response=str(tmp_path / "response.md"),
    )
    result = await stage.run(_ctx())
    assert not result.success
    assert "codex review file not written" in result.error


@pytest.mark.asyncio
async def test_review_disposition_gate_no_findings(tmp_path):
    """Rule 2: review with no F<n> headings → pass (vacuously)."""
    review = tmp_path / "review.md"
    review.write_text(REVIEW_NO_FINDINGS)
    stage = ReviewDispositionGate(
        review=str(review),
        response=str(tmp_path / "response.md"),
    )
    result = await stage.run(_ctx())
    assert result.success


@pytest.mark.asyncio
async def test_review_disposition_gate_missing_response_with_findings(tmp_path):
    """Rule 3: response file missing when findings exist → fail, listing IDs."""
    review = tmp_path / "review.md"
    review.write_text(REVIEW_F1_F2)
    stage = ReviewDispositionGate(
        review=str(review),
        response=str(tmp_path / "response.md"),
    )
    result = await stage.run(_ctx())
    assert not result.success
    assert "review response file not written" in result.error
    assert "F1" in result.error
    assert "F2" in result.error


@pytest.mark.asyncio
async def test_review_disposition_gate_partial_dispositions(tmp_path):
    """Rule 4: F2 not dispositioned → fail, listing F2."""
    review = tmp_path / "review.md"
    review.write_text(REVIEW_F1_F2)
    response = tmp_path / "response.md"
    response.write_text(RESPONSE_F1_ONLY)
    stage = ReviewDispositionGate(review=str(review), response=str(response))
    result = await stage.run(_ctx())
    assert not result.success
    assert "F2" in result.error


@pytest.mark.asyncio
async def test_review_disposition_gate_all_dispositioned(tmp_path):
    """Rule 5: all findings dispositioned → pass."""
    review = tmp_path / "review.md"
    review.write_text(REVIEW_F1_F2)
    response = tmp_path / "response.md"
    response.write_text(RESPONSE_F1_AND_F2)
    stage = ReviewDispositionGate(review=str(review), response=str(response))
    result = await stage.run(_ctx())
    assert result.success


# ---------------------------------------------------------------------------
# ReviewDispositionGate — resolve_run_path integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_disposition_gate_resolves_relative_paths(tmp_path):
    """Relative review= and response= resolve under ctx.working_dir."""
    (tmp_path / "review.md").write_text(REVIEW_F1_F2)
    (tmp_path / "response.md").write_text(RESPONSE_F1_AND_F2)
    stage = ReviewDispositionGate(review="review.md", response="response.md")
    result = await stage.run(_ctx(tmp_path))
    assert result.success


# ---------------------------------------------------------------------------
# Error-shape contract tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_questions_gate_nine_blanks_error_fits_eight_lines(tmp_path):
    """Nine blank questions → error ≤ 8 lines, first line starts with '9 open questions',
    contains '… and 4 more'."""
    lines_md = ["STATUS: NEEDS_INPUT", ""]
    for i in range(1, 10):
        lines_md += [f"## Q{i}. Question number {i}?", "", "**Answer:**", ""]
    f = tmp_path / "q.md"
    f.write_text("\n".join(lines_md))
    stage = OpenQuestionsGate(path=str(f))
    result = await stage.run(_ctx())
    assert not result.success
    error_lines = result.error.splitlines()
    assert len(error_lines) <= 8
    assert error_lines[0].startswith("9 open questions")
    assert any("\u2026 and 4 more" in line for line in error_lines)


@pytest.mark.asyncio
async def test_all_failing_gates_have_non_empty_error_under_200_chars(tmp_path):
    """Every failing gate result has a non-empty error whose first line is < 200 chars."""
    cases: list[tuple[str, object]] = []

    # OpenQuestionsGate: missing file
    cases.append(("oq_missing", OpenQuestionsGate(path=str(tmp_path / "no.md"))))

    # OpenQuestionsGate: no status
    no_status = tmp_path / "no_status.md"
    no_status.write_text("## Q1. Q?\n\n**Answer:** done\n")
    cases.append(("oq_no_status", OpenQuestionsGate(path=str(no_status))))

    # OpenQuestionsGate: NEEDS_INPUT
    needs_input = tmp_path / "ni.md"
    needs_input.write_text(NEEDS_INPUT_SAMPLE)
    cases.append(("oq_needs_input", OpenQuestionsGate(path=str(needs_input))))

    # OpenQuestionsGate: READY with blank
    ready_blank = tmp_path / "rb.md"
    ready_blank.write_text(
        "STATUS: READY\n\n## Q1. Question?\n\n**Answer:**\n"
    )
    cases.append(("oq_ready_blank", OpenQuestionsGate(path=str(ready_blank))))

    # ReviewDispositionGate: missing review
    cases.append((
        "rd_missing_review",
        ReviewDispositionGate(
            review=str(tmp_path / "no_review.md"),
            response=str(tmp_path / "no_resp.md"),
        ),
    ))

    # ReviewDispositionGate: missing response
    review_f = tmp_path / "review2.md"
    review_f.write_text(REVIEW_F1_F2)
    cases.append((
        "rd_missing_response",
        ReviewDispositionGate(
            review=str(review_f),
            response=str(tmp_path / "no_resp2.md"),
        ),
    ))

    # ReviewDispositionGate: partial disposition
    resp_partial = tmp_path / "resp_partial.md"
    resp_partial.write_text(RESPONSE_F1_ONLY)
    cases.append((
        "rd_partial",
        ReviewDispositionGate(review=str(review_f), response=str(resp_partial)),
    ))

    for label, stage in cases:
        result = await stage.run(_ctx())
        assert not result.success, f"{label}: expected failure"
        assert result.error, f"{label}: error must be non-empty"
        first_line = result.error.splitlines()[0]
        assert len(first_line) < 200, (
            f"{label}: first error line is {len(first_line)} chars (≥ 200)"
        )


# ---------------------------------------------------------------------------
# step_test_cmd — parser unit tests
# ---------------------------------------------------------------------------


def test_step_test_cmd_reads_front_matter():
    text = "---\ntest_cmd: uv run python -m pytest tests/test_x.py -v\n---\n\n# Step\n"
    assert step_test_cmd(text) == "uv run python -m pytest tests/test_x.py -v"


def test_step_test_cmd_reads_block_scalar():
    text = "---\ntest_cmd: |\n  sh -ex -c '\n    echo hi\n  '\nmodel: opus\n---\n\n# Step\n"
    assert step_test_cmd(text).startswith("sh -ex -c")


def test_step_test_cmd_none_without_front_matter():
    assert step_test_cmd("# Step\n\nDo the thing.\n") is None


def test_step_test_cmd_none_when_key_missing_or_blank():
    assert step_test_cmd("---\nmodel: opus\n---\n\n# Step\n") is None
    assert step_test_cmd("---\ntest_cmd:\n---\n\n# Step\n") is None


# ---------------------------------------------------------------------------
# StepFilesGate
# ---------------------------------------------------------------------------


def _write_steps(tmp_path, *, index=True, steps=("uv run python -m pytest tests/test_x.py",)):
    """Build a steps directory; each entry in *steps* becomes one step file."""
    steps_dir = tmp_path / "x-plan"
    steps_dir.mkdir(exist_ok=True)
    if index:
        (steps_dir / "index.md").write_text("# Shared context\n")
    for i, cmd in enumerate(steps, start=1):
        body = f"---\ntest_cmd: {cmd}\n---\n\n# Step {i}\n" if cmd else f"---\nmodel: opus\n---\n\n# Step {i}\n"
        (steps_dir / f"step-{i:02d}-thing.md").write_text(body)
    return steps_dir


@pytest.mark.asyncio
async def test_step_files_gate_missing_directory(tmp_path):
    gate = StepFilesGate(steps_dir=str(tmp_path / "x-plan"))
    result = await gate.run(_ctx())
    assert result.success is False
    assert "not written" in result.error


@pytest.mark.asyncio
async def test_step_files_gate_missing_index(tmp_path):
    steps_dir = _write_steps(tmp_path, index=False)
    result = await StepFilesGate(steps_dir=str(steps_dir)).run(_ctx())
    assert result.success is False
    assert "index.md" in result.error


@pytest.mark.asyncio
async def test_step_files_gate_no_step_files(tmp_path):
    steps_dir = _write_steps(tmp_path, steps=())
    result = await StepFilesGate(steps_dir=str(steps_dir)).run(_ctx())
    assert result.success is False
    assert "no step-*.md files" in result.error


@pytest.mark.asyncio
async def test_step_files_gate_missing_test_cmd(tmp_path):
    steps_dir = _write_steps(tmp_path, steps=("uv run python -m pytest tests/test_x.py", ""))
    result = await StepFilesGate(steps_dir=str(steps_dir)).run(_ctx())
    assert result.success is False
    assert "step-02-thing.md: no test_cmd" in result.error


@pytest.mark.asyncio
async def test_step_files_gate_placeholder_test_cmd(tmp_path):
    steps_dir = _write_steps(tmp_path, steps=("true",))
    result = await StepFilesGate(steps_dir=str(steps_dir)).run(_ctx())
    assert result.success is False
    assert "placeholder test_cmd" in result.error


@pytest.mark.asyncio
async def test_step_files_gate_passes_with_real_test_cmds(tmp_path):
    steps_dir = _write_steps(
        tmp_path,
        steps=("uv run python -m pytest tests/test_x.py", "uv run python -m pytest tests/test_y.py"),
    )
    result = await StepFilesGate(steps_dir=str(steps_dir)).run(_ctx())
    assert result.success is True


@pytest.mark.asyncio
async def test_step_files_gate_resolves_relative_path(tmp_path):
    _write_steps(tmp_path)
    result = await StepFilesGate(steps_dir="x-plan").run(_ctx(tmp_path))
    assert result.success is True

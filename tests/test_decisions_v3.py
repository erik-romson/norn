"""Tests for _decisions_v3: gate-file parsers, writers and gate stages.

All tests are offline — no agent calls, no subprocesses, no network.
"""
from __future__ import annotations

import pytest

from norn.models import PipelineContext
from norn.pipelines._decisions_v3 import (
    AutoDecide,
    AutoDecideCodeReview,
    CodeReviewGate,
    DecisionGate,
    ExpandRecommendedAnswers,
    FixesRecordedGate,
    QuestionsGate,
    ShipDecisionGate,
    WriteDecision,
    field_line,
    has_fix,
    parse_code_review,
    parse_recommendations,
    questions_need_revision,
    read_field,
    recommendation_problems,
)


def _ctx() -> PipelineContext:
    return PipelineContext()


def _q(qid: str, rec: str | None, answer: str, *, below: bool = False) -> str:
    rec_line = "" if rec is None else f"**Recommended answer:** {rec}\n"
    if below:
        return f"## {qid}. A question?\n**Why it matters:** x\n**Answer:** {answer}\n{rec_line}\n"
    return f"## {qid}. A question?\n**Why it matters:** x\n{rec_line}**Answer:** {answer}\n\n"


def _qfile(status: str, *sections: str) -> str:
    return f"# Open questions — x\n\nSTATUS: {status}\n\n" + "".join(sections)


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------


def test_parse_recommendations():
    text = _qfile(
        "NEEDS_INPUT",
        _q("Q1", "add it to the run report", ""),
        _q("Q2", "none — only the DBA knows", ""),
    )
    assert parse_recommendations(text) == {
        "Q1": "add it to the run report",
        "Q2": None,
    }
    assert recommendation_problems(text) == []


def test_recommendation_below_answer_is_a_problem():
    text = _qfile("NEEDS_INPUT", _q("Q1", "yes", "", below=True))
    problems = recommendation_problems(text)
    assert len(problems) == 1
    assert "Q1" in problems[0] and "below" in problems[0]


def test_recommendation_missing_and_empty():
    text = _qfile("NEEDS_INPUT", _q("Q1", None, ""), _q("Q2", "", ""))
    problems = recommendation_problems(text)
    assert any(p.startswith("Q1") for p in problems)
    assert any(p.startswith("Q2") and "empty" in p for p in problems)


def test_none_without_reason_is_a_problem():
    for bad in ("none", "none —", "None -"):
        problems = recommendation_problems(_qfile("NEEDS_INPUT", _q("Q1", bad, "")))
        assert len(problems) == 1 and "Q1" in problems[0] and "reason" in problems[0], bad
    assert recommendation_problems(_qfile("NEEDS_INPUT", _q("Q1", "none of the above", ""))) == []


# ---------------------------------------------------------------------------
# questions_need_revision
# ---------------------------------------------------------------------------


def test_need_revision_cases(tmp_path):
    p = tmp_path / "q.md"
    assert questions_need_revision(p) is False  # missing file

    p.write_text(_qfile("NEEDS_INPUT", _q("Q1", "a", ""), _q("Q2", "b", "")))
    assert questions_need_revision(p) is False

    p.write_text(_qfile("NEEDS_INPUT", _q("Q1", "a", "mine"), _q("Q2", "b", "")))
    assert questions_need_revision(p) is True

    p.write_text(_qfile("NEEDS_INPUT"))
    assert questions_need_revision(p) is True

    p.write_text(_qfile("READY", _q("Q1", "a", "mine"), _q("Q2", "b", "yes")))
    assert questions_need_revision(p) is False


# ---------------------------------------------------------------------------
# ExpandRecommendedAnswers / QuestionsGate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("fill_blanks", [False, True])
@pytest.mark.parametrize("word", ["recommended", "Recommended", "RECOMMENDED"])
async def test_expand_shorthand_any_case(tmp_path, word, fill_blanks):
    p = tmp_path / "q.md"
    p.write_text(_qfile("NEEDS_INPUT", _q("Q1", "use the cache", word)))
    result = await ExpandRecommendedAnswers(path=str(p), fill_blanks=fill_blanks).run(_ctx())
    assert result.success
    assert "**Answer:** use the cache _(recommended answer)_" in p.read_text()


@pytest.mark.asyncio
async def test_expand_blank_only_with_fill_blanks(tmp_path):
    p = tmp_path / "q.md"
    original = _qfile("NEEDS_INPUT", _q("Q1", "use the cache", ""))
    p.write_text(original)
    assert (await ExpandRecommendedAnswers(path=str(p), fill_blanks=False).run(_ctx())).success
    assert p.read_text() == original
    assert (await ExpandRecommendedAnswers(path=str(p), fill_blanks=True).run(_ctx())).success
    assert "**Answer:** use the cache _(auto: recommended answer)_" in p.read_text()


@pytest.mark.asyncio
async def test_expand_writes_fillable_then_fails_on_none(tmp_path):
    p = tmp_path / "q.md"
    p.write_text(
        _qfile(
            "NEEDS_INPUT",
            _q("Q1", "none — a human must pick", ""),
            _q("Q2", "use the cache", ""),
        )
    )
    result = await ExpandRecommendedAnswers(path=str(p), fill_blanks=True).run(_ctx())
    assert not result.success
    assert "Q1" in result.error and "Q2" not in result.error
    text = p.read_text()
    assert "use the cache _(auto: recommended answer)_" in text
    # Q1 still blank
    assert "**Answer:** \n" in text or "**Answer:**\n" in text


@pytest.mark.asyncio
async def test_expand_leaves_human_answers_and_status(tmp_path):
    p = tmp_path / "q.md"
    p.write_text(_qfile("NEEDS_INPUT", _q("Q1", "x", "my own answer"), _q("Q2", "y", "recommended")))
    result = await ExpandRecommendedAnswers(path=str(p), fill_blanks=True).run(_ctx())
    assert result.success
    text = p.read_text()
    assert "**Answer:** my own answer\n" in text
    assert "STATUS: NEEDS_INPUT" in text
    assert "**Answer:** y _(recommended answer)_" in text


@pytest.mark.asyncio
async def test_questions_gate_literal_recommended_fails(tmp_path):
    p = tmp_path / "q.md"
    p.write_text(_qfile("READY", _q("Q1", "x", "recommended")))
    result = await QuestionsGate(path=str(p)).run(_ctx())
    assert not result.success
    assert "Q1" in result.error and "[r]etry" in result.error


@pytest.mark.asyncio
async def test_questions_gate_passes_answered_ready(tmp_path):
    p = tmp_path / "q.md"
    p.write_text(_qfile("READY", _q("Q1", "x", "x _(recommended answer)_"), _q("Q2", "y", "mine")))
    assert (await QuestionsGate(path=str(p)).run(_ctx())).success


@pytest.mark.asyncio
async def test_questions_gate_open_question_and_missing_recommendation(tmp_path):
    p = tmp_path / "q.md"
    p.write_text(_qfile("READY", _q("Q1", "x", "")))
    assert not (await QuestionsGate(path=str(p)).run(_ctx())).success
    p.write_text(_qfile("READY", _q("Q1", None, "mine")))
    result = await QuestionsGate(path=str(p)).run(_ctx())
    assert not result.success and "Q1" in result.error


# ---------------------------------------------------------------------------
# read_field
# ---------------------------------------------------------------------------


def test_read_field(tmp_path):
    p = tmp_path / "f.md"
    assert read_field(p, "DECISION") is None  # missing file
    p.write_text("DECISION: approve (auto)\n")
    assert read_field(p, "DECISION") == "approve"
    assert read_field(p, "decision") == "approve"
    assert read_field(p, "OTHER") is None

    p.write_text("<!-- STATUS: DECIDED -->\nSTATUS: PROPOSED\n")
    assert read_field(p, "STATUS") == "PROPOSED"

    p.write_text("<!-- STATUS: DECIDED -->\n")
    assert read_field(p, "STATUS") is None

    p.write_text("<!--\nSTATUS: DECIDED\n-->\nSTATUS: PROPOSED\n")
    assert read_field(p, "STATUS") == "PROPOSED"

    p.write_text("TITLE: CBS-1: fix the thing\n")
    assert read_field(p, "TITLE") == "CBS-1:"
    assert read_field(p, "TITLE", first_token=False) == "CBS-1: fix the thing"


# ---------------------------------------------------------------------------
# Writers and decision gates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_decision_overwrites_decided_file(tmp_path):
    p = tmp_path / "sub" / "d.md"
    p.parent.mkdir()
    p.write_text("STATUS: DECIDED\n")
    result = await WriteDecision(path=str(p), render=lambda ctx: "STATUS: PROPOSED\n").run(_ctx())
    assert result.success
    assert p.read_text() == "STATUS: PROPOSED\n"


@pytest.mark.asyncio
async def test_auto_decide(tmp_path):
    p = tmp_path / "d.md"
    p.write_text("# x\n\nSTATUS: DECIDED\nCOMMITS: keep\n")
    stage = AutoDecide(path=str(p), field="STATUS", value="DECIDED")
    assert (await stage.run(_ctx())).success
    assert p.read_text() == "# x\n\nSTATUS: DECIDED\nCOMMITS: keep\n"

    p.write_text("# x\n\nSTATUS: PROPOSED\nCOMMITS: keep\n")
    assert (await stage.run(_ctx())).success
    assert p.read_text() == "# x\n\nSTATUS: DECIDED (auto)\nCOMMITS: keep\n"

    p.write_text("DECISION: pending\nSet DECISION to approve.\n")
    gate2 = AutoDecide(path=str(p), field="DECISION", value="approve", note="plan_gate=off")
    assert (await gate2.run(_ctx())).success
    assert p.read_text() == "DECISION: approve (plan_gate=off)\nSet DECISION to approve.\n"


@pytest.mark.asyncio
async def test_decision_gate(tmp_path):
    p = tmp_path / "d.md"
    gate = DecisionGate(
        path=str(p), field="DECISION", accept=frozenset({"approve"}), action="Set it to approve."
    )
    result = await gate.run(_ctx())
    assert not result.success

    p.write_text("DECISION: pending\n")
    result = await gate.run(_ctx())
    assert not result.success
    assert result.error == f"{p}: DECISION is pending. Set it to approve."

    p.write_text("DECISION: Approve (auto)\n")
    assert (await gate.run(_ctx())).success


def test_field_line_finds_the_line_read_field_reads(tmp_path):
    p = tmp_path / "ship.md"
    p.write_text("# Ship\n<!--\nSTATUS: commented out\n-->\nstatus:  PROPOSED  \n")

    assert field_line(p, "STATUS") == (5, "status:  PROPOSED")
    assert field_line(p, "TITLE") is None
    assert field_line(tmp_path / "missing.md", "STATUS") is None


@pytest.mark.asyncio
async def test_decision_gate_with_show_line_names_the_line(tmp_path):
    p = tmp_path / "ship.md"
    p.write_text(_ship(status="PROPOSED"))
    gate = DecisionGate(
        path=str(p), field="STATUS", accept=frozenset({"DECIDED"}), action="Set it.", show_line=True
    )

    result = await gate.run(_ctx())

    assert not result.success
    assert result.error.splitlines() == [
        f"{p}:3: STATUS is PROPOSED.",
        "  line 3 now: STATUS: PROPOSED",
        "Set it.",
    ]


@pytest.mark.asyncio
async def test_decision_gate_with_show_line_and_no_field_keeps_the_plain_error(tmp_path):
    p = tmp_path / "ship.md"
    p.write_text("# Ship\n")
    gate = DecisionGate(
        path=str(p), field="STATUS", accept=frozenset({"DECIDED"}), action="Set it.", show_line=True
    )

    result = await gate.run(_ctx())

    assert result.error == f"{p}: STATUS is missing. Set it."


# ---------------------------------------------------------------------------
# Gate 3
# ---------------------------------------------------------------------------


def _finding(n: int, severity: str = "major", disposition: str = "fix", extra: str = "") -> str:
    tag = f" [{severity}]" if severity else ""
    disp = f"Disposition: {disposition}\n" if disposition is not None else ""
    return (
        f"## C{n}. Title {n}{tag}\n"
        f"Where: a/b/C{n}.java:{n}\n"
        f"Problem: it is wrong {n}.\n"
        f"Fix: do it.\n"
        f"Source: claude\n"
        f"{disp}{extra}\n"
    )


def _review(*sections: str, status: str = "PROPOSED", tail: str = "") -> str:
    return (
        "# Code review — CBS-1 (aaaaaaa..bbbbbbb)\n\n"
        f"STATUS: {status}\n"
        "<!-- Set each Disposition to fix, skip or follow-up, then set STATUS to DECIDED. -->\n\n"
        + "".join(sections)
        + tail
    )


@pytest.mark.asyncio
async def test_code_review_gate_passes_and_removes_problems_file(tmp_path):
    p = tmp_path / "r.md"
    pf = tmp_path / "problems.txt"
    pf.write_text("old\n")
    p.write_text(_review(_finding(1), _finding(2, "nit", "skip")))
    result = await CodeReviewGate(path=str(p), problems_file=str(pf)).run(_ctx())
    assert result.success
    assert not pf.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text, needle",
    [
        (_review(_finding(1, disposition=None)), "C1"),
        (_review(_finding(1, disposition="maybe")), "maybe"),
        (_review(_finding(1, extra="l1\nl2\nl3\n")), "9 lines"),
        (_review("### C1. Title [major]\nWhere: a\nProblem: b\nDisposition: fix\n"), "malformed"),
        (_review(), "No findings."),
        (
            _review(_finding(1), tail="\n## Omitted findings\n- Mapper reset races [major]\n"),
            "Mapper reset races",
        ),
    ],
)
async def test_code_review_gate_shape_errors(tmp_path, text, needle):
    p = tmp_path / "r.md"
    pf = tmp_path / "problems.txt"
    p.write_text(text)
    result = await CodeReviewGate(path=str(p), problems_file=str(pf)).run(_ctx())
    assert not result.success
    assert needle in result.error
    assert needle in pf.read_text()


@pytest.mark.asyncio
async def test_code_review_gate_require_decided(tmp_path):
    p = tmp_path / "r.md"
    p.write_text(_review(_finding(1)))
    assert not (await CodeReviewGate(path=str(p), require_decided=True).run(_ctx())).success
    p.write_text(_review(_finding(1), status="DECIDED"))
    assert (await CodeReviewGate(path=str(p), require_decided=True).run(_ctx())).success


@pytest.mark.asyncio
async def test_code_review_gate_no_findings(tmp_path):
    p = tmp_path / "r.md"
    p.write_text(_review("No findings.\n", status="DECIDED"))
    assert (await CodeReviewGate(path=str(p), require_decided=True).run(_ctx())).success


@pytest.mark.asyncio
async def test_code_review_gate_missing_file(tmp_path):
    result = await CodeReviewGate(path=str(tmp_path / "nope.md")).run(_ctx())
    assert not result.success


def test_has_fix(tmp_path):
    p = tmp_path / "r.md"
    assert has_fix(p) is False
    p.write_text(_review(_finding(1, disposition="skip")))
    assert has_fix(p) is False
    p.write_text(_review(_finding(1, disposition="skip"), _finding(2, disposition="Fix")))
    assert has_fix(p) is True


@pytest.mark.asyncio
async def test_auto_decide_code_review(tmp_path):
    p = tmp_path / "r.md"
    dropped = "\n## Dropped as false positives\n- claude R3 Null check missing — final field\n"
    p.write_text(
        _review(
            _finding(1, "critical", "follow-up"),
            _finding(2, "minor", "skip"),
            _finding(3, "", "skip"),
            _finding(4, "nit", "fix"),
            tail=dropped,
        )
    )
    result = await AutoDecideCodeReview(path=str(p)).run(_ctx())
    assert result.success
    review = parse_code_review(p.read_text())
    assert [f.disposition for f in review.findings] == ["fix", "fix", "fix", "skip"]
    text = p.read_text()
    assert "Disposition: skip (auto: nit)" in text
    assert "STATUS: DECIDED (auto)" in text
    assert text.endswith(dropped)
    assert review.problems == []


@pytest.mark.asyncio
async def test_auto_decide_code_review_leaves_decided_alone(tmp_path):
    p = tmp_path / "r.md"
    text = _review(_finding(1, "major", "skip"), status="DECIDED")
    p.write_text(text)
    assert (await AutoDecideCodeReview(path=str(p)).run(_ctx())).success
    assert p.read_text() == text


@pytest.mark.asyncio
async def test_more_than_fifteen_findings(tmp_path):
    p = tmp_path / "r.md"
    sections = [_finding(n, "minor", "skip") for n in range(1, 16)] + [_finding(16, "major", "skip")]
    tail = "\n## Omitted findings\n- Rename helper [nit]\n- Tidy import [nit]\n"
    p.write_text(_review(*sections, tail=tail))

    review = parse_code_review(p.read_text())
    assert len(review.findings) == 16
    assert review.problems == []
    assert review.omitted_nits == ["Rename helper [nit]", "Tidy import [nit]"]
    assert (await CodeReviewGate(path=str(p)).run(_ctx())).success

    assert (await AutoDecideCodeReview(path=str(p)).run(_ctx())).success
    decided = parse_code_review(p.read_text())
    assert decided.findings[15].cid == "C16"
    assert decided.findings[15].disposition == "fix"
    assert decided.omitted_nits == review.omitted_nits


# ---------------------------------------------------------------------------
# FixesRecordedGate
# ---------------------------------------------------------------------------


def _fixes_gate(tmp_path, *, review_text, fixes_text, owned_text, auto):
    review = tmp_path / "review.md"
    fixes = tmp_path / "fixes.md"
    owned = tmp_path / "owned.txt"
    review.write_text(review_text)
    if fixes_text is not None:
        fixes.write_text(fixes_text)
    if owned_text is not None:
        owned.write_text(owned_text)
    return FixesRecordedGate(review=str(review), fixes=str(fixes), owned=str(owned), auto=auto)


@pytest.mark.asyncio
async def test_fixes_recorded_interactive_vs_auto(tmp_path):
    review = _review(_finding(1), _finding(2), status="DECIDED")
    fixes = "- C1: fixed — synchronised\n- C2: not fixed — needs a design call\n"
    gate = _fixes_gate(tmp_path, review_text=review, fixes_text=fixes, owned_text="a.java\n", auto=False)
    assert (await gate.run(_ctx())).success
    gate = _fixes_gate(tmp_path, review_text=review, fixes_text=fixes, owned_text="a.java\n", auto=True)
    result = await gate.run(_ctx())
    assert not result.success and "C2" in result.error


@pytest.mark.asyncio
async def test_fixes_recorded_missing_line(tmp_path):
    review = _review(_finding(1), _finding(2, disposition="skip"), status="DECIDED")
    gate = _fixes_gate(tmp_path, review_text=review, fixes_text=None, owned_text="a.java\n", auto=False)
    result = await gate.run(_ctx())
    assert not result.success and "C1" in result.error and "C2" not in result.error


@pytest.mark.asyncio
@pytest.mark.parametrize("owned", [None, "", "\n"])
async def test_fixed_claim_with_nothing_owned_fails(tmp_path, owned):
    review = _review(_finding(1), status="DECIDED")
    gate = _fixes_gate(
        tmp_path, review_text=review, fixes_text="- C1: fixed — done\n", owned_text=owned, auto=False
    )
    result = await gate.run(_ctx())
    assert not result.success and "changed nothing" in result.error


# ---------------------------------------------------------------------------
# ShipDecisionGate
# ---------------------------------------------------------------------------


def _ship(commits="rewrite", title="CBS-1: fix the mapper", status="DECIDED") -> str:
    return f"# Ship — CBS-1\n\nSTATUS: {status}\nCOMMITS: {commits}\nTITLE: {title}\n"


@pytest.mark.asyncio
async def test_ship_decision_gate(tmp_path):
    p = tmp_path / "ship.md"
    gate = ShipDecisionGate(path=str(p), key="CBS-1")
    assert not (await gate.run(_ctx())).success  # missing

    p.write_text(_ship())
    assert (await gate.run(_ctx())).success
    p.write_text(_ship(commits="keep", status="DECIDED (auto)"))
    assert (await gate.run(_ctx())).success

    p.write_text(_ship(commits="rewite"))
    result = await gate.run(_ctx())
    assert not result.success and "COMMITS" in result.error

    p.write_text(_ship(title="CBS-1: Fix"))
    result = await gate.run(_ctx())
    assert not result.success and "TITLE" in result.error

    p.write_text(_ship(status="PROPOSED"))
    result = await gate.run(_ctx())
    assert not result.success and "STATUS" in result.error


@pytest.mark.asyncio
async def test_ship_decision_gate_with_show_line_numbers_each_problem(tmp_path):
    p = tmp_path / "ship.md"
    p.write_text(_ship(status="PROPOSED", commits="rewite"))
    plain = await ShipDecisionGate(path=str(p), key="CBS-1").run(_ctx())

    result = await ShipDecisionGate(path=str(p), key="CBS-1", show_line=True).run(_ctx())

    assert "  line 3: STATUS is PROPOSED; set it to DECIDED" in result.error.splitlines()
    assert "  line 4: COMMITS is rewite; it must be rewrite or keep" in result.error.splitlines()
    assert "line 3" not in plain.error


@pytest.mark.asyncio
async def test_ship_decision_gate_checks_a_github_subject_prefix(tmp_path):
    # fix_issue_v1 passes the commit prefix, not the file key (`issue-123`).
    p = tmp_path / "ship.md"
    gate = ShipDecisionGate(path=str(p), key="#123")
    p.write_text(_ship(title="#123: add x"))
    assert (await gate.run(_ctx())).success

    p.write_text(_ship(title="issue-123: add x"))
    result = await gate.run(_ctx())
    assert not result.success
    assert "TITLE: subject must start with '#123: '" in result.error

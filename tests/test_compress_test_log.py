from __future__ import annotations

import pytest

from norn.models import PipelineContext, StageResult
from norn.stages.compress_test_log import (
    CompressTestLog,
    _head_tail_truncate,
    extract_bats_failures,
    extract_pytest_failures,
)


# --- pytest extractor ----------------------------------------------------

PYTEST_OUTPUT = """\
============================= test session starts ==============================
platform darwin -- Python 3.13.0, pytest-8.0.0
rootdir: /tmp/x
collected 5 items

tests/test_foo.py::test_a PASSED                                         [ 20%]
tests/test_foo.py::test_b FAILED                                         [ 40%]
tests/test_foo.py::test_c PASSED                                         [ 60%]
tests/test_foo.py::test_d ERROR                                          [ 80%]
tests/test_foo.py::test_e PASSED                                         [100%]

=================================== ERRORS ====================================
______________________________ ERROR at test_d ________________________________
fixtureerror: missing fixture
=================================== FAILURES ===================================
___________________________________ test_b _____________________________________
    def test_b():
>       assert 1 == 2
E       AssertionError
tests/test_foo.py:5: AssertionError
=========================== short test summary info ============================
FAILED tests/test_foo.py::test_b - AssertionError
ERROR tests/test_foo.py::test_d - fixtureerror: missing fixture
========================= 1 failed, 1 error, 3 passed in 0.10s =================
"""


def test_pytest_extractor_keeps_failures_and_summary():
    out = extract_pytest_failures(PYTEST_OUTPUT)
    assert "ERRORS" in out
    assert "FAILURES" in out
    assert "AssertionError" in out
    assert "fixtureerror" in out
    assert "short test summary info" in out
    # Drops collection / progress noise.
    assert "test session starts" not in out
    assert "collected 5 items" not in out
    assert "test_a PASSED" not in out
    assert "test_c PASSED" not in out


def test_pytest_extractor_returns_empty_when_no_markers():
    assert extract_pytest_failures("just some random output\nno markers") == ""


def test_pytest_extractor_handles_summary_only():
    text = (
        "some preamble\n"
        "=========================== short test summary info ============================\n"
        "FAILED tests/x.py::test_y - boom\n"
        "===== 1 failed in 0.01s =====\n"
    )
    out = extract_pytest_failures(text)
    assert "short test summary info" in out
    assert "FAILED tests/x.py::test_y" in out
    assert "preamble" not in out


# --- bats extractor ------------------------------------------------------

BATS_PRETTY = """\
greeter
 ✓ greeter prints hello when given a name (15ms)
 ✗ greeter exits non-zero when given no args
   (in test file bats/test_greeter.bats, line 23)
     `[ "$status" = 1 ]' failed
   actual output: hello
 ✓ greeter accepts unicode

3 tests, 1 failure
"""


def test_bats_pretty_extractor_keeps_failures_and_summary():
    out = extract_bats_failures(BATS_PRETTY)
    assert "✗ greeter exits non-zero" in out
    assert "in test file bats/test_greeter.bats" in out
    assert "actual output: hello" in out
    assert "3 tests, 1 failure" in out
    # Passing tests dropped.
    assert "✓ greeter prints hello" not in out
    assert "✓ greeter accepts unicode" not in out


BATS_TAP = """\
1..3
ok 1 greeter prints hello when given a name
not ok 2 greeter exits non-zero when given no args
# (in test file bats/test_greeter.bats, line 23)
# `[ "$status" = 1 ]' failed
# actual output: hello
ok 3 greeter accepts unicode

3 tests, 1 failure
"""


def test_bats_tap_extractor_keeps_failures_and_summary():
    out = extract_bats_failures(BATS_TAP)
    assert "not ok 2 greeter exits non-zero" in out
    assert "# `[ \"$status\" = 1 ]' failed" in out
    assert "3 tests, 1 failure" in out
    assert "ok 1 greeter" not in out
    assert "ok 3 greeter" not in out


def test_bats_extractor_returns_empty_when_no_failures():
    out = extract_bats_failures("greeter\n ✓ everything passes\n\n1 tests, 0 failures\n")
    assert out == ""


# --- head/tail fallback --------------------------------------------------


def test_head_tail_truncate_passes_through_short_text():
    text = "short text"
    assert _head_tail_truncate(text, head=1000, tail=1000) == text


def test_head_tail_truncate_keeps_head_and_tail():
    text = "A" * 10_000 + "MIDDLE" + "B" * 10_000
    out = _head_tail_truncate(text, head=100, tail=100)
    assert out.startswith("A" * 100)
    assert out.endswith("B" * 100)
    assert "MIDDLE" not in out
    assert "chars omitted" in out


# --- stage integration ---------------------------------------------------


def _ctx_with_result(name: str, *, output, error: str | None = None, success: bool = False) -> PipelineContext:
    ctx = PipelineContext()
    ctx.results[name] = StageResult(name=name, success=success, output=output, error=error)
    return ctx


@pytest.mark.asyncio
async def test_stage_returns_empty_when_source_missing():
    stage = CompressTestLog(source_stage="nope")
    result = await stage.run(PipelineContext())
    assert result.success
    assert result.output == ""
    assert "has no result yet" in (result.error or "")


@pytest.mark.asyncio
async def test_stage_compresses_pytest_dict_output():
    ctx = _ctx_with_result(
        "test foo",
        output={"stdout": PYTEST_OUTPUT, "stderr": "", "returncode": 1},
    )
    stage = CompressTestLog(source_stage="test foo", summarize_with_haiku=False)
    result = await stage.run(ctx)
    assert result.success
    assert "FAILURES" in result.output
    assert "AssertionError" in result.output
    assert "test session starts" not in result.output


@pytest.mark.asyncio
async def test_stage_compresses_bats_output():
    ctx = _ctx_with_result(
        "bats foo",
        output={"stdout": BATS_PRETTY, "stderr": "", "returncode": 1},
    )
    stage = CompressTestLog(source_stage="bats foo", summarize_with_haiku=False)
    result = await stage.run(ctx)
    assert result.success
    assert "✗ greeter exits non-zero" in result.output
    assert "✓ greeter prints hello" not in result.output


@pytest.mark.asyncio
async def test_stage_falls_back_to_head_tail_for_unknown_format():
    raw = "X" * 5000 + "MIDDLE" + "Y" * 20_000
    ctx = _ctx_with_result(
        "build foo",
        output={"stdout": raw, "stderr": "", "returncode": 1},
    )
    stage = CompressTestLog(
        source_stage="build foo",
        summarize_with_haiku=False,
        fallback_head_chars=200,
        fallback_tail_chars=200,
    )
    result = await stage.run(ctx)
    assert result.success
    assert "MIDDLE" not in result.output
    assert "chars omitted" in result.output


@pytest.mark.asyncio
async def test_stage_skips_haiku_for_non_surefire_extracts():
    """Haiku is Java-stack specific — pytest/bats/fallback never invoke it.

    We don't even need to mock the SDK: ``summarize_with_haiku`` and the
    ``is_surefire`` flag together gate the call.
    """
    ctx = _ctx_with_result(
        "test foo",
        output={"stdout": PYTEST_OUTPUT, "stderr": "", "returncode": 1},
    )
    stage = CompressTestLog(
        source_stage="test foo",
        summarize_with_haiku=True,
        haiku_min_chars=10,  # would normally trigger
    )
    result = await stage.run(ctx)
    assert result.success
    # If Haiku had been called it would have replaced the deterministic
    # extract — verify a deterministic-extract fingerprint is present.
    assert "short test summary info" in result.output


@pytest.mark.asyncio
async def test_stage_uses_error_when_output_empty():
    ctx = _ctx_with_result("test foo", output=None, error=PYTEST_OUTPUT)
    stage = CompressTestLog(source_stage="test foo", summarize_with_haiku=False)
    result = await stage.run(ctx)
    assert result.success
    assert "FAILURES" in result.output


@pytest.mark.asyncio
async def test_stage_skips_when_source_succeeded_by_default():
    """``skip_on_success=True`` (the default) keeps successful-test noise out
    of the downstream prompt — avoids the loop's iteration-N+1 fix prompt
    seeing passing pytest output as if it were a failure."""
    ctx = _ctx_with_result(
        "test foo",
        output={"stdout": "all good\n", "stderr": "", "returncode": 0},
        success=True,
    )
    stage = CompressTestLog(source_stage="test foo", summarize_with_haiku=False)
    result = await stage.run(ctx)
    assert result.success
    assert result.output == ""


@pytest.mark.asyncio
async def test_stage_processes_success_when_skip_disabled():
    raw = "X" * 5000 + "MIDDLE" + "Y" * 20_000
    ctx = _ctx_with_result(
        "build foo",
        output={"stdout": raw, "stderr": "", "returncode": 0},
        success=True,
    )
    stage = CompressTestLog(
        source_stage="build foo",
        summarize_with_haiku=False,
        skip_on_success=False,
        fallback_head_chars=200,
        fallback_tail_chars=200,
    )
    result = await stage.run(ctx)
    assert result.success
    assert "chars omitted" in result.output

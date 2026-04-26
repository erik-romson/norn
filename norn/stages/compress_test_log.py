"""CompressTestLog — generic test-log compression stage.

Sibling of :class:`CompressSurefireLog` that handles more than just
Maven/Surefire output. Tries format-aware extractors in order and falls
back to head+tail truncation when none match. Optional Haiku pass on the
deterministic extract (only when the surefire extractor matched — the
prompt is Java-stack specific).

Supported formats:

  - Maven/Surefire — delegates to :func:`extract_surefire_failures`.
  - pytest — keeps the ``=+ FAILURES =+`` / ``=+ ERRORS =+`` blocks plus
    the ``=+ short test summary info =+`` tail.
  - BATS / TAP — keeps ``not ok`` / ``✗`` lines and their indented
    continuation, plus the trailing ``N tests, M failures`` summary.

Wire it after a :class:`RunCommand` whose output you don't want pasted
verbatim into a downstream Generate prompt::

    pipeline.loop(
        "test", max_retries=3, on_exhaust=fail,
        stages=[
            Stage("compress test", CompressTestLog(source_stage="test"),
                  when=stage_failed("test")),
            Stage("fix", Generate(prompt="... {compress test.output} ...")),
            Stage("test", RunCommand(cmd="uv run pytest -q")),
        ],
    )
"""
from __future__ import annotations

import logging
import re
from typing import Any

from norn.models import PipelineContext, StageResult
from norn.stages.base import BaseStage
from norn.stages.check_ci_surefire import (
    _haiku_summarize,
    extract_surefire_failures,
)
from norn.stages.compress_surefire import _extract_text

log = logging.getLogger(__name__)


# --- pytest --------------------------------------------------------------

# Section banner: ``============================ FAILURES ============================``.
_PYTEST_SECTION_RE = re.compile(r"^=+\s+(FAILURES|ERRORS|WARNINGS)\s+=+\s*$")
_PYTEST_SUMMARY_RE = re.compile(r"^=+\s+short test summary info\s+=+\s*$")
# Per-test PASSED/SKIPPED progress lines (verbose mode). Inside the FAILURES
# block these never appear, but the failing assertion sometimes echoes the
# captured stdout of passing tests, so we keep this filter narrow.
_PYTEST_PROGRESS_RE = re.compile(
    r"\s(?:PASSED|SKIPPED|XFAIL|XPASS|DESELECTED)\s+\[\s*\d+%\s*\]\s*$"
)


def extract_pytest_failures(raw: str) -> str:
    """Return the failure / error / summary block from pytest output.

    Empty string when the input doesn't look like pytest output (no
    ``=+ FAILURES =+`` banner and no ``=+ short test summary info =+``).
    """
    lines = raw.splitlines()
    start: int | None = None
    for i, line in enumerate(lines):
        if _PYTEST_SECTION_RE.match(line) or _PYTEST_SUMMARY_RE.match(line):
            start = i
            break
    if start is None:
        return ""

    out: list[str] = []
    for line in lines[start:]:
        if _PYTEST_PROGRESS_RE.search(line):
            continue
        out.append(line.rstrip())
    return "\n".join(out).rstrip()


# --- bats ----------------------------------------------------------------

# Pretty formatter (default): "  ✗ failing test name".
_BATS_PRETTY_FAIL_RE = re.compile(r"^(\s*)✗\s")
_BATS_PRETTY_PASS_RE = re.compile(r"^\s*✓\s")
# TAP formatter: "not ok 2 description".
_BATS_TAP_FAIL_RE = re.compile(r"^not ok\b")
_BATS_TAP_PASS_RE = re.compile(r"^ok\b")
# Trailing one-line summary printed by both formatters.
_BATS_SUMMARY_RE = re.compile(r"^\s*\d+\s+tests?,\s+\d+\s+failures?\b")


def extract_bats_failures(raw: str) -> str:
    """Return only the failing-test blocks + summary line from BATS output.

    Empty string when no BATS failure markers are present.
    """
    lines = raw.splitlines()
    has_fail = any(
        _BATS_PRETTY_FAIL_RE.match(ln) or _BATS_TAP_FAIL_RE.match(ln)
        for ln in lines
    )
    if not has_fail:
        return ""

    out: list[str] = []
    in_block = False
    fail_indent = 0
    for ln in lines:
        m = _BATS_PRETTY_FAIL_RE.match(ln)
        if m:
            in_block = True
            fail_indent = len(m.group(1))
            out.append(ln.rstrip())
            continue
        if _BATS_TAP_FAIL_RE.match(ln):
            in_block = True
            fail_indent = -1  # TAP uses leading "#" continuation, not indent
            out.append(ln.rstrip())
            continue
        if _BATS_PRETTY_PASS_RE.match(ln) or _BATS_TAP_PASS_RE.match(ln):
            in_block = False
            continue
        if _BATS_SUMMARY_RE.match(ln):
            in_block = False
            out.append(ln.rstrip())
            continue
        if not in_block:
            continue
        if not ln.strip():
            continue
        stripped = ln.lstrip()
        if fail_indent < 0:
            # TAP: continuation lines start with "#".
            if stripped.startswith("#"):
                out.append(ln.rstrip())
            else:
                in_block = False
        else:
            indent = len(ln) - len(stripped)
            if indent > fail_indent:
                out.append(ln.rstrip())
            else:
                in_block = False
    return "\n".join(out).rstrip()


# --- head/tail fallback --------------------------------------------------


def _head_tail_truncate(text: str, *, head: int, tail: int) -> str:
    """Keep ``head`` bytes from the start and ``tail`` bytes from the end.

    No-op when the text already fits comfortably (within head+tail+marker).
    """
    if len(text) <= head + tail + 100:
        return text
    omitted = len(text) - head - tail
    return (
        text[:head]
        + f"\n\n... ({omitted} chars omitted from middle) ...\n\n"
        + text[-tail:]
    )


# --- stage --------------------------------------------------------------


class CompressTestLog(BaseStage):
    """Compress arbitrary test/build output emitted by an upstream stage.

    Tries Maven/Surefire, then pytest, then BATS extractors. Falls back to
    head+tail truncation when nothing matches. The Haiku stack-trace
    compression pass is only applied when the surefire extractor matched
    (its prompt is Java-stack-specific).

    Args:
        source_stage: Name of the upstream stage whose
            ``StageResult.output`` (or ``error``) holds the raw log.
        app_packages: Forwarded to the Haiku prompt as ``APP_PACKAGES``.
            Only relevant when the surefire extractor matches. ``None``
            lets the model infer.
        summarize_with_haiku: Disable to skip the LLM compression pass.
            Defaults to ``True``.
        haiku_model: Model shorthand. Defaults to ``"haiku"``.
        haiku_min_chars: Skip Haiku when the deterministic surefire
            extract is below this. Defaults to 4000.
        haiku_max_input_chars: Hard cap on input handed to Haiku — head
            and tail are kept, middle is truncated. Defaults to 30000.
        fallback_head_chars: Bytes from the start of the upstream text
            kept when no extractor matches. Defaults to 4000.
        fallback_tail_chars: Bytes from the end of the upstream text
            kept when no extractor matches. Defaults to 12000.
        skip_on_success: When ``True`` (default), return an empty output
            if the upstream stage succeeded. Removes the need for a
            ``when=stage_failed(...)`` guard at the call site, and avoids
            polluting downstream prompts with passing-test noise.
    """

    needs_agent = False

    def __init__(
        self,
        *,
        source_stage: str,
        app_packages: list[str] | str | None = None,
        summarize_with_haiku: bool = True,
        haiku_model: str = "haiku",
        haiku_min_chars: int = 4000,
        haiku_max_input_chars: int = 30000,
        fallback_head_chars: int = 4000,
        fallback_tail_chars: int = 12000,
        skip_on_success: bool = True,
    ) -> None:
        self.source_stage = source_stage
        self.app_packages = app_packages
        self.summarize_with_haiku = summarize_with_haiku
        self.haiku_model = haiku_model
        self.haiku_min_chars = haiku_min_chars
        self.haiku_max_input_chars = haiku_max_input_chars
        self.fallback_head_chars = fallback_head_chars
        self.fallback_tail_chars = fallback_tail_chars
        self.skip_on_success = skip_on_success

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        prev = ctx.results.get(self.source_stage)
        if prev is None:
            return StageResult(
                name="", success=True, output="",
                error=(
                    f"compress_test_log: source stage "
                    f"'{self.source_stage}' has no result yet"
                ),
            )

        if self.skip_on_success and prev.success:
            return StageResult(name="", success=True, output="")

        text = _extract_text(prev)
        if not text:
            return StageResult(name="", success=True, output="")

        is_surefire = False
        extracted = extract_surefire_failures(text)
        if extracted:
            is_surefire = True
            kind = "surefire"
        else:
            extracted = extract_pytest_failures(text)
            kind = "pytest" if extracted else ""
        if not extracted:
            extracted = extract_bats_failures(text)
            if extracted:
                kind = "bats"
        if not extracted:
            extracted = _head_tail_truncate(
                text,
                head=self.fallback_head_chars,
                tail=self.fallback_tail_chars,
            )
            kind = "fallback"

        log.debug(
            "[compress_test_log] source=%s raw=%d extracted=%d kind=%s",
            self.source_stage, len(text), len(extracted), kind,
        )

        # Haiku pass — only meaningful for Java stack traces. The pytest /
        # BATS / fallback extracts aren't shaped for that prompt and would
        # be paraphrased badly.
        if (
            not is_surefire
            or not self.summarize_with_haiku
            or len(extracted) < self.haiku_min_chars
        ):
            return StageResult(name="", success=True, output=extracted)

        body = extracted
        if len(body) > self.haiku_max_input_chars:
            half = self.haiku_max_input_chars // 2
            body = (
                body[:half]
                + f"\n\n... (truncated {len(body) - self.haiku_max_input_chars} chars) ...\n\n"
                + body[-half:]
            )

        summary = await _haiku_summarize(
            body,
            model=self.haiku_model,
            app_packages=self.app_packages,
        )
        if not summary:
            log.warning(
                "[compress_test_log] Haiku failed — returning deterministic extract",
            )
            return StageResult(name="", success=True, output=extracted)

        log.debug("[compress_test_log] haiku output: %d chars", len(summary))
        return StageResult(name="", success=True, output=summary)

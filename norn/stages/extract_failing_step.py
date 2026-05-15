"""ExtractFailingStep — slice the failing step's log, then compress with Haiku.

GitHub Actions wraps every step in a job log in ``##[group]<step name>`` /
``##[endgroup]`` markers, and ``_get_failed_logs`` (in ``check_ci.py``)
prepends a ``Failed steps: <name1>, <name2>`` header per failed job. Those
two signals are enough to slice the failing step's lines out of a
multi-step job log deterministically — no LLM call required for the
extraction itself.

Two-pass design:

  1. **Python slice (extraction).** Parse the ``Failed steps:`` header,
     walk ``##[group]`` / ``##[endgroup]`` boundaries, and keep only the
     groups whose name matches a failed step. Robust because step names
     in the header and in the group markers come from the same workflow
     definition; no guessing.

  2. **Haiku pass (compression).** Hand the sliced text to Haiku with a
     general-purpose "compress a CI step log" prompt. Keeps error
     messages, stack traces, and the surrounding context; drops
     download progress bars, repeated install lines, and other noise.

Both passes fail open:

  * No ``Failed steps:`` header, no ``##[group]`` markers, or no group
    matches a failed step → the Python slice returns empty and we
    passthrough the raw upstream text. Downstream ``CompressTestLog``
    still gets a chance to apply format-aware extraction or head+tail
    truncation.
  * Haiku unavailable, errors out, or returns empty → we return the
    Python slice unchanged (or passthrough raw if even the slice was
    empty). The pipeline never stalls on a model hiccup.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from norn.models import PipelineContext, StageResult
from norn.stages.base import BaseStage
from norn.stages.compress_surefire import _extract_text

log = logging.getLogger(__name__)


# ``##[group]<step name>`` opens a step section in a GitHub Actions log.
# Raw job logs from ``download_job_logs_for_workflow_run`` prefix every
# line with an ISO-8601 timestamp (``2026-05-13T08:55:28.6109544Z ``);
# we strip an optional prefix in the regex so the parser works on both
# the raw form and the timestamp-stripped form. Some tooling emits its
# own nested ``##[group]...`` calls inside a step — handled by a depth
# counter in the caller.
_TS_PREFIX = r"(?:\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z\s?)?"
_GROUP_OPEN_RE = re.compile(rf"^{_TS_PREFIX}##\[group\](.*)$")
_GROUP_CLOSE_RE = re.compile(rf"^{_TS_PREFIX}##\[endgroup\]\s*$")

# ``_get_failed_logs`` emits "Failed steps: name1, name2" right after the
# ``## Failed job:`` header. Multiple failed jobs each get their own
# header; we union all failed-step names across them.
_FAILED_STEPS_RE = re.compile(r"^Failed steps:\s*(.+)$", re.MULTILINE)


def _parse_failed_step_names(text: str) -> set[str]:
    """Collect every step name listed in any ``Failed steps:`` header."""
    names: set[str] = set()
    for match in _FAILED_STEPS_RE.finditer(text):
        for name in match.group(1).split(","):
            name = name.strip()
            if name:
                names.add(name)
    return names


def _name_matches(failed_name: str, step_name: str) -> bool:
    """Whole-word / phrase match between an API-reported failed step name
    and a runner-emitted group label.

    Exact equality wins. Otherwise we accept either side appearing as a
    whole word (or contiguous phrase of whole words) inside the other,
    via word-boundary regex. This avoids the substring trap where a
    short name like ``"test"`` matches ``"Run docker pull ...:latest"``
    just because ``test`` happens to appear inside ``latest``.
    """
    if failed_name == step_name:
        return True
    if not failed_name or not step_name:
        return False
    pattern = re.compile(rf"\b{re.escape(failed_name)}\b", re.IGNORECASE)
    if pattern.search(step_name):
        return True
    pattern = re.compile(rf"\b{re.escape(step_name)}\b", re.IGNORECASE)
    if pattern.search(failed_name):
        return True
    return False


def _slice_failing_step(text: str) -> str:
    """Return the concatenated body of every ``##[group]`` block whose
    name matches a failed step. Empty string when slicing can't apply
    (missing header, no markers, or no matches).

    Group nesting is handled by a depth counter — we only treat the
    outermost ``##[group]`` as a step boundary, so nested groups
    (download progress bars, etc.) stay inside the step they belong to.
    """
    failed_names = _parse_failed_step_names(text)
    if not failed_names:
        return ""

    lines = text.splitlines()
    blocks: list[list[str]] = []
    current: list[str] | None = None
    depth = 0
    matched = False

    for line in lines:
        open_match = _GROUP_OPEN_RE.match(line)
        if open_match:
            if depth == 0:
                # Start of a new top-level step.
                step_name = open_match.group(1).strip()
                matched = any(
                    _name_matches(name, step_name) for name in failed_names
                )
                if matched:
                    current = [f"##[group]{step_name}"]
            else:
                # Nested group — keep it inside the current step if we're
                # capturing one.
                if current is not None:
                    current.append(line)
            depth += 1
            continue

        if _GROUP_CLOSE_RE.match(line):
            depth = max(0, depth - 1)
            if depth == 0:
                # Outermost step closed. Flush the captured body.
                if current is not None:
                    current.append("##[endgroup]")
                    blocks.append(current)
                    current = None
                    matched = False
            else:
                # Closing a nested group — keep the marker visible.
                if current is not None:
                    current.append(line)
            continue

        if current is not None:
            current.append(line)

    # Unterminated trailing group (CI was killed mid-step): flush what we have.
    if current is not None:
        blocks.append(current)

    if not blocks:
        return ""

    return "\n\n".join("\n".join(block) for block in blocks).strip()


_HAIKU_SYSTEM_PROMPT = """\
You compress the log of a single failing CI step.

The input is the verbatim output of one step from a GitHub Actions job
that failed. Your job is to keep the diagnostic signal and drop the noise.

ALWAYS KEEP
- Error messages, exception names, stack traces (entire trace, not just
  the top frame).
- Assertion failures with their expected/actual values.
- The failing command's invocation line and its non-zero exit signal.
- The 5–20 lines of context immediately surrounding each failure.
- Test names and file:line references for failed tests.
- Configuration / version banners that name the tool, runtime, or
  framework (one line each).

DROP
- Download / install progress bars and percentage updates.
- Lines that repeat the same status verbatim (poll loops, watchers).
- Per-package "installing X" lines beyond a count summary.
- Passing-test progress output that's not adjacent to a failure.
- ANSI escape sequences and other terminal control noise.

RULES
- Preserve original lines verbatim — do not paraphrase or reorder.
- Do not invent content, do not summarize stack traces, do not fold
  multiple distinct errors into one.
- When dropping a run of noisy lines, replace it with a single line of
  the form: ``... N lines omitted (<category>) ...``
- Keep the output under 400 lines unless the failure genuinely has more
  signal-dense content (multiple independent failures, large structured
  diff, etc.).

OUTPUT
- Plain text only. No Markdown fences, no preamble, no commentary.
"""


async def _haiku_compress(
    text: str,
    *,
    model: str,
    provider: str = "claude-code",
) -> str | None:
    """One-shot completion call. Returns ``None`` on any failure."""
    from norn.agents.complete import complete_text

    user_msg = (
        "Compress the following failing-step log per the rules above.\n\n"
        "----- BEGIN STEP LOG -----\n"
        f"{text}\n"
        "----- END STEP LOG -----"
    )

    return await complete_text(
        user_msg,
        provider=provider,
        model=model,
        system_prompt=_HAIKU_SYSTEM_PROMPT,
    )


class ExtractFailingStep(BaseStage):
    """Slice the failing step from a multi-step CI job log, then compress.

    Two passes:

      1. Python slices by ``##[group]<step name>`` / ``##[endgroup]``
         markers, keeping only groups whose names appear in any
         ``Failed steps:`` header. Deterministic and free.
      2. Haiku compresses the slice — keeps errors and surrounding
         context, drops install / progress noise.

    Falls back gracefully: when the Python slice can't apply, returns
    the raw upstream text. When Haiku is unavailable or fails, returns
    the Python slice (or the raw text if the slice was empty too).

    Args:
        source_stage: Upstream stage name whose output (or error) holds
            the raw job log.
        model: ``claude-agent-sdk`` shorthand. Defaults to ``"haiku"``.
        summarize_with_haiku: Disable to keep only the Python slice
            (useful for offline / no-network test runs). Defaults to
            ``True``.
        min_chars_for_haiku: Skip the Haiku call when the input to it is
            below this size — already-small slices don't benefit from
            another compression pass. Defaults to 2000.
        max_haiku_input_chars: Hard cap on what we hand to Haiku;
            head+tail truncate beyond. Defaults to 120000 (Haiku's
            context is much larger but we leave headroom for the
            system prompt and response).
        skip_on_success: When ``True`` (default), return empty output
            if the upstream stage succeeded. Green CI doesn't need
            extraction.
    """

    needs_agent = False

    def __init__(
        self,
        *,
        source_stage: str,
        model: str = "haiku",
        summarize_with_haiku: bool = True,
        min_chars_for_haiku: int = 2000,
        max_haiku_input_chars: int = 120_000,
        skip_on_success: bool = True,
    ) -> None:
        self.source_stage = source_stage
        self.model = model
        self.summarize_with_haiku = summarize_with_haiku
        self.min_chars_for_haiku = min_chars_for_haiku
        self.max_haiku_input_chars = max_haiku_input_chars
        self.skip_on_success = skip_on_success

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        prev = ctx.results.get(self.source_stage)
        if prev is None:
            return StageResult(
                name="", success=True, output="",
                error=(
                    f"extract_failing_step: source stage "
                    f"'{self.source_stage}' has no result yet"
                ),
            )

        if self.skip_on_success and prev.success:
            return StageResult(name="", success=True, output="")

        raw = _extract_text(prev)
        if not raw:
            return StageResult(name="", success=True, output="")

        # Pass 1: Python slice.
        sliced = _slice_failing_step(raw)
        if sliced:
            log.info(
                "[extract_failing_step] python slice: %d → %d chars (%.0f%%)",
                len(raw), len(sliced), 100.0 * len(sliced) / max(1, len(raw)),
            )
            current = sliced
        else:
            log.info(
                "[extract_failing_step] python slice empty (no group markers "
                "or no failed-step match) — passing raw log to Haiku",
            )
            current = raw

        if not self.summarize_with_haiku:
            return StageResult(name="", success=True, output=current)

        if len(current) < self.min_chars_for_haiku:
            log.debug(
                "[extract_failing_step] %d chars below min_chars_for_haiku=%d — skipping Haiku",
                len(current), self.min_chars_for_haiku,
            )
            return StageResult(name="", success=True, output=current)

        # Pass 2: Haiku compression.
        body = current
        if len(body) > self.max_haiku_input_chars:
            half = self.max_haiku_input_chars // 2
            body = (
                body[:half]
                + f"\n\n... ({len(body) - self.max_haiku_input_chars} chars omitted from middle) ...\n\n"
                + body[-half:]
            )
            log.info(
                "[extract_failing_step] truncated %d → %d chars before Haiku",
                len(current), len(body),
            )

        compressed = await _haiku_compress(body, model=self.model, provider=ctx.agent_provider)
        if not compressed:
            log.warning(
                "[extract_failing_step] Haiku returned no content — returning python slice",
            )
            return StageResult(name="", success=True, output=current)

        log.info(
            "[extract_failing_step] haiku compress: %d → %d chars (%.0f%%)",
            len(current), len(compressed),
            100.0 * len(compressed) / max(1, len(current)),
        )
        return StageResult(name="", success=True, output=compressed)

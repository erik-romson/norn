"""CompressSurefireLog — generic Maven/Surefire log compression stage.

Pulls text from a previously-run stage's ``StageResult`` (output or error),
runs the Surefire-aware extractor, then optionally hands the result to
Haiku for stack-trace compression. Same pipeline as ``CheckCISurefire``,
just decoupled from the GitHub-fetch side so it can run on:

  - ``RunCommand`` output (e.g. local ``mvn verify``)
  - Any other stage that emits a Maven/Surefire log as its
    ``StageResult.output`` (string) or ``StageResult.output["stdout"]``
    / ``["stderr"]`` / ``["logs"]`` (dict).

Why a separate stage instead of inlining compression into every producer:

  - Single responsibility — ``RunCommand`` doesn't need to know about
    Maven/Surefire formats; ``CheckCI`` doesn't need to know how to call
    Haiku.
  - Reusable across any text-producing stage (mvn, gradle, pytest, raw
    scripts), not just CI.
  - Testable in isolation.
  - Trade-off: ``CheckCISurefire`` keeps its own bundled compression
    because it's a tight one-shot path. Any other stage should chain a
    ``CompressSurefireLog`` after it.

Typical wiring inside a do-while loop::

    pipeline.loop(
        "local tests",
        max_retries=3,
        on_exhaust=fail,
        stages=[
            Stage("compress local tests",
                  CompressSurefireLog(
                      source_stage="run local tests",
                      app_packages=["com.acme.*"],
                  ),
                  when=stage_failed("run local tests")),
            Stage("fix local",
                  Generate(prompt="... {compress local tests.output} ..."),
                  when=stage_failed("run local tests")),
            Stage("run local tests", RunCommand(cmd="mvn verify ...")),
        ],
    )

The ``when=stage_failed("run local tests")`` guard means the compression
only runs after a failed test execution — no point compressing a clean
green log.
"""
from __future__ import annotations

import logging
from typing import Any

from norn.models import PipelineContext, StageResult
from norn.stages.base import BaseStage
from norn.stages.check_ci_surefire import (
    _haiku_summarize,
    extract_surefire_failures,
)

log = logging.getLogger(__name__)


def _extract_text(prev: StageResult) -> str:
    """Pull the most useful text out of a prior stage's result.

    Prefers structured fields when the output is a dict, falls back to
    ``error`` if the stage failed without an output, and returns ``""``
    when there's nothing to compress.
    """
    out = prev.output
    if isinstance(out, str):
        text = out
    elif isinstance(out, dict):
        # Order matters — for RunCommand we want stdout+stderr glued
        # together; for CheckCI-style dicts the failure text lives under
        # "logs".
        if "logs" in out and isinstance(out["logs"], str) and out["logs"]:
            text = out["logs"]
        else:
            stdout = out.get("stdout") or ""
            stderr = out.get("stderr") or ""
            text = stdout
            if stderr:
                text = f"{text}\n{stderr}" if text else stderr
    else:
        text = ""

    if not text and prev.error:
        text = prev.error
    return text


class CompressSurefireLog(BaseStage):
    """Compress a Maven/Surefire log emitted by a previous stage.

    Args:
        source_stage: Name of the upstream stage whose
            ``StageResult.output`` (or ``error``) holds the raw log.
        app_packages: Application package globs forwarded to the Haiku
            prompt as ``APP_PACKAGES``. ``None`` lets the model infer.
        summarize_with_haiku: Disable to get only the deterministic
            extract (no LLM call). Defaults to ``True``.
        haiku_model: Model shorthand. Defaults to ``"haiku"``.
        haiku_min_chars: Skip Haiku when the deterministic extract is
            below this. Defaults to 500.
        haiku_max_input_chars: Hard cap on input handed to Haiku — head
            and tail are kept, middle is truncated. Defaults to 30000.
        passthrough_on_empty_extract: When the surefire extractor finds
            no matches (the log isn't a Maven/Surefire log at all),
            return the upstream text verbatim. Set to ``False`` to
            return an empty string instead. Defaults to ``True``.
    """

    needs_agent = False

    def __init__(
        self,
        *,
        source_stage: str,
        app_packages: list[str] | str | None = None,
        summarize_with_haiku: bool = True,
        haiku_model: str = "haiku",
        haiku_min_chars: int = 500,
        haiku_max_input_chars: int = 30000,
        passthrough_on_empty_extract: bool = True,
    ) -> None:
        self.source_stage = source_stage
        self.app_packages = app_packages
        self.summarize_with_haiku = summarize_with_haiku
        self.haiku_model = haiku_model
        self.haiku_min_chars = haiku_min_chars
        self.haiku_max_input_chars = haiku_max_input_chars
        self.passthrough_on_empty_extract = passthrough_on_empty_extract

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        prev = ctx.results.get(self.source_stage)
        if prev is None:
            return StageResult(
                name="", success=True,
                output="",
                error=f"compress_surefire: source stage '{self.source_stage}' has no result yet",
            )

        text = _extract_text(prev)
        if not text:
            return StageResult(name="", success=True, output="")

        extracted = extract_surefire_failures(text)
        if not extracted:
            log.debug(
                "[compress_surefire] no surefire markers in '%s' output (%d chars)",
                self.source_stage, len(text),
            )
            return StageResult(
                name="", success=True,
                output=text if self.passthrough_on_empty_extract else "",
            )

        log.debug(
            "[compress_surefire] surefire extract: %d chars (from %d)",
            len(extracted), len(text),
        )

        if not self.summarize_with_haiku or len(extracted) < self.haiku_min_chars:
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
            log.warning("[compress_surefire] Haiku failed — returning deterministic extract")
            return StageResult(name="", success=True, output=extracted)

        log.debug("[compress_surefire] haiku output: %d chars", len(summary))
        return StageResult(name="", success=True, output=summary)

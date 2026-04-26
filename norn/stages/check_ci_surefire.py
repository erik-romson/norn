"""CheckCISurefire — Maven/Surefire-aware CI log extractor.

The default ``CheckCI`` summarizer picks the earliest failure anchor and
takes everything from there to the end of the cleaned log. For
``mvn verify`` runs that produce hundreds of test methods (a mix of passes
and per-test ``<<< ERROR!`` blocks with full Hibernate / Spring stack
traces) that approach drags in passing classes, repeated runner banners,
and unrelated INFO output.

This stage is targeted at projects whose CI prints output like::

    [ERROR] Tests run: 12, Failures: 0, Errors: 5, Skipped: 1, Time elapsed: 19.23 s <<< FAILURE! -- in com.foo.SomeIT
    [ERROR] com.foo.SomeIT.testX -- Time elapsed: 1.422 s <<< ERROR!
    javax.persistence.PersistenceException: ...
        at org.hibernate.internal.ExceptionConverterImpl.convert(ExceptionConverterImpl.java:154)
        ...
    [INFO] Tests run: 13, Failures: 0, Errors: 0, Skipped: 1, Time elapsed: 48.83 s -- in com.foo.PassingIT

Pipeline:

  1. **Strip noise** — ISO-8601 timestamps, GitHub annotation prefixes
     (``Error:`` / ``Warning:``), and the Maven thread+mojo prefix
     (``[edStreamConsumer] [ERROR] IntegrationTestMojo - ``).
  2. **Identify failed classes** — class summary lines where
     ``Failures + Errors > 0``. Passing classes (``[INFO] Tests run: 13,
     Failures: 0, Errors: 0``) are dropped entirely.
  3. **Extract per-test failure blocks** — for each ``Class.method --
     Time elapsed ... <<< ERROR!`` header belonging to a failed class,
     keep the header + the trailing exception message + stack frames
     (lines starting with ``at ``, ``Caused by:``, ``... N more``,
     ``Suppressed:``).
  4. **Tail summary** — keep the final ``[ERROR] Errors:`` /
     ``[ERROR] Failures:`` block Maven prints right before
     ``BUILD FAILURE``.
  5. **Optional Haiku compression** — when the extracted text is still
     large, hand it to Haiku with instructions to preserve every distinct
     exception type/message and the first few stack frames per unique
     exception. Skipped when ``summarize_with_haiku=False`` or when
     ``claude-agent-sdk`` isn't importable.

Returns the same ``StageResult.output`` dict shape as ``CheckCI``
(``run_id``, ``name``, ``status``, ``conclusion``, ``url``, ``logs``),
so it's a drop-in replacement inside ``CheckCIWithLogs``.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from norn.models import PipelineContext, StageResult
from norn.stages.base import BaseStage
from norn.stages.check_ci import (
    _TIMESTAMP_RE,
    _create_client,
    _current_branch,
    _detect_repo,
    _get_failed_logs,
    _get_latest_run,
    _git_head_sha,
    _is_ancestor,
    _resolve_token,
    _summarize_log,
)

log = logging.getLogger(__name__)


# GitHub Actions injects ``Error:`` / ``Warning:`` / ``Notice:`` prefixes
# when a step uses ``::error::`` / ``::warning::`` workflow commands.
_GH_ANNOT_RE = re.compile(r"^(?:Error|Warning|Notice):\s+")

# Maven plugin output line, e.g.:
#   [edStreamConsumer] [ERROR] IntegrationTestMojo - Tests run: ...
# We strip the thread+level+mojo prefix and keep the "Tests run: ..." part.
# The leading ``[`` is optional because GitHub's ``::error::`` annotation
# channel sometimes consumes it (the user-visible form becomes
# ``Error: edStreamConsumer] [ERROR] IntegrationTestMojo - ...``).
_MVN_THREAD_RE = re.compile(
    r"^\[?[^\[\]\s]+\]\s+\[(?:INFO|ERROR|WARNING|WARN|DEBUG)\]\s+\S+\s+-\s+"
)

# Class-level summary line. Captures (failures, errors, fully-qualified class).
_CLASS_SUMMARY_RE = re.compile(
    r"Tests run:\s*\d+,\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),"
    r"\s*Skipped:\s*\d+,\s*Time elapsed:\s*[\d.]+\s*s.*?--\s*in\s+(\S+)"
)

# Per-test failure detail header. Surefire only emits this for failing tests.
_TEST_DETAIL_RE = re.compile(
    r"^\s*(\S+?)\s+--\s+Time elapsed:\s*[\d.]+\s*s\s+<<<\s+(?:ERROR|FAILURE)!\s*$"
)

# Lines that look like Java stack frames or exception continuation.
_STACK_RE = re.compile(
    r"^(?:\s*at\s+\S|\s*Caused by:|\s*\.\.\.\s*\d+\s+more|\s*Suppressed:)"
)


def _strip_prefixes(line: str) -> str:
    line = _TIMESTAMP_RE.sub("", line)
    line = _GH_ANNOT_RE.sub("", line)
    line = _MVN_THREAD_RE.sub("", line)
    return line


def _dedupe_test_blocks(text: str) -> str:
    """Collapse near-duplicate per-test failure blocks.

    Maven/Surefire prints one full block per failing test. When 5 tests in
    one class fail with the same exception → identical stack → 5x repeat
    that bloats the extract from ~10kB to ~1MB. Group blocks by their
    "signature" (top exception line + Caused-by chain class names) and keep
    only the first; replace later duplicates with a one-line marker.
    """
    lines = text.splitlines()
    blocks: list[list[str]] = []
    current: list[str] = []
    for ln in lines:
        if _TEST_DETAIL_RE.match(ln.strip()) and current:
            blocks.append(current)
            current = []
        current.append(ln)
    if current:
        blocks.append(current)

    if len(blocks) <= 1:
        return text

    seen: dict[str, list[str]] = {}
    extra: dict[str, list[str]] = {}
    order: list[str] = []
    for blk in blocks:
        sig_parts: list[str] = []
        method_name = ""
        for ln in blk:
            s = ln.strip()
            m = _TEST_DETAIL_RE.match(s)
            if m:
                method_name = m.group(1)
                continue
            if s.startswith("Caused by:") or (
                ":" in s and not s.startswith(("at ", "...", "[", "Tests run:"))
            ):
                # Strip the variable bits (line numbers, row IDs, timestamps)
                # so equivalent failures hash the same.
                norm = re.sub(r"\d+", "#", s)
                sig_parts.append(norm[:200])
                if len(sig_parts) >= 3:
                    break
        sig = "|".join(sig_parts)
        if sig and sig in seen:
            extra.setdefault(sig, []).append(method_name)
        else:
            seen[sig] = blk
            order.append(sig)

    out: list[str] = []
    for sig in order:
        out.extend(seen[sig])
        if sig in extra:
            others = extra[sig]
            out.append("")
            out.append(
                f"... same root cause for {len(others)} additional test(s): "
                + ", ".join(others)
            )
            out.append("")
    return "\n".join(out)


def extract_surefire_failures(raw: str) -> str:
    """Pull the failed-class headers, per-test error blocks, and tail
    summary out of a Maven/Surefire job log. Empty string when nothing
    matched (caller can fall back to ``_summarize_log``)."""
    lines = [_strip_prefixes(ln) for ln in raw.splitlines()]
    n = len(lines)

    # Pass 1: which classes actually failed?
    failed_classes: set[str] = set()
    for ln in lines:
        m = _CLASS_SUMMARY_RE.search(ln)
        if m and (int(m.group(1)) + int(m.group(2))) > 0:
            failed_classes.add(m.group(3))

    if not failed_classes:
        return ""

    out: list[str] = []
    i = 0
    while i < n:
        ln = lines[i]
        stripped = ln.strip()

        cls_match = _CLASS_SUMMARY_RE.search(ln)
        if cls_match and cls_match.group(3) in failed_classes:
            out.append(ln.rstrip())
            i += 1
            continue

        det_match = _TEST_DETAIL_RE.match(stripped)
        if det_match:
            method_fqn = det_match.group(1)
            cls_name = method_fqn.rsplit(".", 1)[0] if "." in method_fqn else method_fqn
            if cls_name in failed_classes:
                out.append(ln.rstrip())
                i += 1
                # Capture the exception message + stack frames that follow.
                # Boundary: another test/class marker, or a non-stack content
                # line at column 0 that isn't an exception type.
                while i < n:
                    nxt = lines[i]
                    nxt_stripped = nxt.strip()

                    if not nxt_stripped:
                        # Allow a single blank line between exception and
                        # stack continuation, but stop if the next non-empty
                        # line is a new section.
                        j = i + 1
                        while j < n and not lines[j].strip():
                            j += 1
                        if j >= n:
                            break
                        peek = lines[j].strip()
                        if (
                            _TEST_DETAIL_RE.match(peek)
                            or _CLASS_SUMMARY_RE.search(lines[j])
                            or peek.startswith("[INFO]")
                            or peek.startswith("[ERROR] BUILD")
                            or peek.startswith("Results :")
                            or peek.startswith("BUILD ")
                        ):
                            out.append("")
                            i = j
                            break
                        out.append("")
                        i = j
                        continue

                    if _TEST_DETAIL_RE.match(nxt_stripped):
                        break
                    if _CLASS_SUMMARY_RE.search(nxt):
                        break

                    # Keep stack frames + exception messages. Exception
                    # messages typically start at column 0 with a Java
                    # FQCN ending in ":" — e.g. ``javax.persistence.X: msg``.
                    # Anything that survived the prefix strip and doesn't
                    # match a section boundary is in-context here.
                    out.append(nxt.rstrip())
                    i += 1
                continue

        i += 1

    # Tail block: Maven prints "[ERROR] Errors:" / "[ERROR] Failures:" with
    # a flat list of failing tests right before "BUILD FAILURE".
    for j, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("[ERROR] Errors:") or s.startswith("[ERROR] Failures:"):
            out.append("")
            out.append("--- Maven failure summary ---")
            k = j
            while k < n:
                t = lines[k].rstrip()
                out.append(t)
                ts = t.strip()
                if (
                    ts.startswith("[INFO] BUILD FAILURE")
                    or ts.startswith("[ERROR] BUILD")
                    or ts == "BUILD FAILURE"
                ):
                    break
                k += 1
                # Safety bound — don't run away if BUILD FAILURE never appears.
                if k - j > 200:
                    break
            break

    return _dedupe_test_blocks("\n".join(out).strip())


_HAIKU_SYSTEM_TEMPLATE = """\
You compress Java stacktraces. Preserve diagnostic value, remove plumbing.

INPUTS
- Application packages (frames matching these are always kept):
    {APP_PACKAGES}
  Example: com.acme.*, com.acme.shared.*
  If the user did not provide this, infer it from the trace: the package(s)
  appearing in the test class or top-of-stack non-framework frames. State
  your inference in one line above the output.

ALWAYS KEEP
1. The top exception line (class + full message), unmodified.
2. Every "Caused by:" line and its full message, unmodified.
3. Any continuation lines that belong to an exception message — lines that
   do not start with "at " or "...". These often carry the actual root cause
   (DB error details, "Failing row contains (...)", HTTP response bodies,
   validation messages, "Detail:", "Hint:", nested JSON). Keep them verbatim
   even if very long.
4. All frames whose fully-qualified class matches an application package.
5. The single frame immediately above each application-frame block if it
   identifies the concrete failure site — i.e. the call that actually threw.
   Heuristics for "concrete failure site":
     - JDBC / database driver execute* methods
     - HTTP / RPC client send / execute / invoke methods
     - Serialization / deserialization read* / write* methods
     - File I/O read / write / open methods
     - Reflection invoke ONLY when it is the throwing frame, not plumbing
   When in doubt, keep it.

ALWAYS DROP (collapse into a single "... N frames omitted (CATEGORY)" line)
- Test runners: org.junit.*, org.testng.*, org.spockframework.*
- Build / fork plumbing: org.apache.maven.surefire.*, org.gradle.*,
  worker.org.gradle.*
- Mocking: org.mockito.*, net.bytebuddy.*, and any class containing
  $MockitoMock$, $$EnhancerBy, $$FastClassBy, $auxiliary$, $$Lambda$
- Reflection plumbing: jdk.internal.reflect.*, sun.reflect.*,
  java.lang.reflect.Method, java.lang.reflect.Constructor
- Proxy plumbing: com.sun.proxy.*, jdk.proxy*.*
- Framework internals NOT at the failure site:
  org.hibernate.* (except the throwing JDBC bridge),
  org.springframework.* (except the throwing client/template),
  jakarta.*, javax.persistence.*, javax.servlet.*,
  io.netty.*, reactor.core.*, kotlinx.coroutines.*
- Tail markers: lines of the form "... N more"

RULES
- Preserve original frame format: "    at FQCN.method(File.java:line)".
- Preserve indentation and the order of the Caused-by chain.
- Never reorder, never paraphrase messages, never truncate the "Detail:" /
  "Failing row contains" / response-body continuation lines.
- One omission line per consecutive run of dropped frames. Format:
    ... N frames omitted (category1 / category2)
  Use the categories that actually appeared in that run.
- If keeping the rules would still produce >40 lines, keep going — a long
  but signal-dense trace is better than a short one missing the cause.

OUTPUT
- Only the compressed stacktrace. No preamble, no commentary, no fences.
- If application packages had to be inferred, prepend exactly one line:
    # inferred app packages: <list>
"""


def _format_app_packages(app_packages: list[str] | str | None) -> str:
    """Render APP_PACKAGES for the prompt; ``(not provided — infer from trace)``
    when caller didn't supply any."""
    if not app_packages:
        return "(not provided — infer from trace)"
    if isinstance(app_packages, str):
        return app_packages
    return ", ".join(app_packages)


async def _haiku_summarize(
    text: str,
    *,
    model: str = "haiku",
    app_packages: list[str] | str | None = None,
) -> str | None:
    """Run a one-shot Haiku query to compress the extracted failure log.

    Returns ``None`` (caller should fall back to the un-compressed text) if
    the SDK isn't available or the query errors out.

    Args:
        text: The extracted Surefire failure block.
        model: ``claude-agent-sdk`` model shorthand. Defaults to ``"haiku"``.
        app_packages: Application package globs (e.g.
            ``["com.e4marine.*"]``). Frames matching these are always kept by
            the prompt. ``None`` tells the model to infer from the trace.
    """
    try:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            query,
        )
    except ImportError:
        log.warning("claude-agent-sdk not installed — skipping Haiku summarization")
        return None

    from norn.stages.generate import MODEL_MAP

    system = _HAIKU_SYSTEM_TEMPLATE.format(
        APP_PACKAGES=_format_app_packages(app_packages),
    )
    # Compression instructions go in the SYSTEM prompt so they aren't
    # diluted by claude-agent-sdk's default Claude Code system prompt.
    # The user message carries only the stacktrace, with a one-line
    # restatement of the task.
    user_msg = (
        "Compress the following Java stacktrace per the rules above.\n\n"
        "----- BEGIN STACKTRACE -----\n"
        f"{text}\n"
        "----- END STACKTRACE -----"
    )

    chunks: list[str] = []
    stderr_lines: list[str] = []
    try:
        async for msg in query(
            prompt=user_msg,
            options=ClaudeAgentOptions(
                model=MODEL_MAP.get(model, model),
                system_prompt=system,
                allowed_tools=[],
                max_turns=1,
                stderr=lambda line: stderr_lines.append(line),
            ),
        ):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if hasattr(block, "text"):
                        chunks.append(block.text)
    except Exception as e:
        log.warning("Haiku summarization failed: %s", e)
        if stderr_lines:
            log.warning("SDK stderr:\n%s", "\n".join(stderr_lines[-30:]))
        return None

    summary = "".join(chunks).strip()
    return summary or None


class CheckCISurefire(BaseStage):
    """Surefire-aware variant of ``CheckCI``.

    Same constructor knobs as ``CheckCI`` for the GitHub side, plus:

    Args:
        summarize_with_haiku: When ``True`` (default) the extracted failure
            block is compressed further by Haiku. Disable for offline use.
        haiku_model: Model shorthand passed to ``claude-agent-sdk``.
            Defaults to ``"haiku"``.
        haiku_min_chars: Minimum length of the extracted text before Haiku
            is invoked. Below this, the deterministic extract is already
            small enough to use directly. Defaults to 4000.
        haiku_max_input_chars: Hard cap on what we send to Haiku. Anything
            longer is truncated head+tail. Defaults to 80000.
        app_packages: Application package globs (e.g.
            ``["com.e4marine.*", "com.e4marine.shared.*"]``) handed to the
            Haiku prompt as ``APP_PACKAGES``. Frames matching these are
            always preserved. ``None`` tells the model to infer them from
            the trace and prepend ``# inferred app packages: <list>``.
    """

    needs_agent = False

    def __init__(
        self,
        *,
        repo: str | None = None,
        branch: str | None = None,
        workflow: str | None = None,
        poll: bool = False,
        poll_interval: int = 30,
        timeout_minutes: int = 30,
        summarize_with_haiku: bool = True,
        haiku_model: str = "haiku",
        haiku_min_chars: int = 500,
        haiku_max_input_chars: int = 30000,
        app_packages: list[str] | str | None = None,
    ) -> None:
        self.repo = repo
        self.branch = branch
        self.workflow = workflow
        self.poll = poll
        self.poll_interval = poll_interval
        self.timeout_minutes = timeout_minutes
        self.summarize_with_haiku = summarize_with_haiku
        self.haiku_model = haiku_model
        self.haiku_min_chars = haiku_min_chars
        self.haiku_max_input_chars = haiku_max_input_chars
        self.app_packages = app_packages

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        try:
            from githubkit import GitHub  # noqa: F401
        except ImportError:
            return StageResult(
                name="", success=False,
                error="githubkit is not installed. Install with: pip install 'norn[github]'",
            )

        token = await _resolve_token()
        if not token:
            return StageResult(
                name="", success=False,
                error="No GitHub token found. Set GITHUB_TOKEN env var or run 'gh auth login'.",
            )

        repo_slug = self.repo or await _detect_repo()
        branch = self.branch or await _current_branch()
        if not repo_slug:
            return StageResult(
                name="", success=False,
                error="Could not detect GitHub repo. Set repo= explicitly or run inside a git repo with a GitHub remote.",
            )

        owner, repo_name = repo_slug.split("/", 1)
        gh = _create_client(token)
        deadline = asyncio.get_event_loop().time() + self.timeout_minutes * 60
        local_head = await _git_head_sha()

        while True:
            run_info = await _get_latest_run(gh, owner, repo_name, branch, self.workflow)
            if run_info is None:
                return StageResult(
                    name="", success=False,
                    error=f"No workflow runs found for {repo_slug} on branch {branch}"
                    + (f" (workflow: {self.workflow})" if self.workflow else ""),
                )

            status = run_info["status"]
            conclusion = run_info["conclusion"]
            run_sha = run_info.get("head_sha") or ""

            if (
                self.poll and status == "completed" and local_head and run_sha
                and await _is_ancestor(run_sha, local_head)
            ):
                if asyncio.get_event_loop().time() >= deadline:
                    return StageResult(
                        name="", success=False,
                        error=(
                            f"CI timed out after {self.timeout_minutes} minutes "
                            f"waiting for a workflow run on {local_head[:8]}; "
                            f"latest indexed run is for ancestor {run_sha[:8]}."
                        ),
                    )
                await asyncio.sleep(self.poll_interval)
                continue

            if status == "completed":
                logs = ""
                if conclusion != "success":
                    raw = await _get_failed_logs(gh, owner, repo_name, run_info["run_id"])
                    logs = await self._build_logs(raw)

                output = {
                    "run_id": run_info["run_id"],
                    "name": run_info["name"],
                    "status": status,
                    "conclusion": conclusion,
                    "url": run_info["url"],
                    "logs": logs,
                }
                success = conclusion == "success"
                error = None if success else f"CI {conclusion}: {run_info['name']} ({run_info['url']})"
                return StageResult(name="", success=success, output=output, error=error)

            if not self.poll:
                return StageResult(
                    name="", success=False,
                    error=f"CI run is still {status}: {run_info['name']} ({run_info['url']})",
                    output={
                        "run_id": run_info["run_id"],
                        "name": run_info["name"],
                        "status": status,
                        "conclusion": None,
                        "url": run_info["url"],
                        "logs": "",
                    },
                )

            if asyncio.get_event_loop().time() >= deadline:
                return StageResult(
                    name="", success=False,
                    error=f"CI timed out after {self.timeout_minutes} minutes. Last status: {status}",
                )

            log.debug("CI status: %s — waiting %ds", status, self.poll_interval)
            await asyncio.sleep(self.poll_interval)

    async def _build_logs(self, raw: str) -> str:
        """Extract → optionally compress with Haiku → return final string."""
        if not raw:
            return ""

        extracted = extract_surefire_failures(raw)
        if not extracted:
            # No surefire markers at all — fall back to the generic anchor
            # extractor so the caller still gets a usable failure summary.
            return _summarize_log(raw, max_lines=400, lead_context=30)

        if not self.summarize_with_haiku or len(extracted) < self.haiku_min_chars:
            return extracted

        # Truncate head+tail to fit Haiku's input budget without dropping
        # the final summary block (which is at the tail).
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
            return extracted
        return summary

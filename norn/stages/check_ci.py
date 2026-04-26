from __future__ import annotations

import asyncio
import io
import logging
import re
import zipfile
from typing import Any

# githubkit uses httpx, which logs every HTTP request at INFO. That noise
# drowns out the actual stage output, so silence it to WARNING.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# Matches the ISO-8601 timestamp GitHub Actions prepends to every log line,
# e.g. "2026-04-13T15:38:30.2534247Z "
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z\s?")

# Pure GitHub Actions markers we always drop (content is noise or empty).
# ``##[command]`` and ``##[debug]`` lines contain shell commands or debug
# probes that drown out real output; ``##[endgroup]`` is an empty close marker.
_DROP_MARKER_RE = re.compile(r"^##\[(debug|command|endgroup)\]")

# ``##[group]`` lines often PREFIX real content (e.g. Flutter emits
# ``##[group]❌ path/test.dart: description (failed)``). We strip the
# marker prefix and keep whatever comes after — the alternative (drop
# the whole line) throws away the only failure signal some test runners
# emit.
_GROUP_PREFIX_RE = re.compile(r"^##\[group\]")

# Lines beginning with these markers are treated as strong error signals.
# NOTE: warnings are intentionally excluded — they are usually deprecation
# notices that drown out the real failure.
_ERROR_MARKERS = ("##[error]",)

# Substrings that flag likely error lines when no ``##[error]`` markers exist.
_ERROR_KEYWORDS = (
    "error:", "exception", "traceback", "failed", "failure",
    "fatal:", "panic:", "build failed", "test failed", "assertionerror",
)

# Lines matching these patterns are post-run housekeeping / cleanup noise.
# Everything from the FIRST such line onward is dropped from the cleaned log.
#
# Only markers that are GUARANTEED to appear exclusively at the very end of
# a job's log go here. Strings like "Removing SSH command configuration" or
# "Removing HTTP extra header" look cleanup-ish but are actually emitted by
# actions/checkout during its own post-hook AND during pre-build setup, so
# they appear within the first ~100 lines and would truncate the real
# build output.
_CLEANUP_MARKERS = (
    "Post job cleanup.",
    # "Cleaning up orphan processes" is the very last line the Actions
    # runner prints before the job exits — guaranteed to be post-error
    # noise. Some self-hosted / composite actions skip the
    # "Post job cleanup." banner entirely (it only fires when an action
    # defines a ``post:`` hook), so relying on "Post job cleanup." alone
    # leaves the tail-fallback pass picking up git config teardown,
    # Node.js 20 deprecation warnings, etc.
    "Cleaning up orphan processes",
)

# Substrings/prefixes of noisy lines we drop during the cleaning pass.
_NOISE_PREFIXES = (
    "[command]/usr/bin/git",
    "Temporarily overriding HOME=",
    "Adding repository directory to the temporary git global config",
    "git version ",
)

# Tool-specific "this is where the build failed" anchors. The summarizer
# scans for the EARLIEST match in the log and takes everything from that
# line (plus a few lines of lead context) to the end of the cleaned log.
# Patterns are ordered roughly by how structurally distinctive they are —
# the first match wins per line, but across lines the earliest match wins.
#
# When adding a new anchor, prefer a highly specific literal that only
# the failing tool emits, to avoid false positives in prose log lines.
_FAILURE_ANCHORS: list[tuple[str, re.Pattern[str]]] = [
    # --- JVM test runners (surefire / failsafe) ---
    # Match the per-test "<<< FAILURE!" / "<<< ERROR!" markers BEFORE the
    # generic "[INFO] BUILD FAILURE" anchor below. Maven's BUILD FAILURE
    # line appears AFTER the surefire output, so anchoring there means
    # the extracted block skips every test detail (stack traces, MockMvc
    # request/response bodies) and only captures Maven's "Failed to
    # execute goal" boilerplate. Surefire's "<<< FAILURE!" markers
    # appear at the end of each failing test's detail block, so anchoring
    # there with a generous lead_context captures the actual failure
    # context.
    ("surefire_failure", re.compile(r"<<< FAILURE!\s*$")),
    ("surefire_error",   re.compile(r"<<< ERROR!\s*$")),
    # --- JVM build tools ---
    ("maven",      re.compile(r"^\[INFO\] BUILD FAILURE\b")),
    ("gradle",     re.compile(r"^FAILURE: Build failed with an exception\.?")),
    ("ant",        re.compile(r"^BUILD FAILED$")),
    ("sbt",        re.compile(r"^\[error\] \(.*\) Compilation failed")),
    # --- Rust ---
    ("cargo_err",     re.compile(r"^error(?:\[E\d+\])?: ")),
    ("cargo_compile", re.compile(r"^error: could not compile")),
    ("cargo_abort",   re.compile(r"^error: aborting due to \d+ previous error")),
    # --- Go ---
    ("go_test_fail", re.compile(r"^--- FAIL: ")),
    ("go_pkg_fail",  re.compile(r"^FAIL\s+\S+\s+[\d.]+s$")),
    # --- Python test runners ---
    ("pytest_failures", re.compile(r"^=+ FAILURES =+")),
    ("pytest_errors",   re.compile(r"^=+ ERRORS =+")),
    ("pytest_summary",  re.compile(r"^=+ short test summary info =+")),
    ("unittest",        re.compile(r"^(FAIL|ERROR): \w+ \(")),
    # --- JavaScript / TypeScript ---
    ("jest_fail",    re.compile(r"^FAIL\s+\S+\.(test|spec)\.[jt]sx?$")),
    ("jest_suites",  re.compile(r"^Test Suites:.*\d+ failed")),
    ("npm_err",      re.compile(r"^npm ERR! ")),
    ("yarn_err",     re.compile(r"^error Command failed with exit code")),
    ("tsc_diag",     re.compile(r"\berror TS\d+: ")),
    ("eslint_prob",  re.compile(r"^\s*✖ \d+ problems?\b")),
    # --- .NET / MSBuild ---
    ("msbuild",      re.compile(r"^Build FAILED\.$")),
    ("msbuild_err",  re.compile(r"\berror (CS|MSB)\d+: ")),
    # --- Native / C++ ---
    ("cmake",        re.compile(r"^CMake Error")),
    ("bazel",        re.compile(r"^FAILED: Build did NOT complete successfully")),
    ("xcodebuild",   re.compile(r"^\*\* BUILD FAILED \*\*")),
    ("gcc_clang",    re.compile(r"^[^:\s]+:\d+:\d+:\s+(?:fatal )?error: ")),
    # --- Python linters ---
    ("ruff",         re.compile(r"^\S+:\d+:\d+: [A-Z]\d+ ")),
    ("flake8",       re.compile(r"^\S+:\d+:\d+: [EWF]\d+ ")),
    # --- Docker / buildx / BuildKit ---
    # Docker buildx wraps the underlying BuildKit error in its own banner.
    # The banner line ("Error: buildx failed with: ...") is already the
    # summary the user wants to see, so anchor on it directly. We also
    # match the BuildKit-level ``ERROR: failed to solve:`` line, which
    # appears when buildx re-emits the BuildKit error without its own
    # wrapper (e.g. inside a GitHub Actions step that shells out to
    # ``docker buildx build`` without capturing stderr).
    # GitHub Actions wraps errors emitted by docker/build-push-action in
    # a ``##[error]`` tag, so the actual line the summarizer sees is
    # ``##[error]Error: buildx failed with: ...`` — the anchor has to
    # allow that optional prefix.
    ("buildx",          re.compile(r"^(?:##\[error\])?Error: buildx failed with: ")),
    ("buildkit_solve",  re.compile(r"^(?:##\[error\])?ERROR: failed to solve: ")),
    ("docker_build",    re.compile(r"^(?:##\[error\])?ERROR: failed to build: ")),
    # --- Flutter / Dart ---
    # Flutter's JSON test reporter formats each failed test as
    # ``##[group]❌ <path>: <description> (failed)`` — after the
    # _GROUP_PREFIX_RE strip, the leading ``❌ `` is our anchor.
    ("flutter_test",    re.compile(r"^❌ \S+\.dart:")),
    ("flutter_summary", re.compile(r"^##\[error\]\d+ tests? passed, \d+ failed")),
    ("flutter_exc",     re.compile(r"EXCEPTION CAUGHT BY FLUTTER")),
    # Dart analyzer errors (``error - path:line:col - msg - ruleid``)
    ("dart_analyzer",   re.compile(r"^\s*error\s+-\s+\S+:\d+:\d+\s+-")),
]

from norn.models import PipelineContext, StageResult
from norn.stages.base import BaseStage

log = logging.getLogger(__name__)


async def _resolve_token() -> str | None:
    """Resolve a GitHub token using a layered strategy.

    Lookup order:
    1. ``GH_TOKEN`` env var (gh CLI's native name).
    2. ``GITHUB_TOKEN`` env var (Actions runners, fine-grained PATs).
    3. ``gh auth token`` — reuse existing gh CLI auth.

    Returns ``None`` if no token can be found.
    """
    import os

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token

    proc = await asyncio.create_subprocess_exec(
        "gh", "auth", "token",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode == 0:
        val = stdout.decode().strip()
        if val:
            return val

    return None


async def _current_branch() -> str:
    """Get the current git branch name."""
    proc = await asyncio.create_subprocess_exec(
        "git", "branch", "--show-current",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    return stdout.decode().strip()


async def _detect_repo() -> str | None:
    """Detect owner/repo from the git remote origin URL."""
    proc = await asyncio.create_subprocess_exec(
        "git", "remote", "get-url", "origin",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    url = stdout.decode().strip()
    # Handle SSH: git@github.com:owner/repo.git
    if ":" in url and url.startswith("git@"):
        path = url.split(":", 1)[1]
    # Handle HTTPS: https://github.com/owner/repo.git
    elif "github.com/" in url:
        path = url.split("github.com/", 1)[1]
    else:
        return None
    return path.removesuffix(".git")


class CheckCI(BaseStage):
    """Check the latest GitHub Actions workflow run for a branch and return
    a compact, language-agnostic extract of the failure region.

    Inspects the most recent workflow run on a given branch (optionally
    filtered by workflow), and returns structured information about its
    status. When the run has failed, fetches each failed job's raw log
    from the GitHub Actions API, then applies a multi-layered extractor
    (see ``_summarize_log``) to pull out just the part of the log that
    actually explains the failure — usable directly in a follow-up LLM
    prompt without burning thousands of tokens on irrelevant output.

    This is a non-agent stage — pure Python, no LLM invocation, no cost.

    Implementation
    --------------
    Uses ``githubkit`` (async, httpx-based) to call the GitHub REST API:

      - ``GET /repos/{owner}/{repo}/actions/runs``
        (or ``/actions/workflows/{id}/runs`` when ``workflow`` is set)
      - ``GET /repos/{owner}/{repo}/actions/runs/{run_id}/jobs``
      - ``GET /repos/{owner}/{repo}/actions/jobs/{job_id}/logs``
      - ``GET /repos/{owner}/{repo}/actions/runs/{run_id}/logs``
        (fallback zip, extracted with the stdlib ``zipfile`` module)

    At module import time, ``httpx`` and ``httpcore`` loggers are set to
    ``WARNING`` — otherwise every API request would emit a ``HTTP Request:
    GET ...`` INFO line that drowns out the pipeline's own UI output.

    Authentication (tried in order):
      1. ``GH_TOKEN`` env var — the ``gh`` CLI's native env var.
      2. ``GITHUB_TOKEN`` env var — used by GitHub Actions runners and
         fine-grained PATs.
      3. ``gh auth token`` — reuses your existing ``gh`` CLI login.

    Required token scopes:
      - For public repos: no scopes required (unauthenticated works but
        is heavily rate-limited; providing a token is strongly recommended).
      - For private repos: ``repo`` (classic PAT) or ``actions:read`` +
        ``contents:read`` (fine-grained PAT).

    Log extraction strategy
    -----------------------
    When the run has failed, per-job logs are fetched and passed through
    ``_summarize_log``, which runs a four-layer extraction pipeline:

      1. **Cleaning pass** — strip ISO-8601 timestamps, drop pure
         ``##[command]``/``##[debug]``/``##[endgroup]`` markers, strip
         the ``##[group]`` prefix while keeping any trailing content
         (test runners like Flutter emit
         ``##[group]<failure signal>`` lines whose content is the
         primary failure anchor), drop noise prefixes (git
         housekeeping, ``[command]/usr/bin/git ...``,
         ``Temporarily overriding HOME=...``), and hard-truncate at the
         first ``Post job cleanup.`` line — the one guaranteed
         end-of-job marker from the Actions runner.

      2. **Tool-specific anchor pass (primary)** — scan the cleaned log
         against a library of regex anchors keyed to well-known build
         tools and test runners. The earliest match wins and the output
         is "from that line (minus ``lead_context`` lines of leading
         context) to the end of the cleaned log".

         Supported ecosystems:

           - JVM: Maven (``[INFO] BUILD FAILURE``), Gradle
             (``FAILURE: Build failed with an exception``), Ant, sbt
           - Rust: ``error[Ennnn]:`` diagnostics, ``error: could not
             compile``, ``error: aborting due to N previous errors``
           - Go: ``--- FAIL: TestName``, ``FAIL<TAB>pkg<TAB>0.Xs``
           - Python: pytest FAILURES/ERRORS/short-summary sections,
             unittest ``FAIL:``/``ERROR:``, ruff, flake8
           - JavaScript/TypeScript: Jest ``FAIL``/``Test Suites:``,
             npm ``npm ERR!``, yarn errors, ``error TSnnnn:`` (tsc),
             ESLint ``✖ N problems``
           - .NET: MSBuild ``Build FAILED.``, ``error (CS|MSB)nnnn:``
           - Native: CMake ``CMake Error``, Bazel ``FAILED: Build did
             NOT complete successfully``, xcodebuild
             ``** BUILD FAILED **``, GCC/Clang
             ``file.c:line:col: error:``
           - Flutter/Dart: ``❌ path/test.dart:`` (per-test failures,
             after ``##[group]`` prefix strip),
             ``##[error]N tests passed, M failed``,
             ``EXCEPTION CAUGHT BY FLUTTER`` exception banner, Dart
             analyzer ``error - path:line:col - ...``

      3. **Marker + keyword fallback** — if no tool anchor matched,
         find lines with explicit ``##[error]`` markers OR lines
         containing error keywords (``error:``, ``exception``,
         ``traceback``, ``failed``, ``fatal:``, etc.), take
         ``context_lines`` lines before and half that after, and merge
         overlapping windows.

      4. **Tail pass (last resort)** — if none of the above found
         anything, return the last ``max_log_lines`` lines of the log
         with a ``(no error markers found...)`` header.

    Adding a new ecosystem is one line in ``_FAILURE_ANCHORS`` — a
    ``(name, regex)`` tuple — plus a unit test in ``tests/test_check_ci.py``.

    Modes
    -----
    One-shot (``poll=False``, the default):
        Checks the latest run and returns immediately. If the run is still
        in progress, the stage fails with a message including the run URL —
        the stage does NOT wait. Useful when you just want the current state.

    Polling (``poll=True``):
        Polls every ``poll_interval`` seconds until the run reaches a
        terminal state (``completed``) or ``timeout_minutes`` elapses.
        Useful right after pushing a branch, when the run may not have
        started yet.

    Repo and branch detection
    -------------------------
    If ``repo`` is not provided, it is parsed from the ``origin`` remote
    of the current git working directory — both HTTPS
    (``https://github.com/owner/repo.git``) and SSH
    (``git@github.com:owner/repo.git``) URLs are supported.

    If ``branch`` is not provided, it is read from
    ``git branch --show-current``, which returns an empty string in a
    detached-HEAD state. In that case the stage fails with a clear
    error — pass ``branch=`` explicitly.

    Args:
        repo: GitHub repo in ``owner/repo`` format. If ``None``, detected
            from the git remote origin URL.
        branch: Branch to check. Defaults to the current git branch.
        workflow: Optional workflow filename (e.g. ``"ci.yml"``) or
            workflow ID to filter on. When omitted, queries runs across
            all workflows and picks the most recent.
        poll: If ``True``, poll until the run completes (or times out).
            If ``False`` (default), check once and return immediately.
        poll_interval: Seconds between polls (only when ``poll=True``).
            Defaults to 30. Shorter intervals consume more API quota.
        timeout_minutes: Max minutes to wait (only when ``poll=True``).
            Defaults to 30. After this, the stage fails with a timeout.
        summarize: If ``True`` (default), post-process fetched failure
            logs with the layered extractor described above. Set to
            ``False`` to get the raw per-job log verbatim — useful if
            you want to feed the full log into your own parser or an
            LLM with a very large context window.
        context_lines: Lines of context to keep before each error marker
            in the **fallback** marker/keyword pass. Ignored by the
            anchor pass (which extracts from the anchor line to the end
            of the cleaned log). Defaults to 20.
        max_log_lines: Hard upper bound on the number of log lines
            returned in ``output["logs"]``. Defaults to 400. When
            exceeded, the output is truncated with a header indicating
            how many lines were kept.

    Output
    ------
    ``StageResult.output`` is a dict::

        {
            "run_id": int,               # GitHub Actions run ID
            "name": str,                 # workflow display name
            "status": str,               # "completed", "in_progress", "queued", ...
            "conclusion": str | None,    # "success", "failure", "cancelled", ...
            "url": str,                  # html_url to the run page on github.com
            "logs": str,                 # summarized failed-step logs
                                         # (empty when conclusion is "success")
        }

    ``StageResult.success`` is ``True`` when ``conclusion == "success"``,
    otherwise ``False``. On failure, ``StageResult.error`` is a single
    short line in the form
    ``"CI failure: <workflow name> (<run url>)"`` — the full log lives
    in ``output["logs"]`` so it isn't duplicated.

    Downstream usage
    ----------------
    A common pattern is to feed the extracted failure into a ``Generate``
    stage that analyzes and proposes a fix::

        Pipeline("fix-ci")
            .stage("ci", CheckCI(poll=True), on_failure=OnFailure.ASK_USER)
            .stage("fix", Generate(
                prompt=(
                    "The build for branch {param.branch} failed. "
                    "Analyze this failure and propose a fix:\\n\\n"
                    "{ci.output}"
                ),
            ))

    Because the logs are pre-extracted, the Generate prompt is typically
    a few hundred to a couple thousand tokens rather than tens of
    thousands — big enough to be useful, small enough to stay cheap.

    Caveats
    -------
    - **Rate limits**: authenticated GitHub API requests are capped at
      5000/hour per token. Each ``CheckCI`` invocation makes ~2–3 API
      calls plus one log-download per failed job.
    - **Log sizes**: per-job logs are fetched in memory. A job that
      produces several megabytes of output is fine; a job that produces
      gigabytes is not. In practice, the anchor pass avoids putting the
      whole log through to downstream stages even when the raw fetch
      is large.
    - **Re-runs**: if a workflow has been re-run, only the latest
      attempt is returned.
    - **Anchor gaps**: ecosystems not in ``_FAILURE_ANCHORS`` still work
      via the marker/keyword fallback, but the output will be a
      context window around ``##[error]`` markers rather than the full
      failure block. If you hit a recurring false negative, add an
      anchor — it's one regex.
    - **Step zip fallback**: if a single job's log download fails (e.g.
      the blob-storage redirect expires), the stage falls back to
      downloading and unzipping the full run's log archive. This is
      slower but more robust.

    Examples
    --------
    .. code-block:: python

        # Auto-detect repo + branch, one-shot check of the latest run
        Stage("ci", CheckCI())

        # Specific repo/branch, poll until done
        Stage("ci", CheckCI(repo="org/repo", branch="feature-x", poll=True))

        # Filter to a specific workflow, longer poll timeout
        Stage("ci", CheckCI(
            workflow="e2e-tests.yml",
            poll=True,
            timeout_minutes=45,
        ))

        # Raw logs (no summarization) — e.g. feeding into a big-context
        # model that wants to see everything
        Stage("ci", CheckCI(summarize=False))

        # Tighter output cap for small contexts
        Stage("ci", CheckCI(max_log_lines=150))
    """

    def __init__(
        self,
        *,
        repo: str | None = None,
        branch: str | None = None,
        workflow: str | None = None,
        poll: bool = False,
        poll_interval: int = 30,
        timeout_minutes: int = 30,
        summarize: bool = True,
        context_lines: int = 20,
        # Lines of pre-anchor context kept by the anchor pass. Bumped from
        # the previous hard-coded value of 2 because surefire/failsafe
        # anchors fire AFTER each failing test's MockMvc request/response
        # block — ~30 lines of context is needed to keep the actual
        # exception/response body visible for the LLM.
        lead_context: int = 30,
        max_log_lines: int = 400,
        # Before the first status poll, look at the median duration of recent
        # successful runs and sleep ~80% of that. Saves a lot of polls (and
        # therefore API calls) when builds take several minutes — the default
        # poll loop hammers list_workflow_runs every 30s otherwise. Costs one
        # extra API call upfront, cached for an hour at module level.
        smart_wait: bool = True,
        smart_wait_factor: float = 0.8,
    ) -> None:
        self.repo = repo
        self.branch = branch
        self.workflow = workflow
        self.poll = poll
        self.poll_interval = poll_interval
        self.timeout_minutes = timeout_minutes
        self.summarize = summarize
        self.context_lines = context_lines
        self.lead_context = lead_context
        self.max_log_lines = max_log_lines
        self.smart_wait = smart_wait
        self.smart_wait_factor = smart_wait_factor

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        try:
            from githubkit import GitHub
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

        # Cache the local HEAD once per run() call. We only use it to detect
        # the "stale completed run" race described below; if we're not in a
        # git repo (head_sha is None) we just behave as before.
        local_head = await _git_head_sha()

        # Smart wait is deferred until we've actually seen a pending run.
        # The original "sleep before first poll" version slept blindly even
        # when the latest run for local HEAD had already completed, which
        # delayed the fix loop by a full build cycle for no benefit. Now we
        # probe first; smart_wait fires once, only if the first observation
        # is a queued/in_progress run worth waiting on.
        smart_wait_pending = self.poll and self.smart_wait

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

            # Stale-run guard: GitHub's list_workflow_runs endpoint is
            # eventually consistent. Right after a push, it can return a
            # previously-completed run (for an older commit on the same
            # branch) as "the latest" while the run for the new HEAD has
            # been created but not yet indexed. Without this guard we'd
            # short-circuit on `status == "completed"` below and report a
            # stale failure (or success!) for code that's no longer HEAD.
            #
            # If the latest run's head_sha is a strict ancestor of local
            # HEAD, treat it as not-yet-the-real-run and keep polling
            # (subject to the same timeout) until the new run appears.
            if (
                self.poll
                and status == "completed"
                and local_head
                and run_sha
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
                log.debug(
                    "Latest run %s is for ancestor %s of local HEAD %s — "
                    "waiting %ds for the new run to be indexed",
                    run_info["run_id"], run_sha[:8], local_head[:8],
                    self.poll_interval,
                )
                await asyncio.sleep(self.poll_interval)
                continue

            if status == "completed":
                head_match = bool(local_head and run_sha and run_sha == local_head)
                log.info(
                    "Latest run %s for %s is completed (%s) — using it directly%s.",
                    run_info["run_id"],
                    (run_sha[:8] if run_sha else "?"),
                    conclusion,
                    " (matches local HEAD)" if head_match else "",
                )
                logs = ""
                if conclusion != "success":
                    logs = await _get_failed_logs(gh, owner, repo_name, run_info["run_id"])
                    if self.summarize and logs:
                        logs = _summarize_log(
                            logs,
                            context_lines=self.context_lines,
                            max_lines=self.max_log_lines,
                            lead_context=self.lead_context,
                        )

                output = {
                    "run_id": run_info["run_id"],
                    "name": run_info["name"],
                    "status": status,
                    "conclusion": conclusion,
                    "url": run_info["url"],
                    "logs": logs,
                }
                success = conclusion == "success"
                error = None
                if not success:
                    # Keep error short — the full logs live in output["logs"].
                    error = f"CI {conclusion}: {run_info['name']} ({run_info['url']})"
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

            # The latest run is pending (queued / in_progress). Smart wait
            # is worth doing here — sleep ~80% of typical duration before
            # the next poll instead of hammering at poll_interval. Fires
            # at most once per CheckCI invocation.
            if smart_wait_pending:
                smart_wait_pending = False
                estimated = await _estimate_typical_duration(
                    gh, owner, repo_name, self.workflow, branch,
                )
                if estimated is not None and estimated > self.poll_interval:
                    remaining = max(0, deadline - asyncio.get_event_loop().time())
                    wait_secs = max(0, min(int(estimated * self.smart_wait_factor), int(remaining)))
                    if wait_secs > 0:
                        log.info(
                            "Latest run %s is %s; typical successful run for %s "
                            "is ~%ds, sleeping %ds before next poll.",
                            run_info["run_id"], status,
                            self.workflow or "any workflow",
                            int(estimated), wait_secs,
                        )
                        await asyncio.sleep(wait_secs)
                        continue

            if asyncio.get_event_loop().time() >= deadline:
                return StageResult(
                    name="", success=False,
                    error=f"CI timed out after {self.timeout_minutes} minutes. Last status: {status}",
                )

            log.debug("CI status: %s — waiting %ds", status, self.poll_interval)
            await asyncio.sleep(self.poll_interval)


def _create_client(token: str) -> Any:
    """Create a githubkit GitHub client. Separate function for testability."""
    from githubkit import GitHub

    return GitHub(token)


# Module-level cache of typical run durations. Keyed by
# (owner, repo, workflow, branch); value is (cached_at_monotonic_seconds,
# duration_seconds). 1-hour TTL is a sane "build durations don't change
# wildly within an hour" assumption — long enough to amortise the lookup
# across all CheckCI invocations within a single retry loop, short enough
# that a slowdown will be picked up on the next pipeline run.
_DURATION_CACHE: dict[tuple[str, str, str | None, str], tuple[float, float]] = {}
_DURATION_CACHE_TTL_SECONDS = 3600.0


def _cached_duration(key: tuple[str, str, str | None, str]) -> float | None:
    entry = _DURATION_CACHE.get(key)
    if entry is None:
        return None
    cached_at, secs = entry
    if asyncio.get_event_loop().time() - cached_at > _DURATION_CACHE_TTL_SECONDS:
        _DURATION_CACHE.pop(key, None)
        return None
    return secs


async def _estimate_typical_duration(
    gh: Any, owner: str, repo: str, workflow: str | None, branch: str,
) -> float | None:
    """Median wall-clock duration of recent successful runs, in seconds.

    Costs one API call (cached for an hour). Returns None when no successful
    runs are available — callers should skip the smart pre-wait in that case.

    Looks at the latest 5 successful runs on the given branch. If the branch
    has no successful history (typical for a freshly created feature branch)
    we fall back to the workflow's recent successful runs across all
    branches, since the rough duration of "test-pg.yml on main" is still a
    decent estimate for "test-pg.yml on this branch".
    """
    key = (owner, repo, workflow, branch)
    cached = _cached_duration(key)
    if cached is not None:
        return cached

    async def _fetch(use_branch: bool) -> list:
        kwargs: dict[str, Any] = {
            "owner": owner, "repo": repo, "status": "success", "per_page": 5,
        }
        if use_branch:
            kwargs["branch"] = branch
        if workflow:
            kwargs["workflow_id"] = workflow
            resp = await gh.rest.actions.async_list_workflow_runs(**kwargs)
        else:
            resp = await gh.rest.actions.async_list_workflow_runs_for_repo(**kwargs)
        return list(resp.parsed_data.workflow_runs)

    try:
        runs = await _fetch(use_branch=True)
        if not runs:
            runs = await _fetch(use_branch=False)
    except Exception as exc:
        log.debug("Could not estimate run duration: %s", exc)
        return None

    durations: list[float] = []
    for r in runs:
        start = getattr(r, "run_started_at", None) or getattr(r, "created_at", None)
        end = getattr(r, "updated_at", None)
        if start and end:
            durations.append((end - start).total_seconds())
    if not durations:
        return None

    durations.sort()
    median = durations[len(durations) // 2]
    _DURATION_CACHE[key] = (asyncio.get_event_loop().time(), median)
    return median


async def _get_latest_run(
    gh: Any, owner: str, repo: str, branch: str, workflow: str | None,
) -> dict | None:
    """Fetch the most recent workflow run for a branch."""
    if workflow:
        resp = await gh.rest.actions.async_list_workflow_runs(
            owner=owner, repo=repo, workflow_id=workflow,
            branch=branch, per_page=1,
        )
    else:
        resp = await gh.rest.actions.async_list_workflow_runs_for_repo(
            owner=owner, repo=repo, branch=branch, per_page=1,
        )

    runs = resp.parsed_data.workflow_runs
    if not runs:
        return None

    r = runs[0]
    return {
        "run_id": r.id,
        "name": r.name or "",
        "status": r.status or "",
        "conclusion": r.conclusion,
        "url": r.html_url,
        "head_sha": r.head_sha or "",
    }


async def _git_head_sha() -> str | None:
    """Return the local HEAD SHA, or None if not in a git repo."""
    proc = await asyncio.create_subprocess_exec(
        "git", "rev-parse", "HEAD",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    val = stdout.decode().strip()
    return val or None


async def _is_ancestor(ancestor: str, descendant: str) -> bool:
    """Return True if ``ancestor`` is a strict ancestor of ``descendant``.

    Uses ``git merge-base --is-ancestor``. Returns False on any git error
    (missing commit, not a repo, etc.) so callers fall through to normal
    behaviour rather than getting stuck waiting forever.
    """
    if not ancestor or not descendant or ancestor == descendant:
        return False
    proc = await asyncio.create_subprocess_exec(
        "git", "merge-base", "--is-ancestor", ancestor, descendant,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate()
    return proc.returncode == 0


async def _get_failed_logs(gh: Any, owner: str, repo: str, run_id: int) -> str:
    """Fetch failed-job and -step names, plus the log content for a failed run.

    Preferred path is the run-log zip's per-step files (e.g.
    ``build/17_test.txt``). GitHub serves these already-split by step, so
    we can return just the failing step's content with zero parsing
    required downstream — sidestepping the fact that the runner's
    ``##[group]`` labels don't match the API's step ``name:``.

    Falls back to the per-job ``download_job_logs_for_workflow_run``
    response (full job log, all steps concatenated) when the zip is
    unavailable or doesn't contain a file for the failed step.
    """
    resp = await gh.rest.actions.async_list_jobs_for_workflow_run(
        owner=owner, repo=repo, run_id=run_id, per_page=100,
    )
    jobs = resp.parsed_data.jobs

    failed_jobs = [j for j in jobs if j.conclusion in ("failure", "cancelled")]
    if not failed_jobs:
        return ""

    # Try the per-step zip first — much smaller and pre-sliced by GitHub.
    try:
        per_step = await _fetch_failed_step_logs_from_zip(
            gh, owner, repo, run_id, failed_jobs,
        )
    except Exception:
        log.debug("Per-step zip fetch failed for run %d", run_id, exc_info=True)
        per_step = ""
    if per_step:
        return per_step

    parts: list[str] = []
    for job in failed_jobs:
        failed_steps = [s for s in (job.steps or []) if s.conclusion in ("failure", "cancelled")]
        step_info = ", ".join(s.name for s in failed_steps)
        parts.append(f"## Failed job: {job.name}")
        if step_info:
            parts.append(f"Failed steps: {step_info}")

        # Fetch raw log for this specific job
        try:
            log_resp = await gh.rest.actions.async_download_job_logs_for_workflow_run(
                owner=owner, repo=repo, job_id=job.id,
            )
            raw_log = log_resp.text if hasattr(log_resp, "text") else str(log_resp)
            if raw_log:
                parts.append(raw_log)
        except Exception:
            log.debug("Could not fetch log for job %s/%d", job.name, job.id, exc_info=True)
            # Fall back to run-level log zip
            try:
                parts.append(await _extract_run_logs(gh, owner, repo, run_id))
            except Exception:
                log.debug("Could not fetch run logs for %d", run_id, exc_info=True)

    return "\n\n".join(parts)


async def _fetch_failed_step_logs_from_zip(
    gh: Any,
    owner: str,
    repo: str,
    run_id: int,
    failed_jobs: list,
) -> str:
    """Return only the failed steps' logs by reading the run-log zip.

    GitHub Actions packs the run logs as ``<job_name>/<step_number>_<step_name>.txt``
    inside the zip. Step name characters that aren't filesystem-safe
    (slashes, colons, etc.) are replaced with underscores, so we match
    by ``<job_name>/<step_number>_`` prefix rather than reconstructing
    the full filename.

    Returns an empty string when the zip can't be downloaded or contains
    no matching files; the caller is expected to fall back to the
    per-job log download.
    """
    resp = await gh.rest.actions.async_download_workflow_run_logs(
        owner=owner, repo=repo, run_id=run_id,
    )
    content = resp.content if hasattr(resp, "content") else bytes(resp)

    parts: list[str] = []
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        names = zf.namelist()
        for job in failed_jobs:
            failed_steps = [
                s for s in (job.steps or [])
                if s.conclusion in ("failure", "cancelled")
            ]
            if not failed_steps:
                continue

            parts.append(f"## Failed job: {job.name}")
            parts.append(
                "Failed steps: " + ", ".join(s.name for s in failed_steps)
            )

            for step in failed_steps:
                prefix = f"{job.name}/{step.number}_"
                matches = [n for n in names if n.startswith(prefix)]
                if not matches:
                    log.debug(
                        "[per-step-zip] no file for %s step #%d (%s)",
                        job.name, step.number, step.name,
                    )
                    return ""
                # Should be exactly one match; defensive sort if not.
                step_file = sorted(matches)[0]
                body = zf.read(step_file).decode(errors="replace")
                parts.append(
                    f"### Step {step.number}: {step.name}\n"
                    f"(file: {step_file})\n\n"
                    f"{body.rstrip()}"
                )

    if not parts:
        return ""
    return "\n\n".join(parts)


def _summarize_log(
    raw: str,
    *,
    context_lines: int = 20,
    max_lines: int = 400,
    lead_context: int = 2,
) -> str:
    """Reduce a raw GitHub Actions log to the parts that matter.

    Layered extraction strategy (each layer is only tried if the previous
    layer produced nothing):

      1. **Clean pass**: strip ISO-8601 timestamps, ``##[group]`` markers,
         and known noise prefixes (git housekeeping, post-job cleanup).
         Hard-truncate at the first ``Post job cleanup.`` line.

      2. **Anchor pass (primary)**: scan every cleaned line against a
         library of tool-specific failure anchors (see ``_FAILURE_ANCHORS``).
         The ones that match include, for example, Maven's
         ``[INFO] BUILD FAILURE``, Gradle's ``FAILURE: Build failed``,
         Cargo's ``error[E....]:``, pytest's ``==== FAILURES ====``,
         Jest's ``Test Suites: ... failed``, MSBuild's ``Build FAILED.``,
         and GCC/Clang's ``file.c:L:C: error:``.

         If any anchor matches, take the EARLIEST matching line and
         return everything from ``lead_context`` lines above it to the
         end of the cleaned log (capped at ``max_lines``).

         This gives one clean block covering the full failure region —
         regardless of which ecosystem the build is from.

      3. **Marker pass (fallback)**: if no tool-specific anchor matched,
         fall back to the generic ``##[error]`` / error-keyword window
         approach: find lines with explicit markers, take context windows
         around each, and merge overlapping windows.

      4. **Tail pass (last resort)**: if even the marker pass found
         nothing, return the last ``max_lines`` lines of the log with a
         ``(no error markers found...)`` header.

    The goal is a summary compact enough to fit in a follow-up LLM prompt
    without losing the actual failure context.

    Args:
        raw: The raw log text.
        context_lines: Lines of context to keep before each marker in the
            fallback marker pass. Ignored by the anchor pass.
        max_lines: Hard upper bound on the number of lines returned.
        lead_context: Lines of context to include above the anchor line
            when the anchor pass succeeds (so the surrounding structure
            is visible, e.g. the ``[INFO] ---`` separator above Maven's
            ``BUILD FAILURE`` line).
    """
    cleaned: list[str] = []
    for line in raw.splitlines():
        line = _TIMESTAMP_RE.sub("", line)
        # Stop hard at the first post-run cleanup marker: everything that
        # follows is guaranteed to be noise (job cleanup, git housekeeping).
        if any(marker in line for marker in _CLEANUP_MARKERS):
            break
        # Drop pure markers (##[command], ##[debug], ##[endgroup]).
        if _DROP_MARKER_RE.match(line):
            continue
        # Strip ##[group] prefix but keep trailing content. Many test
        # runners emit ``##[group]<failure signal>`` lines — throwing
        # away the whole line destroys the only anchor we have.
        line = _GROUP_PREFIX_RE.sub("", line)
        if not line.strip():
            continue
        if line.startswith(_NOISE_PREFIXES):
            continue
        cleaned.append(line)

    if not cleaned:
        return ""

    # -------- Layer 2: tool-specific anchor pass --------
    earliest_anchor_idx: int | None = None
    detected_tool: str | None = None
    for i, line in enumerate(cleaned):
        for tool, pat in _FAILURE_ANCHORS:
            if pat.search(line):
                if earliest_anchor_idx is None or i < earliest_anchor_idx:
                    earliest_anchor_idx = i
                    detected_tool = tool
                break  # one tool per line is enough

    if earliest_anchor_idx is not None:
        start = max(0, earliest_anchor_idx - lead_context)
        block = cleaned[start:]
        header = f"(detected: {detected_tool})"
        if len(block) > max_lines:
            block = block[:max_lines]
            block.append(f"... (truncated to {max_lines} lines)")
        return header + "\n" + "\n".join(block)

    # -------- Layer 3: marker + keyword window fallback --------
    hit_set: set[int] = set()
    for i, ln in enumerate(cleaned):
        if ln.startswith(_ERROR_MARKERS):
            hit_set.add(i)
            continue
        lower = ln.lower()
        if any(kw in lower for kw in _ERROR_KEYWORDS):
            hit_set.add(i)

    hit_indices = sorted(hit_set)

    # Fallback: tail of the log if truly nothing interesting found.
    if not hit_indices:
        tail = cleaned[-max_lines:]
        header = f"(no error markers found — showing last {len(tail)} lines)"
        return header + "\n" + "\n".join(tail)

    # Build context windows around each hit, then merge overlapping ones.
    after = max(1, context_lines // 2)
    windows: list[tuple[int, int]] = []
    for i in hit_indices:
        start = max(0, i - context_lines)
        end = min(len(cleaned), i + after + 1)
        if windows and start <= windows[-1][1]:
            windows[-1] = (windows[-1][0], max(windows[-1][1], end))
        else:
            windows.append((start, end))

    # Render: one section per window, with a separator line between sections.
    out_lines: list[str] = []
    for start, end in windows:
        if out_lines:
            out_lines.append("...")
        out_lines.extend(cleaned[start:end])
        if len(out_lines) >= max_lines:
            break

    if len(out_lines) > max_lines:
        out_lines = out_lines[-max_lines:]
        out_lines.insert(0, f"(truncated to last {max_lines} lines of summary)")

    return "\n".join(out_lines)


async def _extract_run_logs(gh: Any, owner: str, repo: str, run_id: int) -> str:
    """Download the full run log zip and extract text content."""
    resp = await gh.rest.actions.async_download_workflow_run_logs(
        owner=owner, repo=repo, run_id=run_id,
    )
    content = resp.content if hasattr(resp, "content") else bytes(resp)
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        log_parts = []
        for name in sorted(zf.namelist()):
            if name.endswith(".txt"):
                log_parts.append(f"--- {name} ---\n{zf.read(name).decode(errors='replace')}")
        return "\n".join(log_parts)


# --- standalone CLI -------------------------------------------------------
#
# Run from the repo root:
#
#   uv run python -m norn.stages.check_ci
#   uv run python -m norn.stages.check_ci --workflow ci.yml --branch main
#   uv run python -m norn.stages.check_ci --repo owner/proj --poll
#   uv run python -m norn.stages.check_ci --no-summarize --max-lines 5000
#   uv run python -m norn.stages.check_ci --json > result.json
#
# The CLI mirrors the constructor — every flag corresponds to a kwarg of
# ``CheckCI``. Default output is human-readable; ``--json`` dumps the
# raw ``StageResult.output`` dict.

def _cli_main() -> int:
    import argparse
    import json
    import sys

    from norn.models import PipelineContext

    parser = argparse.ArgumentParser(
        prog="python -m norn.stages.check_ci",
        description=(
            "Run CheckCI standalone and print whatever the stage would return. "
            "Useful for debugging the log summarizer (anchor coverage, "
            "max_log_lines tuning) without spinning up a pipeline."
        ),
    )
    parser.add_argument("--repo", help="owner/name; auto-detected from git origin if omitted")
    parser.add_argument("--branch", help="branch name; auto-detected from current git branch if omitted")
    parser.add_argument("--workflow", help='workflow filename (e.g. "ci.yml") or workflow id')
    parser.add_argument("--poll", action="store_true", help="poll until the run completes")
    parser.add_argument("--poll-interval", type=int, default=30, metavar="SECS")
    parser.add_argument("--timeout", type=int, default=30, metavar="MINUTES",
                        help="poll timeout in minutes (only with --poll)")
    parser.add_argument("--no-summarize", action="store_true",
                        help="skip the layered extractor and return raw per-job logs")
    parser.add_argument("--context-lines", type=int, default=20,
                        help="context window for the marker/keyword fallback pass")
    parser.add_argument("--lead-context", type=int, default=30,
                        help=(
                            "lines of pre-anchor context kept by the anchor pass. "
                            "Bump for JVM tests when surefire/failsafe failure "
                            "markers fire after long MockMvc request/response blocks "
                            "you want included."
                        ))
    parser.add_argument("--max-lines", type=int, default=400,
                        help="cap on log lines returned")
    parser.add_argument("--json", action="store_true",
                        help="emit the raw output dict as JSON instead of pretty-printing")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="enable DEBUG logging (repeat for more)")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(
            level=logging.DEBUG if args.verbose >= 1 else logging.INFO,
            format="%(name)s [%(levelname)s] %(message)s",
        )
        # The module-level WARNING pin on httpx is too aggressive when the
        # user explicitly asks for verbose — re-enable INFO so they see the
        # request URLs they almost certainly want for debugging.
        logging.getLogger("httpx").setLevel(logging.INFO)

    stage = CheckCI(
        repo=args.repo,
        branch=args.branch,
        workflow=args.workflow,
        poll=args.poll,
        poll_interval=args.poll_interval,
        timeout_minutes=args.timeout,
        summarize=not args.no_summarize,
        context_lines=args.context_lines,
        lead_context=args.lead_context,
        max_log_lines=args.max_lines,
    )
    ctx = PipelineContext()
    result = asyncio.run(stage.run(ctx))

    if args.json:
        payload = {
            "success": result.success,
            "error": result.error,
            "output": result.output,
        }
        json.dump(payload, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return 0 if result.success else 1

    # Human-readable rendering. Falls back to plain print if rich isn't
    # installed (it usually is — norn depends on it — but keep this resilient
    # for someone running the file in isolation).
    try:
        from norn.ui import console
        emit = console.print
    except Exception:
        def emit(*a: Any, **kw: Any) -> None:
            print(*a)

    out = result.output if isinstance(result.output, dict) else {}
    status_color = "green" if result.success else "red"
    emit(f"\n[bold {status_color}]── CheckCI result ──[/bold {status_color}]")
    emit(f"  success    : {result.success}")
    if result.error:
        emit(f"  error      : {result.error}")
    if out:
        emit(f"  run_id     : {out.get('run_id')}")
        emit(f"  name       : {out.get('name')}")
        emit(f"  status     : {out.get('status')}")
        emit(f"  conclusion : {out.get('conclusion')}")
        emit(f"  url        : {out.get('url')}")
        logs = out.get("logs", "") or ""
        emit(f"  logs       : {len(logs)} chars\n")
        if logs:
            emit(f"[bold]── extracted logs ──[/bold]")
            # Print logs without rich markup interpretation — they often
            # contain `[INFO]` etc. that rich would misparse as markup.
            print(logs)
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(_cli_main())

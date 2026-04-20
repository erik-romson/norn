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


def _githubkit_available() -> bool:
    """Check whether the ``githubkit`` package is importable."""
    try:
        import githubkit  # noqa: F401
        return True
    except ImportError:
        return False


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
        max_log_lines: int = 400,
    ) -> None:
        self.repo = repo
        self.branch = branch
        self.workflow = workflow
        self.poll = poll
        self.poll_interval = poll_interval
        self.timeout_minutes = timeout_minutes
        self.summarize = summarize
        self.context_lines = context_lines
        self.max_log_lines = max_log_lines

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        if not _githubkit_available():
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

            if status == "completed":
                logs = ""
                if conclusion != "success":
                    logs = await _get_failed_logs(gh, owner, repo_name, run_info["run_id"])
                    if self.summarize and logs:
                        logs = _summarize_log(
                            logs,
                            context_lines=self.context_lines,
                            max_lines=self.max_log_lines,
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
    }


async def _get_failed_logs(gh: Any, owner: str, repo: str, run_id: int) -> str:
    """Fetch failed job names, step names, and raw logs for a failed run."""
    resp = await gh.rest.actions.async_list_jobs_for_workflow_run(
        owner=owner, repo=repo, run_id=run_id, per_page=100,
    )
    jobs = resp.parsed_data.jobs

    failed_jobs = [j for j in jobs if j.conclusion in ("failure", "cancelled")]
    if not failed_jobs:
        return ""

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

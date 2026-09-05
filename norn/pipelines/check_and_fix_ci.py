"""Push the current branch, wait for CI, and auto-fix
failures by feeding GitHub Actions logs back to Claude.

Adapted from ``egnedata/egnedata-kmp/tmp/check.py`` and trimmed for the
norn repo:

  * No plan-step loop — operates on whatever is already committed on the
    current branch. Use the ``implement_features`` pipeline if you need
    plan-driven step implementation; this pipeline is for the
    "I have commits, make CI green" use case.
  * Single workflow gate (``ci.yml``) — override with ``--arg workflow=...``.
  * Local validation with ``uv run python -m pytest tests/ -v`` before
    ever touching CI. Override with ``--arg test_cmd=...``.
  * Same safeguards as the egnedata version: ``MIN_USEFUL_LOG_CHARS``
    bail, identical-logs-across-runs abort, ``MAX_FIX_ATTEMPTS`` cap.

Run with::

    norn run check_and_fix_ci
    norn run check_and_fix_ci --arg workflow=ci.yml
    norn run check_and_fix_ci --arg test_cmd="uv run python -m pytest tests/test_runner.py -v"
    norn run check_and_fix_ci --arg skip_local=true

Set ``FIX_CI_LOOP_VERBOSE=1`` to dump full extracted CI logs to stderr.
"""
from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any


def _ensure_githubkit() -> None:
    """Install ``githubkit`` into the active interpreter if missing.

    ``CheckCI`` imports ``githubkit`` lazily; rather than fail late with
    a "not installed" string the log summarizer can't act on, install it
    eagerly into ``sys.executable`` before the pipeline starts.
    """
    try:
        import githubkit  # noqa: F401
        return
    except ImportError:
        pass

    print("[check_and_fix_ci] installing githubkit into active venv...", file=sys.stderr)
    subprocess.check_call(
        ["uv", "pip", "install", "--quiet", "--python", sys.executable, "githubkit"],
    )
    import githubkit  # noqa: F401


_ensure_githubkit()

from norn.alerts import MacOSChannel
from norn.dsl import Pipeline, Stage, fail, stage_failed
from norn.models import PipelineContext, StageResult
from norn.runner import PipelineError
from norn.stages.base import BaseStage
from norn.stages.check_ci import CheckCI
from norn.stages.compress_test_log import CompressTestLog
from norn.stages.extract_failing_step import ExtractFailingStep
from norn.stages.generate import Generate
from norn.stages.run_command import RunCommand


# Pinned to the launch directory at import time. This pipeline operates on the
# repo it was launched from: its RunCommand stages `cd {PROJECT_DIR}` explicitly
# and its git helpers run against that tree. It is therefore NOT
# worktree-isolated — running it with the TUI worktree toggle will still
# execute against the launch directory.
PROJECT_DIR = os.getcwd()

# Tail bound for local test stdout/stderr handed to the fix prompt — keeps
# pytest blasting megabytes of output from blowing the prompt budget.
LOCAL_TEST_TAIL_CHARS = 12000

# Short log summaries usually mean "no failure anchors matched" — nothing
# the fix prompt can act on. Bail instead of looping on a blank signal.
MIN_USEFUL_LOG_CHARS = 80

MAX_FIX_ATTEMPTS = 3

VERBOSE = os.environ.get("FIX_CI_LOOP_VERBOSE", "").lower() in ("1", "true", "yes")

metadata = {
    "env_vars": ["ANTHROPIC_API_KEY", "GH_TOKEN"],
    "args": {
        "workflow": "GitHub Actions workflow filename (default: ci.yml)",
        "test_cmd": "Local test command (default: auto-detected from project markers)",
        "skip_local": "Set to true to skip the local test loop and go straight to CI",
        "app_packages": (
            "Comma-separated app package globs for the Surefire log compressor "
            "(e.g. 'com.e4marine.*,com.wilhelmsen.*'). Frames matching these "
            "are always preserved when Haiku compresses the failure trace. "
            "Omit to let Haiku infer from the trace."
        ),
    },
}


# --- per-project test command resolution ----------------------------------

def detect_test_cmd(project_dir: str) -> str:
    """Pick a sensible local test command from common project markers.

    Resolution order — first match wins. Add markers as needed for new
    project shapes; this stays a flat lookup on purpose so each project
    can also override via the ``test_cmd`` arg or ``NORN_TEST_CMD`` env
    var without touching this file.
    """
    root = Path(project_dir)
    if (root / "pom.xml").exists():
        return "mvn -q verify"
    if (root / "gradlew").exists():
        return "./gradlew test"
    if (root / "package.json").exists():
        return "npm test"
    if (root / "Cargo.toml").exists():
        return "cargo test"
    if (root / "go.mod").exists():
        return "go test ./..."
    if (root / "tests").is_dir() or (root / "pyproject.toml").exists():
        if (root / "tests").is_dir():
            return "uv run python -m pytest tests/ -v"
        return "uv run python -m pytest -v"
    return "true"


# --- parse --arg flags so we can configure stages at build time -----------

def _parse_args(argv: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    i = 0
    while i < len(argv):
        if argv[i] == "--arg" and i + 1 < len(argv) and "=" in argv[i + 1]:
            k, _, v = argv[i + 1].partition("=")
            out[k] = v
            i += 2
        else:
            i += 1
    return out


_args = _parse_args(sys.argv[1:])
WORKFLOWS = [_args.get("workflow", "ci.yml")]

# Surefire log extractor settings.
_app_pkgs_raw = _args.get("app_packages", "") or os.environ.get("NORN_APP_PACKAGES", "")
APP_PACKAGES: list[str] = [p.strip() for p in _app_pkgs_raw.split(",") if p.strip()]

# Test command resolution: --arg test_cmd > NORN_TEST_CMD env > auto-detect.
# `.norn.env` in the project root is already loaded by norn at startup, so
# putting `NORN_TEST_CMD=...` there gives you a per-project override with
# no extra wiring. The auto-detect fallback handles the common cases.
LOCAL_TEST_CMD = (
    _args.get("test_cmd")
    or os.environ.get("NORN_TEST_CMD")
    or detect_test_cmd(PROJECT_DIR)
)
SKIP_LOCAL = _args.get("skip_local", "false").lower() in ("1", "true", "yes")

# Model selection — both fix stages do code reasoning over a failure
# trace and produce edits across multiple files, so Sonnet is the right
# tier (Haiku is too weak to reliably trace multi-file failures; Opus is
# expensive overkill for typical pytest / CI errors). Override per call
# with --arg local_fix_model=opus when stuck on a hard bug.
LOCAL_FIX_MODEL = _args.get("local_fix_model", "sonnet")
CI_FIX_MODEL = _args.get("ci_fix_model", "sonnet")

print(f"[check_and_fix_ci] test_cmd       = {LOCAL_TEST_CMD}", file=sys.stderr)
print(f"[check_and_fix_ci] local_fix_model = {LOCAL_FIX_MODEL}", file=sys.stderr)
print(f"[check_and_fix_ci] ci_fix_model    = {CI_FIX_MODEL}", file=sys.stderr)
print(
    f"[check_and_fix_ci] app_packages    = "
    f"{','.join(APP_PACKAGES) if APP_PACKAGES else '(infer)'}",
    file=sys.stderr,
)


# --- git / gh helpers used by the stale-run guard ------------------------

async def _capture(*argv: str) -> tuple[int, str]:
    """Spawn ``argv`` (no shell) and return (returncode, stripped stdout)."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    return proc.returncode or 0, stdout.decode().strip()


async def _local_head_sha(project_dir: str) -> str | None:
    rc, out = await _capture("git", "-C", project_dir, "rev-parse", "HEAD")
    return out if rc == 0 and out else None


async def _is_ancestor(project_dir: str, maybe_ancestor: str, descendant: str) -> bool:
    """True if ``maybe_ancestor`` is reachable from ``descendant``.

    Used to recognise a stale CI run (its head_sha is an ancestor of local
    HEAD, meaning local already has commits past it) without false-positive
    aborts on a sibling branch where neither reaches the other.
    """
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", project_dir, "merge-base", "--is-ancestor",
        maybe_ancestor, descendant,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return (await proc.wait()) == 0


async def _run_head_sha(run_id: int) -> str | None:
    """Return the workflow run's ``head_sha`` via gh CLI, or None on miss."""
    rc, out = await _capture(
        "gh", "run", "view", str(run_id), "--json", "headSha", "--jq", ".headSha",
    )
    return out if rc == 0 and out else None


# --- helper stages --------------------------------------------------------

class CheckCIWithLogs(BaseStage):
    """Run ``CheckCI`` across one or more workflows in order, returning on
    the FIRST one that isn't green so the fix prompt has a single failure
    to focus on. Aborts the loop when a NEW run produces byte-identical
    logs to the previous run — the fix made no progress, retrying would
    just burn tokens.

    Emits the raw failure log verbatim. Compression (format-aware
    extract + Haiku for surefire) is the responsibility of a downstream
    ``CompressTestLog`` stage in the pipeline — same separation pattern
    used for the local test loop.
    """

    needs_agent = False

    def __init__(
        self,
        *,
        workflows: list[str],
        **ci_kwargs: Any,
    ) -> None:
        self.workflows = workflows
        # Force raw logs out of CheckCI — downstream compress stage will
        # apply Surefire extract + Haiku.
        ci_kwargs.setdefault("summarize", False)
        self.ci_kwargs = ci_kwargs
        # Tracks (workflow, run_id, logs) from the last red poll. Comparing
        # run_id avoids a false abort when the push didn't trigger a new
        # run — same historical run with same logs doesn't prove the fix
        # failed, it just means we haven't tested it yet.
        self._last_failure: tuple[str, int | None, str] | None = None

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        for raw in self.workflows:
            workflow = raw.strip()
            print(
                f"[check_and_fix_ci] checking workflow {workflow!r} "
                f"(polling up to {self.ci_kwargs.get('timeout_minutes', '?')}m)...",
                file=sys.stderr,
                flush=True,
            )
            check = CheckCI(workflow=workflow, **self.ci_kwargs)
            result = await check.run(ctx, **kwargs)
            output = result.output if isinstance(result.output, dict) else {}
            logs = output.get("logs", "") if isinstance(output, dict) else ""
            url = output.get("url", "") if isinstance(output, dict) else ""
            run_id = output.get("run_id") if isinstance(output, dict) else None
            conclusion = output.get("conclusion") if isinstance(output, dict) else None
            status = "green" if result.success else "RED"
            print(
                f"[check_and_fix_ci]   → {workflow} {status} "
                f"(run_id={run_id}, conclusion={conclusion}) {url}",
                file=sys.stderr,
                flush=True,
            )

            if result.success:
                continue

            if VERBOSE:
                print(
                    f"[check_and_fix_ci] ----- captured logs for {workflow} "
                    f"({len(logs)} chars) -----",
                    file=sys.stderr,
                    flush=True,
                )
                print(logs, file=sys.stderr, flush=True)
                print(
                    f"[check_and_fix_ci] ----- end logs for {workflow} -----",
                    file=sys.stderr,
                    flush=True,
                )

            # Stale-run guard. ``CheckCI`` returns the latest run on the
            # branch — but if local HEAD has commits past that run, the
            # red verdict is for an OLDER tree state. Asking the LLM to
            # "fix" what's already fixed locally just burns retries.
            run_sha = await _run_head_sha(run_id) if isinstance(run_id, int) else None
            local_sha = await _local_head_sha(PROJECT_DIR)
            if (
                run_sha
                and local_sha
                and run_sha != local_sha
                and await _is_ancestor(PROJECT_DIR, run_sha, local_sha)
            ):
                msg = (
                    f"Stale CI run: {workflow} run {run_id} is for "
                    f"{run_sha[:8]}, but local HEAD is {local_sha[:8]} "
                    f"({run_sha[:8]} is an ancestor). Either the fix "
                    "push didn't reach the remote, or a new run hasn't "
                    "been triggered yet. Aborting before the loop wastes "
                    "another fix attempt on an already-addressed failure."
                )
                raise PipelineError(
                    "check ci",
                    StageResult(name="check ci", success=False, error=msg),
                )

            last = self._last_failure
            if (
                last is not None
                and last[0] == workflow
                and last[1] != run_id
                and last[2] == logs
            ):
                msg = (
                    f"CI still failing in {workflow} with identical logs "
                    f"across runs {last[1]} → {run_id} — fix stage made "
                    "no progress, aborting."
                )
                raise PipelineError(
                    "check ci",
                    StageResult(name="check ci", success=False, error=msg),
                )
            self._last_failure = (workflow, run_id, logs)

            body = (
                f"Failing workflow: {workflow}\n"
                f"Run URL: {url}\n\n"
                f"{logs}"
            )
            return StageResult(
                name="",
                success=False,
                error=result.error or f"CI failure in {workflow}",
                output=body,
            )

        return StageResult(name="", success=True, output="")


class AssertFixProducedDiff(BaseStage):
    """Hard-stop the pipeline when the preceding ``fix ci`` stage produced
    no working-tree changes.

    Empty diff after a fix attempt means the LLM concluded "nothing needs
    fixing" — either the failure is already addressed locally or it can't
    figure out what to change. Re-running the loop will hit the same
    conclusion and burn another fix's worth of tokens. Better to raise
    out and let the human look at it.

    Mirrors ``AssertUsefulLogs``: raises ``PipelineError`` directly so
    the loop runner can't swallow it as just another retry-eligible
    failure.
    """

    needs_agent = False

    def __init__(self, *, project_dir: str) -> None:
        self.project_dir = project_dir

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        unstaged_rc, _ = await _capture(
            "git", "-C", self.project_dir, "diff", "--quiet",
        )
        staged_rc, _ = await _capture(
            "git", "-C", self.project_dir, "diff", "--cached", "--quiet",
        )

        # `git diff --quiet` exits 0 when there are no changes, 1 when
        # there are. Both clean → fix stage made no edits.
        if unstaged_rc == 0 and staged_rc == 0:
            msg = (
                "fix ci stage produced no edits — the LLM concluded the "
                "failure is already addressed locally (or it doesn't know "
                "how to fix it). Pushing again will not change CI; "
                "aborting before the loop burns more retries."
            )
            raise PipelineError(
                "assert fix produced diff",
                StageResult(
                    name="assert fix produced diff",
                    success=False,
                    error=msg,
                ),
            )
        return StageResult(name="", success=True)


class AssertUsefulLogs(BaseStage):
    """Hard-stop the pipeline when the named CheckCI stage failed but
    produced almost no log content — there's nothing for the fix prompt
    to act on, so retrying would just burn tokens.

    Raises ``PipelineError`` directly so the loop runner can't swallow
    it as just another retry-eligible failure.
    """

    needs_agent = False

    def __init__(self, *, check_stage_name: str, min_chars: int) -> None:
        self.check_stage_name = check_stage_name
        self.min_chars = min_chars

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        prev = ctx.results.get(self.check_stage_name)
        logs = ""
        if prev is not None and isinstance(prev.output, str):
            logs = prev.output

        if len(logs) < self.min_chars:
            msg = (
                f"CI failed but '{self.check_stage_name}' produced only "
                f"{len(logs)} chars of log — nothing useful to fix, aborting. "
                f"Raw: {logs!r}"
            )
            raise PipelineError(
                "assert useful logs",
                StageResult(name="assert useful logs", success=False, error=msg),
            )
        return StageResult(name="", success=True)


# --- pipeline -------------------------------------------------------------

pipeline = (
    Pipeline("check_and_fix_ci", default_model="sonnet")
    .alert(MacOSChannel())
)

# Don't start on top of uncommitted work — the fix stages would otherwise
# stage and commit unrelated edits.
pipeline.stage(
    "check clean worktree",
    RunCommand(cmd=(
        f"cd {shlex.quote(PROJECT_DIR)} && "
        'if [ -n "$(git status --porcelain)" ]; then '
        'echo "ERROR: Working tree is not clean. Commit or .gitignore these files:" && '
        'git status --short && exit 1; fi'
    )),
)

# Local test fix-loop. Cheaper to fail here than to push and burn CI minutes.
if not SKIP_LOCAL:
    pipeline.loop(
        "local tests",
        max_retries=MAX_FIX_ATTEMPTS,
        on_exhaust=fail,
        stages=[
            Stage(
                "compress local tests",
                # Generic over surefire / pytest / bats with a head+tail
                # fallback — keeps the fix prompt bounded for any project.
                CompressTestLog(
                    source_stage="run local tests",
                    app_packages=APP_PACKAGES or None,
                ),
                # Only compress on a real failure — no point compressing a
                # green log just to throw it away in the next iteration.
                when=stage_failed("run local tests"),
            ),
            Stage(
                "fix local",
                Generate(
                    model=LOCAL_FIX_MODEL,
                    prompt=(
                        f"## Working directory\n{PROJECT_DIR}\n\n"
                        "## Fix local test failure\n"
                        "The local test suite failed. Below is the compressed "
                        "failure trace (raw stdout/stderr was passed through a "
                        "format-aware extractor — Maven/Surefire, pytest, or "
                        "BATS — with a head+tail truncation fallback). Fix the "
                        "root cause and stop — the pipeline re-runs the suite "
                        "next. Do NOT commit or push.\n\n"
                        "### Compressed test failure\n"
                        "{compress local tests.output}\n\n"
                        "## Instructions\n"
                        "- Read the relevant source files before making changes\n"
                        "- Fix the root cause, not the symptom\n"
                        "- Do not disable or skip failing tests to make them pass\n"
                    ),
                    allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
                    permission_mode="acceptEdits",
                    setting_sources=["project"],
                ),
                when=stage_failed("run local tests"),
            ),
            Stage(
                "run local tests",
                RunCommand(
                    cmd=f"cd {shlex.quote(PROJECT_DIR)} && {LOCAL_TEST_CMD}",
                ),
            ),
        ],
    )

    # Commit any local fixes before pushing. Skip silently if nothing changed.
    pipeline.stage(
        "commit local fixes",
        RunCommand(cmd=(
            f"cd {shlex.quote(PROJECT_DIR)} && "
            'git add -u && '
            'if git diff --cached --quiet; then '
            '  echo "No local fixes to commit"; '
            '  exit 0; '
            'fi && '
            'git status --short && '
            'git commit -m "fix: auto-fix from check_and_fix_ci local loop"'
        )),
    )

# Fresh session for the CI block — failures here may span any commit on
# the branch and shouldn't inherit the local-loop context.
pipeline.clear_context()

pipeline.stage(
    "push all",
    RunCommand(cmd=(
        f"cd {shlex.quote(PROJECT_DIR)} && "
        'if git rev-list @{u}..HEAD --count 2>/dev/null | grep -q "^0$"; then '
        '  echo "No new commits to push"; exit 0; '
        'fi; '
        'git push'
    )),
)

# CI wait loop. Polls workflows until green; on failure, hand logs to
# Claude, commit + push the fix, re-poll. Session preserved across
# retries so Claude remembers prior fix attempts.
pipeline.loop(
    "wait ci",
    max_retries=MAX_FIX_ATTEMPTS,
    on_exhaust=fail,
    stages=[
        Stage(
            # Python slices the failing step's per-step log out of the
            # run-log zip (or falls back to slicing the per-job log by
            # ##[group] markers), then Haiku compresses what's left —
            # one bounded, signal-dense block ready for the fix prompt.
            # No second extractor pass: running CompressTestLog after
            # Haiku is destructive because its bats / pytest matchers
            # treat Haiku's clean output as raw output and drop the
            # surrounding context Haiku had already preserved.
            "isolate failing step",
            ExtractFailingStep(source_stage="check ci"),
            when=stage_failed("check ci"),
        ),
        Stage(
            "assert ci logs",
            AssertUsefulLogs(
                # Guards against an empty / nearly-empty Haiku output —
                # without enough signal there's nothing useful for the
                # fix prompt to act on.
                check_stage_name="isolate failing step",
                min_chars=MIN_USEFUL_LOG_CHARS,
            ),
            when=stage_failed("check ci"),
            on_failure=fail,
        ),
        Stage(
            "fix ci",
            Generate(
                model=CI_FIX_MODEL,
                prompt=(
                    f"## Working directory\n{PROJECT_DIR}\n\n"
                    "## Fix CI failure\n"
                    "The GitHub Actions build failed. Below is the failing "
                    "step's log — narrowed to just that step (via GitHub's "
                    "per-step log files when available, otherwise sliced "
                    "from the per-job log) and then compressed by Haiku to "
                    "keep errors, stack traces, and surrounding context "
                    "while dropping install / progress noise. Use "
                    "``git log`` / ``git diff`` to orient yourself — the "
                    "failure may span any commit on the branch.\n\n"
                    "### Failing-step log\n"
                    "{isolate failing step.output}\n\n"
                    "## Instructions\n"
                    "- Read the relevant source files before making changes\n"
                    "- Fix the root cause, not the symptom\n"
                    "- Do not disable or skip failing tests\n"
                    "- Do NOT run git commit or git push — the next stage does that\n"
                    + (
                        "- After your edits the pipeline runs the local test "
                        f"suite (``{LOCAL_TEST_CMD}``). If it fails the loop "
                        "restarts and you'll be asked to fix again — make "
                        "sure your fix passes locally first.\n"
                        if not SKIP_LOCAL
                        else ""
                    )
                ),
                allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
                permission_mode="acceptEdits",
                setting_sources=["project"],
            ),
            when=stage_failed("check ci"),
        ),
        Stage(
            "assert fix produced diff",
            AssertFixProducedDiff(project_dir=PROJECT_DIR),
            # Runs only on iterations where fix ci executed (i.e. after a
            # red check ci result). Hard-fails the loop if the LLM made
            # no edits, preventing 3 retries with identical no-op fixes.
            when=stage_failed("check ci"),
            on_failure=fail,
        ),
        *(
            [
                Stage(
                    "verify local after fix",
                    # Re-run the local test suite on the just-edited tree
                    # before we push. If it fails, the loop iteration
                    # fails and the loop restarts at fix ci — the
                    # agent's session is preserved, so it remembers what
                    # it just tried and can adjust without us having to
                    # thread the failing output through a placeholder.
                    # Catches the "fix breaks local tests" case before
                    # we burn CI minutes finding out the same thing
                    # remotely. Skipped when --arg skip_local=true.
                    RunCommand(
                        cmd=f"cd {shlex.quote(PROJECT_DIR)} && {LOCAL_TEST_CMD}",
                    ),
                    when=stage_failed("check ci"),
                ),
                Stage(
                    "compress verify local",
                    CompressTestLog(
                        source_stage="verify local after fix",
                        app_packages=APP_PACKAGES or None,
                    ),
                    when=stage_failed("verify local after fix"),
                ),
            ]
            if not SKIP_LOCAL
            else []
        ),
        Stage(
            "commit push ci fix",
            RunCommand(cmd=(
                f"cd {shlex.quote(PROJECT_DIR)} && "
                'git add -u && '
                'git status --short && '
                'git commit -m "fix(ci): auto-fix from check_and_fix_ci" && '
                'git push'
            )),
            when=stage_failed("check ci"),
        ),
        Stage(
            "check ci",
            CheckCIWithLogs(
                workflows=WORKFLOWS,
                poll=True,
                poll_interval=60,
                timeout_minutes=20,
            ),
        ),
    ],
)


config = pipeline

"""Push the current branch, wait for CI, and auto-fix
failures by feeding GitHub Actions logs back to Claude.

Adapted from ``egnedata/egnedata-kmp/tmp/check.py`` and trimmed for the
norn repo:

  * No plan-step loop — operates on whatever is already committed on the
    current branch. Use the ``implement_features`` pipeline if you need
    plan-driven step implementation; this pipeline is for the
    "I have commits, make CI green" use case.
  * Workflow gate (``ci.yml``) — override with ``--arg workflow=...``, a
    comma-separated list to check more than one workflow in order.
  * Local validation with ``uv run python -m pytest tests/ -v`` before
    ever touching CI. Override with ``--arg test_cmd=...``.
  * Same safeguards as the egnedata version: ``MIN_USEFUL_LOG_CHARS``
    bail, identical-logs-across-runs abort, ``MAX_FIX_ATTEMPTS`` cap.

Run with::

    norn run check_and_fix_ci_v2
    norn run check_and_fix_ci_v2 --arg workflow=ci.yml
    norn run check_and_fix_ci_v2 --arg workflow=ci.yml,release.yml
    norn run check_and_fix_ci_v2 --arg test_cmd="uv run python -m pytest tests/test_runner.py -v"
    norn run check_and_fix_ci_v2 --arg skip_local=true
    norn run check_and_fix_ci_v2 --arg ci_timeout_minutes=60

Set ``FIX_CI_LOOP_VERBOSE=1`` to dump full extracted CI logs to stderr.

Optional ``--arg`` knobs (not in ``metadata["args"]``, so ``norn describe`` and
the TUI launcher do not prompt for them):

- ``--arg learn=on|off``       — propose a learning once CI is green (default
  ``on``; anything else is rejected at import).  ``off`` drops every learn
  stage and the recorders, but ``assert nothing unpushed`` stays.
- ``--arg learn_model=<label>`` — intent label for the learn agent (default
  ``light``; checked against ``norn/model_labels.toml`` at import).

Learning proposal
-----------------
After ``wait ci`` the pipeline looks at the fix commits it made
(``fix(ci): auto-fix from check_and_fix_ci`` and ``fix: auto-fix from
check_and_fix_ci local loop``).  With none, or with no run journal, it records
a skip and no agent runs.  Otherwise ``collect learning evidence`` assembles
the failed attempts (recorded in the loops by ``record local attempt`` and
``record ci attempt``), the fix commits' diffs and the related learnings;
``learn`` writes at most one learning in the Compound Engineering schema; and
``decide learning`` validates it.  The proposal lands in
``<run dir>/learning.md`` plus ``learning-target``; **nothing is committed,
staged or pushed** — a human copies it into the repository.  The learn stages
never fail the run (they record ``failed: <reason>``), except
``learn postcondition``, which fails when the repository itself changed during
them.  Scratch lives in a temp work root, never in the repository.
``assert nothing unpushed`` closes the run: it fails when the branch has no
upstream, unpushed commits or a dirty tree, so a caller such as
``merge_branch_v2`` can rely on the pushed head being the one CI checked.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import date
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

    print("[check_and_fix_ci_v2] installing githubkit into active venv...", file=sys.stderr)
    subprocess.check_call(
        ["uv", "pip", "install", "--quiet", "--python", sys.executable, "githubkit"],
    )
    import githubkit  # noqa: F401


_ensure_githubkit()

from norn.alerts import MacOSChannel
from norn.dsl import Pipeline, Stage, fail, file_exists, stage_failed
from norn.models import PipelineContext, StageResult
from norn.runner import PipelineError
from norn.stages.base import BaseStage
from norn.stages.check_ci import CheckCI
from norn.stages.compress_test_log import CompressTestLog
from norn.stages.extract_failing_step import ExtractFailingStep
from norn.stages.generate import Generate
from norn.stages.run_command import RunCommand
from norn.pipelines._comment_style import COMMENT_RULE, attribution_guard_sh
from norn.pipelines._find_related import find_related
from norn.pipelines._learnings import (
    LearnGenerate,
    LearnOutcomeStage,
    LearnPaths,
    StoreConfigError,
    decide_learning,
    existing_categories,
    keywords_from,
    learn_prompt,
    marker_reason,
    outcome_text,
    publish_proposal,
    resolve_store,
    store_hint,
    store_relpath,
    write_marker,
)
from norn.pipelines._test_cmd import detect_test_cmd
from norn.profiles import build_write_allowlist_hooks


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

DEFAULT_CI_TIMEOUT_MINUTES = 20

VERBOSE = os.environ.get("FIX_CI_LOOP_VERBOSE", "").lower() in ("1", "true", "yes")

metadata = {
    "env_vars": ["ANTHROPIC_API_KEY", "GH_TOKEN"],
    "args": {
        "workflow": (
            "Comma-separated GitHub Actions workflow filenames, checked in "
            "order (default: ci.yml)"
        ),
        "test_cmd": "Local test command (default: auto-detected from project markers)",
        "skip_local": "Set to true to skip the local test loop and go straight to CI",
        "ci_timeout_minutes": (
            "Minutes to wait for each workflow run before giving up (default: 20)"
        ),
        "app_packages": (
            "Comma-separated app package globs for the Surefire log compressor "
            "(e.g. 'com.e4marine.*,com.wilhelmsen.*'). Frames matching these "
            "are always preserved when Haiku compresses the failure trace. "
            "Omit to let Haiku infer from the trace."
        ),
    },
}


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
WORKFLOWS = [w.strip() for w in _args.get("workflow", "ci.yml").split(",") if w.strip()]

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


def _positive_int_arg(name: str, default: int) -> int:
    raw = _args.get(name, str(default)).strip()
    if not raw.isdigit() or int(raw) < 1:
        raise ValueError(f"--arg {name}: must be a positive whole number, got {raw!r}")
    return int(raw)


CI_TIMEOUT_MINUTES = _positive_int_arg("ci_timeout_minutes", DEFAULT_CI_TIMEOUT_MINUTES)

# Model selection — both fix stages do code reasoning over a failure
# trace and produce edits across multiple files, so Sonnet is the right
# tier (Haiku is too weak to reliably trace multi-file failures; Opus is
# expensive overkill for typical pytest / CI errors). Override per call
# with --arg local_fix_model=deep when stuck on a hard bug.
LOCAL_FIX_MODEL = _args.get("local_fix_model", "standard")
CI_FIX_MODEL = _args.get("ci_fix_model", "standard")


def _require_knob_model(name: str, default: str) -> str:
    from norn.model_labels import labels_for as _labels_for
    _eligible = set(_labels_for("claude-code")) | set(_labels_for("opencode"))
    raw = _args.get(name, default)
    if raw not in _eligible:
        raise ValueError(
            f"--arg {name}: must be a Generate-eligible label "
            f"({', '.join(sorted(_eligible))}), got {raw!r}"
        )
    return raw


LEARN = _args.get("learn", "on")
if LEARN not in ("on", "off"):
    raise ValueError(f"--arg learn: must be 'on' or 'off', got {LEARN!r}")
LEARN_ON = LEARN == "on"
LEARN_MODEL = _require_knob_model("learn_model", "light")


def _effective_agent_provider(argv: list[str]) -> str:
    """The provider the run will use, resolved the way the CLI resolves it.

    SDK hooks are a Claude Code feature, so the learn write-guard is only
    attached when it can run.
    """
    for i, token in enumerate(argv):
        if token == "--agent-provider" and i + 1 < len(argv):
            return argv[i + 1]
        if token.startswith("--agent-provider="):
            return token.split("=", 1)[1]
    return os.environ.get("NORN_AGENT_PROVIDER") or "claude-code"


AGENT_PROVIDER = _effective_agent_provider(sys.argv[1:])

print(f"[check_and_fix_ci_v2] test_cmd       = {LOCAL_TEST_CMD}", file=sys.stderr)
print(f"[check_and_fix_ci_v2] local_fix_model = {LOCAL_FIX_MODEL}", file=sys.stderr)
print(f"[check_and_fix_ci_v2] ci_fix_model    = {CI_FIX_MODEL}", file=sys.stderr)
print(
    f"[check_and_fix_ci_v2] app_packages    = "
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
    just burn tokens. Also aborts when the poll times out: a run that is
    still going has not failed, so there are no logs to fix.

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
                f"[check_and_fix_ci_v2] checking workflow {workflow!r} "
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
                f"[check_and_fix_ci_v2]   → {workflow} {status} "
                f"(run_id={run_id}, conclusion={conclusion}) {url}",
                file=sys.stderr,
                flush=True,
            )

            if result.success:
                continue

            if output.get("timed_out"):
                msg = (
                    f"{result.error} Watched for {workflow}: "
                    f"{output.get('name')} run {run_id} ({url}). The run has "
                    "not failed, so there is nothing to fix. Rerun once it "
                    "finishes, or raise --arg ci_timeout_minutes."
                )
                raise PipelineError(
                    "check ci",
                    StageResult(name="check ci", success=False, error=msg),
                )

            if VERBOSE:
                print(
                    f"[check_and_fix_ci_v2] ----- captured logs for {workflow} "
                    f"({len(logs)} chars) -----",
                    file=sys.stderr,
                    flush=True,
                )
                print(logs, file=sys.stderr, flush=True)
                print(
                    f"[check_and_fix_ci_v2] ----- end logs for {workflow} -----",
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
        # `git status --porcelain` lists new files too, matching the
        # `git add -A` the commit stage runs; `git diff` would call a fix
        # that only adds a file "no edits".
        rc, status = await _capture(
            "git", "-C", self.project_dir, "status", "--porcelain",
        )
        if rc != 0:
            raise PipelineError(
                "assert fix produced diff",
                StageResult(
                    name="assert fix produced diff",
                    success=False,
                    error=f"git status failed in {self.project_dir}",
                ),
            )
        if not status:
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


# --- learning proposal: work root, recorders and learn stages --------------

# Scratch for the learn stages and the attempt ledger.  Outside the repository
# on purpose (nothing this pipeline records may enter the target repo); the
# directory is keyed by the launch directory so two checkouts never share it.
WORK_ROOT = Path(tempfile.gettempdir()) / "norn-learn" / "check_and_fix_ci_v2" / (
    hashlib.sha1(PROJECT_DIR.encode("utf-8")).hexdigest()[:12]
)
START_SHA = WORK_ROOT / "start.sha"
ATTEMPTS_MD = WORK_ROOT / "attempts.md"
LEARN_EVIDENCE = WORK_ROOT / "learn-evidence.md"
LEARN_PATHS = LearnPaths(WORK_ROOT)

# The two commit subjects this pipeline gives its fixes.
LOCAL_FIX_SUBJECT = "fix: auto-fix from check_and_fix_ci local loop"
CI_FIX_SUBJECT = "fix(ci): auto-fix from check_and_fix_ci"
FIX_SUBJECTS = (CI_FIX_SUBJECT, LOCAL_FIX_SUBJECT)

PREPARE_SCRATCH_STAGE = "prepare learning scratch"
RECORD_LOCAL_STAGE = "record local attempt"
RECORD_CI_STAGE = "record ci attempt"
LEARN_COLLECT_STAGE = "collect learning evidence"
LEARN_AGENT_STAGE = "learn"
LEARN_POSTCONDITION_STAGE = "learn postcondition"
LEARN_DECIDE_STAGE = "decide learning"
LEARN_PUBLISH_STAGE = "publish learning proposal"
LEARN_SUMMARY_STAGE = "learning summary"
ASSERT_PUSHED_STAGE = "assert nothing unpushed"

TIMEOUT_LEARN = 300   # applied inside LearnGenerate, never by the runner
MAX_TURNS_LEARN = 4
LEARN_NO_JOURNAL = "no journal to hold the proposal"
LEARN_NO_FIX = "CI was green without a fix in this run"

# Evidence caps.
ATTEMPT_EXCERPT_LINES = 80
COMMIT_PATCH_LINES = 150
LEARN_RELATED_FULL = 3
LEARN_RELATED_FULL_LINES = 300
LEARN_RELATED_HEADS = 7
LEARN_RELATED_HEAD_LINES = 30

_EVIDENCE_FENCE = "~~~~~~"


def _fenced(text: str) -> list[str]:
    body = text.rstrip("\n")
    return [_EVIDENCE_FENCE, body if body else "(empty)", _EVIDENCE_FENCE, ""]


def _cap_lines(text: str, limit: int, *, keep: str = "head") -> str:
    """Keep the first (``head``) or last (``tail``) *limit* lines of *text*."""
    lines = text.splitlines()
    if len(lines) <= limit:
        return text
    cut = len(lines) - limit
    if keep == "tail":
        return f"… ({cut} earlier lines cut)\n" + "\n".join(lines[-limit:])
    return "\n".join(lines[:limit]) + f"\n… ({cut} more lines cut)"


def _repo_git(*args: str) -> str:
    """Run git in the launch directory; a failure raises."""
    proc = subprocess.run(
        ["git", "-C", PROJECT_DIR, *args], capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout


def _toplevel() -> str:
    return _repo_git("rev-parse", "--show-toplevel").strip()


class PrepareLearningScratch(BaseStage):
    """``prepare learning scratch``: empty the work root and record HEAD.

    ``start.sha`` is what ``collect learning evidence`` later reads to find the
    fixes this run made.  A failure here fails the run: it happens before any
    work has started.
    """

    needs_agent = False

    def __init__(self, *, work_root: Path) -> None:
        self.work_root = work_root

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        head = _repo_git("rev-parse", "HEAD").strip()
        if self.work_root.exists():
            shutil.rmtree(self.work_root)
        self.work_root.mkdir(parents=True)
        (self.work_root / "start.sha").write_text(head + "\n", encoding="utf-8")
        return StageResult(name="", success=True, output=f"start {head}")


class RecordAttempt(BaseStage):
    """Append the failed attempt's excerpt to ``attempts.md`` in the work root.

    Runs at the top of the attempt after a failure, so the attempt it records
    is ``attempt - 1``.  ``emits_events`` is set only so the runner passes the
    ``attempt`` kwarg to a non-agent stage; it emits no events.  ``attempt`` has
    no default on purpose: a runner that stopped passing it must raise instead
    of recording attempt 1 every time.
    """

    needs_agent = False
    emits_events = True

    def __init__(
        self,
        *,
        kind: str,
        source_stage: str,
        ledger: Path,
        workflow_stage: str | None = None,
    ) -> None:
        self.kind = kind
        self.source_stage = source_stage
        self.ledger = ledger
        self.workflow_stage = workflow_stage

    def _workflow(self, ctx: PipelineContext) -> str:
        if self.workflow_stage is None:
            return "local tests"
        prev = ctx.results.get(self.workflow_stage)
        for line in str(prev.output if prev is not None else "").splitlines():
            if line.startswith("Failing workflow:"):
                return line.partition(":")[2].strip()
        return "unknown"

    async def run(self, ctx: PipelineContext, *, node_id, attempt, **_: Any) -> StageResult:
        failed = attempt - 1
        source = ctx.results.get(self.source_stage)
        excerpt = str(source.output) if source is not None and source.output else ""
        entry = [
            f"## {self.kind} attempt {failed}",
            "",
            f"Workflow: {self._workflow(ctx)}",
            "",
            *_fenced(_cap_lines(excerpt, ATTEMPT_EXCERPT_LINES)),
        ]
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(entry) + "\n")
        return StageResult(name="", success=True, output=f"recorded {self.kind} attempt {failed}")


def _fix_commits(start: str) -> list[str]:
    """Shas in ``start..HEAD`` whose subject is one of the two fix subjects."""
    out = _repo_git("log", "--reverse", "--format=%H%x09%s", f"{start}..HEAD")
    shas: list[str] = []
    for line in out.splitlines():
        sha, _, subject = line.partition("\t")
        if subject in FIX_SUBJECTS:
            shas.append(sha)
    return shas


def _related_section(store: Path, toplevel: str, keywords: list[str]) -> list[str]:
    categories = existing_categories(store)
    out = [
        "## Related learnings",
        "",
        f"Store: `{store_relpath(toplevel, store)}`",
        "Keywords: " + (", ".join(keywords) or "none"),
        "Existing category directories: " + (", ".join(f"`{c}`" for c in categories) or "none"),
        "",
    ]
    if not store.is_dir():
        return out + ["store does not exist", ""]
    candidates = find_related(store, keywords)[: LEARN_RELATED_FULL + LEARN_RELATED_HEADS]
    if not candidates:
        return out + ["none found", ""]
    for i, cand in enumerate(candidates):
        rel = os.path.relpath(cand.path, toplevel)
        matched = ", ".join(cand.matched)
        if i < LEARN_RELATED_FULL:
            lines = Path(cand.path).read_text(encoding="utf-8", errors="replace").splitlines()
            if len(lines) <= LEARN_RELATED_FULL_LINES:
                out += [f"### `{rel}` — full text (matched: {matched})", ""]
                out += _fenced("\n".join(lines))
            else:
                out += [
                    f"### `{rel}` — first {LEARN_RELATED_FULL_LINES} lines only, not the "
                    f"full text (matched: {matched})",
                    "",
                ]
                out += _fenced("\n".join(lines[:LEARN_RELATED_FULL_LINES]))
        else:
            out += [
                f"### `{rel}` — first {LEARN_RELATED_HEAD_LINES} lines only (matched: {matched})",
                "",
            ]
            out += _fenced("\n".join(cand.head[:LEARN_RELATED_HEAD_LINES]))
    return out


def _learning_evidence(store: Path, toplevel: str, start: str, fixes: list[str]) -> str:
    """Assemble the learn agent's evidence (blocking: run it in a thread)."""
    parts = ["# Learning evidence", "", f"Start: {start}", ""]
    parts += ["## Failed attempts", ""]
    if ATTEMPTS_MD.is_file():
        parts += [ATTEMPTS_MD.read_text(encoding="utf-8", errors="replace").rstrip("\n"), ""]
    else:
        parts += ["no failed attempt was recorded", ""]
    parts += ["## Fix commits", ""]
    changed: list[str] = []
    for sha in fixes:
        shown = _repo_git("show", "--stat", "--patch", "--format=%h %s", sha)
        parts += [f"### {sha[:12]}", ""] + _fenced(_cap_lines(shown, COMMIT_PATCH_LINES))
        for path in _repo_git("show", "--name-only", "--format=", sha).splitlines():
            if path and path not in changed:
                changed.append(path)
    keywords = keywords_from(list(WORKFLOWS), changed)
    parts += _related_section(store, toplevel, keywords)
    return "\n".join(parts).rstrip("\n") + "\n"


class CollectLearningEvidence(LearnOutcomeStage):
    """``collect learning evidence``: reset, find the fixes, build the evidence.

    No fix commit, or no journal, writes ``learn.skipped``; an unusable
    ``docs_root`` writes ``learn.failed``.  Either way collection stops without
    ``learn.pre-head``, so the agent, the postcondition and the decision skip.
    Otherwise ``learn.pre-head`` is its last write, so "the baseline exists"
    means "collection finished and the agent may run".
    """

    async def _run(self, ctx: PipelineContext) -> str:
        paths = self.paths
        paths.reset()
        start = START_SHA.read_text(encoding="utf-8").strip()
        fixes = await asyncio.to_thread(_fix_commits, start)
        if not fixes:
            write_marker(paths.marker_skipped, LEARN_NO_FIX)
            return f"skipped: {LEARN_NO_FIX}"
        if ctx.journal is None:
            write_marker(paths.marker_skipped, LEARN_NO_JOURNAL)
            return f"skipped: {LEARN_NO_JOURNAL}"
        toplevel = await asyncio.to_thread(_toplevel)
        try:
            store = resolve_store(toplevel)
        except StoreConfigError as exc:
            reason = f"the learning store is unusable: {exc}"
            write_marker(paths.marker_failed, reason)
            return f"failed: {reason}"
        evidence = await asyncio.to_thread(_learning_evidence, store, toplevel, start, fixes)
        LEARN_EVIDENCE.write_text(evidence, encoding="utf-8")
        paths.scratch.mkdir(parents=True, exist_ok=True)
        head = await asyncio.to_thread(_repo_git, "rev-parse", "HEAD")
        paths.pre_head.write_text(head.strip() + "\n", encoding="utf-8")
        return evidence


def _learn_prompt_now() -> str:
    """The learn prompt for the store as it is when the agent runs."""
    toplevel = _toplevel()
    store = resolve_store(toplevel)
    return learn_prompt(
        evidence_stage=LEARN_COLLECT_STAGE,
        paths=LEARN_PATHS,
        store_rel=store_relpath(toplevel, store),
        categories=existing_categories(store),
        today=date.today().isoformat(),
    )


class LearnAgent(LearnGenerate):
    """``learn``: the prompt is built when the stage runs, not at import.

    An unusable ``docs_root`` must not stop the module from importing:
    collection reports it as the outcome.  ``prompt`` holds a preview.
    """

    def _resolve_prompt(self, ctx: PipelineContext) -> str:
        return self._resolve(_learn_prompt_now(), ctx)


_LEARN_PROMPT_PREVIEW = learn_prompt(
    evidence_stage=LEARN_COLLECT_STAGE,
    paths=LEARN_PATHS,
    store_rel="<store>",
    categories=["<the store's category directories when the stage runs>"],
    today="<today>",
)

# The learn agent may write only its three scratch files.  Claude Code only;
# under another provider `learn postcondition` is what catches a write into
# the repository.
_LEARN_WRITE_GUARD = (
    build_write_allowlist_hooks([str(p) for p in LEARN_PATHS.writable])
    if AGENT_PROVIDER == "claude-code"
    else None
)

_learn_postcondition_cmd = (
    f"cd {shlex.quote(PROJECT_DIR)} && "
    'if [ -n "$(git status --porcelain)" ]; then '
    'echo "ERROR: the learn stages changed the working tree:" 1>&2 && '
    'git status --short 1>&2 && exit 1; fi && '
    f'pre=$(cat {shlex.quote(str(LEARN_PATHS.pre_head))}) && '
    'head=$(git rev-parse HEAD) && '
    'if [ "$head" != "$pre" ]; then '
    'echo "ERROR: HEAD moved during the learn stages: $pre -> $head" 1>&2 && exit 1; fi'
)

_assert_nothing_unpushed_cmd = (
    f"cd {shlex.quote(PROJECT_DIR)} && "
    'if ! git rev-parse "@{u}" >/dev/null 2>&1; then '
    'echo "ERROR: the current branch has no upstream; the fixes are not pushed." 1>&2 && exit 1; fi && '
    'n=$(git rev-list --count "@{u}..HEAD") && '
    'if [ "$n" != "0" ]; then '
    'echo "ERROR: $n unpushed commit(s) on the current branch." 1>&2 && exit 1; fi && '
    'if [ -n "$(git status --porcelain)" ]; then '
    'echo "ERROR: the working tree is not clean:" 1>&2 && git status --short 1>&2 && exit 1; fi && '
    'echo "nothing unpushed"'
)


class DecideLearning(LearnOutcomeStage):
    """``decide learning``: validate the agent's result; note the instruction file."""

    async def _run(self, ctx: PipelineContext) -> str:
        toplevel = await asyncio.to_thread(_toplevel)
        store_rel = store_relpath(toplevel, resolve_store(toplevel))
        outcome = await asyncio.to_thread(
            decide_learning,
            self.paths,
            repo=toplevel,
            store_rel=store_rel,
            base_sha=START_SHA.read_text(encoding="utf-8").strip(),
        )
        if outcome == "ready":
            hint = store_hint(toplevel, store_rel)
            if hint is not None:
                self.paths.store_hint.write_text(hint + "\n", encoding="utf-8")
        marker = {
            "ready": self.paths.marker_ready,
            "skipped": self.paths.marker_skipped,
            "failed": self.paths.marker_failed,
        }[outcome]
        return f"{outcome}: {marker_reason(marker)}"


class PublishLearningProposal(LearnOutcomeStage):
    """``publish learning proposal``: copy it into the run directory."""

    async def _run(self, ctx: PipelineContext) -> str:
        return f"proposed at {publish_proposal(self.paths, ctx.journal.dir)}"


class LearningSummary(LearnOutcomeStage):
    """``learning summary``: ``outcome.md``, whichever marker exists."""

    async def _run(self, ctx: PipelineContext) -> str:
        text = outcome_text(self.paths)
        self.paths.outcome.write_text(text + "\n", encoding="utf-8")
        return text


# --- pipeline -------------------------------------------------------------

pipeline = (
    Pipeline("check_and_fix_ci_v2", default_model="standard")
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

if LEARN_ON:
    pipeline.stage(PREPARE_SCRATCH_STAGE, PrepareLearningScratch(work_root=WORK_ROOT))

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
            *(
                [
                    Stage(
                        RECORD_LOCAL_STAGE,
                        RecordAttempt(
                            kind="local",
                            source_stage="compress local tests",
                            ledger=ATTEMPTS_MD,
                        ),
                        when=stage_failed("run local tests"),
                    )
                ]
                if LEARN_ON
                else []
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
                        f"{COMMENT_RULE}\n"
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
            'git add -A && '
            'if git diff --cached --quiet; then '
            '  echo "No local fixes to commit"; '
            '  exit 0; '
            'fi && '
            'git status --short && '
            f"{attribution_guard_sh()} && "
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
        *(
            [
                Stage(
                    RECORD_CI_STAGE,
                    RecordAttempt(
                        kind="ci",
                        source_stage="isolate failing step",
                        ledger=ATTEMPTS_MD,
                        workflow_stage="check ci",
                    ),
                    when=stage_failed("check ci"),
                )
            ]
            if LEARN_ON
            else []
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
                    + f"{COMMENT_RULE}\n"
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
                'git add -A && '
                'git status --short && '
                f"{attribution_guard_sh(messages_range='@{u}..HEAD')} && "
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
                timeout_minutes=CI_TIMEOUT_MINUTES,
            ),
        ),
    ],
)

if LEARN_ON:
    pipeline.clear_context()

    pipeline.stage(
        LEARN_COLLECT_STAGE,
        CollectLearningEvidence(paths=LEARN_PATHS, name=LEARN_COLLECT_STAGE),
        on_failure=fail,   # never fails: LearnOutcomeStage
    )

    # No runner timeout: LearnGenerate applies TIMEOUT_LEARN itself and turns
    # a timeout into an outcome, which a runner timeout could not do.
    pipeline.stage(
        LEARN_AGENT_STAGE,
        LearnAgent(
            paths=LEARN_PATHS,
            timeout=TIMEOUT_LEARN,
            prompt=_LEARN_PROMPT_PREVIEW,
            allowed_tools=["Write"],
            permission_mode="acceptEdits",
            add_dirs=[str(LEARN_PATHS.scratch)],
            max_turns=MAX_TURNS_LEARN,
            model=LEARN_MODEL,
            hooks=_LEARN_WRITE_GUARD,
        ),
        when=file_exists(str(LEARN_PATHS.pre_head)),
        on_failure=fail,   # never fails: LearnGenerate
    )

    pipeline.stage(
        LEARN_POSTCONDITION_STAGE,
        RunCommand(cmd=_learn_postcondition_cmd),
        when=file_exists(str(LEARN_PATHS.pre_head)),
        on_failure=fail,   # the one integrity exception to decision 5
    )

    pipeline.stage(
        LEARN_DECIDE_STAGE,
        DecideLearning(paths=LEARN_PATHS, name=LEARN_DECIDE_STAGE),
        when=file_exists(str(LEARN_PATHS.pre_head)),
        on_failure=fail,   # never fails: LearnOutcomeStage
    )

    pipeline.stage(
        LEARN_PUBLISH_STAGE,
        PublishLearningProposal(paths=LEARN_PATHS, name=LEARN_PUBLISH_STAGE),
        when=file_exists(str(LEARN_PATHS.marker_ready)),
        on_failure=fail,   # never fails: LearnOutcomeStage
    )

    pipeline.stage(
        LEARN_SUMMARY_STAGE,
        LearningSummary(paths=LEARN_PATHS, name=LEARN_SUMMARY_STAGE),
        on_failure=fail,   # never fails: LearnOutcomeStage
    )

# A contract of the CI pipeline, not a learn stage, so it stays fatal with
# learn=off too: merge_branch_v2's ci_fix_unpushed guard relies on it.  No
# learn stage commits, stages or pushes anything.
pipeline.stage(
    ASSERT_PUSHED_STAGE,
    RunCommand(cmd=_assert_nothing_unpushed_cmd),
    on_failure=fail,
)


config = pipeline

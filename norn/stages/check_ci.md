# `CheckCI` stage — how it works

Inspect the latest GitHub Actions run for a branch and return a compact,
LLM-prompt-sized extract of the failure region. Pure-Python, no LLM
invocation, no per-call cost beyond GitHub API quota.

This document explains the moving parts in `check_ci.py` so you can
debug, tune, or extend the stage without re-reading the whole file.

---

## 1. Public surface

```python
from norn.stages.check_ci import CheckCI

CheckCI(
    repo=None,                # "owner/name"; auto-detected from git remote
    branch=None,              # auto-detected from `git branch --show-current`
    workflow=None,            # filename ("ci.yml") or workflow id; None = any
    poll=False,               # one-shot vs. poll-until-completed
    poll_interval=30,         # seconds between polls
    timeout_minutes=30,       # poll timeout
    summarize=True,           # apply layered log extraction
    context_lines=20,         # context for the marker/keyword fallback pass
    max_log_lines=400,        # cap on returned log lines
)
```

`StageResult.output` is a `dict`:

```python
{
    "run_id": int,
    "name": str,             # workflow display name
    "status": str,           # "completed", "in_progress", ...
    "conclusion": str | None, # "success", "failure", ...
    "url": str,              # html_url to the run
    "logs": str,             # summarised failed-step logs ("" on green)
}
```

`StageResult.success` is `True` iff `conclusion == "success"`.
`StageResult.error` is a one-liner (`"CI failure: <name> (<url>)"`); the
full log lives only in `output["logs"]`.

---

## 2. Execution flow

The full happy path is in `CheckCI.run()` (lines 471-560). In order:

1. **Import guard** — bail with a clear error if `githubkit` isn't
   installed (`pip install 'norn[github]'`).
2. **Token resolution** — `_resolve_token()` tries `GH_TOKEN`,
   `GITHUB_TOKEN`, then shells out to `gh auth token`. Returns `None`
   if all three miss, which fails the stage with a clear message.
3. **Repo + branch detection** — `_detect_repo()` parses the
   `origin` remote (handles both `git@github.com:owner/repo.git` and
   `https://github.com/owner/repo.git`); `_current_branch()` uses
   `git branch --show-current`.
4. **Client construction** — `_create_client(token)` builds a
   `githubkit.GitHub(token)`. Factored out for test injection.
5. **Poll loop** — until `status == "completed"`, sleep
   `poll_interval` seconds and refetch. The deadline is computed once
   at entry as `loop_time + timeout_minutes*60`.
   * Each poll calls `_get_latest_run`.
   * On `poll=False` and a non-completed status, return immediately
     with `success=False` and a status-explaining error — the stage
     does **not** wait.
6. **Log extraction (only on red)** — when the run is `completed` and
   `conclusion != "success"`, call `_get_failed_logs(...)` to fetch
   per-job logs, then optionally pass the result through
   `_summarize_log(...)`.
7. **Return** — package run metadata + (possibly summarised) logs into
   the dict above.

---

## 3. GitHub API calls

Each call to `CheckCI.run()` spends GitHub API quota as follows:

| When | Endpoint | Cost |
|------|----------|------|
| Every poll cycle | `actions.list_workflow_runs[_for_repo]` (per_page=1) | 1 |
| Poll while in_progress | repeats above | 1 per poll |
| On red verdict | `actions.list_jobs_for_workflow_run` (per_page=100) | 1 |
| On red verdict, per failed job | `actions.download_job_logs_for_workflow_run` | 1 per failed job |
| Fallback when a per-job download fails | `actions.download_workflow_run_logs` (full zip) | 1 |

The polling loop is cheap (one list call per interval). The expensive
calls are the per-job log downloads — those count against the **secondary
rate limit on Actions log downloads**, which is much stricter than the
5000/hour primary quota. If you're getting rate-limited, the suspect is
almost always the log downloads, not the polling itself.

---

## 4. Log fetching (`_get_failed_logs`, lines 598-633)

Given a red run id:

1. **List jobs** — `async_list_jobs_for_workflow_run(per_page=100)`.
2. **Filter to failed** — keep jobs whose `conclusion` is `"failure"` or
   `"cancelled"`. (Jobs that succeeded or were skipped contribute
   nothing actionable.)
3. **Per failed job, append:**
   * A `## Failed job: <name>` heading.
   * `Failed steps: <comma-separated step names>` if any steps within
     the job were red.
   * The raw plaintext log (`async_download_job_logs_for_workflow_run`).
4. **Per-job fallback** — if the per-job download throws (e.g. the
   blob-storage redirect URL has expired), fall back to the run-level
   zip via `_extract_run_logs` → `async_download_workflow_run_logs`,
   unzip with the stdlib `zipfile`, concatenate every `.txt` step file.

The aggregated string (one block per failed job) is what the summariser
operates on next. Note: this is the **raw GitHub Actions log**,
including ISO-8601 timestamps and `##[group]`/`##[command]` markers.

---

## 5. Log summarisation (`_summarize_log`, lines 636-774)

This is where most of the value lives. The raw logs from a real CI
failure can be megabytes; the summariser reduces them to a block
small enough to drop into a follow-up LLM prompt without losing the
actual failure context.

Four passes, each only attempted if the previous one produced nothing:

### Pass 1 — Cleaning

Strip noise that's never useful in any extraction:

* ISO-8601 timestamps prefixed by the runner
  (`2026-04-26T12:54:13.5656911Z `).
* Pure marker lines: `##[command]…`, `##[debug]…`, `##[endgroup]`.
* `##[group]` is **stripped as a prefix** rather than dropping the line —
  test runners like Flutter emit `##[group]❌ path.dart …` where the
  failure signal IS the line content.
* Noise prefixes (git housekeeping, `Temporarily overriding HOME=…`,
  `[command]/usr/bin/git …`).
* Hard truncate at the first line containing `Post job cleanup.` or
  `Cleaning up orphan processes` — both are guaranteed to mark the
  end of useful job output.

### Pass 2 — Tool-specific anchor pass (primary)

Scan every cleaned line against `_FAILURE_ANCHORS` (lines 79-139), a
list of `(tool_name, regex)` tuples. The **earliest matching line wins**;
output is `cleaned[anchor_idx - lead_context:]` capped at
`max_log_lines`, prepended with `(detected: <tool_name>)`.

Currently covered:

| Family | Tools |
|--------|-------|
| JVM | Maven (`[INFO] BUILD FAILURE`), Gradle (`FAILURE: Build failed with an exception`), Ant (`BUILD FAILED`), sbt |
| Rust | `error[Ennnn]:`, `error: could not compile`, `error: aborting due to N previous errors` |
| Go | `--- FAIL:`, `FAIL\tpkg\t0.Xs` |
| Python | pytest `FAILURES`/`ERRORS`/`short test summary`, unittest `FAIL/ERROR`, ruff, flake8 |
| JS/TS | Jest (`FAIL …test.[jt]sx?`, `Test Suites: …failed`), npm (`npm ERR!`), yarn, tsc (`error TSnnnn`), ESLint |
| .NET | MSBuild `Build FAILED.`, `error (CS|MSB)nnnn:` |
| Native | CMake `CMake Error`, Bazel, xcodebuild `** BUILD FAILED **`, GCC/Clang `file:line:col: error:` |
| Docker | buildx, BuildKit `failed to solve`, `failed to build` (with optional `##[error]` prefix) |
| Flutter/Dart | `❌ path.dart:` (after group-prefix strip), `N tests passed, M failed`, `EXCEPTION CAUGHT BY FLUTTER`, Dart analyzer |

**Adding a new ecosystem is one line** in `_FAILURE_ANCHORS` plus a unit
test in `tests/test_check_ci.py`. Prefer highly specific literals.

### Pass 3 — Marker + keyword fallback

If no anchor matched, find lines that either start with `##[error]` or
contain any of `_ERROR_KEYWORDS`
(`error:`, `exception`, `traceback`, `failed`, `failure`, `fatal:`,
`panic:`, `build failed`, `test failed`, `assertionerror`).

For each hit, take `context_lines` lines before and roughly
`context_lines/2` after, then **merge overlapping windows** so adjacent
hits collapse into one section. Sections are joined with `…`
separators.

Warnings are intentionally **excluded** from the keyword set —
deprecation notices drown out real failures.

### Pass 4 — Tail (last resort)

If none of the above produced anything (rare, e.g. a corrupt or
unanchored log), return the last `max_log_lines` lines of the cleaned
log with a `(no error markers found — showing last N lines)` header.

---

## 6. Caveats and edge cases

* **Re-runs**: only the latest attempt is returned (the API endpoint
  exposes only that one in the per_page=1 listing).
* **In-progress runs without `poll=True`**: stage fails immediately
  with the run url in the error; downstream stages can decide whether
  to retry, ask the user, or treat it as success-pending.
* **Path filters / no new run**: when a push doesn't trigger a new run
  (path-filter exclusion, no commits), `_get_latest_run` will return a
  historical run. The CheckCI stage cannot tell that the run is "stale"
  by itself — that comparison (run.head_sha vs. local HEAD) is left to
  callers, e.g. the dogfooding pipeline's `CheckCIWithLogs` wrapper.
* **Detached HEAD**: `_current_branch` returns an empty string and the
  stage fails with a clear error. Pass `branch=` explicitly in that
  case.
* **Large logs**: per-job logs are fetched into memory. A multi-megabyte
  log is fine; a multi-gigabyte log will OOM. The anchor pass keeps the
  *output* compact regardless of input size, but the raw fetch still
  has to fit in RAM.
* **`max_log_lines=400` is conservative.** If you suspect the
  summariser is cutting off the relevant context (the anchor matched
  AFTER the real failure cause), bump `max_log_lines` or set
  `summarize=False` to bypass extraction entirely. Then the raw
  per-job log is returned, capped only by available memory.

---

## 7. Authentication scopes

* **Public repos** — no scopes required. Unauthenticated works but is
  heavily rate-limited; always provide a token in practice.
* **Private repos** — `repo` (classic PAT) or
  `actions:read + contents:read` (fine-grained PAT).

`gh auth login` already grants enough scope for both lookup and log
download.

---

## 8. Logging hygiene

`httpx`/`httpcore` loggers are pinned to `WARNING` at module import
(lines 12-13). Without that, every API call would emit an
`HTTP Request: GET …` line that drowns out the real pipeline UI.

Likewise, the `claude_agent_sdk` logger is muted in
`norn/stages/generate.py` for the same reason — keep operational chatter
out of the run log.

---

## 9. Common patterns

### One-shot check on the current branch

```python
Stage("ci", CheckCI())                  # auto repo + branch
```

### Poll a specific workflow until done

```python
Stage("ci", CheckCI(workflow="ci.yml", poll=True, timeout_minutes=20))
```

### Feed extracted failure into a follow-up Generate

```python
Pipeline("fix-ci")
    .stage("ci", CheckCI(poll=True), on_failure=OnFailure.ASK_USER)
    .stage("fix", Generate(prompt=(
        "The build for branch {param.branch} failed. Fix it.\n\n"
        "{ci.output}"
    )))
```

The interpolated `{ci.output}` is the dict from §1 — when used in a
prompt template, it's rendered as the human-readable log block. This is
the pattern `dogfooding/check_and_fix_ci.py` follows: it wraps `CheckCI`
in `CheckCIWithLogs` (which adds a stale-run guard and string-typed
output) so the same `{check ci.output}` works directly.

### Bypass summarisation for a long-context model

```python
Stage("ci", CheckCI(summarize=False, max_log_lines=10_000))
```

Useful when feeding the failure into Opus/Sonnet with a large context
window and you'd rather trust the model than the regex anchors.

---

## 10. Where to look when something goes wrong

| Symptom | First place to look |
|---------|---------------------|
| "No GitHub token found" | `_resolve_token` (line 147) — env vars vs. `gh auth token` |
| "Could not detect GitHub repo" | `_detect_repo` (line 188) — origin URL parsing |
| Stage hangs forever | `poll=True` with no `timeout_minutes`, or a workflow that's queued indefinitely |
| Logs are empty on a known-failed run | failed jobs filter (line 605) — the run conclusion may be `failure` while no individual job is `failure`/`cancelled` (e.g. `startup_failure`); add the conclusion to the filter |
| Anchor match is in the wrong place | dump raw log, look for an earlier tool-specific signal, add an anchor regex |
| Rate-limited | the log downloads, almost always — increase `poll_interval`, or cache by `run_id` |

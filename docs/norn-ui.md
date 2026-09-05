# Norn UI — reference

The terminal UI for Norn: a typed **event seam** that carries pipeline run
state from the runner to any consumer, and a **Textual TUI** (`norn ui`) that
renders a live run with a launcher, an arguments prompt, a pipeline graph, a
transcript, stage detail and a budget meter.

> **Keep this file in sync.** It is the canonical description of the UI/event
> architecture. When you change anything under `norn/tui/`, the event seam
> (`norn/events.py`, `norn/event_sink.py`), the runner's event emission, the
> launch flow, or the `norn ui` CLI wiring, **update this document in the same
> change**. AGENTS.md points here.

Design background lives in `tmp/norn-ui.md` (the plan) and
`tmp/norn-ui/handoff.md` (the build handoff for the initial 20 steps). This
file describes what is *actually in the code now*.

---

## 1. `norn ui` — usage and flow

```bash
norn ui                  # open the launcher (browse bundled + discovered pipelines)
norn ui <pipeline>       # run a bundled name or a .py path directly (skips the launcher)
```

Textual is a **required** dependency (declared in `[project.dependencies]`),
so `norn ui` always works in a proper install. There is no optional `ui`
extra and no fallback: the command imports `norn.tui` directly and a missing
Textual fails hard with the raw `ImportError`. Textual is imported lazily (only
by the `ui` command) so other subcommands stay light — that's import locality,
not optionality.

Everything runs inside **one** `NornUIApp` as a stack of screens, so
transitions are seamless (no terminal teardown between steps):

```
Launcher ──select──▶ [Args prompt, if the pipeline declares args] ──▶ Run
   ▲                          │ Esc (cancel)                          │ b / Esc (Back)
   └──────────────────────────┴───────────────────────────────────────┘
   q on the launcher quits; q on the run screen quits the whole app
```

- **Launcher** (`LauncherScreen`) — a `DataTable` of bundled + discovered
  pipelines plus an `Open file…` row. Enter selects; **`w`** toggles
  **worktree isolation** (status line shows `Worktree: ON`/`OFF`); **`h`**
  opens the run **history** for the highlighted pipeline; `q`/Esc quits. Enter
  is handled via `on_data_table_row_selected` (the focused `DataTable` consumes
  the key and emits `RowSelected`). Selecting a pipeline dismisses with a
  `LaunchRequest(info, use_worktree)` (replacing the former bare
  `PipelineInfo`). `Open file…` dismisses with the `OPEN_FILE` sentinel (→ fzf
  file picker); `h` dismisses with a `HistoryRequest`.
- **History** (`HistoryBrowserScreen`) — a read-only `DataTable` of past runs
  from the pipeline's `.history` (status, cost, duration, resumable), with a
  per-run `stage_log` detail panel (including error text). **`r`** resumes from
  the `.checkpoint` (starts a run with `resume_checkpoint`); `q`/Esc returns to
  the launcher. The pipeline's history/checkpoint paths are resolved with the
  same state-key logic as `norn run`/`norn history` (`norn/state.py`).
  **Both frontends share one state key**: `build_run_setup` (`norn/tui/launch.py`)
  sets `config_path = history_config_key(ref)` for every TUI run (including
  resumes and direct-mode launches), so runs started from `norn ui` appear in
  the history browser and are resumable, exactly as `norn run` runs are.
- **Args prompt** (`ArgsPromptScreen`) — shown only when the pipeline declares
  `args` in its `metadata`. One input per arg. Path-like args (description
  mentions path/file/folder/dir) can be filled with **`ctrl+o`** → fzf.
  Submit with **`ctrl+s`** or **Enter**; **Esc** cancels back to the launcher.
- **Run** (`RunScreen`) — the live run. **`b`**/**Esc** returns to the
  launcher; **`q`** quits.

In direct mode (`norn ui <pipeline>`) there is no launcher to return to, so
Back exits.

### Key bindings (run screen)

| Key | Action | Notes |
| --- | --- | --- |
| `p` | pause / resume | via `RunController` |
| `c` | cancel the run | cancels the active stage task immediately (see below) |
| `a` | answer a waiting prompt | opens the `InputDecisionModal` (see below) |
| `b` / `Esc` | back to launcher | cancels + awaits run teardown; exits in direct mode |
| `q` | quit | cancels + awaits run teardown; exits the whole app |

The `RunScreen` renders these as a Textual `Footer` at the bottom of the run
view (matching the launcher/history/args screens), so the controls are always
visible. The footer reflects `check_action`/`_is_action_enabled`: `Cancel`
shows only while a run is in flight, and `Answer` shows only when a prompt is
waiting. Bindings also grey out by `AgentCapabilities` (e.g. model-switch /
attach are inert for providers that don't support them).

**Cancel behaviour.** `_run_stage` wraps every stage coroutine in an
`asyncio.Task` and registers it with `RunController.set_active_task`. When
`cancel()` is called, the active task is cancelled immediately — not at the
next between-stage cooperative check. The runner catches the resulting
`asyncio.CancelledError`, emits a `StageFinished(status="cancelled")` event
so the graph does not leave the stage spinning, and raises
`run_control.CancelledError` so the run ends as *cancelled*. The task is
always deregistered in a `finally` block so a later `cancel()` cannot touch a
finished task.

**Back / Quit teardown.** Both `action_back` and `action_quit_app` are async:
they call `_cancel_and_await()`, which cancels the controller and then awaits
the `_run_task` (the `asyncio.Task` wrapping `_drive_run`). This guarantees
the run finishes cleanly — no events post to a dismissed screen, and no
second run can start against the same repo while the first is still live.

---

## 2. Module map

### Core (no Textual; import-safe everywhere)

| File | Role |
| --- | --- |
| `norn/events.py` | Typed run-event classes, `EventKey` (`run_id, unit_id, stage_id, attempt, seq`), `Delivery` enum (`LOSSLESS`/`COALESCIBLE`/`PAGEABLE`). No SDK types. |
| `norn/event_sink.py` | `EventSink` (buffers, redacts, dispatches to `on_event`), `NullSink`. Redaction via `norn.ui.mask()` happens once here. |
| `norn/graph.py` | `PipelineGraph` / `PipelineNode`, `build_graph(pipeline)` with stable IDs (`stage:<name>`, `loop:<name>`, …). Shared by diagram, dry-run and the TUI. |
| `norn/run_control.py` | `RunController` — pause/resume, cancel, answer-input futures. `CancelledError`. |
| `norn/responder.py` | `InputResponder` base; `CLIResponder` (preserves `norn run` prompts), `TUIResponder` (non-blocking). |
| `norn/agents/capabilities.py` | `AgentCapabilities`, `CostMode`, per-provider declarations. Drives feature gating + binding enablement. |
| `norn/cli_render.py` | `CLIRenderer` — Rich CLI output reproduced from the event stream (the `norn run` renderer). |
| `norn/state.py` | Per-pipeline state-key resolution (`.history`/`.checkpoint` paths). Shared by `cli.py`, the TUI launcher, and the TUI history browser so all three resolve to the same file for the same pipeline. |

### TUI (`norn/tui/`; Textual lives here — imported lazily by `norn ui`, never at `norn` core import time)

| File | Role |
| --- | --- |
| `__init__.py` | Import-safe without Textual. |
| `viewmodel.py` | `RunViewModel` — **textual-free** projection of the event stream into render-ready state (header, per-node status, per-stage transcript, usage, waiting-input). Holds the UI logic; unit-tested without Textual. |
| `widgets.py` | `NornHeader`, `NornGraph` (Textual `Tree`), `Transcript` (`RichLog`), `StageDetail`, `BudgetMeter`. Thin; read the ViewModel. `Transcript.refresh_vm()` **appends** only blocks not yet on screen (full redraw on stage switch or a shrinking spool) — the app refreshes on every event, so redrawing the whole log each time is quadratic under streamed command output. |
| `app.py` | `RunScreen` (run UI + drives `run_pipeline` in-process), `NornApp` (thin host of one `RunScreen` for direct/object runs + tests), `NornUIApp` (the unified Launcher→Args→Run navigation app). |
| `screens.py` | `LauncherScreen`/`LauncherApp`, `HistoryBrowserScreen`/`HistoryBrowserApp`, `LaunchRequest`, the `OPEN_FILE` sentinel. |
| `args_prompt.py` | `ArgsPromptScreen`/`ArgsPromptApp`, `is_path_arg`/`is_dir_arg`, `run_fzf`. |
| `modals.py` | `InputDecisionModal` — the in-run retry/continue/abort modal shown when the runner blocks on a `WaitingInput`. Textual-only, imported lazily by the run screen. |
| `launch.py` | Textual-free loading helpers: `pipeline_args_meta`, `resolve_ref`, `load_pipeline_with_args` (sets `sys.argv` + reloads dynamic bundled modules), `build_run_setup` (builds graph/budget/run_kwargs and sets `config_path` so every TUI run persists history and checkpoints), and history helpers (`load_run_history`, `history_config_key`, `load_run_checkpoint`). |

---

## 3. The event seam

The runner is the **single source of truth**; the UI is a pure projection.

- The runner emits typed events at every lifecycle boundary (run/unit/stage
  start+finish, loop attempts, parallel/include/clear-context, usage updates,
  waiting-input, pause/resume, cancel, error). The `Generate` stage wraps each
  provider `AgentEvent` in a `TurnEvent` (no drops). All events carry an
  `EventKey` and a `Delivery` class; **no `claude_agent_sdk` types leak**.
- **`EventKey.stage_id` is the graph node id** (`norn.graph.build_graph`), not a
  flat stage name. A loop/parallel **body** stage emits the *nested* id
  (`loop:<L>/stage:<name>`, `parallel:<P>/stage:<name>`), a top-level stage emits
  `stage:<name>`, and a clear-context marker emits `clear:<N>`. This is what lets
  the TUI graph attribute every event to the right node — a flat `stage:<name>`
  for a loop body would never match the nested node and the node would render as
  pending (`○`) forever. Container nodes get their status from lifecycle events:
  `LoopAttempt`/`ParallelStarted` → `running`, `LoopSuccess`/`ParallelDone` →
  `passed`, `StageRetrying` → `retrying`, `LoopExhausted` → `failed`. The
  `ClearContextNotice` (keyed `clear:<N>`) marks that node `passed`. `RunStarted`
  marks the Tree root (`pipeline:<name>`) `running`; `RunFinished`/`RunCancelled`
  set its terminal status.
- **`stage_id` is produced in exactly one place and never rebuilt by a stage.**
  `_run_stage` (`norn/runner.py`) computes `stage_id = node_id or f"stage:{stage.name}"`
  and passes it to the stage via `agent_kwargs["node_id"]`. Agent-backed stages
  (e.g. `Generate`) read this kwarg and use it for all three agent lifecycle
  events (`CallingAgent`, `TurnEvent`, `GotReply`). Because all events for a
  stage — lifecycle *and* agent — share the same fully-qualified id, the view
  model's transcript spool and node-status map are consistent: blocks stored
  under `loop:L/stage:gen` are still retrievable when `StageFinished` arrives
  keyed `loop:L/stage:gen`, rather than appearing empty because they were stored
  under the flat `stage:gen`.  The one exception is `run_pipeline`'s own item
  loop (cached, skipped, condition-skipped and step-mode `StageFinished`, plus
  `_handle_failure`'s `WaitingInput`), which builds `stage:<name>` inline: those
  paths only ever see the pipeline's top-level items, so there is no parent
  prefix and the flat id *is* the fully-qualified one.  Nested runners
  (`_run_loop` and friends) must pass the pre-computed `stage_node_id` down
  instead of rebuilding it.
- **Stage, loop, parallel, and include names must be unique within their parent
  container.**  `build_graph` (`norn/graph.py`) raises `ValueError` at pipeline
  construction time if two sibling items produce the same node id (e.g. two
  stages both named `commit` both map to `stage:commit`).  The error names the
  colliding id, the pipeline, and the duplicate item name, and tells the user
  to rename one of the conflicting items.  The same name *is* legal across
  different parents: `loop:a/stage:work` and `loop:b/stage:work` are distinct
  ids.  Note that a `Stage` and a `Loop` sharing a name never collide because
  they produce `stage:<name>` and `loop:<name>` respectively.
- **`UsageUpdated` carries run-wide cumulative totals; `StageFinished.usage_*`
  carries per-stage figures.**  `_run_stage` adds `result.usage` to
  `ctx.usage_tracker` (a `UsageTracker` that accumulates across all stages)
  and then emits `UsageUpdated` with `tracker.total_*`.  The viewmodel stores
  the last `UsageUpdated` values directly as the authoritative run totals
  (`_run_input_tokens` etc.) — it does **not** add them on top of any
  previously-finished total.  `StageFinished.usage_*` is the per-stage slice
  and feeds `StageDetailRecord` only.  Any renderer that adds per-stage figures
  together will double-count once a second stage runs.
- **Stage counter invariant:** `stages_done` never exceeds `stages_started`.
  Cached, skipped, and condition-skipped stages emit `StageFinished` with no
  preceding `StageStarted` (the runner bypasses `_run_stage` for those paths).
  The viewmodel tracks which stage ids have been seen in `StageStarted`; for
  stages that arrive only in `StageFinished`, it increments both counters so the
  header always shows `done/started` with `done ≤ started`.  A fully-cached
  resume reads `N/N`.
- **Shell stages stream their output too.** `RunCommand` emits `CommandOutput`
  events (`PAGEABLE`, `text` + `stream` = `stdout`/`stderr`) while the command
  is still running, so a long build or test run fills the transcript live
  instead of leaving it blank until the stage ends. The ViewModel appends each
  chunk as a `TextBlock`, so shell output and agent prose render through the
  same path. Three details matter:
  - **Batched, not per line.** Output is flushed every
    `FLUSH_INTERVAL_SECONDS` (150ms), so one event usually carries several
    lines and the event rate stays ~7/s however loud the command is. A partial
    trailing line is held back until it completes, then flushed at the end.
  - **Capture is unchanged.** `StageResult.output["stdout"]`/`["stderr"]` still
    carry the command's complete output; streaming is a second consumer of the
    same bytes, not a replacement. Both pipes are read concurrently (chunked,
    not `readline()`), which preserves `communicate()`'s deadlock guarantee and
    removes its 64KiB line-length limit. On timeout the partial output captured
    before the kill is returned rather than empty strings.
  - **The stage needs its node id.** `BaseStage.emits_events = True` tells the
    runner to pass `node_id` and `attempt` into `run()`, so streamed events key
    to the same graph node as the surrounding `StageStarted`/`StageFinished`.
    Without it the output would be filed under a different node and never reach
    the transcript pane. `CLIRenderer` ignores `CommandOutput`, so `norn run`
    output is unchanged.
- `EventSink` classifies by delivery so a slow consumer never blocks the
  producer: `LOSSLESS` → buffered; `COALESCIBLE` (usage counters) →
  latest-wins; `PAGEABLE` (transcript) → per-stage spool.
- **Redaction is at the seam**: `EventSink` masks displayable text once (using
  the registered-secrets masker) so every consumer — TUI, `CLIRenderer`, any
  future journal — is safe.  The following `TurnEvent` block fields are
  covered: `AgentEvent.text`, `ToolUseBlock.input_summary`,
  `ToolResultBlock.summary`, and `ThinkingBlock.text` (extended thinking that
  echoes a token, e.g. after an auth failure, is redacted here).
  `CommandOutput.text` is masked on the same path — raw process output is the
  most likely place for a token to appear verbatim.
- **History persistence masks errors on write**: `norn/history.py` applies
  `norn.ui.mask()` to `StageLogEntry.error` when serialising to the `.history`
  JSONL file, so secrets never land on disk and cannot be exposed via
  `norn history` or the TUI stage-detail view.
- **Isolated includes inherit the caller's frontend seams.** When
  `run_pipeline` executes an `.include(..., isolated=True)` item it forwards
  `event_sink`, `input_responder`, and `run_controller` from the parent context
  to the sub-`run_pipeline` call.  This means sub-pipeline stage events arrive
  in the parent's sink (TUI graph and transcript stay live), pause/cancel
  propagates through the shared controller, and `ask_user` prompts reach the
  correct responder.  Results, session, and usage tracking remain isolated in
  the fresh sub-context by design.  The sub-run still emits its own
  `RunStarted`/`RunFinished` envelope into the shared sink; suppressing those
  envelope events for sub-runs is a separate future change.
- `norn run` is unchanged for users: its output now flows through
  `CLIRenderer` (a subscriber) instead of inline `ui.print_*`. (Zero-cost runs
  now show token counts — a fixed bug.) `norn/ui.py` retains only the helpers
  that are still live: `mask`, `register_secrets`, `print_dry_run`,
  `print_running_total`, `print_usage_report`, `print_history_table`,
  `print_history_comparison`, `print_history_run_details`, and the interactive
  prompts (`ask_budget_exceeded`, `ask_user_continue`, `ask_yes_no`,
  `step_prompt`). All former `print_pipeline_start` / `print_stage_*` /
  `print_loop_*` / `print_parallel_*` / `print_include_*` / `print_clear_context`
  duplicates have been removed.
- **Resume line**: `RunStarted.resume_session` carries the session id when a run
  is started with `--continue`. `CLIRenderer._on_run_started` prints
  `Resuming session <id>` when this field is set, restoring the behaviour that
  was previously implemented in the now-deleted `print_pipeline_start`.

`PipelineContext` carries `run_id`, `unit_id` (`"unit-0"` today),
`event_sink`, `input_responder`, `run_controller` — all defaulted, so
`PipelineContext()` and existing `run_pipeline(...)` callers are unaffected.

---

## 4. Arguments prompt and fzf

- Whether to prompt is decided by `launch.pipeline_args_meta(ref)` — the
  pipeline's declared `metadata["args"]` (name → description). Empty → no
  prompt.
- Collected values flow into the run two ways: as `params` to `run_pipeline`
  (so `{param.args}` / `{param.KEY}` resolve), and via `sys.argv` reconstructed
  around the load so pipelines that read argv at **import time** (e.g.
  `implement_features`) see them. `load_pipeline_with_args` also reloads an
  already-imported bundled module so a repeat run with different args isn't
  served a stale structure.
- **fzf** (`args_prompt.run_fzf`): pipes a `find` listing into `fzf`. It
  **prunes** dependency/build/cache/VCS directories (`_FZF_PRUNE_DIRS`:
  `.git`, `.venv`, `node_modules`, `.m2`, `target`, `build`, `__pycache__`,
  `.idea`, … ) so the picker is fast and uncluttered. `dirs_only` adds
  `-type d`. The picker runs under `App.suspend()` (briefly hands the terminal
  to fzf). **No fallback:** if `fzf` isn't on `PATH`, `run_fzf` raises
  `RuntimeError` and the picker fails loudly — it does not silently degrade to
  manual text entry. `run_fzf` returns `None` only when the user cancels.

---

## 5. Testing

All tests are **offline** (mock SDK/subprocess/network). Run with
`uv run python -m pytest tests/`.

- Core seam, graph, ViewModel, responder, capabilities → fast unit tests.
- Runner event emission → `tests/test_runner_events.py`.
- TUI widgets/screens/navigation → **Textual Pilot** (`async with
  app.run_test()`), asserting content/state — **not** snapshots:
  `test_tui_app.py`, `test_tui_graph.py`, `test_tui_transcript.py`,
  `test_tui_launcher.py`, `test_tui_args_prompt.py`, `test_tui_nav.py`,
  `test_history_browser.py`.
- `tests/test_tui_nav.py` drives the real `NornUIApp` (Launcher→Args→Run,
  Back, Quit, Open-file, direct mode) with a stubbed offline loader.

When testing, patch **module objects**, not dotted-string targets
(`monkeypatch.setattr(module, "name", …)`) — string targets resolve
inconsistently under full-suite import ordering.

---

## 6. Extension points

- **Custom subscriber**: `EventSink(on_event=handler)` → `run_pipeline(...,
  event_sink=sink)`. Handler receives redacted events in order.
- **Custom responder**: subclass `InputResponder`; pass via
  `run_pipeline(..., input_responder=...)`.
- **Custom projection**: subclass/extend `RunViewModel`.
- **New event type**: add to `norn/events.py` with an `EventKey` and a
  `Delivery` class; emit from the runner/providers; handle in the renderers.

---

## 7. Input-decision modal (retry / continue / abort)

When the runner blocks on a `WaitingInput` it awaits a single-character answer
through the `TUIResponder`. The run screen surfaces this as `InputDecisionModal`
(`norn/tui/modals.py`), pushed automatically when a `WaitingInput` event
arrives (and re-openable with `a`). The choices depend on `WaitingInput.kind`:

| kind | choices |
| --- | --- |
| `failure_recovery` | **Retry** (`r`) · **Continue** past (`c`) · **Abort** (`a`) |
| `budget` | **Continue** (`c`) · **Abort** (`a`) |
| `step` | **Run** (`r`) · **Skip** (`s`) · **Abort** (`a`) |

Each choice is a button and a single keypress; `Esc` aborts. The chosen code
goes to `RunController.answer_input`, which resolves the responder future and
unblocks the runner.

**Retry is real, end-to-end.** `InputResponder.ask_failure` returns `r`/`c`/`a`.
On `r` the runner re-runs the failed unit: a top-level stage is re-executed in
place (`runner._handle_failure` returns `"retry"` and the item loop re-runs the
stage); an exhausted loop runs another full round of attempts (`_run_loop`
recurses, keeping the agent session). `c` proceeds past the failure; `a` raises.
`CLIResponder`/`norn run` offer the same `[r]etry [c]ontinue [a]bort` prompt, so
CLI and TUI behave identically. Retry only happens for stages/loops whose policy
is `ASK_USER`; `FAIL` (the default) still fails fast.

## 8. Worktree isolation (v1)

The launcher exposes a per-run **worktree toggle** (`w`). When enabled, the run
executes inside a temporary `git worktree` so all file changes are isolated from
the launch branch. On success the work is merged back automatically.

### Flow

1. User presses `w` in the launcher → `Worktree: ON` status line.
2. Enter selects a pipeline → `LaunchRequest(info, use_worktree=True)`.
3. `NornUIApp._start_run` generates a single short run id (`uuid4().hex[:8]`)
   shared by the worktree branch name (`norn/run-<id>`) and
   `run_pipeline(run_id=...)`.
4. `RunScreen._drive_run` calls `WorktreeSession.create(run_id)`, sets
   `kwargs["working_dir"]` and `kwargs["run_id"]`, then runs the pipeline.
5. After a successful pipeline: `session.merge_back(message=...)`.
6. Cleanup per the outcome matrix (see below); result shown as a notification.

### Cleanup matrix

`WorktreeSession.cleanup(keep=…)` has exactly two outcomes — there is no
intermediate state. `keep=True` leaves **both** the worktree directory and the
work branch completely untouched. `keep=False` runs `git worktree remove
--force` (falling back to `shutil.rmtree` + `git worktree prune` on failure)
and then `git branch -D <work_branch>`.

| Outcome | `keep` flag | Worktree dir + branch |
| --- | --- | --- |
| successful merge (ff or no-ff) | `False` | removed / deleted |
| no changes | `False` | removed / deleted |
| merge conflict | `True` | **kept** (user must resolve manually) |
| dirty-launch refusal | `True` | **kept** |
| commit failure (no identity) | `True` | **kept** |
| other git error | `True` | **kept** |
| pipeline stage failure | `True` | **kept** |
| user cancel (`CancelledError`) | `True` | **kept** |

### Restrictions (v1)

- Worktree mode **cannot** be combined with `--resume`/history — a hard error
  is shown and the run does not start.
- The toggle applies **only** to a normal pipeline launch from the launcher.
  `Open file…`, history view / resume, and direct `norn ui <pipeline>` (which
  bypasses the launcher) do **not** offer worktree mode.
- **Contrib stages are not worktree-aware in v1.** Stages under `norn/contrib/`
  (`ci.py`, `ship.py`, `push.py`) shell out relative to the process cwd or to
  `IssueContext.local_path`, not `ctx.working_dir`. The `Clone` stage already
  owns its own checkout dir. Running issue-processing pipelines under the
  worktree toggle is therefore unsupported.
- **`implement_features` and `check_and_fix_ci` are not isolated.** Both pin
  `PROJECT_DIR = os.getcwd()` and `cd` into it from their RunCommand stages, so
  they always run against the launch repo even with the toggle on.

---

## 9. Known limitations / not yet wired

- **No final `RunReport` event.** The end-of-run usage summary is still printed
  directly, not delivered through the seam.
- **No live model switch handler.** The binding is capability-gated but there's
  no in-flight model change.
- **Single unit only.** Events carry `unit_id`, but only `unit-0` exists; fleet
  / transport (Ratatosk) is not built. Worktree isolation **is** built
  (single-unit, local merge-back); remaining gaps: `--resume` + worktree
  disabled, dirty-tree stash not supported, untracked/ignored files must be
  committed before launch, submodules unhandled, subdir-launch semantics
  undefined.
- **No event journal.** The history browser shows summaries, not a full
  transcript replay.
